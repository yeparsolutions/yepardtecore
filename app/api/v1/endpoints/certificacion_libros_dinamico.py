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
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
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
    Obtiene los DTEs emitidos del período dado — SIN duplicados.
    Cada regeneración crea nuevos registros en la BD con el mismo folio.
    Usamos MAX(id) por (tipo_dte, folio) para quedarnos solo con el más reciente.
    """
    from sqlalchemy import func as _func
    from datetime import date as _date
    import calendar

    # Subquery: id más reciente por (tipo_dte, folio)
    sub = (
        select(_func.max(DTE.id).label("max_id"))
        .where(
            DTE.emisor_id == emisor_id,
            DTE.tipo_dte.in_(tipos),
            DTE.ambiente == "certificacion",
            DTE.estado.notin_(["ANULADO"]),
        )
        .group_by(DTE.tipo_dte, DTE.folio)
        .subquery()
    )

    result = await db.execute(
        select(DTE)
        .where(DTE.id.in_(select(sub.c.max_id)))
        .order_by(DTE.tipo_dte, DTE.folio)
    )
    dtes = result.scalars().all()

    # Filtrar por período si tienen created_at
    año, mes   = int(periodo[:4]), int(periodo[5:7])
    fecha_desde = _date(año, mes, 1)
    fecha_hasta = _date(año, mes, calendar.monthrange(año, mes)[1])

    dtes_periodo = []
    for dte in dtes:
        fecha_raw = getattr(dte, 'fecha_emision', None) or getattr(dte, 'created_at', None)
        if fecha_raw:
            try:
                fe = fecha_raw if isinstance(fecha_raw, _date) else _date.fromisoformat(str(fecha_raw)[:10])
                if fecha_desde <= fe <= fecha_hasta:
                    dtes_periodo.append(dte)
            except Exception:
                dtes_periodo.append(dte)
        else:
            dtes_periodo.append(dte)

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

    es_guias    = (tipo_envio_id == "LibroGuias")
    root_tag    = "LibroGuia"         if es_guias else "LibroCompraVenta"
    schema_file = "LibroGuia_v10.xsd" if es_guias else "LibroCV_v10.xsd"

    root = etree.Element(
        f"{{{NS}}}{root_tag}",
        nsmap={None: NS, "xsi": "http://www.w3.org/2001/XMLSchema-instance"},
        attrib={
            "version": "1.0",
            "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation":
                f"{NS} {schema_file}",
        }
    )

    envio = etree.SubElement(root, f"{{{NS}}}EnvioLibro")
    envio.set("ID", tipo_envio_id)

    # ── Carátula ─────────────────────────────────────────────────────────────
    # IMPORTANTE: en LibroGuia_v10.xsd el orden es TipoLibro → TipoOperacion
    # En LibroCV_v10.xsd es TipoOperacion → TipoLibro (diferente al XSD de guías)
    car = etree.SubElement(envio, f"{{{NS}}}Caratula")
    etree.SubElement(car, f"{{{NS}}}RutEmisorLibro").text   = emisor.rut
    etree.SubElement(car, f"{{{NS}}}RutEnvia").text         = emisor.rut
    etree.SubElement(car, f"{{{NS}}}PeriodoTributario").text = periodo
    etree.SubElement(car, f"{{{NS}}}FchResol").text         = fch_resol
    etree.SubElement(car, f"{{{NS}}}NroResol").text         = nro_resol
    if es_guias:
        # LibroGuia: TipoLibro PRIMERO
        etree.SubElement(car, f"{{{NS}}}TipoLibro").text       = "ESPECIAL"
        etree.SubElement(car, f"{{{NS}}}TipoOperacion").text   = tipo_libro
    else:
        # LibroCV: TipoOperacion PRIMERO
        etree.SubElement(car, f"{{{NS}}}TipoOperacion").text   = tipo_libro
        etree.SubElement(car, f"{{{NS}}}TipoLibro").text       = "ESPECIAL"
    etree.SubElement(car, f"{{{NS}}}TipoEnvio").text        = "TOTAL"
    etree.SubElement(car, f"{{{NS}}}FolioNotificacion").text = natencion

    # ── Convertir DTEs a dicts ────────────────────────────────────────────────
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

    # ── ResumenPeriodo ────────────────────────────────────────────────────────
    resumen = etree.SubElement(envio, f"{{{NS}}}ResumenPeriodo")

    if es_guias:
        # LibroGuia_v10.xsd: estructura propia — NO usa TotalesPeriodo
        # TotFolAnulado: folios anulados (0 en certificación)
        # TotGuiaAnulada: guías anuladas (0 en certificación)
        # TotGuiaVenta: número de guías de venta (con monto > 0)
        # TotMntGuiaVta: suma de MntTotal de guías de venta
        guias_venta  = [d for d in docs if d["total"] > 0]
        tot_mnt_vta  = sum(d["total"] for d in guias_venta)
        etree.SubElement(resumen, f"{{{NS}}}TotFolAnulado").text  = "0"
        etree.SubElement(resumen, f"{{{NS}}}TotGuiaAnulada").text = "0"
        etree.SubElement(resumen, f"{{{NS}}}TotGuiaVenta").text   = str(len(guias_venta))
        if tot_mnt_vta > 0:
            etree.SubElement(resumen, f"{{{NS}}}TotMntGuiaVta").text = str(tot_mnt_vta)
    else:
        # LibroCV_v10.xsd: TotalesPeriodo por tipo de documento
        for tipo_doc in sorted(set(d["tipo"] for d in docs)):
            docs_t = [d for d in docs if d["tipo"] == tipo_doc]
            tot = etree.SubElement(resumen, f"{{{NS}}}TotalesPeriodo")
            etree.SubElement(tot, f"{{{NS}}}TpoDoc").text      = str(tipo_doc)
            etree.SubElement(tot, f"{{{NS}}}TotDoc").text      = str(len(docs_t))
            etree.SubElement(tot, f"{{{NS}}}TotMntExe").text   = str(sum(d["exe"]   for d in docs_t))
            etree.SubElement(tot, f"{{{NS}}}TotMntNeto").text  = str(sum(d["neto"]  for d in docs_t))
            etree.SubElement(tot, f"{{{NS}}}TotMntIVA").text   = str(sum(d["iva"]   for d in docs_t))
            etree.SubElement(tot, f"{{{NS}}}TotMntTotal").text = str(sum(d["total"] for d in docs_t))

    # ── Detalle ───────────────────────────────────────────────────────────────
    for doc in docs:
        det = etree.SubElement(envio, f"{{{NS}}}Detalle")

        if es_guias:
            # LibroGuia_v10.xsd: Folio → Anulado → [IndTraslado] → FchDoc → RUTDoc → RznSoc
            #                    → [MntNeto] → [TasaIVA] → [IVA] → [MntExe] → MntTotal
            etree.SubElement(det, f"{{{NS}}}Folio").text   = str(doc["folio"])
            etree.SubElement(det, f"{{{NS}}}Anulado").text = "NO"
            # IndTraslado: no está en el modelo DTE — omitir (campo opcional en XSD)
            etree.SubElement(det, f"{{{NS}}}FchDoc").text  = doc["fecha"]
            if doc["rut"]:
                etree.SubElement(det, f"{{{NS}}}RUTDoc").text = doc["rut"]
            if doc["razon"]:
                etree.SubElement(det, f"{{{NS}}}RznSoc").text = doc["razon"]
            if doc["neto"] != 0:
                etree.SubElement(det, f"{{{NS}}}MntNeto").text = str(doc["neto"])
            if doc["iva"] != 0:
                etree.SubElement(det, f"{{{NS}}}TasaIVA").text = "19"
                etree.SubElement(det, f"{{{NS}}}IVA").text     = str(doc["iva"])
            if doc["exe"] != 0:
                etree.SubElement(det, f"{{{NS}}}MntExe").text  = str(doc["exe"])
            etree.SubElement(det, f"{{{NS}}}MntTotal").text = str(doc["total"])
        else:
            # LibroCV_v10.xsd: TpoDoc → NroDoc → TasaImp → FchDoc → RUTDoc → RznSoc
            #                  → [MntExe] → MntNeto → [MntIVA] → MntTotal
            etree.SubElement(det, f"{{{NS}}}TpoDoc").text  = str(doc["tipo"])
            etree.SubElement(det, f"{{{NS}}}NroDoc").text  = str(doc["folio"])
            etree.SubElement(det, f"{{{NS}}}TasaImp").text = "19"
            etree.SubElement(det, f"{{{NS}}}FchDoc").text  = doc["fecha"]
            etree.SubElement(det, f"{{{NS}}}RUTDoc").text  = doc["rut"]
            etree.SubElement(det, f"{{{NS}}}RznSoc").text  = doc["razon"]
            if doc["exe"] != 0:
                etree.SubElement(det, f"{{{NS}}}MntExe").text = str(doc["exe"])
            etree.SubElement(det, f"{{{NS}}}MntNeto").text = str(doc["neto"])
            if doc["iva"] != 0 or doc["tipo"] in (56, 61):
                etree.SubElement(det, f"{{{NS}}}MntIVA").text = str(doc["iva"])
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


# ── Endpoint 2: Libro desde XMLs de EnvioDTE subidos ─────────────────────────
# Permite generar el libro a partir de los XML aceptados por el SII,
# sin depender de la BD. Resuelve el problema de duplicados y de DTEs
# rechazados que quedan en BD.

def _parsear_dtes_desde_xml(contenido: bytes) -> list[dict]:
    """
    Extrae los datos de cada DTE desde un EnvioDTE XML firmado.
    Retorna lista de dicts con los campos que necesita _construir_libro_xml.
    """
    NS = "http://www.sii.cl/SiiDte"
    try:
        root = etree.fromstring(contenido)
    except Exception as e:
        raise ValueError(f"XML inválido: {e}")

    dtes = []
    for doc in root.findall(f'.//{{{NS}}}Documento'):
        tipo  = doc.findtext(f'.//{{{NS}}}TipoDTE')
        folio = doc.findtext(f'.//{{{NS}}}Folio')
        fecha = doc.findtext(f'.//{{{NS}}}FchEmis')
        rut   = doc.findtext(f'.//{{{NS}}}RUTRecep') or doc.findtext(f'.//{{{NS}}}RUTDoc') or "66666666-6"
        razon = doc.findtext(f'.//{{{NS}}}RznSocRecep') or doc.findtext(f'.//{{{NS}}}RznSoc') or "Consumidor Final"

        neto  = doc.findtext(f'.//{{{NS}}}MntNeto')  or "0"
        iva   = doc.findtext(f'.//{{{NS}}}IVA')       or "0"
        exe   = doc.findtext(f'.//{{{NS}}}MntExe')   or "0"
        total = doc.findtext(f'.//{{{NS}}}MntTotal')

        if not tipo or not folio or not total:
            continue

        dtes.append({
            "tipo_dte":       int(tipo),
            "folio":          int(folio),
            "fecha_emision":  fecha or "",
            "rut_receptor":   rut.replace(".", ""),
            "nombre_receptor": razon[:50],
            "monto_neto":     int(neto),
            "monto_iva":      int(iva),
            "monto_total":    int(total),
            "monto_exe":      int(exe),
        })
    return dtes


class _DTEFake:
    """Objeto DTE liviano para pasar a _construir_libro_xml sin BD."""
    def __init__(self, d: dict):
        self.tipo_dte        = d["tipo_dte"]
        self.folio           = d["folio"]
        self.fecha_emision   = d.get("fecha_emision") or None
        self.created_at      = d.get("fecha_emision") or None
        self.rut_receptor    = d.get("rut_receptor",    "66666666-6")
        self.nombre_receptor = d.get("nombre_receptor", "Consumidor Final")
        self.monto_neto      = d.get("monto_neto",  0)
        self.monto_iva       = d.get("monto_iva",   0)
        self.monto_total     = d.get("monto_total", 0)
        self.estado          = "ACEPTADO"
        self.ambiente        = "certificacion"


@router.post(
    "/desde-xml",
    summary="Genera libro desde XMLs de EnvioDTE (sin BD)",
    description="""
Genera el libro a partir de los XML de EnvioDTE YA ACEPTADOS por el SII.
No usa la BD — evita duplicados y DTEs rechazados.

Útil cuando se generó el set múltiples veces o cuando la BD tiene datos sucios.
Subir los XML exactos que el SII aceptó.
    """,
)
async def generar_libro_desde_xml(
    emisor_id:  int           = Form(...),
    tipo_libro: str           = Form(...),   # ventas | compras | guias
    natencion:  str           = Form(...),
    periodo:    str           = Form(...),   # AAAA-MM
    archivos:   list[UploadFile] = File(...),  # uno o más EnvioDTE XML
    fch_resol:  str           = Form("2026-04-19"),
    nro_resol:  str           = Form("0"),
    db: AsyncSession = Depends(get_db),
):
    if tipo_libro not in ("ventas", "compras", "guias"):
        raise HTTPException(400, "tipo_libro debe ser: ventas | compras | guias")

    # Cargar emisor y certificado
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(404, f"Emisor {emisor_id} no encontrado")

    cert_result = await db.execute(
        select(Certificado).where(Certificado.emisor_id == emisor_id)
    )
    cert = cert_result.scalars().first()
    if not cert:
        raise HTTPException(400, "Sin certificado .p12 cargado")

    # Parsear todos los XMLs subidos
    todos_dtes: list[_DTEFake] = []
    folios_vistos: set = set()

    for archivo in archivos:
        contenido = await archivo.read()
        try:
            dtes_xml = _parsear_dtes_desde_xml(contenido)
        except ValueError as e:
            raise HTTPException(400, f"Error en {archivo.filename}: {e}")

        for d in dtes_xml:
            key = (d["tipo_dte"], d["folio"])
            if key not in folios_vistos:          # deduplicar si suben mismo DTE dos veces
                folios_vistos.add(key)
                todos_dtes.append(_DTEFake(d))

    if not todos_dtes:
        raise HTTPException(404, "No se encontraron DTEs válidos en los XMLs subidos")

    todos_dtes.sort(key=lambda x: (x.tipo_dte, x.folio))

    libro_meta = {
        "ventas":  ("VENTA",  "LibroVentas",  "Ventas"),
        "compras": ("COMPRA", "LibroCompras", "Compras"),
        "guias":   ("VENTA",  "LibroGuias",   "Guías"),
    }
    tipo_op, libro_id, label = libro_meta[tipo_libro]

    logger.info(
        f"[LIBRO DESDE-XML] {label} emisor={emisor.rut} "
        f"natencion={natencion} archivos={len(archivos)} dtes={len(todos_dtes)}"
    )

    xml_str = _construir_libro_xml(
        emisor        = emisor,
        dtes          = todos_dtes,
        tipo_libro    = tipo_op,
        tipo_envio_id = libro_id,
        natencion     = natencion,
        periodo       = periodo,
        fch_resol     = fch_resol,
        nro_resol     = nro_resol,
    )

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
            "X-DTEs-Incluidos":    str(len(todos_dtes)),
        },
    )
