# app/api/v1/endpoints/certificacion_facturas.py
# ══════════════════════════════════════════════════════════════
# SET BASICO de Facturas — Número de Atención: 4784337
# 8 documentos: 4 Facturas (33), 3 NC (61), 1 ND (56)
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

logger = logging.getLogger("yepardtecore.cert_facturas")
router = APIRouter(prefix="/certificacion-facturas", tags=["Certificacion Facturas"])

NATENCION = "4784337"

# Receptor fijo para el set de prueba de facturas
RECEPTOR = {
    "rut":          "77777777-7",
    "razon_social": "EMPRESA LTDA",
    "giro":         "COMPUTACION",
    "direccion":    "SAN DIEGO 2222",
    "comuna":       "LA FLORIDA",
    "ciudad":       "SANTIAGO",
}


def _ref_set(n: int, fecha: str) -> dict:
    """Referencia estándar al caso N del set de prueba."""
    return {
        "tipo_doc_ref": 801,
        "folio_ref":    n,
        "fecha_ref":    fecha,
        "cod_ref":      "SET",
        "razon_ref":    f"CASO-{NATENCION}-{n}",
    }


def _ref_doc(tipo: int, folio: int, fecha: str, cod: int, razon: str) -> dict:
    """Referencia a otro DTE (factura / NC / ND)."""
    return {
        "tipo_doc_ref": tipo,
        "folio_ref":    folio,
        "fecha_ref":    fecha,
        "cod_ref":      cod,
        "razon_ref":    razon,
    }


