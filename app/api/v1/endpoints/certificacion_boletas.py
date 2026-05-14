# app/api/v1/endpoints/certificacion_boletas.py
# ══════════════════════════════════════════════════════════════
# Certificación SII — Set de Prueba BOLETA ELECTRÓNICA
#
# Genera el EnvioBOLETA con los 5 casos exactos del set de
# prueba SII (Set_Prueba_BE.txt).
#
# IMPORTANTE — PRECIOS:
#   El SII entrega precios "con IVA incluido" en su set.
#   Este módulo los convierte a NETO automáticamente (÷1.19).
#   Ejemplo: $19.900 con IVA → neto = round(19900 / 1.19) = 16.723
#
# IMPORTANTE — REFERENCIAS:
#   Cada boleta DEBE referenciar su caso con CASO-N.
#   Sin esto el SII rechaza el set completo.
#
# IMPORTANTE — ENVELOPE:
#   Las boletas van en <EnvioBOLETA> NO en <EnvioDTE>.
#   El schema es EnvioBOLETA_v11.xsd (diferente al de facturas).
#
# Endpoints:
#   POST /v1/certificacion-boletas/generar-xml  — Genera EnvioBOLETA
#   GET  /v1/certificacion-boletas/preview/{id} — Preview de los 5 casos
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

logger = logging.getLogger("yepardtecore.cert_boletas")

router = APIRouter(prefix="/certificacion-boletas", tags=["Certificación Boletas SII"])


# ── Helpers ───────────────────────────────────────────────────

def precio_neto(precio_con_iva: int) -> int:
    """
    Convierte precio con IVA → precio neto (sin IVA).

    Analogía: el precio_con_iva es el precio que paga el cliente
    en la caja. El neto es lo que queda después de que el SII
    se lleva su 19% — nosotros trabajamos con el neto internamente.

    Ejemplo: 19.900 con IVA → 16.723 neto
    El SII valida que neto × 1.19 ≈ total → error si no coincide.
    """
    return round(precio_con_iva / 1.19)


def ref_caso(n: int, fecha: str) -> dict:
    """
    Crea la referencia CASO-N que el SII exige en cada boleta del set.

    El SII usa esto para identificar que la boleta corresponde
    al caso N del set de prueba. Sin esto, el SII rechaza el envío.
    """
    return {
        "tipo_doc_ref": 801,          # Código para "otros documentos"
        "folio_ref":    n,
        "fecha_ref":    fecha,
        "razon_ref":    f"CASO-{n}",
        "cod_ref":      "SET",        # Identifica que es set de prueba
    }


# ── Set de Prueba SII (5 casos exactos) ──────────────────────

def _casos_certificacion_boleta(fecha: str) -> list[dict]:
    """
    Los 5 casos exactos del Set_Prueba_BE.txt del SII.

    Analogía: es como el 'libro de respuestas' del SII — hay que
    reproducir EXACTAMENTE estos casos para que el examen de
    certificación sea aprobado.

    CASO 1: Dos servicios afectos (taller mecánico)
    CASO 2: Un servicio afecto con precio alto
    CASO 3: Servicio afecto con descuento
    CASO 4: Servicio afecto + ítem exento (caso mixto)
    CASO 5: Producto físico con unidad de medida (Kg)
    """
    return [
        # ── CASO 1 ────────────────────────────────────────────
        # Cambio de aceite $19.900 + Alineación $9.900 (precios con IVA)
        {
            "tipo_dte":     39,
            "receptor":     {"rut": "66666666-6", "razon_social": "Consumidor Final"},
            "items": [
                {
                    "nombre":          "Cambio de aceite",
                    "cantidad":        1,
                    "precio_unitario": precio_neto(19900),    # → 16.723 neto
                    "exento":          False,
                },
                {
                    "nombre":          "Alineacion y balanceo",
                    "cantidad":        1,
                    "precio_unitario": precio_neto(9900),     # → 8.319 neto
                    "exento":          False,
                },
            ],
            "referencias":   [ref_caso(1, fecha)],
            "fecha_emision": fecha,
        },

        # ── CASO 2 ────────────────────────────────────────────
        # Servicio de mantención $150.000 (precio con IVA)
        {
            "tipo_dte":     39,
            "receptor":     {"rut": "66666666-6", "razon_social": "Consumidor Final"},
            "items": [
                {
                    "nombre":          "Servicio de mantencion",
                    "cantidad":        1,
                    "precio_unitario": precio_neto(150000),   # → 126.050 neto
                    "exento":          False,
                },
            ],
            "referencias":   [ref_caso(2, fecha)],
            "fecha_emision": fecha,
        },

        # ── CASO 3 ────────────────────────────────────────────
        # Neumático $85.000 con 10% de descuento (precio con IVA)
        {
            "tipo_dte":     39,
            "receptor":     {"rut": "66666666-6", "razon_social": "Consumidor Final"},
            "items": [
                {
                    "nombre":          "Neumatico",
                    "cantidad":        1,
                    "precio_unitario": precio_neto(85000),    # → 71.429 neto
                    "descuento_pct":   10.0,                  # 10% de descuento
                    "exento":          False,
                },
            ],
            "referencias":   [ref_caso(3, fecha)],
            "fecha_emision": fecha,
        },

        # ── CASO 4 ────────────────────────────────────────────
        # ítem afecto 1 (8 × $1.590) + ítem exento 2 (2 × $1.000)
        # El ítem exento NO divide por 1.19 porque ya es precio sin IVA
        {
            "tipo_dte":     39,
            "receptor":     {"rut": "66666666-6", "razon_social": "Consumidor Final"},
            "items": [
                {
                    "nombre":          "item afecto 1",
                    "cantidad":        8,
                    "precio_unitario": precio_neto(1590),     # → 1.336 neto
                    "exento":          False,
                },
                {
                    "nombre":          "item exento 2",
                    "cantidad":        2,
                    "precio_unitario": 1000,    # Exento → precio directo sin IVA
                    "exento":          True,
                },
            ],
            "referencias":   [ref_caso(4, fecha)],
            "fecha_emision": fecha,
        },

        # ── CASO 5 ────────────────────────────────────────────
        # Arroz 5 Kg × $700 (precio con IVA)
        # El SII exige informar la unidad de medida "Kg"
        {
            "tipo_dte":     39,
            "receptor":     {"rut": "66666666-6", "razon_social": "Consumidor Final"},
            "items": [
                {
                    "nombre":          "Arroz",
                    "cantidad":        5,
                    "precio_unitario": precio_neto(700),      # → 588 neto
                    "unidad":          "Kg",                  # Obligatorio según SII
                    "exento":          False,
                },
            ],
            "referencias":   [ref_caso(5, fecha)],
            "fecha_emision": fecha,
        },
    ]


# ── Endpoints ─────────────────────────────────────────────────

@router.post(
    "/generar-xml",
    summary="Generar EnvioBOLETA con los 5 casos del set de prueba SII",
    response_class=Response,
)
async def generar_xml_boletas(
    emisor_id: int,
    fecha_override: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Genera el **EnvioBOLETA** con los 5 casos exactos del set de
    certificación SII. El XML resultante está listo para subir
    manualmente en **maullin.sii.cl**.

    ## Requisitos previos
    - El emisor debe estar en ambiente `certificacion`
    - El emisor debe tener un certificado `.p12` cargado
    - El certificado debe ser **Firma Electrónica Avanzada (FEA)**
      de un PSC autorizado (E-Certchile, FirmaVirtual, Acepta.com)

    ## Diferencias con Factura
    - El envelope es `<EnvioBOLETA>` (schema `EnvioBOLETA_v11.xsd`)
    - Cada boleta usa `IndServicio=3` en lugar de `TpoTranVenta`
    - El emisor usa `RznSocEmisor` en lugar de `RznSoc`
    """

    # Validar que el emisor exista y esté activo
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(status_code=404, detail=f"Emisor {emisor_id} no encontrado")
    if not emisor.activo:
        raise HTTPException(status_code=400, detail="El emisor está desactivado")
    if emisor.ambiente != "certificacion":
        raise HTTPException(
            status_code=400,
            detail=(
                f"El emisor está en ambiente '{emisor.ambiente}'. "
                "Solo se puede certificar en ambiente 'certificacion'."
            ),
        )

    # Verificar que tenga certificado p12 cargado
    cert = emisor.certificado_activo
    if not cert or not cert.certificado_p12:
        raise HTTPException(
            status_code=400,
            detail=(
                "El emisor no tiene certificado .p12. "
                "Súbelo con POST /v1/certificados/{emisor_id}/subir.\n\n"
                "IMPORTANTE: El certificado debe ser Firma Electrónica "
                "Avanzada (FEA) de un PSC autorizado por el SII "
                "(E-Certchile, FirmaVirtual, Acepta.com). "
                "Los certificados FES (Firma Electrónica Simple) NO son "
                "aceptados por el SII y causarán RFR."
            ),
        )

    # Preparar fecha y casos de prueba
    fecha = fecha_override or date.today().isoformat()
    logger.info(f"[CERT-BOLETA] Generando XML — emisor={emisor.rut} fecha={fecha}")

    # Generar los 5 casos del set de prueba
    service       = DTEService(db)
    casos         = _casos_certificacion_boleta(fecha)
    xmls_firmados = []
    errores       = []

    for i, caso in enumerate(casos, start=1):
        try:
            resultado = await service.emitir(
                emisor_id   = emisor_id,
                datos       = {**caso, "emisor_id": emisor_id},
                auto_enviar = False,
            )
            xmls_firmados.append(resultado["xml_firmado"])
            logger.info(
                f"[CERT-BOLETA] Caso {i} OK — "
                f"folio={resultado['folio']} total=${resultado['monto_total']:,.0f}"
            )
        except Exception as e:
            errores.append(f"Caso {i}: {str(e)}")
            logger.error(f"[CERT-BOLETA] Error caso {i}: {e}", exc_info=True)

    if not xmls_firmados:
        raise HTTPException(
            status_code=500,
            detail=f"No se generó ningún caso. Errores: {'; '.join(errores)}",
        )

    if errores:
        logger.warning(f"[CERT-BOLETA] {len(errores)} errores: {errores}")

    # Construir el sobre EnvioBOLETA y firmarlo
    firma  = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    sender = SIISender(ambiente="certificacion")

    try:
        sobre_xml = await sender.construir_sobre(
            dtes_xml     = xmls_firmados,
            rut_emisor   = emisor.rut,
            rut_enviador = firma.rut_certificado or emisor.rut,
            firma_service = firma,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error armando el sobre: {str(e)}")

    logger.info(
        f"[CERT-BOLETA] Sobre listo — {len(xmls_firmados)}/5 boletas"
        + (f" — errores: {errores}" if errores else " — sin errores ✓")
    )

    # Retornar XML descargable
    rut_limpio     = emisor.rut.replace(".", "").replace("-", "")
    fecha_limpia   = fecha.replace("-", "")
    nombre_archivo = f"EnvioBOLETA_SetBasico_{rut_limpio}_{fecha_limpia}.xml"

    return Response(
        content    = sobre_xml.encode("ISO-8859-1"),
        media_type = "application/octet-stream",
        headers    = {
            "Content-Disposition": f'attachment; filename="{nombre_archivo}"',
            "X-Tipo-Sobre":        "EnvioBOLETA",
            "X-Schema":            "EnvioBOLETA_v11.xsd",
            "X-Casos-Generados":   str(len(xmls_firmados)),
            "X-Casos-Error":       str(len(errores)),
            "X-Errores-Detalle":   " | ".join(errores) if errores else "",
        },
    )


@router.get(
    "/preview/{emisor_id}",
    summary="Preview de los 5 casos de boleta (sin generar ni firmar)",
)
async def preview_boletas(
    emisor_id: int,
    fecha_override: Optional[str] = None,
):
    """
    Muestra los 5 casos del set de certificación de boletas
    con los precios ya convertidos a neto.

    Útil para verificar los datos antes de generar el XML real.
    """
    fecha = fecha_override or date.today().isoformat()
    casos = _casos_certificacion_boleta(fecha)

    return {
        "emisor_id":    emisor_id,
        "fecha":        fecha,
        "tipo_sobre":   "EnvioBOLETA",
        "schema":       "EnvioBOLETA_v11.xsd",
        "nota_precios": "Precios convertidos a neto (precio_con_iva ÷ 1.19)",
        "nota_cert":    (
            "El certificado debe ser FEA (Firma Electrónica Avanzada). "
            "Firmadox FES y otros certificados simples son RECHAZADOS por el SII (RFR)."
        ),
        "casos": [
            {
                "caso":       i + 1,
                "tipo_dte":   c["tipo_dte"],
                "descripcion": f"CASO-{i+1}",
                "referencia": ref_caso(i + 1, fecha),
                "items": [
                    {
                        "nombre":      it["nombre"],
                        "cantidad":    it["cantidad"],
                        "precio_neto": it["precio_unitario"],
                        "unidad":      it.get("unidad", ""),
                        "exento":      it.get("exento", False),
                    }
                    for it in c["items"]
                ],
            }
            for i, c in enumerate(casos)
        ],
    }
