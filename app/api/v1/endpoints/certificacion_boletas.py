# app/api/v1/endpoints/certificacion_boletas.py
# ══════════════════════════════════════════════════════════════
# SET DE PRUEBA BOLETA ELECTRÓNICA
# 5 casos exactos del Set_Prueba_BE.txt
#
# PRECIOS: el SII da precios CON IVA → convertir a neto (÷1.19)
# EXCEPTO ítems exentos → precio ya es final, sin dividir
#
# CASO 4 mixto: afecto con IVA + exento sin IVA
# CASO 5: requiere UnmdItem="Kg"
# REFERENCIAS: RazonRef="CASO-1", "CASO-2" etc. (con guion)
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

logger = logging.getLogger("yepardtecore.cert_boletas")
router = APIRouter(prefix="/certificacion-boletas", tags=["Certificacion Boletas"])


def _neto(precio_con_iva: int) -> int:
    """Precio con IVA → neto. Ítems exentos NO deben pasar por aquí."""
    return round(precio_con_iva / 1.19)


def _ref_caso(n: int, fecha: str) -> dict:
    """Referencia SET obligatoria. RazonRef usa guion: CASO-1, CASO-2..."""
    return {
        "tipo_doc_ref": "SET",
        "folio_ref":    n,
        "fecha_ref":    fecha,
        "razon_ref":    f"CASO-{n}",
    }


async def _emitir_set_boletas(fecha: str, service: DTEService, emisor_id: int):
    """
    Genera los 5 casos del set de prueba de boletas.
    Fuente única de verdad para /generar-xml y /enviar.

    Reglas clave:
    - Precios CON IVA del set → dividir por 1.19 para obtener neto
    - Ítems exentos: el precio ya es final, NO dividir
    - Caso 4: afecto (÷1.19) + exento (precio directo)
    - Caso 5: requiere unidad="Kg"
    - tipo_dte=39 para todos (boleta afecta)
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
            logger.info(f"[CERT BOL] Caso {caso_n} OK folio={r['folio']} total=${r['monto_total']:,.0f}")
        except Exception as e:
            errores.append(f"Caso {caso_n}: {e}")
            logger.error(f"[CERT BOL] Error caso {caso_n}: {e}", exc_info=True)

    RECEPTOR = {"rut": "66666666-6", "razon_social": "Consumidor Final"}

    # ── CASO 1 — Dos servicios afectos ────────────────────────
    # Cambio aceite 1×19.900 + Alineacion 1×9.900 (con IVA)
    await emitir(1, {
        "tipo_dte": 39, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "Cambio de aceite",      "cantidad": 1, "precio_unitario": 19900, "exento": False},
            {"nombre": "Alineacion y balanceo", "cantidad": 1, "precio_unitario": 9900,  "exento": False},
        ],
        "referencias": [_ref_caso(1, fecha)],
    })

    # ── CASO 2 — Un ítem afecto ────────────────────────────────
    # Papel de regalo 17×120 (con IVA)
    await emitir(2, {
        "tipo_dte": 39, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "Papel de regalo", "cantidad": 17, "precio_unitario": 120, "exento": False},
        ],
        "referencias": [_ref_caso(2, fecha)],
    })

    # ── CASO 3 — Dos ítems afectos ─────────────────────────────
    # Sandwic 2×1.500 + Bebida 2×550 (con IVA)
    await emitir(3, {
        "tipo_dte": 39, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "Sandwic", "cantidad": 2, "precio_unitario": 1500, "exento": False},
            {"nombre": "Bebida",  "cantidad": 2, "precio_unitario": 550,  "exento": False},
        ],
        "referencias": [_ref_caso(3, fecha)],
    })

    # ── CASO 4 — Mixto: afecto + exento ───────────────────────
    # item afecto 1: 8×1.590 con IVA → neto
    # item exento 2: 2×1.000 SIN IVA → precio directo (no dividir)
    await emitir(4, {
        "tipo_dte": 39, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "item afecto 1", "cantidad": 8, "precio_unitario": 1590, "exento": False},
            {"nombre": "item exento 2", "cantidad": 2, "precio_unitario": 1000,        "exento": True},
        ],
        "referencias": [_ref_caso(4, fecha)],
    })

    # ── CASO 5 — Con unidad de medida ─────────────────────────
    # Arroz 5×700 con IVA, unidad=Kg (obligatorio según set SII)
    await emitir(5, {
        "tipo_dte": 39, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "Arroz", "cantidad": 5, "precio_unitario": 700,
             "unidad": "Kg", "exento": False},
        ],
        "referencias": [_ref_caso(5, fecha)],
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


@router.post("/generar-xml", summary="Genera EnvioBOLETA Set de Prueba (5 casos)")
async def generar_xml_boletas(
    emisor_id: int,
    fecha_override: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    emisor, cert = await _get_emisor_y_cert(emisor_id, db)
    logger.info(f"[CERT BOL] Certificado OK: {cert.rut_firmante}")

    fecha   = fecha_override or date.today().isoformat()
    service = DTEService(db)

    xmls_firmados, folios, errores = await _emitir_set_boletas(fecha, service, emisor_id)

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
    nombre     = f"EnvioBOLETA_SetPrueba_{rut_limpio}_{fecha.replace('-','')}.xml"

    logger.info(
        f"[CERT BOL] Sobre listo {len(xmls_firmados)}/5 docs"
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
        }
    )


@router.post("/enviar", summary="Genera Y envía EnvioBOLETA directo al SII")
async def enviar_boletas(
    emisor_id: int,
    fecha_override: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    emisor, cert = await _get_emisor_y_cert(emisor_id, db)

    fecha   = fecha_override or date.today().isoformat()
    service = DTEService(db)

    xmls_firmados, folios, errores = await _emitir_set_boletas(fecha, service, emisor_id)

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
        logger.info(f"[CERT BOL] SII resultado: {resultado}")
        return {
            "estado":         resultado.get("estado"),
            "track_id":       resultado.get("track_id"),
            "mensaje":        resultado.get("mensaje"),
            "docs_generados": len(xmls_firmados),
            "errores":        errores,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error enviando al SII: {e}")
