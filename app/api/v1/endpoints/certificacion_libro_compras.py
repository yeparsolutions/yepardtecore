# app/api/v1/endpoints/certificacion_libro_compras.py
# ══════════════════════════════════════════════════════════════
# LIBRO DE COMPRAS — NÚMERO DE ATENCIÓN: 4841545
#
# Casos especiales según el set:
#   FAC 234:     afecto normal, derecho a crédito
#   FAC-E 32:    mixta (exento + afecto), derecho a crédito
#   FAC 781:     IVA uso común, factor 0.60
#   NC 451:      descuento sobre FAC 234
#   FAC-E 67:    entrega gratuita (IVANoRec cod=6)
#   FCOMPRA-E 9: retención total IVA (IVARetTotal)
#   NC-E 211:    descuento sobre FAC-E 32
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

logger = logging.getLogger("yepardtecore.cert_libro_compras")
router = APIRouter(prefix="/certificacion-libro-compras", tags=["Certificacion Libro Compras"])

NATENCION = "4841545"
NS        = "http://www.sii.cl/SiiDte"
PERIODO   = "2026-05"

# ── Documentos del período ────────────────────────────────────────────────────
# RUT proveedor genérico para certificación
RUT_PROV = "76354771-K"

def _iva(n): return round(n * 0.19)

DOCUMENTOS = [
    # FAC 234: normal afecta, derecho a crédito
    {
        "tipo": 30, "folio": 234, "fecha": "2026-05-21",
        "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
        "neto": 34744, "exe": 0, "iva": _iva(34744),
        "total": 34744 + _iva(34744),
        "tipo_especial": None,
    },
    # FAC-E 32: mixta exento + afecto
    {
        "tipo": 33, "folio": 32, "fecha": "2026-05-21",
        "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
        "neto": 8608, "exe": 9597, "iva": _iva(8608),
        "total": 8608 + _iva(8608) + 9597,
        "tipo_especial": None,
    },
    # FAC 781: IVA uso común, factor 0.60
    {
        "tipo": 30, "folio": 781, "fecha": "2026-05-21",
        "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
        "neto": 29947, "exe": 0,
        "iva": 0,           # MntIVA=0 cuando es IVA uso común
        "iva_uso_comun": _iva(29947),
        "total": 29947 + _iva(29947),
        "tipo_especial": "iva_uso_comun",
    },
    # NC 451: descuento sobre FAC 234
    {
        "tipo": 60, "folio": 451, "fecha": "2026-05-21",
        "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
        "neto": 2807, "exe": 0, "iva": _iva(2807),
        "total": 2807 + _iva(2807),
        "tipo_especial": None,
    },
    # FAC-E 67: entrega gratuita → IVA no recuperable cod=6
    {
        "tipo": 33, "folio": 67, "fecha": "2026-05-21",
        "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
        "neto": 10913, "exe": 0,
        "iva": 0,           # MntIVA=0 cuando no es recuperable
        "iva_no_rec": _iva(10913),
        "cod_iva_no_rec": 9,  # 9=otros (entrega gratuita — cod 6 no existe en XSD)
        "total": 10913 + _iva(10913),
        "tipo_especial": "iva_no_rec",
    },
    # FCOMPRA-E 9: retención total IVA
    {
        "tipo": 46, "folio": 9, "fecha": "2026-05-21",
        "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
        "neto": 10019, "exe": 0,
        "iva": _iva(10019),
        "iva_ret_total": _iva(10019),
        "total": 10019 + _iva(10019),  # Total = Neto + IVA (valor doc, no del pago)
        "tipo_especial": "iva_ret_total",
    },
    # NC-E 211: descuento sobre FAC-E 32 (exento)
    {
        "tipo": 61, "folio": 211, "fecha": "2026-05-21",
        "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
        "neto": 0, "exe": 6396, "iva": 0,
        "total": 6396,
        "tipo_especial": None,
    },
]

# Factor de proporcionalidad IVA uso común
FCT_PROP = "0.60"


