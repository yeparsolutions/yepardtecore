# app/api/v1/endpoints/certificacion_libro_guias.py
# ══════════════════════════════════════════════════════════════
# LIBRO DE GUÍAS — NÚMERO DE ATENCIÓN: 4841547
# Schema: LibroGuia (distinto a LibroCompraVenta)
# ══════════════════════════════════════════════════════════════

import logging
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

logger = logging.getLogger("yepardtecore.cert_libro_guias")
router = APIRouter(prefix="/certificacion-libro-guias", tags=["Certificacion Libro Guias"])

NATENCION = "4841547"
NS        = "http://www.sii.cl/SiiDte"
PERIODO   = "2026-05"
RUT_EMISOR = "78377021-0"

DOCUMENTOS = [
    {"folio": 54, "fecha": "2026-05-21", "rut_doc": RUT_EMISOR,
     "razon": "YEPAR SOLUTIONS SPA",
     "ind_traslado": 5,
     "neto": 0, "iva": 0, "total": 0},
    {"folio": 55, "fecha": "2026-05-21", "rut_doc": "77777777-7",
     "razon": "EMPRESA LTDA",
     "ind_traslado": 1,
     "neto": 3826814, "iva": 727095, "total": 4553909},
    {"folio": 56, "fecha": "2026-05-21", "rut_doc": "77777777-7",
     "razon": "EMPRESA LTDA",
     "ind_traslado": 2,
     "neto": 2830303, "iva": 537758, "total": 3368061},
]


def _construir_libro_xml(emisor: Emisor, periodo: str, tmst: str) -> bytes:
    """
    Libro de Guías de Despacho Electrónicas.
    Schema raíz: LibroGuia (no LibroCompraVenta).
    Ref: https://www.sii.cl/factura_electronica/formato_lgd.pdf
    """
    root = etree.Element(f"{{{NS}}}LibroGuia",
        nsmap={None: NS, "xsi": "http://www.w3.org/2001/XMLSchema-instance"},
        attrib={
            "version": "1.0",
            "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation":
                f"{NS} LibroGuia_v10.xsd",
        })

    envio = etree.SubElement(root, f"{{{NS}}}EnvioLibro")
    envio.set("ID", "LibroGuias")

    # Caratula — TipoDespacho en vez de TipoOperacion
    car = etree.SubElement(envio, f"{{{NS}}}Caratula")
    etree.SubElement(car, f"{{{NS}}}RutEmisorLibro").text   = emisor.rut
    etree.SubElement(car, f"{{{NS}}}RutEnvia").text          = "25648612-1"
    etree.SubElement(car, f"{{{NS}}}PeriodoTributario").text = periodo
    etree.SubElement(car, f"{{{NS}}}FchResol").text          = "2026-04-19"
    etree.SubElement(car, f"{{{NS}}}NroResol").text          = "0"
    etree.SubElement(car, f"{{{NS}}}TipoLibro").text         = "ESPECIAL"
    etree.SubElement(car, f"{{{NS}}}TipoEnvio").text         = "TOTAL"
    etree.SubElement(car, f"{{{NS}}}FolioNotificacion").text = NATENCION

    # ResumenPeriodo ANTES del Detalle
    t_neto  = sum(d["neto"]  for d in DOCUMENTOS)
    t_iva   = sum(d["iva"]   for d in DOCUMENTOS)
    t_total = sum(d["total"] for d in DOCUMENTOS)

    resumen = etree.SubElement(envio, f"{{{NS}}}ResumenPeriodo")
    ventas    = [d for d in DOCUMENTOS if d["ind_traslado"] in (1, 2)]
    traslados = [d for d in DOCUMENTOS if d["ind_traslado"] not in (1, 2)]
    etree.SubElement(resumen, f"{{{NS}}}TotFolAnulado").text  = "0"
    etree.SubElement(resumen, f"{{{NS}}}TotGuiaAnulada").text = "0"
    # TotGuiaVenta = entero (cantidad), TotMntGuiaVta = monto total ventas
    etree.SubElement(resumen, f"{{{NS}}}TotGuiaVenta").text    = str(len(ventas))
    etree.SubElement(resumen, f"{{{NS}}}TotMntGuiaVta").text   = str(sum(d["total"] for d in ventas))
    etree.SubElement(resumen, f"{{{NS}}}TotMntModificado").text = "0"
    # TotTraslado es complejo: un elemento por tipo de traslado (IndTraslado)
    # IndTraslado=5 = traslado interno
    for ind, docs_ind in {5: [d for d in traslados if d["ind_traslado"]==5]}.items():
        if docs_ind:
            tt = etree.SubElement(resumen, f"{{{NS}}}TotTraslado")
            etree.SubElement(tt, f"{{{NS}}}TpoTraslado").text = str(ind)
            etree.SubElement(tt, f"{{{NS}}}CantGuia").text    = str(len(docs_ind))
            etree.SubElement(tt, f"{{{NS}}}MntGuia").text     = str(sum(d["total"] for d in docs_ind))

    # Detalle — incluye IndTraslado
    for doc in DOCUMENTOS:
        det = etree.SubElement(envio, f"{{{NS}}}Detalle")
        # Orden según LibroGuia_v10: Folio → FchDoc → RUTDoc → RznSoc
        #   → IndTraslado → MntNeto → TasaImp → IVA → MntTotal
        # Orden Detalle: Folio → FchDoc → RUTDoc → RznSoc
        #   → [MntNeto] → [TasaImp] → [IVA] → MntTotal → [IndTraslado]
        etree.SubElement(det, f"{{{NS}}}Folio").text    = str(doc["folio"])
        etree.SubElement(det, f"{{{NS}}}FchDoc").text   = doc["fecha"]
        etree.SubElement(det, f"{{{NS}}}RUTDoc").text   = doc["rut_doc"]
        etree.SubElement(det, f"{{{NS}}}RznSoc").text   = doc["razon"][:50]
        if doc["neto"]:
            etree.SubElement(det, f"{{{NS}}}MntNeto").text = str(doc["neto"])
        if doc["iva"]:
            etree.SubElement(det, f"{{{NS}}}TasaImp").text = "19"
            etree.SubElement(det, f"{{{NS}}}IVA").text     = str(doc["iva"])
        etree.SubElement(det, f"{{{NS}}}MntTotal").text    = str(doc["total"])
        # IndTraslado va al final (después de MntModificado y refs opcionales)

    etree.SubElement(envio, f"{{{NS}}}TmstFirma").text = tmst

    xml_bytes = etree.tostring(root, encoding="ISO-8859-1",
                               xml_declaration=True, pretty_print=True)
    xml_str = xml_bytes.decode("ISO-8859-1").replace(
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


@router.post("/generar-xml", summary="Genera Libro de Guías N° Atención 4841547")
async def generar_libro_guias(
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
        logger.error(f"[LIBRO GUIAS] Error firmando: {e}", exc_info=True)
        raise HTTPException(500, f"Error firmando: {e}")

    libro_firmado = libro_firmado.replace(
        "<?xml version='1.0' encoding='ISO-8859-1'?>",
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
    )

    rut_limpio = emisor.rut.replace(".", "").replace("-", "")
    nombre = f"LibroGuias_{rut_limpio}_{periodo.replace('-','')}.xml"
    return Response(
        content=libro_firmado.encode("ISO-8859-1"),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )
