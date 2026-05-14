# app/api/v1/endpoints/certificacion_notas.py
# ══════════════════════════════════════════════════════════════
# Certificación SII — Set de Prueba NOTAS DE CRÉDITO (61) y
#                    NOTAS DE DÉBITO (56)
#
# Estos documentos van junto con las facturas en el EnvioDTE.
# Se separan en este módulo para mayor claridad y mantenibilidad.
#
# Tipo 56 — Nota de Débito:   corrige facturas aumentando el monto
# Tipo 61 — Nota de Crédito:  corrige facturas disminuyendo el monto
#
# Analogía: la Nota de Débito es el "cobro adicional" y la
# Nota de Crédito es la "devolución parcial" de una factura.
# Ambas siempre deben REFERENCIAR la factura original.
#
# Endpoints:
#   POST /v1/certificacion-notas/generar-xml   — Genera XML con las notas
#   GET  /v1/certificacion-notas/preview/{id}  — Preview sin generar
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

logger = logging.getLogger("yepardtecore.cert_notas")

router = APIRouter(prefix="/certificacion-notas", tags=["Certificación Notas SII"])


def _casos_certificacion_notas(fecha: str) -> list[dict]:
    """
    Casos de Nota de Crédito (61) y Nota de Débito (56) para el set
    de certificación del SII.

    Cada nota DEBE referenciar una factura existente con:
    - TpoDocRef: tipo del documento referenciado (33 = Factura)
    - FolioRef: folio de la factura referenciada
    - CodRef: 1 (anula), 2 (corrige texto), 3 (corrige monto)

    IMPORTANTE: Los folios de referencia deben corresponder a
    facturas REALES emitidas en el mismo set de certificación.
    """
    return [
        # ── NOTA DE CRÉDITO 1 ─────────────────────────────────
        # Anula una factura completa (CodRef=1)
        {
            "tipo_dte": 61,
            "receptor": {
                "rut": "77777777-7",
                "razon_social": "EMPRESA LTDA",
                "giro": "COMPUTACION",
                "direccion": "SAN DIEGO 2222",
                "comuna": "LA FLORIDA",
                "ciudad": "SANTIAGO",
            },
            "items": [
                {
                    "nombre":          "ANULACION FACTURA",
                    "cantidad":        1,
                    "precio_unitario": 1305263,    # Monto neto de la factura anulada
                    "exento":          False,
                },
            ],
            "referencias": [
                {
                    "tipo_doc_ref": 33,            # Tipo: Factura Electrónica
                    "folio_ref":    29,            # Folio de la factura a anular
                    "fecha_ref":    fecha,
                    "razon_ref":    "CASO-7794671-1",
                    "cod_ref":      1,             # 1 = Anula documento referenciado
                }
            ],
            "fecha_emision": fecha,
            "forma_pago":    1,
        },

        # ── NOTA DE CRÉDITO 2 ─────────────────────────────────
        # Corrige el monto de una factura (CodRef=3)
        {
            "tipo_dte": 61,
            "receptor": {
                "rut": "77777777-7",
                "razon_social": "EMPRESA LTDA",
                "giro": "COMPUTACION",
                "direccion": "SAN DIEGO 2222",
                "comuna": "LA FLORIDA",
                "ciudad": "SANTIAGO",
            },
            "items": [
                {
                    "nombre":          "CORRECCION DE MONTO",
                    "cantidad":        1,
                    "precio_unitario": 100000,     # Monto a descontar
                    "exento":          False,
                },
            ],
            "referencias": [
                {
                    "tipo_doc_ref": 33,
                    "folio_ref":    30,
                    "fecha_ref":    fecha,
                    "razon_ref":    "CASO-7794671-2",
                    "cod_ref":      3,             # 3 = Corrige montos
                }
            ],
            "fecha_emision": fecha,
            "forma_pago":    1,
        },

        # ── NOTA DE CRÉDITO 3 ─────────────────────────────────
        # Corrige texto de una factura (CodRef=2)
        {
            "tipo_dte": 61,
            "receptor": {
                "rut": "77777777-7",
                "razon_social": "EMPRESA LTDA",
                "giro": "COMPUTACION",
                "direccion": "SAN DIEGO 2222",
                "comuna": "LA FLORIDA",
                "ciudad": "SANTIAGO",
            },
            "items": [
                {
                    "nombre":          "CORRECCION DE TEXTO",
                    "cantidad":        1,
                    "precio_unitario": 1,          # Monto simbólico para corrección de texto
                    "exento":          False,
                },
            ],
            "referencias": [
                {
                    "tipo_doc_ref": 33,
                    "folio_ref":    31,
                    "fecha_ref":    fecha,
                    "razon_ref":    "CASO-7794671-3",
                    "cod_ref":      2,             # 2 = Corrige textos
                }
            ],
            "fecha_emision": fecha,
            "forma_pago":    1,
        },

        # ── NOTA DE DÉBITO 1 ──────────────────────────────────
        # Aumenta el monto de una factura (solo CodRef=3 aplica a ND)
        {
            "tipo_dte": 56,
            "receptor": {
                "rut": "77777777-7",
                "razon_social": "EMPRESA LTDA",
                "giro": "COMPUTACION",
                "direccion": "SAN DIEGO 2222",
                "comuna": "LA FLORIDA",
                "ciudad": "SANTIAGO",
            },
            "items": [
                {
                    "nombre":          "AJUSTE DE MONTO",
                    "cantidad":        1,
                    "precio_unitario": 50000,      # Monto adicional a cobrar
                    "exento":          False,
                },
            ],
            "referencias": [
                {
                    "tipo_doc_ref": 33,
                    "folio_ref":    32,
                    "fecha_ref":    fecha,
                    "razon_ref":    "CASO-7794671-4",
                    "cod_ref":      3,             # 3 = Corrige montos (aumenta)
                }
            ],
            "fecha_emision": fecha,
            "forma_pago":    1,
        },
    ]


@router.post(
    "/generar-xml",
    summary="Generar EnvioDTE con Notas de Crédito y Débito del set SII",
    response_class=Response,
)
async def generar_xml_notas(
    emisor_id: int,
    fecha_override: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Genera el **EnvioDTE** con las Notas de Crédito (61) y
    Notas de Débito (56) del set de certificación.

    **Importante**: Los folios de referencia deben corresponder a
    facturas ya emitidas en el mismo set de certificación.

    El sobre usa `EnvioDTE_v10.xsd` (igual que las facturas).
    """
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(status_code=404, detail=f"Emisor {emisor_id} no encontrado")
    if emisor.ambiente != "certificacion":
        raise HTTPException(
            status_code=400,
            detail="Solo se puede certificar en ambiente 'certificacion'.",
        )

    cert = emisor.certificado_activo
    if not cert or not cert.certificado_p12:
        raise HTTPException(
            status_code=400,
            detail="El emisor no tiene certificado .p12 cargado.",
        )

    fecha = fecha_override or date.today().isoformat()
    logger.info(f"[CERT-NOTAS] Generando XML — emisor={emisor.rut} fecha={fecha}")

    service       = DTEService(db)
    casos         = _casos_certificacion_notas(fecha)
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
                f"[CERT-NOTAS] Doc {i} OK — "
                f"tipo={caso['tipo_dte']} folio={resultado['folio']}"
            )
        except Exception as e:
            errores.append(f"Doc {i} (tipo {caso['tipo_dte']}): {str(e)}")
            logger.error(f"[CERT-NOTAS] Error doc {i}: {e}", exc_info=True)

    if not xmls_firmados:
        raise HTTPException(
            status_code=500,
            detail=f"No se generó ningún documento. Errores: {'; '.join(errores)}",
        )

    firma  = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    sender = SIISender(ambiente="certificacion")

    try:
        sobre_xml = await sender.construir_sobre(
            dtes_xml      = xmls_firmados,
            rut_emisor    = emisor.rut,
            rut_enviador  = firma.rut_certificado or emisor.rut,
            firma_service = firma,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error armando el sobre: {str(e)}")

    rut_limpio     = emisor.rut.replace(".", "").replace("-", "")
    fecha_limpia   = fecha.replace("-", "")
    nombre_archivo = f"EnvioDTE_Notas_{rut_limpio}_{fecha_limpia}.xml"

    return Response(
        content    = sobre_xml.encode("ISO-8859-1"),
        media_type = "application/octet-stream",
        headers    = {
            "Content-Disposition": f'attachment; filename="{nombre_archivo}"',
            "X-Tipos":             "56 (Nota Débito), 61 (Nota Crédito)",
            "X-Casos-Generados":   str(len(xmls_firmados)),
            "X-Casos-Error":       str(len(errores)),
            "X-Errores-Detalle":   " | ".join(errores) if errores else "",
        },
    )


@router.get(
    "/preview/{emisor_id}",
    summary="Preview de las notas de crédito/débito",
)
async def preview_notas(emisor_id: int, fecha_override: Optional[str] = None):
    """Preview de los casos de notas de crédito y débito."""
    fecha = fecha_override or date.today().isoformat()
    casos = _casos_certificacion_notas(fecha)
    return {
        "emisor_id":  emisor_id,
        "fecha":      fecha,
        "tipo_sobre": "EnvioDTE",
        "schema":     "EnvioDTE_v10.xsd",
        "nota":       "Los folios de referencia deben existir como facturas emitidas.",
        "casos": [
            {
                "indice":   i + 1,
                "tipo_dte": c["tipo_dte"],
                "tipo_desc": "Nota de Crédito" if c["tipo_dte"] == 61 else "Nota de Débito",
                "referencias": c.get("referencias", []),
                "items": c["items"],
            }
            for i, c in enumerate(casos)
        ],
    }
