# app/api/v1/endpoints/certificacion_facturas2.py
# ══════════════════════════════════════════════════════════════
# SET BASICO FACTURAS — NÚMERO DE ATENCIÓN: 4841543
# 8 documentos: 4 Facturas (33), 3 NC (61), 1 ND (56)
# Mismo patrón que certificacion_facturas.py, nuevos valores
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

logger = logging.getLogger("yepardtecore.cert_facturas2")
router = APIRouter(prefix="/certificacion-facturas2", tags=["Certificacion Facturas 2"])

NATENCION = "4841543"

RECEPTOR = {
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


def _ref_doc(tipo: int, folio: int, fecha: str, cod: int, razon: str) -> dict:
    return {
        "tipo_doc_ref": tipo,
        "folio_ref":    folio,
        "fecha_ref":    fecha,
        "cod_ref":      cod,
        "razon_ref":    razon,
    }


async def _emitir_set(fecha: str, service: DTEService, emisor_id: int):
    """
    8 casos del Set Básico N° 4841543.

    Caso 1: FAC 2 ítems afectos
    Caso 2: FAC descuentos por línea (6% y 13%)
    Caso 3: FAC afecto + exento
    Caso 4: FAC descuento global 14%
    Caso 5: NC CodRef=2 corrige giro → MntTotal=0
    Caso 6: NC CodRef=3 devolución parcial
    Caso 7: NC CodRef=1 anula FAC caso 3 (ítems exactos)
    Caso 8: ND CodRef=1 anula NC caso 5 → MntTotal=0 espejo
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
            logger.info(f"[CERT FAC2] Caso {caso_n} OK folio={r['folio']} total=${r['monto_total']:,.0f}")
        except Exception as e:
            errores.append(f"Caso {caso_n}: {e}")
            logger.error(f"[CERT FAC2] Error caso {caso_n}: {e}", exc_info=True)

    # ── CASO 1 ────────────────────────────────────────────────
    await emitir(1, {
        "tipo_dte": 33, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "Cajón AFECTO",   "cantidad": 143, "precio_unitario": 2075, "exento": False},
            {"nombre": "Relleno AFECTO", "cantidad":  61, "precio_unitario": 3420, "exento": False},
        ],
        "referencias": [_ref_set(1, fecha)],
    })

    # ── CASO 2 ────────────────────────────────────────────────
    await emitir(2, {
        "tipo_dte": 33, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "Pañuelo AFECTO", "cantidad": 470, "precio_unitario": 3697, "exento": False, "descuento_pct": 6},
            {"nombre": "ITEM 2 AFECTO",  "cantidad": 404, "precio_unitario": 2755, "exento": False, "descuento_pct": 13},
        ],
        "referencias": [_ref_set(2, fecha)],
    })

    # ── CASO 3 ────────────────────────────────────────────────
    await emitir(3, {
        "tipo_dte": 33, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "Pintura B y W AFECTO",  "cantidad":  35, "precio_unitario":  4282, "exento": False},
            {"nombre": "ITEM 2 AFECTO",          "cantidad": 188, "precio_unitario":  3354, "exento": False},
            {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad":   1, "precio_unitario": 34968, "exento": True},
        ],
        "referencias": [_ref_set(3, fecha)],
    })

    # ── CASO 4 — descuento global 14% sobre afectos ───────────
    await emitir(4, {
        "tipo_dte": 33, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "ITEM 1 AFECTO",          "cantidad": 230, "precio_unitario": 3577, "exento": False},
            {"nombre": "ITEM 2 AFECTO",          "cantidad":  98, "precio_unitario": 4006, "exento": False},
            {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad":   2, "precio_unitario": 6797, "exento": True},
        ],
        "descuento_global_pct": 14,
        "referencias": [_ref_set(4, fecha)],
    })

    # ── CASO 5 — NC corrige giro (CodRef=2) → MntTotal=0 ──────
    if 1 in folios:
        await emitir(5, {
            "tipo_dte": 61, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "CORRIGE GIRO DEL RECEPTOR", "cantidad": 1,
                 "precio_unitario": 0, "exento": True},
            ],
            "forzar_monto_cero": True,
            "referencias": [
                _ref_set(5, fecha),
                _ref_doc(33, folios[1], fecha, 2, "CORRIGE GIRO DEL RECEPTOR"),
            ],
        })

    # ── CASO 6 — NC devolución parcial (CodRef=3) ─────────────
    if 2 in folios:
        await emitir(6, {
            "tipo_dte": 61, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "Pañuelo AFECTO", "cantidad": 172, "precio_unitario": 3697, "exento": False, "descuento_pct": 6},
                {"nombre": "ITEM 2 AFECTO",  "cantidad": 274, "precio_unitario": 2755, "exento": False, "descuento_pct": 13},
            ],
            "referencias": [
                _ref_set(6, fecha),
                _ref_doc(33, folios[2], fecha, 3, "DEVOLUCION DE MERCADERIAS"),
            ],
        })

    # ── CASO 7 — NC anula FAC caso 3 (CodRef=1, ítems exactos) ─
    if 3 in folios:
        await emitir(7, {
            "tipo_dte": 61, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "Pintura B y W AFECTO",  "cantidad":  35, "precio_unitario":  4282, "exento": False},
                {"nombre": "ITEM 2 AFECTO",          "cantidad": 188, "precio_unitario":  3354, "exento": False},
                {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad":   1, "precio_unitario": 34968, "exento": True},
            ],
            "referencias": [
                _ref_set(7, fecha),
                _ref_doc(33, folios[3], fecha, 1, "ANULA FACTURA"),
            ],
        })

    # ── CASO 8 — ND anula NC5 (CodRef=1) → MntTotal=0 espejo ──
    if 5 in folios:
        await emitir(8, {
            "tipo_dte": 56, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "CORRIGE GIRO DEL RECEPTOR", "cantidad": 1,
                 "precio_unitario": 0, "exento": True},
            ],
            "forzar_monto_cero": True,
            "referencias": [
                _ref_set(8, fecha),
                _ref_doc(61, folios[5], fecha, 1, "ANULA NOTA DE CREDITO ELECTRONICA"),
            ],
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


@router.post("/generar-xml", summary="Genera EnvioDTE SET BASICO Facturas N° 4841543")
async def generar_xml_facturas2(
    emisor_id: int,
    fecha_override: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    emisor, cert = await _get_emisor_y_cert(emisor_id, db)
    fecha   = fecha_override or date.today().isoformat()
    service = DTEService(db)

    xmls_firmados, folios, errores = await _emitir_set(fecha, service, emisor_id)

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
    nombre     = f"EnvioDTE_SetBasico2_{rut_limpio}_{fecha.replace('-','')}.xml"
    logger.info(f"[CERT FAC2] Sobre listo {len(xmls_firmados)}/8" + (f" errores: {errores}" if errores else " ✓"))

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
async def enviar_facturas2(
    emisor_id: int,
    fecha_override: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    emisor, cert = await _get_emisor_y_cert(emisor_id, db)
    fecha   = fecha_override or date.today().isoformat()
    service = DTEService(db)

    xmls_firmados, folios, errores = await _emitir_set(fecha, service, emisor_id)

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
