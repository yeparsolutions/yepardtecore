# app/api/v1/endpoints/certificacion_guia.py
# ══════════════════════════════════════════════════════════════
# SET GUÍA DE DESPACHO — NÚMERO DE ATENCIÓN: 4841546
# 3 documentos: tipo 52
#
# CASO 1: Traslado interno (IndTraslado=5)
#         - Receptor = mismo RUT emisor
#         - Sin precios (operación no constituye venta)
# CASO 2: Venta, despacha el emisor (IndTraslado=1)
#         - Con precios, receptor cliente
# CASO 3: Venta, retira el cliente (IndTraslado=2)
#         - Con precios, receptor cliente
#
# IndTraslado valores SII:
#   1 = Operación constituye venta (despacha vendedor)
#   2 = Ventas por efectuar (retira comprador)
#   3 = Consignaciones
#   4 = Entrega gratuita
#   5 = Traslados internos
#   6 = Otros traslados no venta
#   7 = Guía de devolución
#   8 = Traslado para exportación
#   9 = Venta para exportación
# ══════════════════════════════════════════════════════════════

import logging
from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.base import get_db
from app.models.emisor import Emisor
from app.models.certificado import Certificado
from app.services.dte_service import DTEService
from app.services.firma_digital import FirmaDigital
from app.services.sii_sender import SIISender

logger = logging.getLogger("yepardtecore.cert_guia")
router = APIRouter(prefix="/certificacion-guia", tags=["Certificacion Guia Despacho"])

NATENCION = "4841546"

RECEPTOR_CLIENTE = {
    "rut":          "77777777-7",
    "razon_social": "EMPRESA LTDA",
    "giro":         "COMPUTACION",
    "direccion":    "SAN DIEGO 2222",
    "comuna":       "LA FLORIDA",
    "ciudad":       "SANTIAGO",
}


def _ref_set(n: int, fecha: str) -> dict:
    return {
        "tipo_doc_ref": "SET",
        "folio_ref":    n,
        "fecha_ref":    fecha,
        "razon_ref":    f"CASO {NATENCION}-{n}",
    }


async def _emitir_set(fecha: str, service: DTEService, emisor_id: int,
                      emisor_rut: str, emisor_razon: str, emisor_giro: str,
                      emisor_dir: str, emisor_comuna: str, emisor_ciudad: str):
    """
    3 casos del Set Guía de Despacho N° 4841546.

    Caso 1: Traslado interno — receptor = emisor, sin precios
    Caso 2: Venta IndTraslado=1 — con precios
    Caso 3: Venta IndTraslado=2 — con precios
    """
    xmls_firmados: list[str] = []
    folios: dict[int, int] = {}
    errores: list[str] = []

    async def emitir(caso_n: int, datos: dict):
        try:
            r = await service.emitir(
                emisor_id=emisor_id,
                datos={**datos, "emisor_id": emisor_id},
                auto_enviar=False,
            )
            xmls_firmados.append(r["xml_firmado"])
            folios[caso_n] = r["folio"]
            logger.info(f"[CERT GUIA] Caso {caso_n} OK folio={r['folio']} total=${r['monto_total']:,.0f}")
        except Exception as e:
            errores.append(f"Caso {caso_n}: {e}")
            logger.error(f"[CERT GUIA] Error caso {caso_n}: {e}", exc_info=True)

    # ── CASO 1 — Traslado interno entre bodegas (IndTraslado=5) ─
    # Receptor = mismo emisor (exigido por SII para traslado interno)
    # Sin precios — no es venta, no genera montos
    receptor_interno = {
        "rut":          emisor_rut,
        "razon_social": emisor_razon,
        "giro":         emisor_giro,
        "direccion":    emisor_dir,
        "comuna":       emisor_comuna,
        "ciudad":       emisor_ciudad,
    }
    await emitir(1, {
        "tipo_dte": 52, "fecha_emision": fecha,
        "receptor": receptor_interno,
        "indicador_traslado": 5,
        "indicador_despacho": 1,   # Traslado interno: despacha el emisor
        "items": [
            {"nombre": "ITEM 1", "cantidad":  80, "precio_unitario": 0, "exento": True},
            {"nombre": "ITEM 2", "cantidad": 127, "precio_unitario": 0, "exento": True},
            {"nombre": "ITEM 3", "cantidad":  87, "precio_unitario": 0, "exento": True},
        ],
        "referencias": [_ref_set(1, fecha)],
    })

    # ── CASO 2 — Venta, despacha emisor (IndTraslado=1) ────────
    await emitir(2, {
        "tipo_dte": 52, "fecha_emision": fecha,
        "receptor": RECEPTOR_CLIENTE,
        "indicador_traslado": 1,
        "indicador_despacho": 2,   # Venta: despacha emisor al local del cliente
        "items": [
            {"nombre": "ITEM 1", "cantidad": 361, "precio_unitario": 7374, "exento": False},
            {"nombre": "ITEM 2", "cantidad": 700, "precio_unitario": 1664, "exento": False},
        ],
        "referencias": [_ref_set(2, fecha)],
    })

    # ── CASO 3 — Venta, retira cliente (IndTraslado=2) ─────────
    await emitir(3, {
        "tipo_dte": 52, "fecha_emision": fecha,
        "receptor": RECEPTOR_CLIENTE,
        "indicador_traslado": 2,
        "indicador_despacho": 3,   # Venta: retira el cliente
        "items": [
            {"nombre": "ITEM 1", "cantidad": 174, "precio_unitario": 2001, "exento": False},
            {"nombre": "ITEM 2", "cantidad": 431, "precio_unitario": 5759, "exento": False},
        ],
        "referencias": [_ref_set(3, fecha)],
    })

    return xmls_firmados, folios, errores


