# app/api/v1/endpoints/certificacion_libro_ventas.py
# ══════════════════════════════════════════════════════════════
# LIBRO DE VENTAS — NÚMERO DE ATENCIÓN: 4841544
#
# Construido con documentos de:
#   - Set Básico 4841543 (tipos 33, 61, 56)
#   - Set Exentas 4841548 (tipos 34, 61, 56)
#   - Set Guía    4841546 (tipo 52)
#
# El libro de ventas incluye TODOS los documentos emitidos
# en el período, incluyendo NC y ND que afectan las ventas.
# ══════════════════════════════════════════════════════════════

import logging
from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from lxml import etree

from app.db.base import get_db
from app.models.emisor import Emisor
from app.models.certificado import Certificado
from sqlalchemy import select
from app.services.firma_digital import FirmaDigital

logger = logging.getLogger("yepardtecore.cert_libro_ventas")
router = APIRouter(prefix="/certificacion-libro-ventas", tags=["Certificacion Libro Ventas"])

NATENCION   = "4841544"
NS          = "http://www.sii.cl/SiiDte"
RUT_EMISOR  = "78377021-0"
PERIODO     = "2026-05"  # Mayo 2026 — formato gYearMonth (AAAA-MM)

# ── Documentos del período (extraídos de los XMLs aprobados) ──────────────────
DOCUMENTOS = [
    # Set Básico 4841543
    {"tipo": 33, "folio":  97, "fecha": "2026-05-21", "rut_doc": "77777777-7", "razon": "EMPRESA LTDA",
     "neto": 505345,   "exe": 0,     "iva": 96016,  "total": 601361},
    {"tipo": 33, "folio":  98, "fecha": "2026-05-21", "rut_doc": "77777777-7", "razon": "EMPRESA LTDA",
     "neto": 2601662,  "exe": 0,     "iva": 494316, "total": 3095978},
    {"tipo": 33, "folio":  99, "fecha": "2026-05-21", "rut_doc": "77777777-7", "razon": "EMPRESA LTDA",
     "neto": 780422,   "exe": 34968, "iva": 148280, "total": 963670},
    {"tipo": 33, "folio": 101, "fecha": "2026-05-21", "rut_doc": "77777777-7", "razon": "EMPRESA LTDA",
     "neto": 1045156,  "exe": 13594, "iva": 198580, "total": 1257330},
    {"tipo": 61, "folio":  88, "fecha": "2026-05-21", "rut_doc": "77777777-7", "razon": "EMPRESA LTDA",
     "neto": 0,        "exe": 0,     "iva": 0,      "total": 0},
    {"tipo": 61, "folio":  89, "fecha": "2026-05-21", "rut_doc": "77777777-7", "razon": "EMPRESA LTDA",
     "neto": 1254468,  "exe": 0,     "iva": 238349, "total": 1492817},
    {"tipo": 61, "folio":  91, "fecha": "2026-05-21", "rut_doc": "77777777-7", "razon": "EMPRESA LTDA",
     "neto": 780422,   "exe": 34968, "iva": 148280, "total": 963670},
    {"tipo": 56, "folio":  69, "fecha": "2026-05-21", "rut_doc": "77777777-7", "razon": "EMPRESA LTDA",
     "neto": 0,        "exe": 0,     "iva": 0,      "total": 0},
    # Set Exentas 4841548
    {"tipo": 34, "folio":  64, "fecha": "2026-05-21", "rut_doc": "77777777-7", "razon": "EMPRESA LTDA",
     "neto": 0,        "exe": 41496, "iva": 0,      "total": 41496},
    {"tipo": 61, "folio": 109, "fecha": "2026-05-21", "rut_doc": "77777777-7", "razon": "EMPRESA LTDA",
     "neto": 0,        "exe": 5184,  "iva": 0,      "total": 5184},
    {"tipo": 34, "folio":  65, "fecha": "2026-05-21", "rut_doc": "77777777-7", "razon": "EMPRESA LTDA",
     "neto": 0,        "exe": 522026,"iva": 0,      "total": 522026},
    {"tipo": 61, "folio": 110, "fecha": "2026-05-21", "rut_doc": "77777777-7", "razon": "EMPRESA LTDA",
     "neto": 0,        "exe": 0,     "iva": 0,      "total": 0},
    {"tipo": 56, "folio":  78, "fecha": "2026-05-21", "rut_doc": "77777777-7", "razon": "EMPRESA LTDA",
     "neto": 0,        "exe": 0,     "iva": 0,      "total": 0},
    {"tipo": 34, "folio":  66, "fecha": "2026-05-21", "rut_doc": "77777777-7", "razon": "EMPRESA LTDA",
     "neto": 0,        "exe": 530803,"iva": 0,      "total": 530803},
    {"tipo": 61, "folio": 111, "fecha": "2026-05-21", "rut_doc": "77777777-7", "razon": "EMPRESA LTDA",
     "neto": 0,        "exe": 159439,"iva": 0,      "total": 159439},
    {"tipo": 56, "folio":  79, "fecha": "2026-05-21", "rut_doc": "77777777-7", "razon": "EMPRESA LTDA",
     "neto": 0,        "exe": 42385, "iva": 0,      "total": 42385},
    # Nota: Guías de Despacho (tipo 52) NO van en Libro de Ventas (no en enumeración XSD)
    # Las guías tienen su propio Libro de Guías
]


