# app/api/public/router.py
# ══════════════════════════════════════════════════════════════
# API Pública para Desarrolladores — Opción A
# Autenticación: API Key en header X-API-Key
# Prefix: /api
#
# Endpoints:
#   GET  /api/health          → estado del servicio
#   POST /api/emitir          → firma + envía DTE al SII
#   GET  /api/estado/{id}     → consulta estado en SII
#   GET  /api/folios          → folios disponibles por tipo
# ══════════════════════════════════════════════════════════════

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.base import get_db
from app.models.emisor import Emisor
from app.models.caf import CAF
from app.models.dte import DTE
from app.services.dte_service import DTEService
from app.services.sii_sender import SIISender
from app.models.certificado import Certificado

router = APIRouter(prefix="/api", tags=["API Desarrolladores"])


# ══════════════════════════════════════════════════════════════
# AUTH — API Key
# ══════════════════════════════════════════════════════════════

async def get_emisor_by_api_key(
    x_api_key: str = Header(..., description="API Key del desarrollador"),
    db: AsyncSession = Depends(get_db),
) -> Emisor:
    emisor = (await db.execute(
        select(Emisor).where(
            Emisor.api_key == x_api_key,
            Emisor.activo  == True,
        )
    )).scalar_one_or_none()
    if not emisor:
        raise HTTPException(401, "API Key inválida o inactiva")
    return emisor


# ══════════════════════════════════════════════════════════════
# HEALTH
# ══════════════════════════════════════════════════════════════

@router.get("/health")
async def health():
    return {
        "ok":       True,
        "servicio": "YeparDTEcore",
        "version":  "1.0",
        "docs":     "https://yepardtecore.cl/api/docs",
    }


# ══════════════════════════════════════════════════════════════
# EMITIR DTE
# ══════════════════════════════════════════════════════════════

class ReceptorInput(BaseModel):
    rut:       Optional[str] = "66666666-6"
    nombre:    Optional[str] = "Consumidor Final"
    giro:      Optional[str] = ""
    direccion: Optional[str] = ""
    comuna:    Optional[str] = ""
    ciudad:    Optional[str] = ""
    email:     Optional[str] = None

class ItemInput(BaseModel):
    nombre:  str
    cantidad: float = 1
    precio:  float
    exento:  bool = False

class EmitirInput(BaseModel):
    tipo:          int            # 33=Factura, 34=F.Exenta, 39=Boleta, 41=B.Exenta, 52=Guía, 56=N.Débito, 61=N.Crédito
    receptor:      ReceptorInput
    items:         list[ItemInput]
    exento:        bool = False
    fecha:         Optional[str] = None   # YYYY-MM-DD, default hoy
    auto_enviar:   bool = True            # False = solo firma, no envía al SII
    referencia:    Optional[dict] = None  # para NC/ND


@router.post("/emitir")
async def emitir_dte(
    datos:  EmitirInput,
    emisor: Emisor = Depends(get_emisor_by_api_key),
    db:     AsyncSession = Depends(get_db),
):
    """
    Genera, firma y envía un DTE al SII.
    Retorna el folio, XML firmado y TrackID.
    """
    tipos_validos = {33, 34, 39, 41, 52, 56, 61}
    if datos.tipo not in tipos_validos:
        raise HTTPException(422, f"Tipo DTE no válido: {datos.tipo}. Válidos: {tipos_validos}")

    fecha = datos.fecha or datetime.now().strftime("%Y-%m-%d")

    datos_dte = {
        "tipo_dte":      datos.tipo,
        "fecha_emision": fecha,
        "receptor": {
            "rut":          datos.receptor.rut or "66666666-6",
            "razon_social": datos.receptor.nombre or "Consumidor Final",
            "giro":         datos.receptor.giro or "",
            "direccion":    datos.receptor.direccion or "",
            "comuna":       datos.receptor.comuna or "",
            "ciudad":       datos.receptor.ciudad or "",
            "correo":       datos.receptor.email or "",
        },
        "items": [
            {
                "nombre":          it.nombre,
                "cantidad":        it.cantidad,
                "precio_unitario": it.precio,
                "exento":          it.exento or datos.exento,
            }
            for it in datos.items
        ],
        "referencias": [datos.referencia] if datos.referencia else [],
    }

    try:
        svc = DTEService(db=db)
        resultado = await svc.emitir(
            emisor_id=emisor.id,
            datos=datos_dte,
            auto_enviar=datos.auto_enviar,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Error emitiendo DTE: {e}")

    # DTEService retorna el resultado directo (sin wrapper "dte")
    doc = resultado
    tipo_label = {
        33: "Factura Electrónica",
        34: "Factura Exenta",
        39: "Boleta Electrónica",
        41: "Boleta Exenta",
        52: "Guía de Despacho",
        56: "Nota de Débito",
        61: "Nota de Crédito",
    }

    # ── Envío automático al SII ──────────────────────────────────────────────
    track_id = None
    estado_sii = "BORRADOR"
    if datos.auto_enviar and doc.get("xml_firmado"):
        try:
            from app.models.certificado import Certificado
            cert = (await db.execute(
                select(Certificado).where(
                    Certificado.emisor_id == emisor.id,
                    Certificado.activo    == True,
                ).limit(1)
            )).scalar_one_or_none()

            if cert:
                sender = SIISender(ambiente=emisor.ambiente or "certificacion")
                rut_enviador = cert.rut_firmante or emisor.rut
                sobre_xml = await sender.construir_sobre(
                    dtes_xml    = [doc["xml_firmado"]],
                    rut_emisor  = emisor.rut,
                    rut_enviador= rut_enviador,
                    fecha_resol = "2026-04-19",
                    nro_resol   = "0",
                )
                resultado_envio = await sender.enviar_sobre(
                    sobre_xml   = sobre_xml,
                    rut_emisor  = emisor.rut,
                    rut_enviador= rut_enviador,
                    p12_bytes   = bytes(cert.certificado_p12),
                    password    = cert.certificado_password or "",
                )
                track_id   = resultado_envio.get("track_id")
                estado_sii = resultado_envio.get("estado", "ENVIADO")
        except Exception as e:
            logger.error(f"Error enviando al SII: {e}", exc_info=True)
            estado_sii = "ERROR_ENVIO"

    return {
        "ok":           True,
        "tipo":         tipo_label.get(datos.tipo, str(datos.tipo)),
        "folio":        doc.get("folio"),
        "folio_fmt":    doc.get("folio_fmt"),
        "monto_total":  doc.get("monto_total"),
        "estado":       estado_sii,
        "track_id":     track_id,
        "fecha":        fecha,
        "receptor":     datos.receptor.nombre,
        "xml_firmado":  doc.get("xml_firmado"),
    }


# ══════════════════════════════════════════════════════════════
# ESTADO DEL ENVÍO
# ══════════════════════════════════════════════════════════════

@router.get("/estado/{track_id}")
async def estado_envio(
    track_id: str,
    emisor:   Emisor = Depends(get_emisor_by_api_key),
    db:       AsyncSession = Depends(get_db),
):
    """Consulta el estado de un envío en el SII por TrackID."""
    cert = (await db.execute(
        select(Certificado).where(
            Certificado.emisor_id == emisor.id,
            Certificado.activo    == True,
        ).limit(1)
    )).scalar_one_or_none()
    if not cert:
        raise HTTPException(400, "No hay certificado configurado")

    sender = SIISender(ambiente=emisor.ambiente or "certificacion")
    try:
        resultado = await sender.consultar_estado(
            track_id=track_id,
            rut_emisor=emisor.rut,
        )
    except Exception as e:
        raise HTTPException(500, f"Error consultando SII: {e}")

    return {
        "track_id": track_id,
        "estado":   resultado.get("estado"),
        "glosa":    resultado.get("glosa"),
        "detalle":  resultado.get("detalle"),
    }


# ══════════════════════════════════════════════════════════════
# FOLIOS DISPONIBLES
# ══════════════════════════════════════════════════════════════

@router.get("/folios")
async def folios_disponibles(
    emisor: Emisor = Depends(get_emisor_by_api_key),
    db:     AsyncSession = Depends(get_db),
):
    """Retorna los folios disponibles por tipo de DTE."""
    cafs = (await db.execute(
        select(CAF).where(
            CAF.emisor_id == emisor.id,
            CAF.activo    == True,
        ).order_by(CAF.tipo_dte.asc())
    )).scalars().all()

    tipo_label = {
        33: "Factura", 34: "F.Exenta", 39: "Boleta",
        41: "B.Exenta", 52: "Guía", 56: "N.Débito", 61: "N.Crédito"
    }

    return {
        "folios": [
            {
                "tipo":        c.tipo_dte,
                "descripcion": tipo_label.get(c.tipo_dte, str(c.tipo_dte)),
                "desde":       c.folio_desde,
                "hasta":       c.folio_hasta,
                "actual":      c.folio_actual,
                "disponibles": max(0, c.folio_hasta - c.folio_actual + 1),
            }
            for c in cafs
        ]
    }