async def _get_emisor_y_cert(emisor_id: int, db: AsyncSession):
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(status_code=404, detail=f"Emisor {emisor_id} no encontrado")
    cert_result = await db.execute(
        select(Certificado).where(
            Certificado.emisor_id == emisor_id,
            Certificado.activo == True
        ).limit(1)
    )
    cert = cert_result.scalar_one_or_none()
    if not cert or not cert.certificado_p12:
        raise HTTPException(status_code=400, detail="Sin certificado .p12 cargado")
    return emisor, cert


@router.post("/generar-xml", summary="Genera EnvioDTE SET GUIA DESPACHO N° 4841546")
async def generar_xml_guia(
    emisor_id: int,
    fecha_override: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    emisor, cert = await _get_emisor_y_cert(emisor_id, db)
    fecha   = fecha_override or date.today().isoformat()
    service = DTEService(db)

    xmls_firmados, folios, errores = await _emitir_set(
        fecha, service, emisor_id,
        emisor_rut=emisor.rut,
        emisor_razon=emisor.razon_social,
        emisor_giro=emisor.giro,
        emisor_dir=emisor.direccion,
        emisor_comuna=emisor.comuna,
        emisor_ciudad=emisor.ciudad,
    )

    if not xmls_firmados:
        raise HTTPException(status_code=500,
            detail=f"No se generó ningún documento. Errores: {'; '.join(errores)}")

    firma  = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    sender = SIISender(ambiente=emisor.ambiente)
    try:
        sobre_xml = await sender.construir_sobre(
            dtes_xml=xmls_firmados,
            rut_emisor=emisor.rut,
            rut_enviador=firma.rut_certificado or emisor.rut,
            firma_service=firma,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error armando sobre: {e}")

    rut_limpio = emisor.rut.replace(".", "").replace("-", "")
    nombre     = f"EnvioDTE_SetGuia_{rut_limpio}_{fecha.replace('-','')}.xml"
    logger.info(f"[CERT GUIA] Sobre listo {len(xmls_firmados)}/3" + (f" errores: {errores}" if errores else " ✓"))

    return Response(
        content=sobre_xml.encode("ISO-8859-1"),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{nombre}"',
            "X-Casos-Generados": str(len(xmls_firmados)),
            "X-Casos-Error":     str(len(errores)),
            "X-Errores-Detalle": " | ".join(errores) if errores else "",
            "X-NroAtencion":     NATENCION,
        }
    )


@router.post("/enviar", summary="Genera Y envía directo al SII")
async def enviar_guia(
    emisor_id: int,
    fecha_override: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    emisor, cert = await _get_emisor_y_cert(emisor_id, db)
    fecha   = fecha_override or date.today().isoformat()
    service = DTEService(db)

    xmls_firmados, folios, errores = await _emitir_set(
        fecha, service, emisor_id,
        emisor_rut=emisor.rut,
        emisor_razon=emisor.razon_social,
        emisor_giro=emisor.giro,
        emisor_dir=emisor.direccion,
        emisor_comuna=emisor.comuna,
        emisor_ciudad=emisor.ciudad,
    )

    if not xmls_firmados:
        raise HTTPException(status_code=500,
            detail=f"No se generó ningún documento. Errores: {'; '.join(errores)}")

    firma  = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    sender = SIISender(ambiente=emisor.ambiente)
    try:
        sobre_xml = await sender.construir_sobre(
            dtes_xml=xmls_firmados,
            rut_emisor=emisor.rut,
            rut_enviador=firma.rut_certificado or emisor.rut,
            firma_service=firma,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error armando sobre: {e}")

    try:
        resultado = await sender.enviar_sobre(
            sobre_xml=sobre_xml,
            rut_emisor=emisor.rut,
            rut_enviador=firma.rut_certificado or emisor.rut,
            p12_bytes=cert.certificado_p12,
            password=cert.certificado_password or "",
            auth_p12_bytes=cert.certificado_auth_p12 or None,
            auth_password=cert.certificado_auth_password or None,
        )
        return {
            "estado":         resultado.get("estado"),
            "track_id":       resultado.get("track_id"),
            "mensaje":        resultado.get("mensaje"),
            "docs_generados": len(xmls_firmados),
            "errores":        errores,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error enviando al SII: {e}")
