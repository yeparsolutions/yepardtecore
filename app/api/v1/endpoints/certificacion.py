# app/api/v1/endpoints/certificacion.py
# ══════════════════════════════════════════════════════════════
# Endpoints de Certificación SII
#
# Flujo correcto de certificación:
#   1. Generar y firmar las 5 boletas individualmente (con TED)
#   2. Armar el sobre EnvioBOLETA firmado
#   3. Devolver el XML para que el usuario lo suba
#      manualmente en maullin.sii.cl
#
# NOTA: El envío automático al SII se activará en producción
# a través de Vultr (IP fija chilena). Por ahora el XML
# se descarga y se sube manualmente al portal SII.
#
# Endpoints:
#   POST /v1/certificacion/generar-xml     — Genera XML descargable
#   GET  /v1/certificacion/preview/{id}   — Preview sin generar
# ══════════════════════════════════════════════════════════════

import logging
from datetime import date
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.db.base import get_db
from app.models.emisor import Emisor
from app.services.dte_service import DTEService
from app.services.firma_digital import FirmaDigital
from app.services.sii_sender import SIISender

logger = logging.getLogger("yepardtecore.certificacion")

router = APIRouter(prefix="/certificacion", tags=["Certificación SII"])


# ── Casos oficiales del set de certificación SII ─────────────
def _casos(fecha: str) -> list[dict]:
    """
    5 casos oficiales que el SII exige para certificar boletas.
    Fuente: Manual de Certificación DTE, SII Chile.
    """
    return [
        {
            "caso": 1, "descripcion": "Boleta afecta simple (1 item sin descuento)",
            "tipo_dte": 39,
            "receptor": {"rut": "66666666-6", "razon_social": "Consumidor Final"},
            "items": [{"nombre": "Producto certificación caso 1", "cantidad": 1,
                       "precio_unitario": 10000, "exento": False}],
            "fecha_emision": fecha, "forma_pago": 1,
        },
        {
            "caso": 2, "descripcion": "Boleta afecta con descuento por línea (10%)",
            "tipo_dte": 39,
            "receptor": {"rut": "66666666-6", "razon_social": "Consumidor Final"},
            "items": [{"nombre": "Producto con descuento línea", "cantidad": 2,
                       "precio_unitario": 15000, "descuento_pct": 10.0, "exento": False}],
            "fecha_emision": fecha, "forma_pago": 1,
        },
        {
            "caso": 3, "descripcion": "Boleta afecta con 3 ítems distintos",
            "tipo_dte": 39,
            "receptor": {"rut": "66666666-6", "razon_social": "Consumidor Final"},
            "items": [
                {"nombre": "Item certificación A", "cantidad": 1, "precio_unitario": 5000, "exento": False},
                {"nombre": "Item certificación B", "cantidad": 3, "precio_unitario": 3000, "exento": False},
                {"nombre": "Item certificación C", "cantidad": 2, "precio_unitario": 8500, "exento": False},
            ],
            "fecha_emision": fecha, "forma_pago": 1,
        },
        {
            "caso": 4, "descripcion": "Boleta exenta (tipo 41, sin IVA)",
            "tipo_dte": 41,
            "receptor": {"rut": "66666666-6", "razon_social": "Consumidor Final"},
            "items": [{"nombre": "Servicio exento de IVA", "cantidad": 1,
                       "precio_unitario": 20000, "exento": True}],
            "fecha_emision": fecha, "forma_pago": 1,
        },
        {
            "caso": 5, "descripcion": "Boleta afecta con descuento global (15%)",
            "tipo_dte": 39,
            "receptor": {"rut": "66666666-6", "razon_social": "Consumidor Final"},
            "items": [{"nombre": "Producto descuento global", "cantidad": 4,
                       "precio_unitario": 12000, "exento": False}],
            "descuento_global_pct": 15.0,
            "fecha_emision": fecha, "forma_pago": 1,
        },
    ]


# ── Endpoint principal ────────────────────────────────────────

@router.post(
    "/generar-xml",
    summary="Generar XML de certificación (para subir manualmente al SII)",
    response_class=Response,
)
async def generar_xml_certificacion(
    emisor_id: int,
    fecha_override: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Genera el sobre **EnvioBOLETA** con los 5 casos oficiales del SII,
    listo para subir manualmente en **maullin.sii.cl**.

    **Flujo:**
    1. Firma las 5 boletas individualmente (cada una con su TED)
    2. Arma el sobre `EnvioBOLETA` firmado
    3. Devuelve el XML como archivo descargable

    **Siguiente paso del usuario:**
    Ir a https://maullin.sii.cl → Factura Electrónica →
    Envío de DTE → subir el archivo descargado.
    """

    # ── Validar emisor ────────────────────────────────────────
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(status_code=404, detail=f"Emisor {emisor_id} no encontrado")
    if not emisor.activo:
        raise HTTPException(status_code=400, detail="El emisor está desactivado")
    if emisor.ambiente != "certificacion":
        raise HTTPException(
            status_code=400,
            detail=f"El emisor está en ambiente '{emisor.ambiente}'. "
                   "Este endpoint es solo para ambiente 'certificacion'."
        )

    cert = emisor.certificado_activo
    if not cert or not cert.certificado_p12:
        raise HTTPException(
            status_code=400,
            detail="El emisor no tiene certificado .p12 cargado. "
                   "Súbelo con POST /v1/certificados/{emisor_id}/subir"
        )

    fecha = fecha_override or date.today().isoformat()
    logger.info(f"[CERT] Generando XML certificación — emisor={emisor.rut} fecha={fecha}")

    # ── Emitir los 5 casos con auto_enviar=False ──────────────
    service = DTEService(db)
    casos   = _casos(fecha)
    xmls_firmados = []
    errores       = []

    for caso in casos:
        num  = caso.pop("caso")
        desc = caso.pop("descripcion")
        try:
            resultado = await service.emitir(
                emisor_id   = emisor_id,
                datos       = {**caso, "emisor_id": emisor_id},
                auto_enviar = False,
            )
            xmls_firmados.append(resultado["xml_firmado"])
            logger.info(f"[CERT] Caso {num} OK — folio={resultado['folio']}")
        except Exception as e:
            errores.append(f"Caso {num} ({desc}): {str(e)}")
            logger.error(f"[CERT] Error caso {num}: {e}", exc_info=True)

    if not xmls_firmados:
        raise HTTPException(
            status_code=500,
            detail=f"No se pudo generar ningún caso. Errores: {'; '.join(errores)}"
        )

    if errores:
        logger.warning(f"[CERT] {len(errores)} casos fallaron: {errores}")

    # ── Construir el sobre EnvioBOLETA ────────────────────────
    firma  = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    sender = SIISender(ambiente="certificacion")

    try:
        sobre_xml = sender.construir_sobre(
            dtes_xml      = xmls_firmados,
            rut_emisor    = emisor.rut,
            rut_enviador  = firma.rut_certificado or emisor.rut,
            firma_service = firma,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error armando el sobre: {str(e)}")

    logger.info(
        f"[CERT] Sobre generado OK — "
        f"{len(xmls_firmados)}/5 DTEs firmados"
        + (f" — {len(errores)} errores" if errores else "")
    )

    # ── Devolver como archivo descargable ─────────────────────
    rut_limpio  = emisor.rut.replace(".", "").replace("-", "")
    nombre_archivo = f"EnvioBOLETA_cert_{rut_limpio}_{fecha.replace('-','')}.xml"

    return Response(
        content     = sobre_xml.encode("ISO-8859-1"),
        media_type  = "application/xml",
        headers     = {
            "Content-Disposition": f'attachment; filename="{nombre_archivo}"',
            "X-Casos-Generados":   str(len(xmls_firmados)),
            "X-Casos-Error":       str(len(errores)),
        }
    )


@router.get(
    "/preview/{emisor_id}",
    summary="Preview de los 5 casos (sin generar ni firmar)",
)
async def preview(
    emisor_id: int,
    fecha_override: Optional[str] = None,
):
    """Muestra los 5 casos sin emitir nada."""
    fecha = fecha_override or date.today().isoformat()
    casos = _casos(fecha)
    return {
        "emisor_id":   emisor_id,
        "fecha":       fecha,
        "total_casos": 5,
        "casos": [
            {
                "caso":        i + 1,
                "tipo_dte":    c["tipo_dte"],
                "items":       len(c["items"]),
                "descuento_global": c.get("descuento_global_pct", 0),
            }
            for i, c in enumerate(casos)
        ],
    }
