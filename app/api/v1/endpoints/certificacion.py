# app/api/v1/endpoints/certificacion.py
# ══════════════════════════════════════════════════════════════
# Endpoints de Certificación SII — Set de Prueba Boleta
#
# CASOS EXACTOS según Set_Prueba_BE.txt entregado por el SII.
#
# IMPORTANTE sobre precios:
#   El SII entrega los precios "con IVA incluido" en el set.
#   Nuestro motor trabaja con precios NETOS (sin IVA).
#   Conversión: precio_neto = round(precio_con_iva / 1.19)
#
# IMPORTANTE sobre referencias:
#   El SII exige que cada boleta referencie su caso:
#   <TpoDocRef>SET</TpoDocRef>
#   <RazonRef>CASO-1</RazonRef>
#   Sin esto el SII rechaza la certificación.
#
# Endpoints:
#   POST /v1/certificacion/generar-xml     — Genera XML descargable
#   GET  /v1/certificacion/preview/{id}    — Preview sin generar
# ══════════════════════════════════════════════════════════════

import logging
from datetime import date
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.db.base import get_db
from app.models.emisor import Emisor
from app.services.dte_service import DTEService
from app.services.firma_digital import FirmaDigital
from app.services.sii_sender import SIISender

logger = logging.getLogger("yepardtecore.certificacion")

router = APIRouter(prefix="/certificacion", tags=["Certificación SII"])


# ── Helpers de precio ─────────────────────────────────────────

def neto(precio_con_iva: int) -> int:
    """
    Convierte precio con IVA incluido → precio neto.
    El SII entrega precios 'con IVA' en el set de prueba.
    Nuestro motor suma el 19% sobre el neto para calcular el total.
    Ejemplo: 19900 con IVA → neto = round(19900 / 1.19) = 16723
    """
    return round(precio_con_iva / 1.19)


def ref_caso(n: int, fecha: str) -> dict:
    """
    Referencia obligatoria que el SII exige en cada boleta del set.
    El SII indica: <TpoDocRef>SET / <RazonRef>CASO-N
    Usamos tipo_doc_ref=0 (sin tipo de documento formal)
    y folio_ref=n para identificar el caso.
    """
    return {
        "tipo_doc_ref": 801,  # Código SII para "otros documentos"
        "folio_ref":    n,
        "fecha_ref":    fecha,
        "razon_ref":    f"CASO-{n}",
        "cod_ref":      "SET",  # Identifica que es un set de prueba de certificación
    }


# ── Casos exactos del Set de Prueba SII ──────────────────────
#
# Fuente: Set_Prueba_BE.txt entregado por el SII.
# Precios convertidos de "con IVA" a neto dividiendo por 1.19.

def _casos_sii(fecha: str) -> list[dict]:
    """
    Los 5 casos oficiales del set de certificación de boletas.
    Cada uno lleva la referencia obligatoria CASO-N que exige el SII.

    Analogía: es como un examen donde el SII ya te dio las respuestas
    correctas — hay que reproducirlas exactamente para aprobar.
    """
    return [
        # ── CASO 1 ────────────────────────────────────────────
        # Cambio de aceite (1 × $19.900) + Alineacion y balanceo (1 × $9.900)
        # Precios vienen "con IVA" → dividir por 1.19 para obtener neto
        {
            "tipo_dte": 39,
            "receptor": {"rut": "66666666-6", "razon_social": "Consumidor Final"},
            "items": [
                {
                    "nombre":          "Cambio de aceite",
                    "cantidad":        1,
                    "precio_unitario": neto(19900),   # 16723
                    "exento":          False,
                },
                {
                    "nombre":          "Alineacion y balanceo",
                    "cantidad":        1,
                    "precio_unitario": neto(9900),    # 8319
                    "exento":          False,
                },
            ],
            "referencias":   [ref_caso(1, fecha)],
            "fecha_emision": fecha,
            "forma_pago":    1,
        },

        # ── CASO 2 ────────────────────────────────────────────
        # Papel de regalo (17 × $120)
        {
            "tipo_dte": 39,
            "receptor": {"rut": "66666666-6", "razon_social": "Consumidor Final"},
            "items": [
                {
                    "nombre":          "Papel de regalo",
                    "cantidad":        17,
                    "precio_unitario": neto(120),     # 101
                    "exento":          False,
                },
            ],
            "referencias":   [ref_caso(2, fecha)],
            "fecha_emision": fecha,
            "forma_pago":    1,
        },

        # ── CASO 3 ────────────────────────────────────────────
        # Sandwic (2 × $1.500) + Bebida (2 × $550)
        {
            "tipo_dte": 39,
            "receptor": {"rut": "66666666-6", "razon_social": "Consumidor Final"},
            "items": [
                {
                    "nombre":          "Sandwic",
                    "cantidad":        2,
                    "precio_unitario": neto(1500),    # 1261
                    "exento":          False,
                },
                {
                    "nombre":          "Bebida",
                    "cantidad":        2,
                    "precio_unitario": neto(550),     # 462
                    "exento":          False,
                },
            ],
            "referencias":   [ref_caso(3, fecha)],
            "fecha_emision": fecha,
            "forma_pago":    1,
        },

        # ── CASO 4 ────────────────────────────────────────────
        # item afecto 1 (8 × $1.590) + item exento 2 (2 × $1.000)
        # Observación SII: "El item 1 es un servicio afecto.
        #                   El item 2 es un servicio exento."
        # El item exento NO divide por 1.19 porque no lleva IVA.
        {
            "tipo_dte": 39,
            "receptor": {"rut": "66666666-6", "razon_social": "Consumidor Final"},
            "items": [
                {
                    "nombre":          "item afecto 1",
                    "cantidad":        8,
                    "precio_unitario": neto(1590),    # 1336 — afecto, precio con IVA
                    "exento":          False,
                },
                {
                    "nombre":          "item exento 2",
                    "cantidad":        2,
                    "precio_unitario": 1000,          # exento, precio sin IVA
                    "exento":          True,
                },
            ],
            "referencias":   [ref_caso(4, fecha)],
            "fecha_emision": fecha,
            "forma_pago":    1,
            "observacion":   "El item 1 es un servicio afecto. El item 2 es un servicio exento.",
        },

        # ── CASO 5 ────────────────────────────────────────────
        # Arroz (5 × $700)
        # Observación SII: "Se debe informar en el XML Unidad de medida en Kg."
        {
            "tipo_dte": 39,
            "receptor": {"rut": "66666666-6", "razon_social": "Consumidor Final"},
            "items": [
                {
                    "nombre":          "Arroz",
                    "cantidad":        5,
                    "precio_unitario": neto(700),     # 588
                    "unidad":          "Kg",          # ← obligatorio según SII
                    "exento":          False,
                },
            ],
            "referencias":   [ref_caso(5, fecha)],
            "fecha_emision": fecha,
            "forma_pago":    1,
        },
    ]


