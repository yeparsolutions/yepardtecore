# app/api/v1/endpoints/libro_guias.py
# ═══════════════════════════════════════════════════════════════════
# Endpoint LIMPIO para Libro de Guías de Despacho (T52)
# POST /v1/libro-guias/
# Body: { emisor_id, natencion, periodo, guias: [...] }
# ═══════════════════════════════════════════════════════════════════

import logging
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from lxml import etree

from app.db.base import get_db
from app.models.emisor import Emisor
from app.models.certificado import Certificado
from app.services.firma_digital import FirmaDigital

logger = logging.getLogger("yepardtecore.libro_guias")
router = APIRouter(prefix="/libro-guias", tags=["Libro Guías"])

NS = "http://www.sii.cl/SiiDte"


class GuiaDespacho(BaseModel):
    folio: int
    fecha: str
    rut: str = "66666666-6"
    razon: str = ""
    exe: int = 0
    neto: int = 0
    iva: int = 0
    total: int = 0
    anulado: bool = False


class LibroGuiasRequest(BaseModel):
    emisor_id: int
    natencion: str
    periodo: str = "2026-05"
    fch_resol: str = "2026-04-19"
    nro_resol: str = "0"
    guias: List[GuiaDespacho]


def _xml_libro_guias(emisor_rut: str, rut_envia: str, req: LibroGuiasRequest) -> str:
    tmst = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # Clasificar guías
    guias_anuld = [g for g in req.guias if g.anulado]
    guias_venta = [g for g in req.guias if not g.anulado]
    tot_mnt_vta = sum(g.total for g in guias_venta)

    root = etree.Element(
        f"{{{NS}}}LibroGuia",
        nsmap={None: NS, "xsi": "http://www.w3.org/2001/XMLSchema-instance"},
        attrib={
            "version": "1.0",
            "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation": f"{NS} LibroGuia_v10.xsd",
        },
    )
    envio = etree.SubElement(root, f"{{{NS}}}EnvioLibro")
    envio.set("ID", "LibroGuias")

    # Carátula — SIN TipoOperacion (exclusivo de LibroCV)
    car = etree.SubElement(envio, f"{{{NS}}}Caratula")
    etree.SubElement(car, f"{{{NS}}}RutEmisorLibro").text    = emisor_rut
    etree.SubElement(car, f"{{{NS}}}RutEnvia").text          = rut_envia
    etree.SubElement(car, f"{{{NS}}}PeriodoTributario").text = req.periodo
    etree.SubElement(car, f"{{{NS}}}FchResol").text          = req.fch_resol
    etree.SubElement(car, f"{{{NS}}}NroResol").text          = req.nro_resol
    etree.SubElement(car, f"{{{NS}}}TipoLibro").text         = "ESPECIAL"
    etree.SubElement(car, f"{{{NS}}}TipoEnvio").text         = "TOTAL"
    etree.SubElement(car, f"{{{NS}}}FolioNotificacion").text = req.natencion

    # ResumenPeriodo
    # Reglas verificadas contra el SII:
    #   - Guías VIGENTES: sin campo <Anulado> en Detalle → cuentan en TotGuiaVenta
    #   - Guías ANULADAS: <Anulado>2</Anulado> en Detalle → cuentan en TotGuiaAnulada
    #   - TotFolAnulado: solo cuando hay <Anulado>1</Anulado> (anulado PREVIO al envío)
    #                    se OMITE si no hay ninguno (minOccurs=0 en XSD)
    resumen = etree.SubElement(envio, f"{{{NS}}}ResumenPeriodo")
    # TotFolAnulado solo si hay guías con Anulado=1 — en este set no hay, se omite
    # etree.SubElement(resumen, f"{{{NS}}}TotFolAnulado").text = "0"
    etree.SubElement(resumen, f"{{{NS}}}TotGuiaAnulada").text = str(len(guias_anuld))
    etree.SubElement(resumen, f"{{{NS}}}TotGuiaVenta").text   = str(len(guias_venta))
    if tot_mnt_vta:
        etree.SubElement(resumen, f"{{{NS}}}TotMntGuiaVta").text = str(tot_mnt_vta)

    # Detalle — todas las guías
    for g in req.guias:
        det = etree.SubElement(envio, f"{{{NS}}}Detalle")
        etree.SubElement(det, f"{{{NS}}}Folio").text = str(g.folio)
        # Anulado SOLO si está anulada — vigentes NO llevan este campo
        if g.anulado:
            etree.SubElement(det, f"{{{NS}}}Anulado").text = "2"
        etree.SubElement(det, f"{{{NS}}}FchDoc").text = g.fecha
        if g.rut:
            etree.SubElement(det, f"{{{NS}}}RUTDoc").text = g.rut
        if g.razon:
            etree.SubElement(det, f"{{{NS}}}RznSoc").text = g.razon[:50]
        if g.neto:
            etree.SubElement(det, f"{{{NS}}}MntNeto").text = str(g.neto)
        if g.iva:
            etree.SubElement(det, f"{{{NS}}}TasaImp").text = "19"
            etree.SubElement(det, f"{{{NS}}}IVA").text     = str(g.iva)
        if g.exe:
            etree.SubElement(det, f"{{{NS}}}MntExe").text = str(g.exe)
        etree.SubElement(det, f"{{{NS}}}MntTotal").text = str(g.total)

    etree.SubElement(envio, f"{{{NS}}}TmstFirma").text = tmst

    raw = etree.tostring(root, encoding="ISO-8859-1", xml_declaration=True, pretty_print=True)
    return raw.decode("ISO-8859-1").replace(
        "<?xml version='1.0' encoding='ISO-8859-1'?>",
        '<?xml version="1.0" encoding="ISO-8859-1"?>',
    )


@router.post("/", summary="Genera Libro de Guías firmado")
async def generar_libro_guias(req: LibroGuiasRequest, db: AsyncSession = Depends(get_db)):
    emisor = await db.get(Emisor, req.emisor_id)
    if not emisor:
        raise HTTPException(404, f"Emisor {req.emisor_id} no encontrado")

    res = await db.execute(
        select(Certificado).where(
            Certificado.emisor_id == req.emisor_id,
            Certificado.activo == True,
        ).limit(1)
    )
    cert = res.scalar_one_or_none()
    if not cert or not cert.certificado_p12:
        raise HTTPException(400, "Sin certificado .p12 activo para este emisor")

    if not req.guias:
        raise HTTPException(400, "El libro debe tener al menos una guía")

    rut_envia = cert.rut_firmante or emisor.rut
    guias_anuld = [g for g in req.guias if g.anulado]
    guias_venta = [g for g in req.guias if not g.anulado]

    logger.info(
        f"[LIBRO GUIAS] emisor={emisor.rut} natencion={req.natencion} "
        f"total={len(req.guias)} vigentes={len(guias_venta)} anuladas={len(guias_anuld)}"
    )

    try:
        xml_str = _xml_libro_guias(emisor.rut, rut_envia, req)
    except Exception as e:
        logger.error(f"Error construyendo LibroGuias: {e}", exc_info=True)
        raise HTTPException(500, f"Error al construir el libro: {e}")

    firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    try:
        xml_firmado = await firma.firmar_libro(xml_str)
    except Exception as e:
        logger.error(f"Error firmando LibroGuias: {e}", exc_info=True)
        raise HTTPException(500, f"Error al firmar: {e}")

    rut_limpio = emisor.rut.replace(".", "").replace("-", "")
    nombre = f"LibroGuias_{req.natencion}_{rut_limpio}_{req.periodo}.xml"

    return Response(
        content=xml_firmado.encode("ISO-8859-1"),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )
