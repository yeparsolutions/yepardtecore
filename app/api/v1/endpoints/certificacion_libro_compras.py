# app/api/v1/endpoints/certificacion_libro_compras.py
# Libro de Compras con lógica especial: IVA uso común, no recuperable, retención

import logging
from datetime import datetime
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

NS        = "http://www.sii.cl/SiiDte"
RUT_PROV  = "76354771-K"
FCT_PROP  = "0.60"

def _iva(n): return round(n * 0.19)

DOCUMENTOS = [
    # Doc 30 - Factura No Afecta o Exenta (IVA normal)
    {"tipo": 30, "folio": 234, "fecha": "2026-05-22", "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
     "neto": 34744, "exe": 0, "iva": _iva(34744), "total": 34744 + _iva(34744), "tipo_especial": None},

    # Doc 30 - Factura No Afecta (IVA uso común)
    {"tipo": 30, "folio": 781, "fecha": "2026-05-22", "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
     "neto": 29947, "exe": 0, "iva": 0, "iva_uso_comun": _iva(29947),
     "total": 29947 + _iva(29947), "tipo_especial": "iva_uso_comun"},

    # Doc 33 - Factura Electrónica (con monto exento + IVA normal)
    {"tipo": 33, "folio": 32, "fecha": "2026-05-22", "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
     "neto": 8608, "exe": 9597, "iva": _iva(8608), "total": 8608 + _iva(8608) + 9597, "tipo_especial": None},

    # Doc 33 - Factura Electrónica (IVA no recuperable cod. 9)
    # FIX REPARO 1: iva=0 en el dict para que TotMntIVA del resumen no lo sume;
    # el IVA no recuperable va SOLO en iva_no_rec / TotIVANoRec.
    {"tipo": 33, "folio": 67, "fecha": "2026-05-22", "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
     "neto": 10913, "exe": 0, "iva": 0, "iva_no_rec": _iva(10913), "cod_iva_no_rec": 9,
     "total": 10913 + _iva(10913), "tipo_especial": "iva_no_rec"},

    # Doc 46 - Factura de Compra (IVA retenido total)
    # FIX REPARO 2: iva=0 para que MntIVA sea 0 en el detalle y TotMntIVA=0 en resumen.
    # El IVA retenido se informa SOLO en iva_ret_total / IVARetTotal.
    # TotMntTotal = neto + iva_ret_total (el comprador igual registra el total del doc).
    {"tipo": 46, "folio": 9, "fecha": "2026-05-22", "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
     "neto": 10019, "exe": 0, "iva": 0, "iva_ret_total": _iva(10019),
     "total": 10019 + _iva(10019), "tipo_especial": "iva_ret_total"},

    # Doc 60 - Liquidación-Factura Electrónica
    # FIX REPARO 3: tipo 60 agregado; el set de prueba lo requiere y no existía.
    {"tipo": 60, "folio": 1, "fecha": "2026-05-22", "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
     "neto": 5000, "exe": 0, "iva": _iva(5000), "total": 5000 + _iva(5000), "tipo_especial": None},

    # Doc 61 - Nota de Débito Electrónica (con IVA)
    {"tipo": 61, "folio": 451, "fecha": "2026-05-22", "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
     "neto": 2807, "exe": 0, "iva": _iva(2807), "total": 2807 + _iva(2807), "tipo_especial": None},

    # Doc 61 - Nota de Débito Electrónica (solo exento)
    {"tipo": 61, "folio": 211, "fecha": "2026-05-22", "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
     "neto": 0, "exe": 6396, "iva": 0, "total": 6396, "tipo_especial": None},
]

def _construir_libro_xml(emisor: Emisor, rut_envia: str, natencion: str,
                          periodo: str, tmst: str) -> str:
    root = etree.Element(f"{{{NS}}}LibroCompraVenta",
        nsmap={None: NS, "xsi": "http://www.w3.org/2001/XMLSchema-instance"},
        attrib={"version": "1.0",
                "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation":
                    f"{NS} LibroCV_v10.xsd"})
    envio = etree.SubElement(root, f"{{{NS}}}EnvioLibro")
    envio.set("ID", "LibroCompras")

    car = etree.SubElement(envio, f"{{{NS}}}Caratula")
    etree.SubElement(car, f"{{{NS}}}RutEmisorLibro").text   = emisor.rut
    etree.SubElement(car, f"{{{NS}}}RutEnvia").text          = rut_envia
    etree.SubElement(car, f"{{{NS}}}PeriodoTributario").text = periodo
    etree.SubElement(car, f"{{{NS}}}FchResol").text          = "2026-04-19"
    etree.SubElement(car, f"{{{NS}}}NroResol").text          = "0"
    etree.SubElement(car, f"{{{NS}}}TipoOperacion").text     = "COMPRA"
    etree.SubElement(car, f"{{{NS}}}TipoLibro").text         = "ESPECIAL"
    etree.SubElement(car, f"{{{NS}}}TipoEnvio").text         = "TOTAL"
    etree.SubElement(car, f"{{{NS}}}FolioNotificacion").text = natencion

    resumen = etree.SubElement(envio, f"{{{NS}}}ResumenPeriodo")
    for tipo_doc in sorted(set(d["tipo"] for d in DOCUMENTOS)):
        dt = [d for d in DOCUMENTOS if d["tipo"] == tipo_doc]
        tot = etree.SubElement(resumen, f"{{{NS}}}TotalesPeriodo")
        etree.SubElement(tot, f"{{{NS}}}TpoDoc").text     = str(tipo_doc)
        etree.SubElement(tot, f"{{{NS}}}TotDoc").text     = str(len(dt))
        etree.SubElement(tot, f"{{{NS}}}TotMntExe").text  = str(sum(d["exe"] for d in dt))
        etree.SubElement(tot, f"{{{NS}}}TotMntNeto").text = str(sum(d["neto"] for d in dt))

        # TotMntIVA: solo IVA recuperable normal (iva=0 en docs con ret/no-rec)
        etree.SubElement(tot, f"{{{NS}}}TotMntIVA").text  = str(sum(d["iva"] for d in dt))

        # FIX REPARO 1: TotIVANoRec informa el IVA no recuperable por separado
        t_nr = sum(d.get("iva_no_rec", 0) for d in dt)
        if t_nr:
            inr = etree.SubElement(tot, f"{{{NS}}}TotIVANoRec")
            # Agrupar por código si hubiera varios; aquí todos son cod 9
            cod_nr = next(d.get("cod_iva_no_rec", 9) for d in dt if d.get("iva_no_rec", 0))
            etree.SubElement(inr, f"{{{NS}}}CodIVANoRec").text    = str(cod_nr)
            etree.SubElement(inr, f"{{{NS}}}TotOpIVANoRec").text  = str(sum(1 for d in dt if d.get("iva_no_rec", 0)))
            etree.SubElement(inr, f"{{{NS}}}TotMntIVANoRec").text = str(t_nr)

        t_uc = sum(d.get("iva_uso_comun", 0) for d in dt)
        if t_uc:
            etree.SubElement(tot, f"{{{NS}}}TotIVAUsoComun").text    = str(t_uc)
            etree.SubElement(tot, f"{{{NS}}}FctProp").text            = FCT_PROP
            etree.SubElement(tot, f"{{{NS}}}TotCredIVAUsoComun").text = str(round(t_uc * float(FCT_PROP)))

        # FIX REPARO 2: TotIVARetTotal informa la retención; TotMntIVA ya es 0 para estos docs
        t_ret = sum(d.get("iva_ret_total", 0) for d in dt)
        if t_ret:
            etree.SubElement(tot, f"{{{NS}}}TotOpIVARetTotal").text = str(sum(1 for d in dt if d.get("iva_ret_total", 0)))
            etree.SubElement(tot, f"{{{NS}}}TotIVARetTotal").text   = str(t_ret)

        etree.SubElement(tot, f"{{{NS}}}TotMntTotal").text = str(sum(d["total"] for d in dt))

    for doc in DOCUMENTOS:
        det = etree.SubElement(envio, f"{{{NS}}}Detalle")
        etree.SubElement(det, f"{{{NS}}}TpoDoc").text  = str(doc["tipo"])
        etree.SubElement(det, f"{{{NS}}}NroDoc").text  = str(doc["folio"])
        etree.SubElement(det, f"{{{NS}}}TasaImp").text = "19"
        etree.SubElement(det, f"{{{NS}}}FchDoc").text  = doc["fecha"]
        etree.SubElement(det, f"{{{NS}}}RUTDoc").text  = doc["rut_doc"]
        etree.SubElement(det, f"{{{NS}}}RznSoc").text  = doc["razon"][:50]
        if doc["exe"]:
            etree.SubElement(det, f"{{{NS}}}MntExe").text = str(doc["exe"])
        etree.SubElement(det, f"{{{NS}}}MntNeto").text = str(doc["neto"])

        te = doc.get("tipo_especial")
        if te == "iva_uso_comun":
            etree.SubElement(det, f"{{{NS}}}MntIVA").text      = "0"
            etree.SubElement(det, f"{{{NS}}}IVAUsoComun").text = str(doc["iva_uso_comun"])
        elif te == "iva_no_rec":
            # FIX REPARO 1: MntIVA=0, el monto va en IVANoRec
            etree.SubElement(det, f"{{{NS}}}MntIVA").text = "0"
            inr = etree.SubElement(det, f"{{{NS}}}IVANoRec")
            etree.SubElement(inr, f"{{{NS}}}CodIVANoRec").text = str(doc["cod_iva_no_rec"])
            etree.SubElement(inr, f"{{{NS}}}MntIVANoRec").text = str(doc["iva_no_rec"])
        elif te == "iva_ret_total":
            # FIX REPARO 2: MntIVA=0 (no es crédito fiscal), retención en IVARetTotal
            etree.SubElement(det, f"{{{NS}}}MntIVA").text      = "0"
            etree.SubElement(det, f"{{{NS}}}IVARetTotal").text = str(doc["iva_ret_total"])
        else:
            etree.SubElement(det, f"{{{NS}}}MntIVA").text = str(doc["iva"])

        etree.SubElement(det, f"{{{NS}}}MntTotal").text = str(doc["total"])

    etree.SubElement(envio, f"{{{NS}}}TmstFirma").text = tmst
    xml_bytes = etree.tostring(root, encoding="ISO-8859-1",
                               xml_declaration=True, pretty_print=True)
    return xml_bytes.decode("ISO-8859-1").replace(
        "<?xml version='1.0' encoding='ISO-8859-1'?>",
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
    )


@router.post("/generar-xml", summary="Genera Libro de Compras N° Atención 4841545")
async def generar_libro_compras(
    emisor_id: int,
    natencion: Optional[str] = "4841545",
    periodo:   Optional[str] = "2026-05",
    db: AsyncSession = Depends(get_db),
):
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(404, f"Emisor {emisor_id} no encontrado")

    res = await db.execute(
        select(Certificado).where(Certificado.emisor_id == emisor_id).limit(1)
    )
    cert = res.scalar_one_or_none()
    if not cert or not cert.certificado_p12:
        raise HTTPException(400, "Sin certificado .p12")

    rut_envia = cert.rut_firmante or emisor.rut
    tmst      = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    try:
        xml_str = _construir_libro_xml(emisor, rut_envia, natencion, periodo, tmst)
    except Exception as e:
        raise HTTPException(500, f"Error construyendo libro: {e}")

    firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    try:
        xml_firmado = await firma.firmar_libro(xml_str)
    except Exception as e:
        raise HTTPException(500, f"Error firmando: {e}")

    rut_limpio = emisor.rut.replace(".", "").replace("-", "")
    nombre = f"LibroCompras_{natencion}_{rut_limpio}_{periodo}.xml"
    return Response(
        content    = xml_firmado.encode("ISO-8859-1"),
        media_type = "application/octet-stream",
        headers    = {"Content-Disposition": f'attachment; filename="{nombre}"'},
    )
