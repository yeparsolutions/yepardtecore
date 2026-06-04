# app/api/v1/endpoints/libro_compras.py
# ═══════════════════════════════════════════════════════════════════
# Endpoint LIMPIO para Libro de Compras
#
# POST /v1/libro-compras
# Body: { emisor_id, natencion, periodo, documentos: [...] }
#
# Orden correcto en ResumenPeriodo (LibroCV_v10.xsd):
#   TotMntExe → TotMntNeto → TotMntIVA → TotIVANoRec → TotIVAUsoComun
#   → FctProp → TotCredIVAUsoComun → TotOpIVARetTotal → TotIVARetTotal
#   → TotMntTotal
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

logger = logging.getLogger("yepardtecore.libro_compras")
router = APIRouter(prefix="/libro-compras", tags=["Libro Compras"])

NS = "http://www.sii.cl/SiiDte"


# ── Modelos ───────────────────────────────────────────────────────────────────

class DocumentoCompra(BaseModel):
    tipo: int
    folio: int
    fecha: str
    rut: str = "66666666-6"
    razon: str = ""
    exe: int = 0
    neto: int = 0
    iva: int = 0
    tipo_especial: str = ""
    iva_uso_comun: int = 0
    fct_prop: str = "0.60"
    iva_no_rec: int = 0
    cod_iva_no_rec: int = 9
    iva_ret_total: int = 0
    total: int = 0


class LibroComprasRequest(BaseModel):
    emisor_id: int
    natencion: str
    periodo: str = "2026-05"
    fch_resol: str = "2026-04-19"
    nro_resol: str = "0"
    tipo_libro: str = "ESPECIAL"
    tipo_envio: str = "TOTAL"
    cod_aut_rec: str = ""
    documentos: List[DocumentoCompra]
    totales_periodo: list = []


# ── Constructor XML ───────────────────────────────────────────────────────────

def _xml_libro_compras(emisor_rut: str, rut_envia: str,
                       req: LibroComprasRequest) -> str:
    tmst = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    docs = [d.model_dump() for d in req.documentos]

    root = etree.Element(
        f"{{{NS}}}LibroCompraVenta",
        nsmap={None: NS, "xsi": "http://www.w3.org/2001/XMLSchema-instance"},
        attrib={
            "version": "1.0",
            "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation":
                f"{NS} LibroCV_v10.xsd",
        },
    )
    envio = etree.SubElement(root, f"{{{NS}}}EnvioLibro")
    envio.set("ID", "LibroCompras")

    # ── Carátula ─────────────────────────────────────────────────────────────
    car = etree.SubElement(envio, f"{{{NS}}}Caratula")
    etree.SubElement(car, f"{{{NS}}}RutEmisorLibro").text    = emisor_rut
    etree.SubElement(car, f"{{{NS}}}RutEnvia").text          = rut_envia
    etree.SubElement(car, f"{{{NS}}}PeriodoTributario").text = req.periodo
    etree.SubElement(car, f"{{{NS}}}FchResol").text          = req.fch_resol
    etree.SubElement(car, f"{{{NS}}}NroResol").text          = req.nro_resol
    etree.SubElement(car, f"{{{NS}}}TipoOperacion").text     = "COMPRA"
    etree.SubElement(car, f"{{{NS}}}TipoLibro").text         = req.tipo_libro
    etree.SubElement(car, f"{{{NS}}}TipoEnvio").text         = req.tipo_envio
    etree.SubElement(car, f"{{{NS}}}FolioNotificacion").text = req.natencion
    if req.cod_aut_rec:
        etree.SubElement(car, f"{{{NS}}}CodAutRec").text     = req.cod_aut_rec

    # ── ResumenSegmento — obligatorio en AJUSTE ──────────────────────────────
    if req.tipo_envio == "AJUSTE":
        resumen_seg = etree.SubElement(envio, f"{{{NS}}}ResumenSegmento")
        for tipo_doc in sorted(set(d["tipo"] for d in docs)):
            grp = [d for d in docs if d["tipo"] == tipo_doc]
            tot = etree.SubElement(resumen_seg, f"{{{NS}}}TotalesSegmento")
            etree.SubElement(tot, f"{{{NS}}}TpoDoc").text     = str(tipo_doc)
            etree.SubElement(tot, f"{{{NS}}}TotDoc").text     = str(len(grp))
            etree.SubElement(tot, f"{{{NS}}}TotMntExe").text  = str(sum(d["exe"]  for d in grp))
            etree.SubElement(tot, f"{{{NS}}}TotMntNeto").text = str(sum(d["neto"] for d in grp))
            # Para iva_ret_total: TotMntIVA=0 (no es crédito fiscal)
            tot_iva_grp = sum(d["iva"] for d in grp if d.get("tipo_especial") != "iva_ret_total")
            etree.SubElement(tot, f"{{{NS}}}TotMntIVA").text  = str(tot_iva_grp)
            t_nr = sum(d["iva_no_rec"] for d in grp)
            if t_nr:
                cod = next(d["cod_iva_no_rec"] for d in grp if d["iva_no_rec"])
                inr = etree.SubElement(tot, f"{{{NS}}}TotIVANoRec")
                etree.SubElement(inr, f"{{{NS}}}CodIVANoRec").text    = str(cod)
                etree.SubElement(inr, f"{{{NS}}}TotOpIVANoRec").text  = str(sum(1 for d in grp if d["iva_no_rec"]))
                etree.SubElement(inr, f"{{{NS}}}TotMntIVANoRec").text = str(t_nr)
            t_uc = sum(d["iva_uso_comun"] for d in grp)
            if t_uc:
                etree.SubElement(tot, f"{{{NS}}}TotOpIVAUsoComun").text = str(sum(1 for d in grp if d["iva_uso_comun"]))
                etree.SubElement(tot, f"{{{NS}}}TotIVAUsoComun").text   = str(t_uc)
            t_rt = sum(d["iva_ret_total"] for d in grp)
            if t_rt:
                etree.SubElement(tot, f"{{{NS}}}TotOpIVARetTotal").text = str(sum(1 for d in grp if d["iva_ret_total"]))
                etree.SubElement(tot, f"{{{NS}}}TotIVARetTotal").text   = str(t_rt)
            etree.SubElement(tot, f"{{{NS}}}TotMntTotal").text = str(sum(d["total"] for d in grp))

    # ── ResumenPeriodo ────────────────────────────────────────────────────────
    resumen = etree.SubElement(envio, f"{{{NS}}}ResumenPeriodo")

    from collections import defaultdict
    neto_por_tipo = defaultdict(lambda: {'count':0,'neto':0,'exe':0,'iva':0,'total':0,
                                          'iva_nr':0,'iva_uc':0,'iva_ret':0})
    for d in docs:
        t = d["tipo"]
        neto_por_tipo[t]['count'] += 1
        neto_por_tipo[t]['neto']  += d["neto"]
        neto_por_tipo[t]['exe']   += d["exe"]
        # Para iva_ret_total: el IVA no es crédito fiscal → TotMntIVA = 0
        neto_por_tipo[t]['iva']   += d["iva"] if d.get("tipo_especial") != "iva_ret_total" else 0
        # Para iva_ret_total: total = neto (el IVA retenido no se paga al proveedor)
        neto_por_tipo[t]['total'] += d["neto"] if d.get("tipo_especial") == "iva_ret_total" else d["total"]
        neto_por_tipo[t]['iva_nr'] += d["iva_no_rec"]
        neto_por_tipo[t]['iva_uc'] += d["iva_uso_comun"]
        neto_por_tipo[t]['iva_ret'] += d["iva_ret_total"]

    # Si se pasan totales_periodo explícitos, usarlos directamente
    if req.totales_periodo:
        for tp in req.totales_periodo:
            tot = etree.SubElement(resumen, f"{{{NS}}}TotalesPeriodo")
            etree.SubElement(tot, f"{{{NS}}}TpoDoc").text     = str(tp.get("tipo"))
            etree.SubElement(tot, f"{{{NS}}}TotDoc").text     = str(tp.get("tot_doc", 0))
            etree.SubElement(tot, f"{{{NS}}}TotMntExe").text  = str(tp.get("tot_exe", 0))
            etree.SubElement(tot, f"{{{NS}}}TotMntNeto").text = str(tp.get("tot_neto", 0))
            etree.SubElement(tot, f"{{{NS}}}TotMntIVA").text  = str(tp.get("tot_iva", 0))
            t_nr = tp.get("iva_no_rec", 0)
            if t_nr:
                inr = etree.SubElement(tot, f"{{{NS}}}TotIVANoRec")
                etree.SubElement(inr, f"{{{NS}}}CodIVANoRec").text    = str(tp.get("cod_iva_no_rec", 9))
                etree.SubElement(inr, f"{{{NS}}}TotOpIVANoRec").text  = str(tp.get("tot_doc", 1))
                etree.SubElement(inr, f"{{{NS}}}TotMntIVANoRec").text = str(t_nr)
            t_uc = tp.get("iva_uso_comun", 0)
            if t_uc:
                fct = tp.get("fct_prop", "0.60")
                etree.SubElement(tot, f"{{{NS}}}TotIVAUsoComun").text    = str(t_uc)
                etree.SubElement(tot, f"{{{NS}}}FctProp").text            = fct
                etree.SubElement(tot, f"{{{NS}}}TotCredIVAUsoComun").text = str(round(t_uc * float(fct)))
            t_rt = tp.get("iva_ret_total", 0)
            if t_rt:
                etree.SubElement(tot, f"{{{NS}}}TotOpIVARetTotal").text = str(tp.get("tot_doc", 1))
                etree.SubElement(tot, f"{{{NS}}}TotIVARetTotal").text   = str(t_rt)
            etree.SubElement(tot, f"{{{NS}}}TotMntTotal").text = str(tp.get("tot_total", 0))
        tipos_periodo = []
    else:
        tipos_periodo = sorted(neto_por_tipo.keys())

    for tipo_doc in tipos_periodo:
        v = neto_por_tipo[tipo_doc]
        tot_doc   = v['count']
        tot_exe   = v['exe']
        tot_neto  = v['neto']
        tot_iva   = v['iva']   # ya excluye iva_ret_total
        tot_total = v['total']
        t_nr      = v['iva_nr']
        t_uc      = v['iva_uc']
        t_rt      = v['iva_ret']

        grp_nr = [d for d in docs if d["tipo"] == tipo_doc and d["iva_no_rec"]]
        cod_nr = grp_nr[0]["cod_iva_no_rec"] if grp_nr else 9
        grp_uc = [d for d in docs if d["tipo"] == tipo_doc and d["iva_uso_comun"]]
        fct_uc = grp_uc[0]["fct_prop"] if grp_uc else "0.60"

        tot = etree.SubElement(resumen, f"{{{NS}}}TotalesPeriodo")
        etree.SubElement(tot, f"{{{NS}}}TpoDoc").text     = str(tipo_doc)
        etree.SubElement(tot, f"{{{NS}}}TotDoc").text     = str(tot_doc)
        etree.SubElement(tot, f"{{{NS}}}TotMntExe").text  = str(tot_exe)
        etree.SubElement(tot, f"{{{NS}}}TotMntNeto").text = str(tot_neto)
        etree.SubElement(tot, f"{{{NS}}}TotMntIVA").text  = str(tot_iva)

        if t_nr:
            inr = etree.SubElement(tot, f"{{{NS}}}TotIVANoRec")
            etree.SubElement(inr, f"{{{NS}}}CodIVANoRec").text    = str(cod_nr)
            etree.SubElement(inr, f"{{{NS}}}TotOpIVANoRec").text  = str(sum(1 for d in docs if d["tipo"] == tipo_doc and d["iva_no_rec"]))
            etree.SubElement(inr, f"{{{NS}}}TotMntIVANoRec").text = str(t_nr)

        if t_uc:
            etree.SubElement(tot, f"{{{NS}}}TotIVAUsoComun").text    = str(t_uc)
            etree.SubElement(tot, f"{{{NS}}}FctProp").text            = fct_uc
            etree.SubElement(tot, f"{{{NS}}}TotCredIVAUsoComun").text = str(round(t_uc * float(fct_uc)))

        if t_rt:
            etree.SubElement(tot, f"{{{NS}}}TotOpIVARetTotal").text = str(sum(1 for d in docs if d["tipo"] == tipo_doc and d["iva_ret_total"]))
            etree.SubElement(tot, f"{{{NS}}}TotIVARetTotal").text   = str(t_rt)

        etree.SubElement(tot, f"{{{NS}}}TotMntTotal").text = str(tot_total)

    # ── Detalle ───────────────────────────────────────────────────────────────
    for doc in docs:
        det = etree.SubElement(envio, f"{{{NS}}}Detalle")
        etree.SubElement(det, f"{{{NS}}}TpoDoc").text  = str(doc["tipo"])
        etree.SubElement(det, f"{{{NS}}}NroDoc").text  = str(doc["folio"])
        etree.SubElement(det, f"{{{NS}}}TasaImp").text = "19"
        etree.SubElement(det, f"{{{NS}}}FchDoc").text  = doc["fecha"]
        etree.SubElement(det, f"{{{NS}}}RUTDoc").text  = doc["rut"]
        if doc["razon"]:
            etree.SubElement(det, f"{{{NS}}}RznSoc").text = doc["razon"][:50]
        if doc["exe"]:
            etree.SubElement(det, f"{{{NS}}}MntExe").text = str(doc["exe"])
        etree.SubElement(det, f"{{{NS}}}MntNeto").text = str(doc["neto"])

        te = doc["tipo_especial"]
        if te == "iva_uso_comun":
            etree.SubElement(det, f"{{{NS}}}MntIVA").text      = "0"
            etree.SubElement(det, f"{{{NS}}}IVAUsoComun").text = str(doc["iva_uso_comun"])
        elif te == "iva_no_rec":
            etree.SubElement(det, f"{{{NS}}}MntIVA").text = "0"
            inr = etree.SubElement(det, f"{{{NS}}}IVANoRec")
            etree.SubElement(inr, f"{{{NS}}}CodIVANoRec").text = str(doc["cod_iva_no_rec"])
            etree.SubElement(inr, f"{{{NS}}}MntIVANoRec").text = str(doc["iva_no_rec"])
        elif te == "iva_ret_total":
            # MntIVA = IVA normal del doc; IVARetTotal informa la retención
            etree.SubElement(det, f"{{{NS}}}MntIVA").text      = str(doc["iva"])
            etree.SubElement(det, f"{{{NS}}}IVARetTotal").text = str(doc["iva_ret_total"])
        else:
            etree.SubElement(det, f"{{{NS}}}MntIVA").text = str(doc["iva"])

        etree.SubElement(det, f"{{{NS}}}MntTotal").text = str(doc["total"])

    etree.SubElement(envio, f"{{{NS}}}TmstFirma").text = tmst

    raw = etree.tostring(root, encoding="ISO-8859-1",
                         xml_declaration=True, pretty_print=True)
    return raw.decode("ISO-8859-1").replace(
        "<?xml version='1.0' encoding='ISO-8859-1'?>",
        '<?xml version="1.0" encoding="ISO-8859-1"?>',
    )


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/", summary="Genera Libro de Compras firmado")
async def generar_libro_compras(
    req: LibroComprasRequest,
    db: AsyncSession = Depends(get_db),
):
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

    rut_envia = cert.rut_firmante or emisor.rut

    if not req.documentos:
        raise HTTPException(400, "El libro debe tener al menos un documento")

    logger.info(
        f"[LIBRO COMPRAS] emisor={emisor.rut} natencion={req.natencion} "
        f"docs={len(req.documentos)} tipos={sorted(set(d.tipo for d in req.documentos))}"
    )

    try:
        xml_str = _xml_libro_compras(emisor.rut, rut_envia, req)
    except Exception as e:
        logger.error(f"Error construyendo LibroCompras: {e}", exc_info=True)
        raise HTTPException(500, f"Error al construir el libro: {e}")

    firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    try:
        xml_firmado = await firma.firmar_libro(xml_str)
    except Exception as e:
        logger.error(f"Error firmando LibroCompras: {e}", exc_info=True)
        raise HTTPException(500, f"Error al firmar: {e}")

    rut_limpio = emisor.rut.replace(".", "").replace("-", "")
    nombre = f"LibroCompras_{req.natencion}_{rut_limpio}_{req.periodo}.xml"

    return Response(
        content=xml_firmado.encode("ISO-8859-1"),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )
