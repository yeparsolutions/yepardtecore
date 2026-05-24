# app/api/v1/endpoints/certificacion_libros_dinamico.py
# ══════════════════════════════════════════════════════════════
# Libros de Ventas y Compras DINÁMICOS
# Lee los DTEs emitidos directamente desde la BD del emisor.
# Cero hardcode — funciona para cualquier usuario.
#
# Reemplaza:
#   certificacion_libro_ventas.py   (NATENCION 4841544 hardcodeado)
#   certificacion_libro_compras.py  (NATENCION 4841545 hardcodeado)
#   certificacion_libro_guias.py    (NATENCION 4841547 hardcodeado)
# ══════════════════════════════════════════════════════════════

import logging
from datetime import date, datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from lxml import etree

from app.db.base import get_db
from app.models.emisor import Emisor
from app.models.certificado import Certificado
from app.models.dte import DTE
from app.services.firma_digital import FirmaDigital

logger = logging.getLogger("yepardtecore.cert_libros_din")
router = APIRouter(prefix="/certificacion-libros", tags=["Certificacion Libros Dinamico"])

NS = "http://www.sii.cl/SiiDte"

# Tipos DTE que van en cada libro
TIPOS_VENTAS  = {33, 34, 56, 61}   # Facturas + NC + ND (NO guías)
TIPOS_COMPRAS = {46, 56, 61}        # Facturas de compra + NC + ND
TIPOS_GUIAS   = {52}


# ── Helpers ───────────────────────────────────────────────────

async def _get_emisor_y_cert(emisor_id: int, db: AsyncSession):
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(404, f"Emisor {emisor_id} no encontrado")
    cert_result = await db.execute(
        select(Certificado).where(
            Certificado.emisor_id == emisor_id,
            Certificado.activo == True,
        ).limit(1)
    )
    cert = cert_result.scalar_one_or_none()
    if not cert or not cert.certificado_p12:
        raise HTTPException(400, "Sin certificado .p12 cargado")
    return emisor, cert


async def _obtener_dtes(
    emisor_id: int,
    tipos: set,
    periodo: str,
    db: AsyncSession,
) -> list[DTE]:
    """
    Obtiene los DTEs emitidos del período dado.
    periodo formato: "AAAA-MM"
    """
    año, mes = int(periodo[:4]), int(periodo[5:7])
    from datetime import date as _date
    import calendar
    ultimo_dia = calendar.monthrange(año, mes)[1]
    fecha_desde = _date(año, mes, 1)
    fecha_hasta = _date(año, mes, ultimo_dia)

    result = await db.execute(
        select(DTE).where(
            DTE.emisor_id == emisor_id,
            DTE.tipo_dte.in_(tipos),
            DTE.ambiente == "certificacion",
            # Incluir BORRADOR y PENDIENTE_ENVIO — son DTEs válidos de certificación
            # Solo excluir ANULADO
            DTE.estado.notin_(["ANULADO"]),
        ).order_by(DTE.tipo_dte, DTE.folio)
    )
    dtes = result.scalars().all()

    # Filtrar por período si tienen fecha_emision
    dtes_periodo = []
    for dte in dtes:
        if hasattr(dte, 'fecha_emision') and dte.fecha_emision:
            fe = dte.fecha_emision if isinstance(dte.fecha_emision, _date) else _date.fromisoformat(str(dte.fecha_emision)[:10])
            if fecha_desde <= fe <= fecha_hasta:
                dtes_periodo.append(dte)
        else:
            dtes_periodo.append(dte)  # si no tiene fecha, incluir igual

    return dtes_periodo


def _construir_libro_xml(
    emisor: Emisor,
    dtes: list[DTE],
    tipo_libro: str,       # "VENTA" o "COMPRA"
    tipo_envio_id: str,    # "LibroVentas" o "LibroCompras" o "LibroGuias"
    natencion: str,        # N° de atención del set del libro
    periodo: str,          # "AAAA-MM"
    fch_resol: str = "2026-04-19",
    nro_resol: str = "0",
) -> str:
    """Construye el XML del Libro dinámicamente desde los DTEs de BD."""

    tmst = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    root = etree.Element(
        f"{{{NS}}}LibroCompraVenta",
        nsmap={None: NS, "xsi": "http://www.w3.org/2001/XMLSchema-instance"},
        attrib={
            "version": "1.0",
            "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation":
                f"{NS} LibroCV_v10.xsd",
        }
    )

    envio = etree.SubElement(root, f"{{{NS}}}EnvioLibro")
    envio.set("ID", tipo_envio_id)

    # Carátula
    car = etree.SubElement(envio, f"{{{NS}}}Caratula")
    etree.SubElement(car, f"{{{NS}}}RutEmisorLibro").text   = emisor.rut
    etree.SubElement(car, f"{{{NS}}}RutEnvia").text          = emisor.rut  # emisor mismo envía
    etree.SubElement(car, f"{{{NS}}}PeriodoTributario").text = periodo
    etree.SubElement(car, f"{{{NS}}}FchResol").text          = fch_resol
    etree.SubElement(car, f"{{{NS}}}NroResol").text          = nro_resol
    etree.SubElement(car, f"{{{NS}}}TipoOperacion").text     = tipo_libro
    etree.SubElement(car, f"{{{NS}}}TipoLibro").text         = "ESPECIAL"
    etree.SubElement(car, f"{{{NS}}}TipoEnvio").text         = "TOTAL"
    etree.SubElement(car, f"{{{NS}}}FolioNotificacion").text = natencion  # dinámico ✓

    # Convertir DTEs a dicts para el resumen y detalle
    docs = []
    for dte in dtes:
        # El modelo DTE no tiene fecha_emision — usar created_at como fecha del documento
        fecha_raw = getattr(dte, 'fecha_emision', None) or getattr(dte, 'created_at', None)
        fecha_str = str(fecha_raw)[:10] if fecha_raw else periodo + "-01"
        rut_doc   = (dte.rut_receptor or "66666666-6").replace(".", "")
        razon_doc = (dte.nombre_receptor or "Consumidor Final")[:50]
        docs.append({
            "tipo":  dte.tipo_dte,
            "folio": dte.folio,
            "fecha": fecha_str,
            "rut":   rut_doc,
            "razon": razon_doc,
            "neto":  int(dte.monto_neto  or 0),
            "iva":   int(dte.monto_iva   or 0),
            "exe":   int((dte.monto_total or 0) - (dte.monto_neto or 0) - (dte.monto_iva or 0)),
            "total": int(dte.monto_total or 0),
        })

    # ResumenPeriodo — ANTES de Detalle (orden XSD)
    resumen = etree.SubElement(envio, f"{{{NS}}}ResumenPeriodo")
    for tipo_doc in sorted(set(d["tipo"] for d in docs)):
        docs_t = [d for d in docs if d["tipo"] == tipo_doc]
        tot = etree.SubElement(resumen, f"{{{NS}}}TotalesPeriodo")
        etree.SubElement(tot, f"{{{NS}}}TpoDoc").text      = str(tipo_doc)
        etree.SubElement(tot, f"{{{NS}}}TotDoc").text      = str(len(docs_t))
        etree.SubElement(tot, f"{{{NS}}}TotMntExe").text   = str(sum(d["exe"]   for d in docs_t))
        etree.SubElement(tot, f"{{{NS}}}TotMntNeto").text  = str(sum(d["neto"]  for d in docs_t))
        etree.SubElement(tot, f"{{{NS}}}TotMntIVA").text   = str(sum(d["iva"]   for d in docs_t))
        etree.SubElement(tot, f"{{{NS}}}TotMntTotal").text = str(sum(d["total"] for d in docs_t))

    # Detalle — uno por DTE
    for doc in docs:
        det = etree.SubElement(envio, f"{{{NS}}}Detalle")
        etree.SubElement(det, f"{{{NS}}}TpoDoc").text  = str(doc["tipo"])
        etree.SubElement(det, f"{{{NS}}}NroDoc").text  = str(doc["folio"])
        etree.SubElement(det, f"{{{NS}}}TasaImp").text = "19"
        etree.SubElement(det, f"{{{NS}}}FchDoc").text  = doc["fecha"]
        etree.SubElement(det, f"{{{NS}}}RUTDoc").text  = doc["rut"]
        etree.SubElement(det, f"{{{NS}}}RznSoc").text  = doc["razon"]
        if doc["exe"] != 0:
            etree.SubElement(det, f"{{{NS}}}MntExe").text  = str(doc["exe"])
        etree.SubElement(det, f"{{{NS}}}MntNeto").text  = str(doc["neto"])
        if doc["iva"] != 0:
            etree.SubElement(det, f"{{{NS}}}MntIVA").text  = str(doc["iva"])
        etree.SubElement(det, f"{{{NS}}}MntTotal").text = str(doc["total"])

    etree.SubElement(envio, f"{{{NS}}}TmstFirma").text = tmst

    xml_bytes = etree.tostring(root, encoding="ISO-8859-1", xml_declaration=True, pretty_print=True)
    xml_str   = xml_bytes.decode("ISO-8859-1")
    return xml_str.replace(
        "<?xml version='1.0' encoding='ISO-8859-1'?>",
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
    )