# ── Endpoint principal ────────────────────────────────────────

@router.post(
    "/generar-xml",
    summary="Generar XML de certificación con los casos exactos del SII",
    response_class=Response,
)
async def generar_xml_certificacion(
    emisor_id: int,
    fecha_override: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Genera el `EnvioBOLETA` con los **5 casos exactos** del set de
    certificación entregado por el SII, listo para subir manualmente
    en **maullin.sii.cl**.

    Cada boleta incluye la referencia `CASO-N` que el SII exige.
    Los precios se convierten automáticamente de "con IVA" a neto.
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
                   "Solo se puede certificar en ambiente 'certificacion'."
        )

    cert = emisor.certificado_activo
    if not cert or not cert.certificado_p12:
        raise HTTPException(
            status_code=400,
            detail="El emisor no tiene certificado .p12. "
                   "Súbelo con POST /v1/certificados/{emisor_id}/subir"
        )

    fecha = fecha_override or date.today().isoformat()
    logger.info(f"[CERT] Generando XML — emisor={emisor.rut} fecha={fecha}")

    # ── Emitir los 5 casos ────────────────────────────────────
    service       = DTEService(db)
    casos         = _casos_sii(fecha)
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
                f"[CERT] Caso {i} OK — "
                f"tipo={caso['tipo_dte']} folio={resultado['folio']} "
                f"total=${resultado['monto_total']:,.0f}"
            )
        except Exception as e:
            errores.append(f"Caso {i}: {str(e)}")
            logger.error(f"[CERT] Error caso {i}: {e}", exc_info=True)

    if not xmls_firmados:
        raise HTTPException(
            status_code=500,
            detail=f"No se generó ningún caso. Errores: {'; '.join(errores)}"
        )

    if errores:
        logger.warning(f"[CERT] {len(errores)} errores: {errores}")

    # ── Armar el sobre EnvioBOLETA ────────────────────────────
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
        f"[CERT] Sobre listo — {len(xmls_firmados)}/5 DTEs"
        + (f" — errores: {errores}" if errores else " — sin errores ✓")
    )

    # ── Devolver XML descargable ──────────────────────────────
    # La declaración <?xml version="1.0" encoding="ISO-8859-1"?> ya viene
    # incluida desde firma_digital.firmar_sobre() — no hay que agregarla aquí.
    rut_limpio     = emisor.rut.replace(".", "").replace("-", "")
    nombre_archivo = f"EnvioBOLETA_cert_{rut_limpio}_{fecha.replace('-','')}.xml"

    return Response(
        content    = sobre_xml.encode("ISO-8859-1"),
        media_type = "application/xml",
        headers    = {
            "Content-Disposition": f'attachment; filename="{nombre_archivo}"',
            "X-Casos-Generados":   str(len(xmls_firmados)),
            "X-Casos-Error":       str(len(errores)),
            "X-Errores-Detalle":   " | ".join(errores) if errores else "",
        }
    )


# ── Preview ───────────────────────────────────────────────────

@router.get(
    "/preview/{emisor_id}",
    summary="Preview de los 5 casos (sin generar ni firmar)",
)
async def preview(emisor_id: int, fecha_override: Optional[str] = None):
    """Muestra los 5 casos con los precios exactos del set SII."""
    fecha = fecha_override or date.today().isoformat()
    casos = _casos_sii(fecha)
    return {
        "emisor_id": emisor_id,
        "fecha":     fecha,
        "nota":      "Precios ya convertidos a neto (divididos por 1.19)",
        "casos": [
            {
                "caso":     i + 1,
                "tipo_dte": c["tipo_dte"],
                "referencia": f"CASO-{i+1}",
                "items": [
                    {
                        "nombre":    it["nombre"],
                        "cantidad":  it["cantidad"],
                        "precio_neto": it["precio_unitario"],
                        "unidad":    it.get("unidad", "UN"),
                        "exento":    it.get("exento", False),
                    }
                    for it in c["items"]
                ],
            }
            for i, c in enumerate(casos)
        ],
    }


# ── Endpoint de diagnóstico ───────────────────────────────────────────────
@router.get(
    "/debug-xml/{emisor_id}",
    summary="Diagnóstico: muestra los primeros 400 chars del XML generado",
    response_class=PlainTextResponse,
)
async def debug_xml(emisor_id: int, db: AsyncSession = Depends(get_db)):
    """
    Genera el sobre de certificación y retorna los primeros 400 caracteres
    como texto plano. Útil para verificar que la declaración XML está presente
    y que el formato es correcto antes de subir al SII.
    """
    from app.repositories.emisor_repository import EmisorRepository
    from app.repositories.caf_repository import CAFRepository
    from app.services.sii_sender import SIISender
    from app.services.firma_digital import FirmaDigital
    from datetime import date

    try:
        repo = EmisorRepository(db)
        emisor = await repo.get_by_id(emisor_id)
        if not emisor:
            return PlainTextResponse(f"ERROR: Emisor {emisor_id} no encontrado", status_code=404)

        cert_b64 = emisor.certificado_p12_b64
        cert_pwd  = emisor.certificado_password
        if not cert_b64 or not cert_pwd:
            return PlainTextResponse("ERROR: Certificado no cargado", status_code=400)

        import base64
        firma = FirmaDigital(base64.b64decode(cert_b64), cert_pwd)

        caf_repo = CAFRepository(db)
        cafs = await caf_repo.get_by_emisor(emisor_id)
        caf_39 = next((c for c in cafs if c.tipo_dte == 39), None)
        if not caf_39:
            return PlainTextResponse("ERROR: No hay CAF tipo 39", status_code=400)

        fecha = date.today().isoformat()
        casos = _casos_sii(fecha)
        sender = SIISender()

        from app.services.xml_builder import XMLBuilder, InputDTE, EmisorDTE, ReceptorDTE, ItemDTE, ReferenciaDTE
        dtes_xml = []
        for i, caso in enumerate(casos):
            folio = caf_39.folio_siguiente
            caf_39.folio_siguiente += 1
            items = [ItemDTE(nombre=it["nombre"], cantidad=it["cantidad"],
                             precio_unitario=it["precio_unitario"],
                             unidad=it.get("unidad","UN"), exento=it.get("exento",False))
                     for it in caso["items"]]
            refs = [ReferenciaDTE(tipo_doc_ref="801", folio_ref=str(i+1), cod_ref="SET", razon_ref=f"CASO-{i+1}")]
            datos = InputDTE(tipo_dte=caso["tipo_dte"], folio=folio,
                             fecha_emision=date.fromisoformat(fecha),
                             emisor=EmisorDTE(rut=emisor.rut, razon_social=emisor.razon_social,
                                              giro=emisor.giro, direccion=emisor.direccion,
                                              comuna=emisor.comuna, ciudad=emisor.ciudad),
                             receptor=ReceptorDTE(rut="66666666-6", razon_social="Consumidor Final"),
                             items=items, referencias=refs, ambiente="certificacion")
            xml_bytes = XMLBuilder(datos).construir()
            xml_firmado = firma.firmar_dte(xml_bytes=xml_bytes, folio=folio,
                                           tipo_dte=caso["tipo_dte"], xml_caf=caf_39.xml_caf,
                                           fecha_emision=fecha, rut_emisor=emisor.rut,
                                           monto_total=XMLBuilder(datos).monto_total,
                                           it1_nombre=caso["items"][0]["nombre"])
            dtes_xml.append(xml_firmado.decode("ISO-8859-1"))

        sobre = sender.construir_sobre(dtes_xml, emisor.rut, "25648612-1", firma)
        primeros = sobre[:400]

        info = (
            f"=== DIAGNÓSTICO XML (primeros 400 chars) ===\n"
            f"Empieza con <?xml: {sobre.startswith('<?xml')}\n"
            f"Contiene EnvioBOLETA: {'EnvioBOLETA' in sobre[:200]}\n"
            f"Contiene schemaLocation: {'schemaLocation' in sobre[:300]}\n"
            f"Total chars: {len(sobre)}\n"
            f"=== CONTENIDO ===\n"
            f"{primeros}\n"
        )
        return PlainTextResponse(info)
    except Exception as e:
        import traceback
        return PlainTextResponse(f"ERROR: {e}\n{traceback.format_exc()}", status_code=500)