def _construir_libro_xml(emisor: Emisor, periodo: str, tmst: str) -> bytes:
    """Construye el XML del Libro de Compras según schema LibroCV_v10.xsd"""

    root = etree.Element(f"{{{NS}}}LibroCompraVenta",
        nsmap={None: NS, "xsi": "http://www.w3.org/2001/XMLSchema-instance"},
        attrib={
            "version": "1.0",
            "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation":
                f"{NS} LibroCV_v10.xsd",
        })

    envio = etree.SubElement(root, f"{{{NS}}}EnvioLibro")
    envio.set("ID", "LibroCompras")

    # Caratula
    car = etree.SubElement(envio, f"{{{NS}}}Caratula")
    etree.SubElement(car, f"{{{NS}}}RutEmisorLibro").text   = emisor.rut
    etree.SubElement(car, f"{{{NS}}}RutEnvia").text          = "25648612-1"
    etree.SubElement(car, f"{{{NS}}}PeriodoTributario").text = periodo
    etree.SubElement(car, f"{{{NS}}}FchResol").text          = "2026-04-19"
    etree.SubElement(car, f"{{{NS}}}NroResol").text          = "0"
    etree.SubElement(car, f"{{{NS}}}TipoOperacion").text     = "COMPRA"
    etree.SubElement(car, f"{{{NS}}}TipoLibro").text         = "ESPECIAL"
    etree.SubElement(car, f"{{{NS}}}TipoEnvio").text         = "TOTAL"
    etree.SubElement(car, f"{{{NS}}}FolioNotificacion").text = NATENCION

    # ResumenPeriodo — ANTES de Detalle
    # Agrupar por tipo de documento
    resumen = etree.SubElement(envio, f"{{{NS}}}ResumenPeriodo")
    for tipo_doc in sorted(set(d["tipo"] for d in DOCUMENTOS)):
        docs_tipo  = [d for d in DOCUMENTOS if d["tipo"] == tipo_doc]
        t_exe      = sum(d["exe"]  for d in docs_tipo)
        t_neto     = sum(d["neto"] for d in docs_tipo)
        t_iva      = sum(d["iva"]  for d in docs_tipo)
        t_total    = sum(d["total"] for d in docs_tipo)
        t_iva_uc   = sum(d.get("iva_uso_comun", 0) for d in docs_tipo)
        t_iva_nr   = sum(d.get("iva_no_rec", 0)    for d in docs_tipo)
        t_iva_ret  = sum(d.get("iva_ret_total", 0) for d in docs_tipo)

        tot = etree.SubElement(resumen, f"{{{NS}}}TotalesPeriodo")
        etree.SubElement(tot, f"{{{NS}}}TpoDoc").text     = str(tipo_doc)
        etree.SubElement(tot, f"{{{NS}}}TotDoc").text     = str(len(docs_tipo))
        etree.SubElement(tot, f"{{{NS}}}TotMntExe").text  = str(t_exe)
        etree.SubElement(tot, f"{{{NS}}}TotMntNeto").text = str(t_neto)
        etree.SubElement(tot, f"{{{NS}}}TotMntIVA").text  = str(t_iva)
        # IVA no recuperable — estructura: TotIVANoRec > CodIVANoRec + TotOpIVANoRec + TotMntIVANoRec
        if t_iva_nr:
            inr = etree.SubElement(tot, f"{{{NS}}}TotIVANoRec")
            etree.SubElement(inr, f"{{{NS}}}CodIVANoRec").text    = str(docs_tipo[0].get("cod_iva_no_rec", 9))
            etree.SubElement(inr, f"{{{NS}}}TotOpIVANoRec").text  = str(sum(1 for d in docs_tipo if d.get("iva_no_rec",0)))
            etree.SubElement(inr, f"{{{NS}}}TotMntIVANoRec").text = str(t_iva_nr)
        # IVA uso común — va después de TotIVANoRec
        if t_iva_uc:
            etree.SubElement(tot, f"{{{NS}}}TotIVAUsoComun").text     = str(t_iva_uc)
            etree.SubElement(tot, f"{{{NS}}}FctProp").text             = FCT_PROP
            etree.SubElement(tot, f"{{{NS}}}TotCredIVAUsoComun").text  = str(round(t_iva_uc * 0.60))
        # IVA retenido total — TotOpIVARetTotal + TotIVARetTotal
        if t_iva_ret:
            etree.SubElement(tot, f"{{{NS}}}TotOpIVARetTotal").text = str(sum(1 for d in docs_tipo if d.get("iva_ret_total",0)))
            etree.SubElement(tot, f"{{{NS}}}TotIVARetTotal").text   = str(t_iva_ret)
        etree.SubElement(tot, f"{{{NS}}}TotMntTotal").text = str(t_total)

    # Detalle de documentos
    for doc in DOCUMENTOS:
        det = etree.SubElement(envio, f"{{{NS}}}Detalle")
        etree.SubElement(det, f"{{{NS}}}TpoDoc").text  = str(doc["tipo"])
        etree.SubElement(det, f"{{{NS}}}NroDoc").text  = str(doc["folio"])
        etree.SubElement(det, f"{{{NS}}}TasaImp").text = "19"
        etree.SubElement(det, f"{{{NS}}}FchDoc").text  = doc["fecha"]
        etree.SubElement(det, f"{{{NS}}}RUTDoc").text  = doc["rut_doc"]
        etree.SubElement(det, f"{{{NS}}}RznSoc").text  = doc["razon"][:50]

        # Montos según tipo especial
        if doc["exe"]:
            etree.SubElement(det, f"{{{NS}}}MntExe").text  = str(doc["exe"])
        etree.SubElement(det, f"{{{NS}}}MntNeto").text = str(doc["neto"])

        te = doc.get("tipo_especial")
        if te == "iva_uso_comun":
            # MntIVA=0, IVAUsoComun=monto
            etree.SubElement(det, f"{{{NS}}}MntIVA").text      = "0"
            etree.SubElement(det, f"{{{NS}}}IVAUsoComun").text = str(doc["iva_uso_comun"])
        elif te == "iva_no_rec":
            # MntIVA=0, IVANoRec con CodIVANoRec
            etree.SubElement(det, f"{{{NS}}}MntIVA").text = "0"
            inr = etree.SubElement(det, f"{{{NS}}}IVANoRec")
            etree.SubElement(inr, f"{{{NS}}}CodIVANoRec").text = str(doc["cod_iva_no_rec"])
            etree.SubElement(inr, f"{{{NS}}}MntIVANoRec").text = str(doc["iva_no_rec"])
        elif te == "iva_ret_total":
            # MntIVA normal + IVARetTotal
            etree.SubElement(det, f"{{{NS}}}MntIVA").text       = str(doc["iva"])
            etree.SubElement(det, f"{{{NS}}}IVARetTotal").text  = str(doc["iva_ret_total"])
        else:
            # Siempre emitir MntIVA (SII lo exige aunque sea 0)
            etree.SubElement(det, f"{{{NS}}}MntIVA").text = str(doc["iva"])

        etree.SubElement(det, f"{{{NS}}}MntTotal").text = str(doc["total"])

    etree.SubElement(envio, f"{{{NS}}}TmstFirma").text = tmst

    xml_bytes = etree.tostring(root, encoding="ISO-8859-1",
                               xml_declaration=True, pretty_print=True)
    xml_str   = xml_bytes.decode("ISO-8859-1").replace(
        "<?xml version='1.0' encoding='ISO-8859-1'?>",
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
    )
    return xml_str.encode("ISO-8859-1")


async def _get_emisor_y_cert(emisor_id: int, db: AsyncSession):
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(404, f"Emisor {emisor_id} no encontrado")
    res = await db.execute(
        select(Certificado).where(
            Certificado.emisor_id == emisor_id,
            Certificado.activo == True
        ).limit(1))
    cert = res.scalar_one_or_none()
    if not cert or not cert.certificado_p12:
        raise HTTPException(400, "Sin certificado .p12")
    return emisor, cert


@router.post("/generar-xml", summary="Genera Libro de Compras N° Atención 4841545")
async def generar_libro_compras(
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
        raise HTTPException(500, f"Error construyendo libro: {e}")

    firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    try:
        libro_firmado = await firma.firmar_libro(libro_xml.decode("ISO-8859-1"))
    except Exception as e:
        logger.error(f"[LIBRO COMPRAS] Error firmando: {e}", exc_info=True)
        raise HTTPException(500, f"Error firmando: {e}")

    libro_firmado = libro_firmado.replace(
        "<?xml version='1.0' encoding='ISO-8859-1'?>",
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
    )

    rut_limpio = emisor.rut.replace(".", "").replace("-", "")
    nombre = f"LibroCompras_{rut_limpio}_{periodo.replace('-','')}.xml"
    return Response(
        content=libro_firmado.encode("ISO-8859-1"),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )
