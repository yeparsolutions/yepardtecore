# app/api/public/router.py
# ══════════════════════════════════════════════════════════════
# API Pública YeparDTEcore — v2.0 Stateless
#
# Endpoints stateful (usan BD de YeparDTEcore):
#   GET  /api/health
#   POST /api/emitir          → emisor registrado en DTEcore
#   GET  /api/estado/{id}
#   GET  /api/folios
#
# Endpoints stateless (el cliente trae todo):
#   POST /api/firmar-y-enviar → firma + envía con cert/CAF del cliente
#   POST /api/firmar          → solo firma, no envía
#   POST /api/enviar-sobre    → envía un sobre ya firmado
# ══════════════════════════════════════════════════════════════

import base64
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
import logging
from app.services.sii_sender import SIISender
logger = logging.getLogger("yepardtecore.api")
from app.models.certificado import Certificado

router = APIRouter(prefix="/api", tags=["API Desarrolladores"])


# ══════════════════════════════════════════════════════════════
# AUTH — API Key (solo para endpoints stateful)
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
        "version":  "2.0",
        "docs":     "https://yepardtecore.cl/api/docs",
    }


# ══════════════════════════════════════════════════════════════
# STATELESS — FIRMAR Y ENVIAR
# El cliente trae su propio certificado, CAF y XML del DTE.
# YeparDTEcore solo firma, arma sobre y envía al SII.
# NO guarda nada en BD.
# ══════════════════════════════════════════════════════════════

class EmisorStateless(BaseModel):
    rut:            str
    razon_social:   str
    giro:           str
    direccion:      str
    comuna:         str
    ciudad:         str
    nro_resolucion: str = "0"
    fch_resolucion: str = "2000-01-01"

class ReceptorStateless(BaseModel):
    rut:       str = "66666666-6"
    nombre:    str = "Consumidor Final"
    giro:      Optional[str] = ""
    direccion: Optional[str] = ""
    comuna:    Optional[str] = ""
    ciudad:    Optional[str] = ""
    email:     Optional[str] = None

class ItemStateless(BaseModel):
    nombre:   str
    cantidad: float = 1
    precio:   float
    exento:   bool = False
    descuento: float = 0  # % descuento

class ReferenciaStateless(BaseModel):
    tipo_doc_ref:  int
    folio_ref:     str
    fecha_ref:     str
    razon:         Optional[str] = None
    cod_ref:       Optional[int] = None

class FirmarYEnviarInput(BaseModel):
    # Datos del emisor
    emisor:         EmisorStateless

    # Certificado del cliente (base64)
    pfx_base64:     str
    pfx_password:   str

    # CAF del cliente (XML en base64)
    caf_base64:     str

    # Datos del DTE
    tipo:           int
    receptor:       ReceptorStateless
    items:          list[ItemStateless]
    exento:         bool = False
    fecha:          Optional[str] = None   # YYYY-MM-DD, default hoy
    referencias:    list[ReferenciaStateless] = []

    # Control
    ambiente:       str = "certificacion"  # "certificacion" | "produccion"
    auto_enviar:    bool = True


@router.post("/firmar-y-enviar")
async def firmar_y_enviar(datos: FirmarYEnviarInput):
    """
    Endpoint stateless — firma y envía un DTE al SII.
    El cliente trae su certificado (.p12 en base64) y su CAF (XML en base64).
    YeparDTEcore NO guarda nada — solo procesa y devuelve el resultado.

    Retorna: {xml_firmado, folio, track_id, estado, ambiente}
    """
    tipos_validos = {33, 34, 39, 41, 52, 56, 61}
    if datos.tipo not in tipos_validos:
        raise HTTPException(422, f"Tipo DTE no válido: {datos.tipo}")

    if datos.ambiente not in ("certificacion", "produccion"):
        raise HTTPException(422, "ambiente debe ser 'certificacion' o 'produccion'")

    # ── Decodificar certificado y CAF ─────────────────────────────────────────
    try:
        pfx_bytes = base64.b64decode(datos.pfx_base64)
    except Exception:
        raise HTTPException(400, "pfx_base64 no es base64 válido")

    try:
        caf_xml = base64.b64decode(datos.caf_base64).decode("utf-8")
    except Exception:
        raise HTTPException(400, "caf_base64 no es base64 válido")

    fecha = datos.fecha or datetime.now().strftime("%Y-%m-%d")

    # ── Construir datos del DTE ───────────────────────────────────────────────
    datos_dte = {
        "tipo_dte":      datos.tipo,
        "fecha_emision": fecha,
        "emisor": {
            "rut":          datos.emisor.rut,
            "razon_social": datos.emisor.razon_social,
            "giro":         datos.emisor.giro,
            "direccion":    datos.emisor.direccion,
            "comuna":       datos.emisor.comuna,
            "ciudad":       datos.emisor.ciudad,
        },
        "receptor": {
            "rut":          datos.receptor.rut,
            "razon_social": datos.receptor.nombre,
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
                "descuento_pct":   it.descuento,
            }
            for it in datos.items
        ],
        "referencias": [
            {
                "tipo_doc_ref": r.tipo_doc_ref,
                "folio_ref":    r.folio_ref,
                "fecha_ref":    r.fecha_ref,
                "razon":        r.razon or "",
                "cod_ref":      r.cod_ref,
            }
            for r in datos.referencias
        ],
    }

    # ── Firma digital con el certificado del cliente ──────────────────────────
    try:
        from app.services.firma_digital import FirmaDigital
        from app.services.xml_builder import XMLBuilder

        firma_svc = FirmaDigital(
            pfx_bytes,
            datos.pfx_password,
            ambiente=datos.ambiente,
        )

        # Parsear CAF del cliente
        from lxml import etree as _etree
        caf_el = _etree.fromstring(caf_xml.encode("utf-8"))

        # Obtener folio del CAF (primer folio disponible)
        folio_desde = int(caf_el.findtext(".//DESDE") or 1)
        folio_hasta = int(caf_el.findtext(".//HASTA") or folio_desde)
        # Usar folio_desde — el cliente maneja qué folio usar
        folio = folio_desde

        # Construir XML del DTE
        builder = XMLBuilder(
            emisor_rut=datos.emisor.rut,
            emisor_razon=datos.emisor.razon_social,
            emisor_giro=datos.emisor.giro,
            emisor_dir=datos.emisor.direccion,
            emisor_comuna=datos.emisor.comuna,
            emisor_ciudad=datos.emisor.ciudad,
            nro_resol=datos.emisor.nro_resolucion,
            fch_resol=datos.emisor.fch_resolucion,
            ambiente=datos.ambiente,
        )

        xml_sin_firma = builder.construir(datos_dte, folio, caf_xml)
        xml_firmado   = await firma_svc.firmar_dte(xml_sin_firma, caf_el)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[STATELESS] Error firmando DTE: {e}", exc_info=True)
        raise HTTPException(500, f"Error firmando DTE: {e}")

    # ── Envío al SII ──────────────────────────────────────────────────────────
    track_id   = None
    estado_sii = "FIRMADO"

    if datos.auto_enviar:
        try:
            sender = SIISender(
                ambiente=datos.ambiente,
                nro_resol=datos.emisor.nro_resolucion,
                fch_resol=datos.emisor.fch_resolucion,
            )
            rut_firmante = firma_svc.rut_firmante or datos.emisor.rut

            sobre_xml = await sender.construir_sobre(
                dtes_xml     = [xml_firmado],
                rut_emisor   = datos.emisor.rut,
                rut_enviador = rut_firmante,
                firma_service= firma_svc,
            )
            resultado_envio = await sender.enviar_sobre(
                sobre_xml    = sobre_xml,
                rut_emisor   = datos.emisor.rut,
                rut_enviador = rut_firmante,
                p12_bytes    = pfx_bytes,
                password     = datos.pfx_password,
                auth_p12_bytes = pfx_bytes,
                auth_password  = datos.pfx_password,
            )
            track_id   = resultado_envio.get("track_id")
            estado_sii = resultado_envio.get("estado", "ENVIADO")
            logger.info(f"[STATELESS] Enviado al SII — track_id={track_id} estado={estado_sii}")

        except Exception as e:
            logger.error(f"[STATELESS] Error enviando al SII: {e}", exc_info=True)
            estado_sii = "ERROR_ENVIO"

    tipo_label = {
        33: "Factura Electrónica", 34: "Factura Exenta",
        39: "Boleta Electrónica",  41: "Boleta Exenta",
        52: "Guía de Despacho",    56: "Nota de Débito",
        61: "Nota de Crédito",
    }

    return {
        "ok":           True,
        "tipo":         tipo_label.get(datos.tipo, str(datos.tipo)),
        "folio":        folio,
        "xml_firmado":  xml_firmado,
        "track_id":     track_id,
        "estado":       estado_sii,
        "ambiente":     datos.ambiente,
        "fecha":        fecha,
    }


@router.post("/firmar")
async def solo_firmar(datos: FirmarYEnviarInput):
    """
    Igual que /firmar-y-enviar pero auto_enviar=False.
    Útil para previsualizar el XML antes de enviar.
    """
    datos.auto_enviar = False
    return await firmar_y_enviar(datos)


# ══════════════════════════════════════════════════════════════
# STATEFUL — EMITIR (emisor registrado en YeparDTEcore)
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
    nombre:   str
    cantidad: float = 1
    precio:   float
    exento:   bool = False

class EmitirInput(BaseModel):
    tipo:        int
    receptor:    ReceptorInput
    items:       list[ItemInput]
    exento:      bool = False
    fecha:       Optional[str] = None
    auto_enviar: bool = True
    referencia:  Optional[dict] = None


@router.post("/emitir")
async def emitir_dte(
    datos:  EmitirInput,
    emisor: Emisor = Depends(get_emisor_by_api_key),
    db:     AsyncSession = Depends(get_db),
):
    tipos_validos = {33, 34, 39, 41, 52, 56, 61}
    if datos.tipo not in tipos_validos:
        raise HTTPException(422, f"Tipo DTE no válido: {datos.tipo}")

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

    doc = resultado
    tipo_label = {
        33: "Factura Electrónica", 34: "Factura Exenta",
        39: "Boleta Electrónica",  41: "Boleta Exenta",
        52: "Guía de Despacho",    56: "Nota de Débito",
        61: "Nota de Crédito",
    }

    track_id   = None
    estado_sii = "BORRADOR"
    if datos.auto_enviar and doc.get("xml_firmado"):
        try:
            cert = (await db.execute(
                select(Certificado).where(
                    Certificado.emisor_id == emisor.id,
                    Certificado.activo    == True,
                ).limit(1)
            )).scalar_one_or_none()

            if cert:
                sender = SIISender(
                    ambiente=emisor.ambiente or "certificacion",
                    nro_resol=emisor.nro_resolucion or "0",
                    fch_resol=emisor.fch_resolucion or "2000-01-01",
                )
                rut_enviador = cert.rut_firmante or emisor.rut
                from app.services.firma_digital import FirmaDigital
                firma_svc = FirmaDigital(
                    bytes(cert.certificado_p12),
                    cert.certificado_password or "",
                    ambiente=emisor.ambiente or "certificacion",
                )
                sobre_xml = await sender.construir_sobre(
                    dtes_xml     = [doc["xml_firmado"]],
                    rut_emisor   = emisor.rut,
                    rut_enviador = rut_enviador,
                    firma_service= firma_svc,
                )
                resultado_envio = await sender.enviar_sobre(
                    sobre_xml    = sobre_xml,
                    rut_emisor   = emisor.rut,
                    rut_enviador = rut_enviador,
                    p12_bytes    = bytes(cert.certificado_p12),
                    password     = cert.certificado_password or "",
                )
                track_id   = resultado_envio.get("track_id")
                estado_sii = resultado_envio.get("estado", "ENVIADO")
        except Exception as e:
            logger.error(f"Error enviando al SII: {e}", exc_info=True)
            estado_sii = "ERROR_ENVIO"

    return {
        "ok":          True,
        "tipo":        tipo_label.get(datos.tipo, str(datos.tipo)),
        "folio":       doc.get("folio"),
        "folio_fmt":   doc.get("folio_fmt"),
        "monto_total": doc.get("monto_total"),
        "estado":      estado_sii,
        "track_id":    track_id,
        "fecha":       fecha,
        "receptor":    datos.receptor.nombre,
        "xml_firmado": doc.get("xml_firmado"),
        "ambiente":    emisor.ambiente or "certificacion",
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
    cert = (await db.execute(
        select(Certificado).where(
            Certificado.emisor_id == emisor.id,
            Certificado.activo    == True,
        ).limit(1)
    )).scalar_one_or_none()
    if not cert:
        raise HTTPException(400, "No hay certificado configurado")

    sender = SIISender(
        ambiente=emisor.ambiente or "certificacion",
        nro_resol=emisor.nro_resolucion or "0",
        fch_resol=emisor.fch_resolucion or "2000-01-01",
    )
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