# ── Endpoints ─────────────────────────────────────────────────

@router.post(
    "/generar-xml",
    summary="Genera libro (ventas/compras/guías) dinámico desde DTEs en BD",
    description="""
Genera el XML del libro de certificación leyendo los DTEs emitidos
directamente desde la BD del emisor. Sin datos hardcodeados.

Parámetros:
- tipo_libro: "ventas" | "compras" | "guias"
- natencion:  N° de atención del libro (viene del .txt del SII)
- periodo:    Período a reportar en formato "AAAA-MM" (ej: "2026-05")
    """,
)
async def generar_libro_dinamico(
    emisor_id:  int,
    tipo_libro: str,                    # ventas | compras | guias
    natencion:  str,                    # N° de atención del libro
    periodo:    Optional[str] = None,   # AAAA-MM, default: mes actual
    fch_resol:  Optional[str] = None,
    nro_resol:  Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    tipo_libro = tipo_libro.lower().strip()
    if tipo_libro not in ("ventas", "compras", "guias"):
        raise HTTPException(400, "tipo_libro debe ser: ventas | compras | guias")

    if not periodo:
        hoy = date.today()
        periodo = f"{hoy.year}-{hoy.month:02d}"

    emisor, cert = await _get_emisor_y_cert(emisor_id, db)

    # Seleccionar tipos DTE según el libro
    tipos_map = {
        "ventas":  TIPOS_VENTAS,
        "compras": TIPOS_COMPRAS,
        "guias":   TIPOS_GUIAS,
    }
    tipos = tipos_map[tipo_libro]

    # Leer DTEs desde BD
    dtes = await _obtener_dtes(emisor_id, tipos, periodo, db)

    if not dtes:
        raise HTTPException(404,
            f"No hay DTEs de tipo {tipos} para el emisor {emisor_id} "
            f"en el período {periodo}. Genera primero los sets de prueba."
        )

    # Metadatos del libro
    libro_meta = {
        "ventas":  ("VENTA",  "LibroVentas",  "EnvioLibro de Ventas"),
        "compras": ("COMPRA", "LibroCompras", "EnvioLibro de Compras"),
        "guias":   ("VENTA",  "LibroGuias",   "EnvioLibro de Guías"),
    }
    tipo_op, libro_id, descripcion = libro_meta[tipo_libro]

    logger.info(
        f"[LIBRO DIN] {descripcion} emisor={emisor.rut} "
        f"natencion={natencion} periodo={periodo} dtes={len(dtes)}"
    )

    # Construir XML
    xml_str = _construir_libro_xml(
        emisor    = emisor,
        dtes      = dtes,
        tipo_libro = tipo_op,
        tipo_envio_id = libro_id,
        natencion  = natencion,
        periodo    = periodo,
        fch_resol  = fch_resol or "2026-04-19",
        nro_resol  = nro_resol or "0",
    )

    # Firmar
    firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    try:
        xml_firmado = await firma.firmar_libro(xml_str)
    except Exception as e:
        raise HTTPException(500, f"Error al firmar el libro: {e}")

    rut_limpio = emisor.rut.replace(".", "").replace("-", "")
    nombre = f"Libro{tipo_libro.capitalize()}_{natencion}_{rut_limpio}_{periodo}.xml"

    return Response(
        content    = xml_firmado.encode("ISO-8859-1"),
        media_type = "application/octet-stream",
        headers    = {
            "Content-Disposition": f'attachment; filename="{nombre}"',
            "X-NroAtencion":       natencion,
            "X-Periodo":           periodo,
            "X-TipoLibro":         tipo_libro,
            "X-DTEs-Incluidos":    str(len(dtes)),
        },
    )
