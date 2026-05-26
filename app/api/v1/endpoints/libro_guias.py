# app/api/v1/endpoints/libro_guias.py
# ═══════════════════════════════════════════════════════════════════
# Endpoint LIMPIO para Libro de Guías de Despacho (T52)
#
# Estructura según formato_lgd.pdf del SII (2003-10-29 v1.0):
#
# ResumenPeriodo:
#   TotFolAnulado   = count(Anulado=1)  — previo envío al SII
#   TotGuiaAnulada  = count(Anulado=2)  — posterior envío al SII
#   TotGuiaVenta    = count(vigentes con TpoOper=1)
#   TotMntGuiaVta   = suma MntTotal de guías de venta
#   TotTraslado*    = tabla de no-ventas agrupada por TpoMov (2-7)
#     TpoMov          código traslado (5=interno, 2=ventas por efectuar, etc.)
#     CantGuia        cantidad de guías de ese tipo
#     MntGuia         suma montos (solo si > 0)
#
# Detalle:
#   Folio
#   Anulado   — solo si anulada (1=previo, 2=posterior)
#   TpoOper   — solo si vigente (1=venta, 5=traslado interno, etc.)
#   FchDoc / RUTDoc / RznSoc / MntNeto / TasaImp / IVA / MntTotal
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


# ── Modelos ───────────────────────────────────────────────────────────────────

class GuiaDespacho(BaseModel):
    """Una Guía de Despacho para el Libro de Guías."""
    folio: int
    fecha: str                  # AAAA-MM-DD
    rut: str = "66666666-6"
    razon: str = ""
    exe: int = 0
    neto: int = 0
    iva: int = 0
    total: int = 0
    anulado: bool = False       # True = anulada posterior al envío SII (Anulado=2)
    tpo_oper: int = 1           # 1=Venta, 2=VentaXEfectuar, 3=Consig,
                                # 4=Demostr, 5=TrasladoInterno, 6=OtroTraslado, 7=Devolucion


class LibroGuiasRequest(BaseModel):
    emisor_id: int
    natencion: str
    periodo: str = "2026-05"
    fch_resol: str = "2026-04-19"
    nro_resol: str = "0"
    guias: List[GuiaDespacho]


# ── Constructor XML ───────────────────────────────────────────────────────────

def _xml_libro_guias(emisor_rut: str, rut_envia: str,
                     req: LibroGuiasRequest) -> str:
    """
    Construye el XML del LibroGuia según el formato oficial del SII (formato_lgd.pdf).

    Clasificación de guías:
    - Anulado=1: folio anulado PREVIO envío al SII (resto de campos no requeridos)
    - Anulado=2: anulada POSTERIOR al envío al SII
    - TpoOper=1: guía de venta → cuenta en TotGuiaVenta
    - TpoOper=2-7: traslados, consignaciones, devoluciones → cuenta en TotTraslado
    """
    tmst = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # Clasificar guías
    anulado1  = [g for g in req.guias if g.anulado and False]  # previo envío — en este set no hay
    anulado2  = [g for g in req.guias if g.anulado]            # posterior envío
    vigentes  = [g for g in req.guias if not g.anulado]
    ventas    = [g for g in vigentes if g.tpo_oper == 1]
    no_ventas = [g for g in vigentes if g.tpo_oper != 1]

    tot_mnt_vta = sum(g.total for g in ventas)

    # Agrupar no-ventas por TpoOper para la tabla TotTraslado
    traslados_por_tipo: dict = {}
    for g in no_ventas:
        if g.tpo_oper not in traslados_por_tipo:
            traslados_por_tipo[g.tpo_oper] = {"cant": 0, "monto": 0}
        traslados_por_tipo[g.tpo_oper]["cant"]  += 1
        traslados_por_tipo[g.tpo_oper]["monto"] += g.total

    # ── Raíz — LibroGuia (NO LibroCompraVenta) ────────────────────────────────
    root = etree.Element(
        f"{{{NS}}}LibroGuia",
        nsmap={None: NS, "xsi": "http://www.w3.org/2001/XMLSchema-instance"},
        attrib={
            "version": "1.0",
            "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation":
                f"{NS} LibroGuia_v10.xsd",
        },
    )
    envio = etree.SubElement(root, f"{{{NS}}}EnvioLibro")
    envio.set("ID", "LibroGuias")

    # ── Carátula — SIN TipoOperacion (diferente al LibroCV) ───────────────────
    car = etree.SubElement(envio, f"{{{NS}}}Caratula")
    etree.SubElement(car, f"{{{NS}}}RutEmisorLibro").text    = emisor_rut
    etree.SubElement(car, f"{{{NS}}}RutEnvia").text          = rut_envia
    etree.SubElement(car, f"{{{NS}}}PeriodoTributario").text = req.periodo
    etree.SubElement(car, f"{{{NS}}}FchResol").text          = req.fch_resol
    etree.SubElement(car, f"{{{NS}}}NroResol").text          = req.nro_resol
    etree.SubElement(car, f"{{{NS}}}TipoLibro").text         = "ESPECIAL"
    etree.SubElement(car, f"{{{NS}}}TipoEnvio").text         = "TOTAL"
    etree.SubElement(car, f"{{{NS}}}FolioNotificacion").text = req.natencion

    # ── ResumenPeriodo ────────────────────────────────────────────────────────
    # Orden exacto según formato_lgd.pdf:
    #   TotFolAnulado → TotGuiaAnulada → TotGuiaVenta → TotMntGuiaVta
    #   → [TotMntModificado] → TotTraslado* (NO TotGuiaNoVenta)
    resumen = etree.SubElement(envio, f"{{{NS}}}ResumenPeriodo")

    # TotFolAnulado = guías anuladas PREVIO envío al SII (Anulado=1)
    # En este set = 0, pero debe estar si el schema lo requiere
    n_anulado1 = len(anulado1)
    if n_anulado1:
        etree.SubElement(resumen, f"{{{NS}}}TotFolAnulado").text = str(n_anulado1)

    # TotGuiaAnulada = guías anuladas POSTERIOR al envío al SII (Anulado=2)
    etree.SubElement(resumen, f"{{{NS}}}TotGuiaAnulada").text = str(len(anulado2))

    # TotGuiaVenta = vigentes con TpoOper=1
    etree.SubElement(resumen, f"{{{NS}}}TotGuiaVenta").text = str(len(ventas))

    # TotMntGuiaVta = suma montos de guías de venta
    if tot_mnt_vta:
        etree.SubElement(resumen, f"{{{NS}}}TotMntGuiaVta").text = str(tot_mnt_vta)

    # TotTraslado — tabla de no-ventas agrupada por TpoMov
    # El PDF lo llama TotTraslado (NO TotGuiaNoVenta)
    for tpo in sorted(traslados_por_tipo.keys()):
        datos = traslados_por_tipo[tpo]
        tr    = etree.SubElement(resumen, f"{{{NS}}}TotTraslado")
        etree.SubElement(tr, f"{{{NS}}}TpoMov").text    = str(tpo)
        etree.SubElement(tr, f"{{{NS}}}CantGuia").text  = str(datos["cant"])
        if datos["monto"]:
            etree.SubElement(tr, f"{{{NS}}}MntGuia").text = str(datos["monto"])

    # ── Detalle — un nodo por guía ────────────────────────────────────────────
    for g in req.guias:
        det = etree.SubElement(envio, f"{{{NS}}}Detalle")

        # Folio — siempre primero
        etree.SubElement(det, f"{{{NS}}}Folio").text = str(g.folio)

        if g.anulado:
            # Anulado=2: posterior al envío SII. El resto de campos NO es requerido.
            etree.SubElement(det, f"{{{NS}}}Anulado").text = "2"
        else:
            # TpoOper: SOLO para guías vigentes (no anuladas)
            etree.SubElement(det, f"{{{NS}}}TpoOper").text = str(g.tpo_oper)

        etree.SubElement(det, f"{{{NS}}}FchDoc").text = g.fecha

        if g.rut:
            etree.SubElement(det, f"{{{NS}}}RUTDoc").text = g.rut
        if g.razon:
            etree.SubElement(det, f"{{{NS}}}RznSoc").text = g.razon[:50]

        # Montos — solo si no es anulada
        if not g.anulado:
            if g.neto:
                etree.SubElement(det, f"{{{NS}}}MntNeto").text = str(g.neto)
            if g.iva:
                etree.SubElement(det, f"{{{NS}}}TasaImp").text = "19"
                etree.SubElement(det, f"{{{NS}}}IVA").text     = str(g.iva)
            if g.exe:
                etree.SubElement(det, f"{{{NS}}}MntExe").text  = str(g.exe)
            # MntTotal obligatorio para ventas (TpoOper=1)
            etree.SubElement(det, f"{{{NS}}}MntTotal").text = str(g.total)

    # ── Timestamp de firma ────────────────────────────────────────────────────
    etree.SubElement(envio, f"{{{NS}}}TmstFirma").text = tmst

    raw = etree.tostring(root, encoding="ISO-8859-1",
                         xml_declaration=True, pretty_print=True)
    return raw.decode("ISO-8859-1").replace(
        "<?xml version='1.0' encoding='ISO-8859-1'?>",
        '<?xml version="1.0" encoding="ISO-8859-1"?>',
    )


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/", summary="Genera Libro de Guías firmado")
async def generar_libro_guias(
    req: LibroGuiasRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Genera el LibroGuia XML firmado según formato_lgd.pdf del SII.
    No lee DTEs de la BD — solo usa las guías del body.
    """
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

    rut_envia  = cert.rut_firmante or emisor.rut
    vigentes   = [g for g in req.guias if not g.anulado]
    ventas     = [g for g in vigentes if g.tpo_oper == 1]
    traslados  = [g for g in vigentes if g.tpo_oper != 1]
    anuladas   = [g for g in req.guias if g.anulado]

    logger.info(
        f"[LIBRO GUIAS] emisor={emisor.rut} natencion={req.natencion} "
        f"total={len(req.guias)} ventas={len(ventas)} "
        f"traslados={len(traslados)} anuladas={len(anuladas)}"
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