def _construir_libro_xml(emisor: Emisor, periodo: str, tmst: str) -> bytes:
    """Construye el XML del Libro de Ventas según schema LibroCV_v10.xsd"""

    root = etree.Element(f"{{{NS}}}LibroCompraVenta",
        nsmap={None: NS,
               "xsi": "http://www.w3.org/2001/XMLSchema-instance"},
        attrib={
            "version": "1.0",
            "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation":
                f"{NS} LibroCV_v10.xsd",
        }
    )

    # ── EnvioLibro ────────────────────────────────────────────
    envio = etree.SubElement(root, f"{{{NS}}}EnvioLibro")
    envio.set("ID", "LibroVentas")

    # Caratula
    car = etree.SubElement(envio, f"{{{NS}}}Caratula")
    etree.SubElement(car, f"{{{NS}}}RutEmisorLibro").text  = emisor.rut
    etree.SubElement(car, f"{{{NS}}}RutEnvia").text         = "25648612-1"
    etree.SubElement(car, f"{{{NS}}}PeriodoTributario").text = periodo   # AAAA-MM
    etree.SubElement(car, f"{{{NS}}}FchResol").text          = "2026-04-19"
    etree.SubElement(car, f"{{{NS}}}NroResol").text          = "0"
    etree.SubElement(car, f"{{{NS}}}TipoOperacion").text     = "VENTA"
    etree.SubElement(car, f"{{{NS}}}TipoLibro").text         = "ESPECIAL"
    etree.SubElement(car, f"{{{NS}}}TipoEnvio").text         = "TOTAL"
    etree.SubElement(car, f"{{{NS}}}FolioNotificacion").text = NATENCION

    # ResumenPeriodo va ANTES de Detalle (orden XSD)
    # TotalesPeriodo: TpoDoc → TotDoc → TotMntExe(req) → TotMntNeto(req) → TotMntIVA(req) → TotMntTotal(req)
    resumen = etree.SubElement(envio, f"{{{NS}}}ResumenPeriodo")
    for tipo_doc in sorted(set(d["tipo"] for d in DOCUMENTOS)):
        docs_tipo = [d for d in DOCUMENTOS if d["tipo"] == tipo_doc]
        t_exe   = sum(d["exe"]   for d in docs_tipo)
        t_neto  = sum(d["neto"]  for d in docs_tipo)
        t_iva   = sum(d["iva"]   for d in docs_tipo)
        t_total = sum(d["total"] for d in docs_tipo)
        tot = etree.SubElement(resumen, f"{{{NS}}}TotalesPeriodo")
        etree.SubElement(tot, f"{{{NS}}}TpoDoc").text      = str(tipo_doc)
        etree.SubElement(tot, f"{{{NS}}}TotDoc").text      = str(len(docs_tipo))
        etree.SubElement(tot, f"{{{NS}}}TotMntExe").text   = str(t_exe)   # obligatorio
        etree.SubElement(tot, f"{{{NS}}}TotMntNeto").text  = str(t_neto)  # obligatorio
        etree.SubElement(tot, f"{{{NS}}}TotMntIVA").text   = str(t_iva)   # obligatorio
        etree.SubElement(tot, f"{{{NS}}}TotMntTotal").text = str(t_total) # obligatorio

    # Detalle de documentos
    for i, doc in enumerate(DOCUMENTOS, 1):
        det = etree.SubElement(envio, f"{{{NS}}}Detalle")
        etree.SubElement(det, f"{{{NS}}}TpoDoc").text   = str(doc["tipo"])
        etree.SubElement(det, f"{{{NS}}}NroDoc").text   = str(doc["folio"])
        etree.SubElement(det, f"{{{NS}}}TasaImp").text  = "19"
        etree.SubElement(det, f"{{{NS}}}FchDoc").text   = doc["fecha"]
        etree.SubElement(det, f"{{{NS}}}RUTDoc").text   = doc["rut_doc"]
        etree.SubElement(det, f"{{{NS}}}RznSoc").text   = doc["razon"][:50]

        # Orden XSD Detalle: MntExe → MntNeto → MntIVA → MntTotal
        # El SII exige al menos uno de estos campos aunque sea 0
        if doc["exe"] != 0:
            etree.SubElement(det, f"{{{NS}}}MntExe").text   = str(doc["exe"])
        etree.SubElement(det, f"{{{NS}}}MntNeto").text  = str(doc["neto"])
        if doc["iva"] != 0:
            etree.SubElement(det, f"{{{NS}}}MntIVA").text   = str(doc["iva"])
        etree.SubElement(det, f"{{{NS}}}MntTotal").text = str(doc["total"])

    # Resumen del período (guías tipo 52 excluidas — no están en enumeración XSD)
    total_neto  = sum(d["neto"]  for d in DOCUMENTOS if d["tipo"] in (33,34))
    total_neto -= sum(d["neto"]  for d in DOCUMENTOS if d["tipo"] in (61,56))
    total_exe   = sum(d["exe"]   for d in DOCUMENTOS if d["tipo"] in (33,34))
    total_exe  -= sum(d["exe"]   for d in DOCUMENTOS if d["tipo"] in (61,56))
    total_iva   = sum(d["iva"]   for d in DOCUMENTOS if d["tipo"] in (33,34))
    total_iva  -= sum(d["iva"]   for d in DOCUMENTOS if d["tipo"] in (61,56))
    total_tot   = sum(d["total"] for d in DOCUMENTOS if d["tipo"] in (33,34))
    total_tot  -= sum(d["total"] for d in DOCUMENTOS if d["tipo"] in (61,56))

    etree.SubElement(envio, f"{{{NS}}}TmstFirma").text = tmst

    xml_bytes = etree.tostring(root, encoding="ISO-8859-1", xml_declaration=True, pretty_print=True)
    xml_str   = xml_bytes.decode("ISO-8859-1")
    xml_str   = xml_str.replace(
        "<?xml version='1.0' encoding='ISO-8859-1'?>",
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
    )
    return xml_str.encode("ISO-8859-1")


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


@router.post("/generar-xml", summary="Genera Libro de Ventas N° Atención 4841544")
async def generar_libro_ventas(
    emisor_id: int,
    periodo: Optional[str] = PERIODO,
    db: AsyncSession = Depends(get_db),
):
    from datetime import datetime
    emisor, cert = await _get_emisor_y_cert(emisor_id, db)
    tmst = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    try:
        libro_xml = _construir_libro_xml(emisor, periodo, tmst)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error construyendo libro: {e}")

    # Firmar el libro
    firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    try:
        libro_firmado = await firma.firmar_libro(libro_xml.decode("ISO-8859-1"))
        logger.info("[LIBRO VENTAS] Firma OK")
    except Exception as e:
        logger.error(f"[LIBRO VENTAS] Error firmando: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error firmando libro: {e}")

    # Garantizar comillas dobles en declaración XML (SII rechaza comillas simples)
    libro_firmado = libro_firmado.replace(
        "<?xml version='1.0' encoding='ISO-8859-1'?>",
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
    )

    rut_limpio = emisor.rut.replace(".", "").replace("-", "")
    nombre = f"LibroVentas_{rut_limpio}_{periodo.replace('-','')}.xml"

    return Response(
        content=libro_firmado.encode("ISO-8859-1"),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )
