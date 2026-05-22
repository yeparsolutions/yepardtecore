# app/api/v1/endpoints/certificacion_exentas.py
# ══════════════════════════════════════════════════════════════
# SET FACTURA EXENTA — NÚMERO DE ATENCIÓN: 4841548
# 8 documentos: 3 FAC exentas (34), 3 NC (61), 2 ND (56)
#
# REGLA CLAVE: Tipo 34 → solo MntExe + MntTotal, SIN IVA
# NC caso 4: CodRef=2 corrige giro → MntTotal=0
# ND caso 5: CodRef=1 anula NC4   → MntTotal=0 espejo
# NC caso 7: CodRef=3 modifica monto FAC6 (Capacit1)
# ND caso 8: CodRef=3 modifica monto FAC6 (Capacit2)
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

logger = logging.getLogger("yepardtecore.cert_exentas")
router = APIRouter(prefix="/certificacion-exentas", tags=["Certificacion Exentas"])

NATENCION = "4841548"

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
    8 casos del Set Factura Exenta N° 4841548.

    Caso 1: FAC34 Horas Programador 8×5187
    Caso 2: NC CodRef=3 modifica monto caso1 (nuevo prc=648)
    Caso 3: FAC34 2 ítems consultoría
    Caso 4: NC CodRef=2 corrige giro caso3 → MntTotal=0
    Caso 5: ND CodRef=1 anula NC4 → MntTotal=0 espejo
    Caso 6: FAC34 2 ítems capacitación
    Caso 7: NC CodRef=3 modifica monto Capacit1 (nuevo=159439)
    Caso 8: ND CodRef=3 modifica monto Capacit2 (nuevo=42385)

    IMPORTANTE: todos los ítems son exento=True (tipo 34)
    xml_builder debe emitir solo MntExe + MntTotal, sin IVA
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
            logger.info(f"[CERT EXE] Caso {caso_n} OK folio={r['folio']} total=${r['monto_total']:,.0f}")
        except Exception as e:
            errores.append(f"Caso {caso_n}: {e}")
            logger.error(f"[CERT EXE] Error caso {caso_n}: {e}", exc_info=True)

    # ── CASO 1 — FAC exenta, 1 ítem ───────────────────────────
    # Horas Programador 8×5.187 = 41.496 (todo exento)
    await emitir(1, {
        "tipo_dte": 34, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "HORAS PROGRAMADOR", "cantidad": 8, "unidad": "Hora",
             "precio_unitario": 5187, "exento": True},
        ],
        "referencias": [_ref_set(1, fecha)],
    })

    # ── CASO 2 — NC modifica monto caso 1 (CodRef=3) ──────────
    # Nuevo valor unitario=648, misma cantidad=8
    if 1 in folios:
        await emitir(2, {
            "tipo_dte": 61, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "HORAS PROGRAMADOR", "cantidad": 8,
                 "precio_unitario": 648, "exento": True},
            ],
            "referencias": [
                _ref_set(2, fecha),
                _ref_doc(34, folios[1], fecha, 3, "MODIFICA MONTO"),
            ],
        })

    # ── CASO 3 — FAC exenta, 2 ítems consultoría ──────────────
    await emitir(3, {
        "tipo_dte": 34, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "SERV CONSULTORIA FACT ELECTRONICA",  "cantidad": 1,
             "precio_unitario": 286842, "exento": True},
            {"nombre": "SERV CONSULTORIA GUIA DESPACHO ELECT", "cantidad": 1,
             "precio_unitario": 235184, "exento": True},
        ],
        "referencias": [_ref_set(3, fecha)],
    })

    # ── CASO 4 — NC corrige giro caso 3 (CodRef=2) → MntTotal=0
    # Ítems = los de FAC caso 3 con precio=0
    if 3 in folios:
        await emitir(4, {
            "tipo_dte": 61, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "SERV CONSULTORIA FACT ELECTRONICA",   "cantidad": 1, "precio_unitario": 0, "exento": True},
                {"nombre": "SERV CONSULTORIA GUIA DESPACHO ELECT", "cantidad": 1, "precio_unitario": 0, "exento": True},
            ],
            "forzar_monto_cero": True,
            "referencias": [
                _ref_set(4, fecha),
                _ref_doc(34, folios[3], fecha, 2, "CORRIGE GIRO"),
            ],
        })

    # ── CASO 5 — ND anula NC4 (CodRef=1) → MntTotal=0 espejo ──
    # Ítems = los de NC4 (= FAC caso 3 con precio=0)
    if 4 in folios:
        await emitir(5, {
            "tipo_dte": 56, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "SERV CONSULTORIA FACT ELECTRONICA",   "cantidad": 1, "precio_unitario": 0, "exento": True},
                {"nombre": "SERV CONSULTORIA GUIA DESPACHO ELECT", "cantidad": 1, "precio_unitario": 0, "exento": True},
            ],
            "forzar_monto_cero": True,
            "referencias": [
                _ref_set(5, fecha),
                _ref_doc(61, folios[4], fecha, 1, "ANULA NOTA DE CREDITO ELECTRONICA"),
            ],
        })

    # ── CASO 6 — FAC exenta, 2 ítems capacitación ─────────────
    await emitir(6, {
        "tipo_dte": 34, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "CAPACITACION USO CIGÜEÑALES", "cantidad": 1,
             "precio_unitario": 318879, "exento": True},
            {"nombre": "CAPACITACION USO PLC's CNC",   "cantidad": 1,
             "precio_unitario": 211924, "exento": True},
        ],
        "referencias": [_ref_set(6, fecha)],
    })

    # ── CASO 7 — NC modifica monto Capacit1 (CodRef=3) ────────
    # Nuevo valor unitario=159.439
    if 6 in folios:
        await emitir(7, {
            "tipo_dte": 61, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "CAPACITACION USO CIGÜEÑALES", "cantidad": 1,
                 "precio_unitario": 159439, "exento": True},
            ],
            "referencias": [
                _ref_set(7, fecha),
                _ref_doc(34, folios[6], fecha, 3, "MODIFICA MONTO"),
            ],
        })

    # ── CASO 8 — ND modifica monto Capacit2 (CodRef=3) ────────
    # Nuevo valor unitario=42.385
    if 6 in folios:
        await emitir(8, {
            "tipo_dte": 56, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "CAPACITACION USO PLC's CNC", "cantidad": 1,
                 "precio_unitario": 42385, "exento": True},
            ],
            "referencias": [
                _ref_set(8, fecha),
                _ref_doc(34, folios[6], fecha, 3, "MODIFICA MONTO"),
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


@router.post("/generar-xml", summary="Genera EnvioDTE SET FACTURA EXENTA N° 4841548")
async def generar_xml_exentas(
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
    nombre     = f"EnvioDTE_SetExentas_{rut_limpio}_{fecha.replace('-','')}.xml"
    logger.info(f"[CERT EXE] Sobre listo {len(xmls_firmados)}/8" + (f" errores: {errores}" if errores else " ✓"))

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
async def enviar_exentas(
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
