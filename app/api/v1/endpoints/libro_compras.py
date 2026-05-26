# app/api/v1/endpoints/libro_compras.py
# ═══════════════════════════════════════════════════════════════════
# Endpoint LIMPIO para Libro de Compras
#
# Recibe exactamente los documentos que el usuario envía — no lee
# la BD ni filtra por tipo. Lo que entra es lo que se incluye.
#
# POST /v1/libro-compras
# Body: { emisor_id, natencion, periodo, documentos: [...] }
# ═══════════════════════════════════════════════════════════════════

import logging
from datetime import datetime
from typing import List, Optional

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


# ── Modelos de entrada ────────────────────────────────────────────────────────

class DocumentoCompra(BaseModel):
    """Un documento que entra al Libro de Compras."""
    tipo: int                   # Tipo DTE (30, 33, 46, 61, etc.)
    folio: int
    fecha: str                  # AAAA-MM-DD
    rut: str = "66666666-6"
    razon: str = ""
    exe: int = 0                # Monto exento
    neto: int = 0               # Monto afecto
    iva: int = 0                # MntIVA base (0 si usa campo especial)
    tipo_especial: str = ""     # "" | "iva_uso_comun" | "iva_no_rec" | "iva_ret_total"
    iva_uso_comun: int = 0      # IVA para facturas con destino parcial exento
    fct_prop: str = "0.60"      # Factor de proporcionalidad (usado si iva_uso_comun > 0)
    iva_no_rec: int = 0         # IVA no recuperable (art. 23 N°5 DL825)
    cod_iva_no_rec: int = 9     # Código IVA no rec (1-4, 9)
    iva_ret_total: int = 0      # IVA retenido total (Factura de Compra T46)
    total: int = 0


class LibroComprasRequest(BaseModel):
    emisor_id: int
    natencion: str
    periodo: str = "2026-05"
    fch_resol: str = "2026-04-19"
    nro_resol: str = "0"
    tipo_libro: str = "ESPECIAL"   # "ESPECIAL" | "RECTIFICA"
    cod_aut_rec: str = ""          # Código de autorización (requerido solo para RECTIFICA)
    documentos: List[DocumentoCompra]


# ── Constructor XML ───────────────────────────────────────────────────────────

def _xml_libro_compras(emisor_rut: str, rut_envia: str,
                       req: LibroComprasRequest) -> str:
    """
    Construye el XML del LibroCompraVenta para COMPRAS.

    Analogía: es como armar una caja de envío postal —
    primero la etiqueta (Caratula), luego el resumen
    del contenido (ResumenPeriodo) y finalmente los
    artículos uno a uno (Detalle).
    """
    tmst = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    docs = [d.model_dump() for d in req.documentos]

    # ── Raíz ─────────────────────────────────────────────────────────────────
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
    etree.SubElement(car, f"{{{NS}}}TipoEnvio").text         = "TOTAL"
    etree.SubElement(car, f"{{{NS}}}FolioNotificacion").text = req.natencion
    # CodAutRec — solo requerido cuando TipoLibro = "RECTIFICA"
    if req.tipo_libro == "RECTIFICA" and req.cod_aut_rec:
        etree.SubElement(car, f"{{{NS}}}CodAutRec").text = req.cod_aut_rec

    # ── ResumenPeriodo — un TotalesPeriodo por tipo de documento ─────────────
    resumen = etree.SubElement(envio, f"{{{NS}}}ResumenPeriodo")

    for tipo_doc in sorted(set(d["tipo"] for d in docs)):
        grp = [d for d in docs if d["tipo"] == tipo_doc]
        tot = etree.SubElement(resumen, f"{{{NS}}}TotalesPeriodo")

        etree.SubElement(tot, f"{{{NS}}}TpoDoc").text      = str(tipo_doc)
        etree.SubElement(tot, f"{{{NS}}}TotDoc").text      = str(len(grp))
        etree.SubElement(tot, f"{{{NS}}}TotMntExe").text   = str(sum(d["exe"]  for d in grp))
        etree.SubElement(tot, f"{{{NS}}}TotMntNeto").text  = str(sum(d["neto"] for d in grp))
        etree.SubElement(tot, f"{{{NS}}}TotMntIVA").text   = str(sum(d["iva"]  for d in grp))

        # IVA Uso Común
        t_uc = sum(d["iva_uso_comun"] for d in grp)
        if t_uc:
            fct = grp[0]["fct_prop"]
            etree.SubElement(tot, f"{{{NS}}}TotIVAUsoComun").text    = str(t_uc)
            etree.SubElement(tot, f"{{{NS}}}FctProp").text            = fct
            etree.SubElement(tot, f"{{{NS}}}TotCredIVAUsoComun").text = str(round(t_uc * float(fct)))

        # IVA No Recuperable
        t_nr = sum(d["iva_no_rec"] for d in grp)
        if t_nr:
            cod = next(d["cod_iva_no_rec"] for d in grp if d["iva_no_rec"])
            inr = etree.SubElement(tot, f"{{{NS}}}TotIVANoRec")
            etree.SubElement(inr, f"{{{NS}}}CodIVANoRec").text    = str(cod)
            etree.SubElement(inr, f"{{{NS}}}TotOpIVANoRec").text  = str(sum(1 for d in grp if d["iva_no_rec"]))
            etree.SubElement(inr, f"{{{NS}}}TotMntIVANoRec").text = str(t_nr)

        # IVA Retención Total
        t_rt = sum(d["iva_ret_total"] for d in grp)
        if t_rt:
            etree.SubElement(tot, f"{{{NS}}}TotOpIVARetTotal").text = str(sum(1 for d in grp if d["iva_ret_total"]))
            etree.SubElement(tot, f"{{{NS}}}TotIVARetTotal").text   = str(t_rt)

        etree.SubElement(tot, f"{{{NS}}}TotMntTotal").text = str(sum(d["total"] for d in grp))

    # ── Detalle — un nodo por documento ──────────────────────────────────────
    for doc in docs:
        det = etree.SubElement(envio, f"{{{NS}}}Detalle")

        etree.SubElement(det, f"{{{NS}}}TpoDoc").text  = str(doc["tipo"])
        etree.SubElement(det, f"{{{NS}}}NroDoc").text  = str(doc["folio"])
        etree.SubElement(det, f"{{{NS}}}TasaImp").text = "19"
        etree.SubElement(det, f"{{{NS}}}FchDoc").text  = doc["fecha"]
        etree.SubElement(det, f"{{{NS}}}RUTDoc").text  = doc["rut"]
        if doc["razon"]:
            etree.SubElement(det, f"{{{NS}}}RznSoc").text = doc["razon"][:50]

        # Monto exento — solo si existe
        if doc["exe"]:
            etree.SubElement(det, f"{{{NS}}}MntExe").text = str(doc["exe"])

        # Monto neto (afecto)
        etree.SubElement(det, f"{{{NS}}}MntNeto").text = str(doc["neto"])

        # IVA según tipo especial
        te = doc["tipo_especial"]

        if te == "iva_uso_comun":
            # IVA va en IVAUsoComun, MntIVA = 0
            etree.SubElement(det, f"{{{NS}}}MntIVA").text      = "0"
            etree.SubElement(det, f"{{{NS}}}IVAUsoComun").text = str(doc["iva_uso_comun"])

        elif te == "iva_no_rec":
            # IVA va en IVANoRec, MntIVA = 0
            etree.SubElement(det, f"{{{NS}}}MntIVA").text = "0"
            inr = etree.SubElement(det, f"{{{NS}}}IVANoRec")
            etree.SubElement(inr, f"{{{NS}}}CodIVANoRec").text = str(doc["cod_iva_no_rec"])
            etree.SubElement(inr, f"{{{NS}}}MntIVANoRec").text = str(doc["iva_no_rec"])

        elif te == "iva_ret_total":
            # MntIVA contiene el IVA del doc, IVARetTotal lo repite (retenido)
            etree.SubElement(det, f"{{{NS}}}MntIVA").text      = str(doc["iva"])
            etree.SubElement(det, f"{{{NS}}}IVARetTotal").text = str(doc["iva_ret_total"])

        else:
            # Normal: MntIVA siempre presente (aunque sea 0)
            # El SII rechaza el Detalle si falta MntIVA: "Falta [MntIVA MntIVANoRec IVAUsoComun]"
            etree.SubElement(det, f"{{{NS}}}MntIVA").text = str(doc["iva"])

        etree.SubElement(det, f"{{{NS}}}MntTotal").text = str(doc["total"])

    # ── Timestamp de firma ────────────────────────────────────────────────────
    etree.SubElement(envio, f"{{{NS}}}TmstFirma").text = tmst

    # Serializar en ISO-8859-1 con comillas dobles (requisito SII)
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
    """
    Recibe la lista de documentos explícitamente y genera el XML firmado.
    No lee DTEs de la BD — lo que viene en 'documentos' es exactamente
    lo que entra al libro.
    """
    # Cargar emisor y certificado activo
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

    # Validar que tenga al menos un documento
    if not req.documentos:
        raise HTTPException(400, "El libro debe tener al menos un documento")

    logger.info(
        f"[LIBRO COMPRAS] emisor={emisor.rut} natencion={req.natencion} "
        f"docs={len(req.documentos)} tipos={sorted(set(d.tipo for d in req.documentos))}"
    )

    # Construir XML
    try:
        xml_str = _xml_libro_compras(emisor.rut, rut_envia, req)
    except Exception as e:
        logger.error(f"Error construyendo LibroCompras: {e}", exc_info=True)
        raise HTTPException(500, f"Error al construir el libro: {e}")

    # Firmar
    firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    try:
        xml_firmado = await firma.firmar_libro(xml_str)
    except Exception as e:
        logger.error(f"Error firmando LibroCompras: {e}", exc_info=True)
        raise HTTPException(500, f"Error al firmar: {e}")

    # Nombre del archivo
    rut_limpio = emisor.rut.replace(".", "").replace("-", "")
    nombre = f"LibroCompras_{req.natencion}_{rut_limpio}_{req.periodo}.xml"

    return Response(
        content=xml_firmado.encode("ISO-8859-1"),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )
