# app/api/v1/endpoints/certificacion_facturas.py
# ══════════════════════════════════════════════════════════════
# Endpoint para SET BASICO de Facturas — Número de Atención: 4784337
#
# Genera y firma el EnvioDTE con 8 documentos:
#   CASO 1: Factura tipo 33 — 2 ítems afectos
#   CASO 2: Factura tipo 33 — 2 ítems afectos con descuento por línea
#   CASO 3: Factura tipo 33 — 2 ítems afectos + 1 exento
#   CASO 4: Factura tipo 33 — 2 ítems afectos + 1 exento + desc global 28%
#   CASO 5: Nota de Crédito tipo 61 — anula giro receptor (ref CASO 1)
#   CASO 6: Nota de Crédito tipo 61 — devolución parcial (ref CASO 2)
#   CASO 7: Nota de Crédito tipo 61 — anula factura (ref CASO 3)
#   CASO 8: Nota de Débito tipo 56  — anula NC (ref CASO 5)
# ══════════════════════════════════════════════════════════════

import logging
from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.models.emisor import Emisor
from app.services.dte_service import DTEService
from app.services.firma_digital import FirmaDigital
from app.services.sii_sender import SIISender

logger = logging.getLogger("yepardtecore.cert_facturas")

router = APIRouter(prefix="/certificacion-facturas", tags=["Certificacion Facturas"])

NATENCION = "4784337"
FECHA_HOY = date.today().isoformat()


def _casos_set_basico(fecha: str) -> list[dict]:
    """
    Los 8 casos del SET BASICO 4784337.
    Los precios en facturas son NETOS (sin IVA).
    Las NC y ND no llevan ítems de precio — referencian al DTE original.
    """
    return [
        # ── CASO 1 ─ Factura 2 ítems afectos ──────────────────
        {
            "caso": 1,
            "tipo_dte": 33,
            "items": [
                {"nombre": "Cajón AFECTO",   "cantidad": 185, "precio_unitario": 4490, "exento": False},
                {"nombre": "Relleno AFECTO",  "cantidad": 78,  "precio_unitario": 7503, "exento": False},
            ],
            "descuento_global_pct": 0,
        },
        # ── CASO 2 ─ Factura con descuentos por línea ─────────
        {
            "caso": 2,
            "tipo_dte": 33,
            "items": [
                {"nombre": "Pañuelo AFECTO", "cantidad": 963, "precio_unitario": 7412, "exento": False, "descuento_pct": 12},
                {"nombre": "ITEM 2 AFECTO",  "cantidad": 914, "precio_unitario": 6458, "exento": False, "descuento_pct": 30},
            ],
            "descuento_global_pct": 0,
        },
        # ── CASO 3 ─ Factura afecto + exento ──────────────────
        {
            "caso": 3,
            "tipo_dte": 33,
            "items": [
                {"nombre": "Pintura B&W AFECTO",    "cantidad": 93,  "precio_unitario": 8524,  "exento": False},
                {"nombre": "ITEM 2 AFECTO",          "cantidad": 270, "precio_unitario": 4567,  "exento": False},
                {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad": 1,   "precio_unitario": 35521, "exento": True},
            ],
            "descuento_global_pct": 0,
        },
        # ── CASO 4 ─ Factura con descuento global 28% ─────────
        {
            "caso": 4,
            "tipo_dte": 33,
            "items": [
                {"nombre": "ITEM 1 AFECTO",          "cantidad": 544, "precio_unitario": 7572, "exento": False},
                {"nombre": "ITEM 2 AFECTO",          "cantidad": 230, "precio_unitario": 9462, "exento": False},
                {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad": 2,   "precio_unitario": 6858, "exento": True},
            ],
            "descuento_global_pct": 28,
        },
    ]


def _casos_nc_nd() -> list[dict]:
    """
    Casos 5-8: NC y ND que referencian a las facturas.
    Los folios de referencia se pasan al momento de generar.
    """
    return [
        # ── CASO 5 ─ NC corrige giro receptor (ref caso 1) ────
        {"caso": 5, "tipo_dte": 61, "ref_caso": 1, "cod_ref": 1, "razon_ref": "CORRIGE GIRO DEL RECEPTOR"},
        # ── CASO 6 ─ NC devolución parcial (ref caso 2) ───────
        {"caso": 6, "tipo_dte": 61, "ref_caso": 2, "cod_ref": 3, "razon_ref": "DEVOLUCION DE MERCADERIAS",
         "items": [
             {"nombre": "Pañuelo AFECTO", "cantidad": 353, "precio_unitario": 7412, "exento": False, "descuento_pct": 12},
             {"nombre": "ITEM 2 AFECTO",  "cantidad": 620, "precio_unitario": 6458, "exento": False, "descuento_pct": 30},
         ]},
        # ── CASO 7 ─ NC anula factura (ref caso 3) ────────────
        {"caso": 7, "tipo_dte": 61, "ref_caso": 3, "cod_ref": 1, "razon_ref": "ANULA FACTURA"},
        # ── CASO 8 ─ ND anula NC caso 5 (ref caso 5) ──────────
        {"caso": 8, "tipo_dte": 56, "ref_caso": 5, "cod_ref": 2, "razon_ref": "ANULA NOTA DE CREDITO ELECTRONICA"},
    ]


@router.post(
    "/generar-xml",
    summary="Genera EnvioDTE con SET BASICO de Facturas (N° Atención 4784337)",
)
async def generar_xml_facturas(
    emisor_id: int,
    fecha_override: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Genera el EnvioDTE completo con los 8 documentos del SET BASICO
    para certificación de Factura Electrónica.
    Número de Atención: 4784337
    """
    from app.services.xml_builder import (
        XMLBuilder, InputDTE, EmisorDTE, ReceptorDTE, ItemDTE, ReferenciaDTE
    )

    # ── Validar emisor ────────────────────────────────────────
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(status_code=404, detail=f"Emisor {emisor_id} no encontrado")

    cert = emisor.certificado_activo
    if not cert or not cert.certificado_p12:
        raise HTTPException(status_code=400, detail="Sin certificado .p12 cargado")

    fecha = fecha_override or date.today().isoformat()
    fecha_dt = date.fromisoformat(fecha)

    service = DTEService(db)
    sender  = SIISender(ambiente=emisor.ambiente)

    emisor_dte = EmisorDTE(
        rut=emisor.rut, razon_social=emisor.razon_social,
        giro=emisor.giro, direccion=emisor.direccion,
        comuna=emisor.comuna, ciudad=emisor.ciudad,
    )
    receptor = ReceptorDTE(rut="77777777-7", razon_social="EMPRESA LTDA",
                           giro="COMPUTACION", direccion="SAN DIEGO 2222",
                           ciudad="SANTIAGO", comuna="LA FLORIDA")

    xmls_firmados   = []
    folios_emitidos = {}   # caso → folio (para referencias NC/ND)
    errores         = []

    # ── Emitir las 4 facturas ─────────────────────────────────
    for caso_data in _casos_set_basico(fecha):
        caso_n  = caso_data["caso"]
        tipo    = caso_data["tipo_dte"]
        try:
            items = [
                ItemDTE(
                    nombre=it["nombre"],
                    cantidad=it["cantidad"],
                    precio_unitario=it["precio_unitario"],
                    exento=it.get("exento", False),
                    descuento_pct=it.get("descuento_pct", 0.0),
                )
                for it in caso_data["items"]
            ]
            refs = [ReferenciaDTE(
                tipo_doc_ref=801,
                folio_ref=caso_n,
                fecha_ref=fecha_dt,
                cod_ref="SET",
                razon_ref=f"CASO-{NATENCION}-{caso_n}",
            )]
            datos = InputDTE(
                tipo_dte=tipo, folio=0,  # folio se asigna en service.emitir
                fecha_emision=fecha_dt,
                emisor=emisor_dte, receptor=receptor,
                items=items,
                referencias=refs,
                descuento_global_pct=caso_data.get("descuento_global_pct", 0),
                ambiente=emisor.ambiente,
            )
            resultado = await service.emitir(
                emisor_id=emisor_id,
                datos={
                    "tipo_dte": tipo,
                    "fecha_emision": fecha,
                    "emisor_id": emisor_id,
                    "items": [
                        {
                            "nombre": it.nombre,
                            "cantidad": it.cantidad,
                            "precio_unitario": it.precio_unitario,
                            "exento": it.exento,
                            "descuento_pct": it.descuento_pct,
                        }
                        for it in items
                    ],
                    "referencias": [
                        {
                            "tipo_doc_ref": r.tipo_doc_ref,
                            "folio_ref": r.folio_ref,
                            "fecha_ref": fecha,
                            "cod_ref": r.cod_ref,
                            "razon_ref": r.razon_ref,
                        }
                        for r in refs
                    ],
                    "descuento_global_pct": caso_data.get("descuento_global_pct", 0),
                    "rut_receptor": receptor.rut,
                    "nombre_receptor": receptor.razon_social,
                    "giro_receptor": receptor.giro,
                    "direccion_receptor": receptor.direccion,
                    "ciudad_receptor": receptor.ciudad,
                },
                auto_enviar=False,
            )
            xmls_firmados.append(resultado["xml_firmado"])
            folios_emitidos[caso_n] = resultado["folio"]
            logger.info(f"[CERT FAC] Caso {caso_n} OK folio={resultado['folio']} total={resultado['monto_total']:,}")
        except Exception as e:
            errores.append(f"Caso {caso_n}: {e}")
            logger.error(f"[CERT FAC] Error caso {caso_n}: {e}", exc_info=True)

    # ── Emitir NC y ND ────────────────────────────────────────
    for nc_data in _casos_nc_nd():
        caso_n   = nc_data["caso"]
        tipo     = nc_data["tipo_dte"]
        ref_caso = nc_data["ref_caso"]
        folio_ref = folios_emitidos.get(ref_caso, ref_caso)
        tipo_ref  = 33 if ref_caso <= 4 else (61 if ref_caso == 5 else 33)
        try:
            items = [
                ItemDTE(
                    nombre=it["nombre"],
                    cantidad=it["cantidad"],
                    precio_unitario=it["precio_unitario"],
                    exento=it.get("exento", False),
                    descuento_pct=it.get("descuento_pct", 0.0),
                )
                for it in nc_data.get("items", [])
            ] if nc_data.get("items") else []

            refs = [
                ReferenciaDTE(
                    tipo_doc_ref=tipo_ref,
                    folio_ref=folio_ref,
                    fecha_ref=fecha_dt,
                    cod_ref=nc_data["cod_ref"],
                    razon_ref=nc_data["razon_ref"],
                ),
                ReferenciaDTE(
                    tipo_doc_ref=801,
                    folio_ref=caso_n,
                    fecha_ref=fecha_dt,
                    cod_ref="SET",
                    razon_ref=f"CASO-{NATENCION}-{caso_n}",
                ),
            ]

            datos_api = {
                "tipo_dte": tipo,
                "fecha_emision": fecha,
                "emisor_id": emisor_id,
                "items": [
                    {
                        "nombre": it.nombre,
                        "cantidad": it.cantidad,
                        "precio_unitario": it.precio_unitario,
                        "exento": it.exento,
                        "descuento_pct": it.descuento_pct,
                    }
                    for it in items
                ] if items else [
                    # NC/ND sin ítems: monto simbólico 1 peso para que el builder no falle
                    # El SII acepta NC con solo referencias si el monto es igual al doc original
                    # En la práctica se pone el monto total de la factura referenciada
                ],
                "referencias": [
                    {
                        "tipo_doc_ref": r.tipo_doc_ref,
                        "folio_ref": r.folio_ref,
                        "fecha_ref": fecha,
                        "cod_ref": r.cod_ref,
                        "razon_ref": r.razon_ref,
                    }
                    for r in refs
                ],
                "rut_receptor": receptor.rut,
                "nombre_receptor": receptor.razon_social,
                "giro_receptor": receptor.giro,
                "direccion_receptor": receptor.direccion,
                "ciudad_receptor": receptor.ciudad,
            }

            # Para NC/ND sin ítems, usamos los mismos ítems de la factura referenciada
            if not items:
                caso_orig = _casos_set_basico(fecha)[ref_caso - 1]
                datos_api["items"] = [
                    {
                        "nombre": it["nombre"],
                        "cantidad": it["cantidad"],
                        "precio_unitario": it["precio_unitario"],
                        "exento": it.get("exento", False),
                        "descuento_pct": it.get("descuento_pct", 0.0),
                    }
                    for it in caso_orig["items"]
                ]
                datos_api["descuento_global_pct"] = caso_orig.get("descuento_global_pct", 0)

            resultado = await service.emitir(
                emisor_id=emisor_id,
                datos=datos_api,
                auto_enviar=False,
            )
            xmls_firmados.append(resultado["xml_firmado"])
            folios_emitidos[caso_n] = resultado["folio"]
            logger.info(f"[CERT FAC] Caso {caso_n} OK folio={resultado['folio']} total={resultado['monto_total']:,}")
        except Exception as e:
            errores.append(f"Caso {caso_n}: {e}")
            logger.error(f"[CERT FAC] Error caso {caso_n}: {e}", exc_info=True)

    if not xmls_firmados:
        raise HTTPException(status_code=500, detail=f"No se generó ningún caso. Errores: {'; '.join(errores)}")

    # ── Armar sobre ───────────────────────────────────────────
    firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    try:
        sobre_xml = sender.construir_sobre(
            dtes_xml=xmls_firmados,
            rut_emisor=emisor.rut,
            rut_enviador="25648612-1",
            firma_service=firma,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error armando sobre: {e}")

    rut_limpio     = emisor.rut.replace(".", "").replace("-", "")
    nombre_archivo = f"EnvioDTE_SetBasico_{rut_limpio}_{fecha.replace('-','')}.xml"

    logger.info(f"[CERT FAC] Sobre listo {len(xmls_firmados)}/8 docs" +
                (f" errores: {errores}" if errores else " ✓"))

    return Response(
        content=sobre_xml.encode("ISO-8859-1"),
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="{nombre_archivo}"',
            "X-Casos-Generados": str(len(xmls_firmados)),
            "X-Casos-Error": str(len(errores)),
            "X-NroAtencion": NATENCION,
        }
    )