@router.post("/generar-xml", summary="Genera EnvioDTE SET BASICO Facturas (N° Atención 4784337)")
async def generar_xml_facturas(
    emisor_id: int,
    fecha_override: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    # ── Validar emisor ────────────────────────────────────────
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
    logger.info(f"[CERT FAC] Certificado OK: {cert.rut_firmante}")

    fecha = fecha_override or date.today().isoformat()
    service = DTEService(db)
    xmls_firmados = []
    folios: dict[int, int] = {}
    errores = []

    async def emitir(caso_n: int, datos: dict):
        """Emite un DTE y guarda el folio."""
        try:
            r = await service.emitir(
                emisor_id=emisor_id,
                datos={**datos, "emisor_id": emisor_id},
                auto_enviar=False,
            )
            xmls_firmados.append(r["xml_firmado"])
            folios[caso_n] = r["folio"]
            logger.info(f"[CERT FAC] Caso {caso_n} OK folio={r['folio']} total=${r['monto_total']:,.0f}")
        except Exception as e:
            errores.append(f"Caso {caso_n}: {e}")
            logger.error(f"[CERT FAC] Error caso {caso_n}: {e}", exc_info=True)

    # ── CASO 1 — Factura 2 ítems afectos ─────────────────────
    await emitir(1, {
        "tipo_dte": 33,
        "fecha_emision": fecha,
        "receptor": RECEPTOR,
        "items": [
            {"nombre": "Cajón AFECTO",  "cantidad": 185, "precio_unitario": 4490, "exento": False},
            {"nombre": "Relleno AFECTO","cantidad": 78,  "precio_unitario": 7503, "exento": False},
        ],
        "referencias": [_ref_set(1, fecha)],
    })

    # ── CASO 2 — Factura con descuentos por línea ─────────────
    await emitir(2, {
        "tipo_dte": 33,
        "fecha_emision": fecha,
        "receptor": RECEPTOR,
        "items": [
            {"nombre": "Pañuelo AFECTO","cantidad": 963, "precio_unitario": 7412, "exento": False, "descuento_pct": 12},
            {"nombre": "ITEM 2 AFECTO", "cantidad": 914, "precio_unitario": 6458, "exento": False, "descuento_pct": 30},
        ],
        "referencias": [_ref_set(2, fecha)],
    })

    # ── CASO 3 — Factura afecto + exento ─────────────────────
    await emitir(3, {
        "tipo_dte": 33,
        "fecha_emision": fecha,
        "receptor": RECEPTOR,
        "items": [
            {"nombre": "Pintura B&W AFECTO",    "cantidad": 93,  "precio_unitario": 8524,  "exento": False},
            {"nombre": "ITEM 2 AFECTO",          "cantidad": 270, "precio_unitario": 4567,  "exento": False},
            {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad": 1,   "precio_unitario": 35521, "exento": True},
        ],
        "referencias": [_ref_set(3, fecha)],
    })

    # ── CASO 4 — Factura con descuento global 28% ────────────
    await emitir(4, {
        "tipo_dte": 33,
        "fecha_emision": fecha,
        "receptor": RECEPTOR,
        "items": [
            {"nombre": "ITEM 1 AFECTO",          "cantidad": 544, "precio_unitario": 7572, "exento": False},
            {"nombre": "ITEM 2 AFECTO",          "cantidad": 230, "precio_unitario": 9462, "exento": False},
            {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad": 2,   "precio_unitario": 6858, "exento": True},
        ],
        "descuento_global_pct": 28,
        "referencias": [_ref_set(4, fecha)],
    })

    # ── CASO 5 — NC corrige giro receptor (ref CASO 1) ────────
    if 1 in folios:
        await emitir(5, {
            "tipo_dte": 61,
            "fecha_emision": fecha,
            "receptor": RECEPTOR,
            "items": [
                {"nombre": "Cajón AFECTO",  "cantidad": 185, "precio_unitario": 4490, "exento": False},
                {"nombre": "Relleno AFECTO","cantidad": 78,  "precio_unitario": 7503, "exento": False},
            ],
            "referencias": [
                _ref_doc(33, folios[1], fecha, 1, "CORRIGE GIRO DEL RECEPTOR"),
                _ref_set(5, fecha),
            ],
        })

    # ── CASO 6 — NC devolución parcial (ref CASO 2) ───────────
    if 2 in folios:
        await emitir(6, {
            "tipo_dte": 61,
            "fecha_emision": fecha,
            "receptor": RECEPTOR,
            "items": [
                {"nombre": "Pañuelo AFECTO","cantidad": 353, "precio_unitario": 7412, "exento": False, "descuento_pct": 12},
                {"nombre": "ITEM 2 AFECTO", "cantidad": 620, "precio_unitario": 6458, "exento": False, "descuento_pct": 30},
            ],
            "referencias": [
                _ref_doc(33, folios[2], fecha, 3, "DEVOLUCION DE MERCADERIAS"),
                _ref_set(6, fecha),
            ],
        })

    # ── CASO 7 — NC anula factura (ref CASO 3) ────────────────
    if 3 in folios:
        await emitir(7, {
            "tipo_dte": 61,
            "fecha_emision": fecha,
            "receptor": RECEPTOR,
            "items": [
                {"nombre": "Pintura B&W AFECTO",    "cantidad": 93,  "precio_unitario": 8524,  "exento": False},
                {"nombre": "ITEM 2 AFECTO",          "cantidad": 270, "precio_unitario": 4567,  "exento": False},
                {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad": 1,   "precio_unitario": 35521, "exento": True},
            ],
            "referencias": [
                _ref_doc(33, folios[3], fecha, 1, "ANULA FACTURA"),
                _ref_set(7, fecha),
            ],
        })

    # ── CASO 8 — ND anula NC caso 5 (ref CASO 5) ─────────────
    if 5 in folios:
        await emitir(8, {
            "tipo_dte": 56,
            "fecha_emision": fecha,
            "receptor": RECEPTOR,
            "items": [
                {"nombre": "Cajón AFECTO",  "cantidad": 185, "precio_unitario": 4490, "exento": False},
                {"nombre": "Relleno AFECTO","cantidad": 78,  "precio_unitario": 7503, "exento": False},
            ],
            "referencias": [
                _ref_doc(61, folios[5], fecha, 2, "ANULA NOTA DE CREDITO ELECTRONICA"),
                _ref_set(8, fecha),
            ],
        })

    if not xmls_firmados:
        raise HTTPException(
            status_code=500,
            detail=f"No se generó ningún documento. Errores: {'; '.join(errores)}"
        )

    # ── Armar sobre EnvioDTE ──────────────────────────────────
    firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    sender = SIISender(ambiente=emisor.ambiente)
    try:
        sobre_xml = sender.construir_sobre(
            dtes_xml=xmls_firmados,
            rut_emisor=emisor.rut,
            rut_enviador="25648612-1",
            firma_service=firma,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error armando sobre: {e}")

    rut_limpio = emisor.rut.replace(".", "").replace("-", "")
    nombre     = f"EnvioDTE_SetBasico_{rut_limpio}_{fecha.replace('-','')}.xml"

    logger.info(
        f"[CERT FAC] Sobre listo {len(xmls_firmados)}/8 docs"
        + (f" — errores: {errores}" if errores else " ✓")
    )

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
