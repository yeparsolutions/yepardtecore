# app/api/v1/endpoints/certificacion_libro_compras_dinamico.py
# ══════════════════════════════════════════════════════════════
# Libro de Compras DINÁMICO — funciona para cualquier emisor/usuario.
#
# Flujo:
#   1. POST /certificacion-libro-compras-din/set
#      → El usuario carga los documentos del set desde el .txt del SII
#   2. POST /certificacion-libro-compras-din/generar-xml/{set_id}
#      → Genera el LibroCompras firmado con exactamente esos documentos
#
# Sin hardcode. Sin depender de DTEs emitidos en BD.
# ══════════════════════════════════════════════════════════════

import logging
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from lxml import etree

from app.db.base import get_db
from app.models.emisor import Emisor
from app.models.certificado import Certificado
from app.models.libro_compras_set import SetLibroCompras, ItemSetLibroCompras
from app.services.firma_digital import FirmaDigital

logger = logging.getLogger("yepardtecore.cert_libro_compras_din")
router = APIRouter(
    prefix="/certificacion-libro-compras-din",
    tags=["Certificacion Libro Compras Dinamico"],
)

NS = "http://www.sii.cl/SiiDte"


# ── Schemas ───────────────────────────────────────────────────

class ItemSetInput(BaseModel):
    tipo_dte:       int   = Field(..., description="30=Factura, 33=FactElec, 46=FactCompra, 55=NDFísica, 60=NCFísica, 61=NCElec")
    folio:          int
    fecha_doc:      str   = Field(..., description="AAAA-MM-DD")
    rut_doc:        str   = Field(..., description="RUT del proveedor, ej: 76354771-K")
    razon_doc:      str   = Field(..., description="Razón social del proveedor")
    monto_neto:     float = 0.0
    monto_exe:      float = 0.0
    monto_iva:      float = 0.0
    monto_total:    float
    # Tipo especial IVA — crítico para LibroCompras
    # ""             → IVA normal con crédito fiscal
    # "iva_uso_comun"→ IVA proporcional (factura con IVA uso común)
    # "iva_no_rec"   → IVA no recuperable art.23 N°5 (entrega gratuita, etc.)
    # "iva_ret_total"→ IVA retenido total (Doc 46, Factura de Compra)
    tipo_especial:  str   = ""
    iva_uso_comun:  float = 0.0
    iva_no_rec:     float = 0.0
    cod_iva_no_rec: int   = 9
    iva_ret_total:  float = 0.0

    class Config:
        json_schema_extra = {
            "examples": [
                {
                    "tipo_dte": 33, "folio": 67, "fecha_doc": "2026-05-22",
                    "rut_doc": "76354771-K", "razon_doc": "PROVEEDOR SA",
                    "monto_neto": 10913, "monto_exe": 0, "monto_iva": 0,
                    "monto_total": 12986,
                    "tipo_especial": "iva_no_rec", "iva_no_rec": 2073, "cod_iva_no_rec": 9,
                },
                {
                    "tipo_dte": 46, "folio": 9, "fecha_doc": "2026-05-22",
                    "rut_doc": "76354771-K", "razon_doc": "PROVEEDOR SA",
                    "monto_neto": 10019, "monto_exe": 0, "monto_iva": 1904,
                    "monto_total": 11923,
                    "tipo_especial": "iva_ret_total", "iva_ret_total": 1904,
                },
            ]
        }


class CrearSetRequest(BaseModel):
    emisor_id:  int
    natencion:  str   = Field(..., description="N° de atención del SII, ej: 4841545")
    periodo:    str   = Field(..., description="Período tributario, ej: 2026-05")
    fch_resol:  str   = "2026-04-19"
    nro_resol:  str   = "0"
    fct_prop:   str   = Field("0.60", description="Factor proporcionalidad IVA uso común")
    documentos: List[ItemSetInput]


# ── Helpers ───────────────────────────────────────────────────

async def _get_emisor_y_cert(emisor_id: int, db: AsyncSession):
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(404, f"Emisor {emisor_id} no encontrado")
    res = await db.execute(
        select(Certificado).where(
            Certificado.emisor_id == emisor_id,
            Certificado.activo == True,
        ).limit(1)
    )
    cert = res.scalar_one_or_none()
    if not cert or not cert.certificado_p12:
        raise HTTPException(400, "Sin certificado .p12 cargado")
    return emisor, cert


def _construir_xml(
    emisor: Emisor,
    rut_envia: str,
    set_data: SetLibroCompras,
    items: list[ItemSetLibroCompras],
) -> str:
    tmst = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

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

    # ── Carátula ─────────────────────────────────────────────
    car = etree.SubElement(envio, f"{{{NS}}}Caratula")
    etree.SubElement(car, f"{{{NS}}}RutEmisorLibro").text   = emisor.rut
    etree.SubElement(car, f"{{{NS}}}RutEnvia").text          = rut_envia
    etree.SubElement(car, f"{{{NS}}}PeriodoTributario").text = set_data.periodo
    etree.SubElement(car, f"{{{NS}}}FchResol").text          = set_data.fch_resol
    etree.SubElement(car, f"{{{NS}}}NroResol").text          = set_data.nro_resol
    etree.SubElement(car, f"{{{NS}}}TipoOperacion").text     = "COMPRA"
    etree.SubElement(car, f"{{{NS}}}TipoLibro").text         = "ESPECIAL"
    etree.SubElement(car, f"{{{NS}}}TipoEnvio").text         = "TOTAL"
    etree.SubElement(car, f"{{{NS}}}FolioNotificacion").text = set_data.natencion

    fct = float(set_data.fct_prop or "0.60")

    # ── ResumenPeriodo ────────────────────────────────────────
    resumen = etree.SubElement(envio, f"{{{NS}}}ResumenPeriodo")
    for tipo_doc in sorted(set(i.tipo_dte for i in items)):
        grp = [i for i in items if i.tipo_dte == tipo_doc]
        tot = etree.SubElement(resumen, f"{{{NS}}}TotalesPeriodo")
        etree.SubElement(tot, f"{{{NS}}}TpoDoc").text     = str(tipo_doc)
        etree.SubElement(tot, f"{{{NS}}}TotDoc").text     = str(len(grp))
        etree.SubElement(tot, f"{{{NS}}}TotMntExe").text  = str(int(sum(i.monto_exe  for i in grp)))
        etree.SubElement(tot, f"{{{NS}}}TotMntNeto").text = str(int(sum(i.monto_neto for i in grp)))
        etree.SubElement(tot, f"{{{NS}}}TotMntIVA").text  = str(int(sum(i.monto_iva  for i in grp)))

        # IVA No Recuperable
        t_nr = int(sum(i.iva_no_rec for i in grp))
        if t_nr:
            cod = next(i.cod_iva_no_rec for i in grp if i.iva_no_rec)
            inr = etree.SubElement(tot, f"{{{NS}}}TotIVANoRec")
            etree.SubElement(inr, f"{{{NS}}}CodIVANoRec").text    = str(cod)
            etree.SubElement(inr, f"{{{NS}}}TotOpIVANoRec").text  = str(sum(1 for i in grp if i.iva_no_rec))
            etree.SubElement(inr, f"{{{NS}}}TotMntIVANoRec").text = str(t_nr)

        # IVA Uso Común
        t_uc = int(sum(i.iva_uso_comun for i in grp))
        if t_uc:
            etree.SubElement(tot, f"{{{NS}}}TotIVAUsoComun").text    = str(t_uc)
            etree.SubElement(tot, f"{{{NS}}}FctProp").text            = set_data.fct_prop
            etree.SubElement(tot, f"{{{NS}}}TotCredIVAUsoComun").text = str(round(t_uc * fct))

        # IVA Retención Total
        t_ret = int(sum(i.iva_ret_total for i in grp))
        if t_ret:
            etree.SubElement(tot, f"{{{NS}}}TotOpIVARetTotal").text = str(sum(1 for i in grp if i.iva_ret_total))
            etree.SubElement(tot, f"{{{NS}}}TotIVARetTotal").text   = str(t_ret)

        etree.SubElement(tot, f"{{{NS}}}TotMntTotal").text = str(int(sum(i.monto_total for i in grp)))

    # ── Detalle ───────────────────────────────────────────────
    for item in items:
        det = etree.SubElement(envio, f"{{{NS}}}Detalle")
        etree.SubElement(det, f"{{{NS}}}TpoDoc").text  = str(item.tipo_dte)
        etree.SubElement(det, f"{{{NS}}}NroDoc").text  = str(item.folio)
        etree.SubElement(det, f"{{{NS}}}TasaImp").text = "19"
        etree.SubElement(det, f"{{{NS}}}FchDoc").text  = item.fecha_doc
        etree.SubElement(det, f"{{{NS}}}RUTDoc").text  = item.rut_doc
        etree.SubElement(det, f"{{{NS}}}RznSoc").text  = item.razon_doc[:50]

        if item.monto_exe:
            etree.SubElement(det, f"{{{NS}}}MntExe").text = str(int(item.monto_exe))

        etree.SubElement(det, f"{{{NS}}}MntNeto").text = str(int(item.monto_neto))

        te = item.tipo_especial or ""
        if te == "iva_uso_comun":
            etree.SubElement(det, f"{{{NS}}}MntIVA").text      = "0"
            etree.SubElement(det, f"{{{NS}}}IVAUsoComun").text = str(int(item.iva_uso_comun))
        elif te == "iva_no_rec":
            etree.SubElement(det, f"{{{NS}}}MntIVA").text = "0"
            inr = etree.SubElement(det, f"{{{NS}}}IVANoRec")
            etree.SubElement(inr, f"{{{NS}}}CodIVANoRec").text = str(item.cod_iva_no_rec)
            etree.SubElement(inr, f"{{{NS}}}MntIVANoRec").text = str(int(item.iva_no_rec))
        elif te == "iva_ret_total":
            # MntIVA = IVA del doc, IVARetTotal = mismo valor (retenido)
            etree.SubElement(det, f"{{{NS}}}MntIVA").text      = str(int(item.monto_iva))
            etree.SubElement(det, f"{{{NS}}}IVARetTotal").text = str(int(item.iva_ret_total))
        else:
            # Normal y tipos 60/55: MntIVA siempre presente aunque sea 0
            etree.SubElement(det, f"{{{NS}}}MntIVA").text = str(int(item.monto_iva))

        etree.SubElement(det, f"{{{NS}}}MntTotal").text = str(int(item.monto_total))

    etree.SubElement(envio, f"{{{NS}}}TmstFirma").text = tmst

    xml_bytes = etree.tostring(root, encoding="ISO-8859-1",
                               xml_declaration=True, pretty_print=True)
    return xml_bytes.decode("ISO-8859-1").replace(
        "<?xml version='1.0' encoding='ISO-8859-1'?>",
        '<?xml version="1.0" encoding="ISO-8859-1"?>',
    )


# ── Endpoints ─────────────────────────────────────────────────

@router.post(
    "/set",
    summary="Carga el set de documentos de compras del .txt del SII",
    description="""
Guarda los documentos del set de compras ingresados por el usuario.
Reemplaza el set anterior del mismo emisor+período si existe.

Campos tipo_especial:
- ""             → IVA normal con crédito fiscal completo
- "iva_uso_comun"→ Factura con IVA de uso común (indicar iva_uso_comun y fct_prop)
- "iva_no_rec"   → IVA no recuperable art.23 N°5 (indicar iva_no_rec y cod_iva_no_rec)
- "iva_ret_total"→ Factura de Compra con retención total IVA (indicar iva_ret_total)
    """,
    status_code=201,
)
async def crear_set_libro_compras(
    body: CrearSetRequest,
    db: AsyncSession = Depends(get_db),
):
    emisor = await db.get(Emisor, body.emisor_id)
    if not emisor:
        raise HTTPException(404, f"Emisor {body.emisor_id} no encontrado")

    # Eliminar set anterior del mismo emisor+período+natencion si existe
    res = await db.execute(
        select(SetLibroCompras).where(
            SetLibroCompras.emisor_id == body.emisor_id,
            SetLibroCompras.periodo   == body.periodo,
            SetLibroCompras.natencion == body.natencion,
        )
    )
    set_existente = res.scalar_one_or_none()
    if set_existente:
        await db.delete(set_existente)
        await db.flush()

    # Crear nuevo set
    nuevo_set = SetLibroCompras(
        emisor_id = body.emisor_id,
        natencion = body.natencion,
        periodo   = body.periodo,
        fch_resol = body.fch_resol,
        nro_resol = body.nro_resol,
        fct_prop  = body.fct_prop,
    )
    db.add(nuevo_set)
    await db.flush()

    # Guardar documentos
    for doc in body.documentos:
        db.add(ItemSetLibroCompras(
            set_id         = nuevo_set.id,
            tipo_dte       = doc.tipo_dte,
            folio          = doc.folio,
            fecha_doc      = doc.fecha_doc,
            rut_doc        = doc.rut_doc.replace(".", ""),
            razon_doc      = doc.razon_doc[:100],
            monto_neto     = doc.monto_neto,
            monto_exe      = doc.monto_exe,
            monto_iva      = doc.monto_iva,
            monto_total    = doc.monto_total,
            tipo_especial  = doc.tipo_especial,
            iva_uso_comun  = doc.iva_uso_comun,
            iva_no_rec     = doc.iva_no_rec,
            cod_iva_no_rec = doc.cod_iva_no_rec,
            iva_ret_total  = doc.iva_ret_total,
        ))

    await db.commit()

    logger.info(
        f"[SET COMPRAS] emisor={body.emisor_id} natencion={body.natencion} "
        f"periodo={body.periodo} docs={len(body.documentos)}"
    )

    return {
        "set_id":    nuevo_set.id,
        "natencion": nuevo_set.natencion,
        "periodo":   nuevo_set.periodo,
        "documentos": len(body.documentos),
        "mensaje":   "Set cargado correctamente. Use /generar-xml/{set_id} para generar el libro.",
    }


@router.get(
    "/sets/{emisor_id}",
    summary="Lista los sets de compras de un emisor",
)
async def listar_sets(emisor_id: int, db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(SetLibroCompras)
        .where(SetLibroCompras.emisor_id == emisor_id)
        .order_by(SetLibroCompras.created_at.desc())
    )
    sets = res.scalars().all()
    return [
        {
            "set_id":    s.id,
            "natencion": s.natencion,
            "periodo":   s.periodo,
            "docs":      len(s.items),
            "created_at": str(s.created_at)[:19],
        }
        for s in sets
    ]


@router.post(
    "/generar-xml/{set_id}",
    summary="Genera el LibroCompras firmado desde el set cargado",
)
async def generar_libro_compras_din(
    set_id: int,
    db: AsyncSession = Depends(get_db),
):
    # Cargar set con sus items
    set_data = await db.get(SetLibroCompras, set_id)
    if not set_data:
        raise HTTPException(404, f"Set {set_id} no encontrado")

    if not set_data.items:
        raise HTTPException(400, "El set no tiene documentos cargados")

    emisor, cert = await _get_emisor_y_cert(set_data.emisor_id, db)
    rut_envia = cert.rut_firmante or emisor.rut

    # Construir XML
    try:
        xml_str = _construir_xml(emisor, rut_envia, set_data, set_data.items)
    except Exception as e:
        raise HTTPException(500, f"Error construyendo XML: {e}")

    # Firmar
    firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    try:
        xml_firmado = await firma.firmar_libro(xml_str)
    except Exception as e:
        raise HTTPException(500, f"Error firmando: {e}")

    rut_limpio = emisor.rut.replace(".", "").replace("-", "")
    nombre = f"LibroCompras_{set_data.natencion}_{rut_limpio}_{set_data.periodo}.xml"

    logger.info(
        f"[LIBRO COMPRAS DIN] emisor={emisor.rut} set_id={set_id} "
        f"natencion={set_data.natencion} docs={len(set_data.items)}"
    )

    return Response(
        content    = xml_firmado.encode("ISO-8859-1"),
        media_type = "application/octet-stream",
        headers    = {
            "Content-Disposition": f'attachment; filename="{nombre}"',
            "X-SetId":             str(set_id),
            "X-NroAtencion":       set_data.natencion,
            "X-Periodo":           set_data.periodo,
            "X-Docs":              str(len(set_data.items)),
        },
    )


@router.post(
    "/preview/{set_id}",
    summary="Preview XML sin firma para revisión antes de enviar",
    response_class=Response,
)
async def preview_libro_compras_din(
    set_id: int,
    db: AsyncSession = Depends(get_db),
):
    set_data = await db.get(SetLibroCompras, set_id)
    if not set_data:
        raise HTTPException(404, f"Set {set_id} no encontrado")
    if not set_data.items:
        raise HTTPException(400, "El set no tiene documentos")

    emisor, cert = await _get_emisor_y_cert(set_data.emisor_id, db)
    xml_str = _construir_xml(emisor, cert.rut_firmante or emisor.rut, set_data, set_data.items)

    return Response(
        content    = xml_str.encode("UTF-8"),
        media_type = "application/xml",
        headers    = {"Content-Disposition": f'inline; filename="preview_{set_data.natencion}.xml"'},
    )
