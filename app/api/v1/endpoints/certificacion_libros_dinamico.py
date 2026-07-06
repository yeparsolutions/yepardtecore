# app/api/v1/endpoints/certificacion_libros_dinamico.py
# ══════════════════════════════════════════════════════════════
# Libros de Ventas, Compras y Guías DINÁMICOS  —  ARCHIVO CANÓNICO ÚNICO
#
# ⚠ IMPORTANTE: Este es el ÚNICO archivo de libros dinámicos que debe
#   existir en el proyecto. Eliminar cualquier copia duplicada
#   (p.ej. certificacion_libro_compras_dinamico.py) y su include_router,
#   porque comparten el prefix "/certificacion-libros" y FastAPI solo
#   sirve uno de los dos → los cambios "desaparecen".
#
# FIX T46 (2026-07-02, corregido 2026-07-06) — Factura de Compra con
# retención total de IVA:
#   El SII rechazó el set 4929133 con:
#     "No Informa Adecuadamente IVA Retenido Total / El Monto IVA No Cuadra"
#   y el reparo LBR-2: [MntIVA] distinto a [MntNeto]*[TasaImp].
#   Regla confirmada empíricamente contra maullin:
#     - Detalle:  MntIVA = neto × 19% (NUNCA 0) + IVARetTotal = mismo monto
#     - Resumen:  TotMntIVA incluye el IVA del T46 + TotOpIVARetTotal/TotIVARetTotal
#     - MntTotal = neto (el IVA se retiene, no se paga al proveedor) → SIN reparo
#   Analogía: el comprador es "cajero del fisco" — el IVA existe y se
#   declara completo, solo que en vez de pagárselo al proveedor lo retiene
#   y lo entera él mismo. Declarar MntIVA=0 es negar que hubo IVA.
#
#   ⚠ CAUSA RAÍZ ENCONTRADA EL 2026-07-06: el fix del 07-02 solo escribía
#   str(doc["iva"]) en el XML, pero doc["iva"] viene de dte.monto_iva,
#   que para estos DTE está guardado en 0 (el DTE original no le cobra
#   IVA al proveedor, lo retiene). El "fix" nunca calculaba nada, solo
#   repetía el 0 ya guardado → volvió a rechazarse el envío 252737990.
#   Ahora se RECALCULA doc["iva"] = round(neto * 0.19) exclusivamente
#   para el Libro, sin tocar el DTE original en la base de datos.
#
# Lo demás NO se toca: tipos 30/33/60, IVA uso común e IVA no recuperable
# con MntIVA=0 pasaron SIN reparo en el envío 252601730 (LOK - Cuadrado).
# ══════════════════════════════════════════════════════════════

import logging
from datetime import date, datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
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
TIPOS_VENTAS  = {33, 34, 56, 61}
TIPOS_COMPRAS = {46, 56, 61}
TIPOS_GUIAS   = {52}


# ── Helpers ───────────────────────────────────────────────────

async def _get_emisor_y_cert(emisor_id: int, db: AsyncSession):
    # Carga el emisor y su certificado activo; falla temprano si falta algo
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

    # Filtrar por período usando la fecha de emisión (o created_at como respaldo)
    año, mes    = int(periodo[:4]), int(periodo[5:7])
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
    tipo_envio_id: str,    # "LibroVentas" | "LibroCompras" | "LibroGuias"
    natencion: str,        # N° de atención del set del libro
    periodo: str,          # "AAAA-MM"
    fch_resol: str = "2026-04-19",
    nro_resol: str = "0",
    rut_envia: str | None = None,   # RUT firmante del cert; si None usa emisor.rut
    cod_aut_rec: str | None = None, # Código de Autorización de Reemplazo (rectificación)
) -> str:
    """Construye el XML del Libro dinámicamente desde los DTEs de BD.

    Si cod_aut_rec viene informado, la carátula se genera como RECTIFICA
    con el tag <CodAutRec> — necesario cuando el período ya está cerrado
    (estado LTC) y el SII exige un código de reemplazo para aceptar un
    nuevo envío del mismo libro. Con cod_aut_rec=None el comportamiento
    es EXACTAMENTE el mismo de siempre (ESPECIAL/TOTAL).
    """

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

    # ── Carátula ──────────────────────────────────────────────
    car = etree.SubElement(envio, f"{{{NS}}}Caratula")
    _limpiar = lambda r: r.replace(".", "").strip() if r else r
    etree.SubElement(car, f"{{{NS}}}RutEmisorLibro").text    = _limpiar(emisor.rut)
    # RutEnvia: RUT del firmante del certificado (puede diferir del emisor)
    rut_env = rut_envia or emisor.rut
    etree.SubElement(car, f"{{{NS}}}RutEnvia").text          = _limpiar(rut_env)
    etree.SubElement(car, f"{{{NS}}}PeriodoTributario").text = periodo
    etree.SubElement(car, f"{{{NS}}}FchResol").text          = fch_resol
    etree.SubElement(car, f"{{{NS}}}NroResol").text          = nro_resol
    # TipoLibro: RECTIFICA solo si viene código de reemplazo del SII
    tipo_libro_caratula = "RECTIFICA" if cod_aut_rec else "ESPECIAL"
    if es_guias:
        # LibroGuia: TipoLibro directo, sin TipoOperacion
        etree.SubElement(car, f"{{{NS}}}TipoLibro").text = tipo_libro_caratula
    else:
        # LibroCV: TipoOperacion → TipoLibro
        etree.SubElement(car, f"{{{NS}}}TipoOperacion").text = tipo_libro
        etree.SubElement(car, f"{{{NS}}}TipoLibro").text     = tipo_libro_caratula
    etree.SubElement(car, f"{{{NS}}}TipoEnvio").text         = "TOTAL"
    etree.SubElement(car, f"{{{NS}}}FolioNotificacion").text = natencion
    if cod_aut_rec:
        # CodAutRec va al final de la carátula (después de FolioNotificacion)
        etree.SubElement(car, f"{{{NS}}}CodAutRec").text = cod_aut_rec

    # ── Convertir DTEs a dicts ────────────────────────────────
    docs = []
    for dte in dtes:
        fecha_raw = getattr(dte, 'fecha_emision', None) or getattr(dte, 'created_at', None)
        fecha_str = str(fecha_raw)[:10] if fecha_raw else periodo + "-01"
        rut_doc   = (dte.rut_receptor or "66666666-6").replace(".", "")
        razon_doc = (dte.nombre_receptor or "Consumidor Final")[:50]
        docs.append({
            "tipo":           dte.tipo_dte,
            "folio":          dte.folio,
            "fecha":          fecha_str,
            "rut":            rut_doc,
            "razon":          razon_doc,
            "neto":           int(dte.monto_neto  or 0),
            "iva":            int(dte.monto_iva   or 0),
            "exe":            int(getattr(dte, 'monto_exe', 0) or 0),
            "total":          int(dte.monto_total or 0),
            "anulado":        getattr(dte, 'anulado', False),
            "ind_traslado":   getattr(dte, 'ind_traslado', None),
            "tipo_despacho":  getattr(dte, 'tipo_despacho', None),
            # Campos especiales IVA (LibroCompras)
            "tipo_especial":  getattr(dte, 'tipo_especial', ''),
            "iva_uso_comun":  getattr(dte, 'iva_uso_comun', 0),
            "fct_prop":       getattr(dte, 'fct_prop', '0.60'),
            "iva_no_rec":     getattr(dte, 'iva_no_rec', 0),
            "cod_iva_no_rec": getattr(dte, 'cod_iva_no_rec', 9),
            "iva_ret_total":  getattr(dte, 'iva_ret_total', 0),
        })

    # ═══ FIX T46 (parte 0/2) — RECALCULAR, no confiar en la BD ═══
    # dte.monto_iva llega en 0 para las Facturas de Compra (T46) con
    # retención total, porque así se guardó el DTE original (al
    # proveedor no se le cobra el IVA, se lo retiene el comprador).
    # El Libro de Compras es OTRO documento con OTRA regla: el SII
    # exige que ahí el IVA "exista" (MntIVA = MntNeto × 19%), aunque
    # el DTE de origen lo muestre en 0.
    # Analogía: la boleta de sueldo muestra el líquido a pagar (post-
    # descuentos), pero el certificado de renta para el SII exige
    # mostrar el sueldo bruto completo. Son dos documentos, dos reglas.
    # Por eso NO basta con leer dte.monto_iva: hay que recalcularlo
    # aquí mismo para el Libro, sin tocar el DTE original.
    for d in docs:
        if d.get("tipo_especial") == "iva_ret_total":
            iva_calculado = round(d["neto"] * 0.19)
            d["iva"] = iva_calculado
            # Si iva_ret_total tampoco venía informado (0), usamos
            # el mismo cálculo — deben ser siempre iguales en este caso.
            if not d.get("iva_ret_total"):
                d["iva_ret_total"] = iva_calculado

    # ── ResumenPeriodo ────────────────────────────────────────
    resumen = etree.SubElement(envio, f"{{{NS}}}ResumenPeriodo")

    if es_guias:
        # LibroGuia: totales directos en ResumenPeriodo
        guias_anuld  = [d for d in docs if d.get("anulado")]
        guias_vgtes  = [d for d in docs if not d.get("anulado")]
        guias_venta  = [d for d in guias_vgtes if (d.get("ind_traslado") or 1) == 1]
        guias_no_vta = [d for d in guias_vgtes if (d.get("ind_traslado") or 1) != 1]
        tot_mnt_vta  = sum(d["total"] for d in guias_venta)
        etree.SubElement(resumen, f"{{{NS}}}TotGuiaAnulada").text = str(len(guias_anuld))
        etree.SubElement(resumen, f"{{{NS}}}TotGuiaVenta").text   = str(len(guias_venta))
        if tot_mnt_vta > 0:
            etree.SubElement(resumen, f"{{{NS}}}TotMntGuiaVta").text = str(tot_mnt_vta)
        # Tabla no-ventas (traslados, devoluciones, etc.)
        no_vta_por_tipo = {}
        for g in guias_no_vta:
            tpo = g.get("ind_traslado") or 5
            if tpo not in no_vta_por_tipo:
                no_vta_por_tipo[tpo] = {"cantidad": 0, "monto": 0}
            no_vta_por_tipo[tpo]["cantidad"] += 1
            no_vta_por_tipo[tpo]["monto"]    += g["total"]
        for tpo, datos in sorted(no_vta_por_tipo.items()):
            nv = etree.SubElement(resumen, f"{{{NS}}}TotTraslado")
            etree.SubElement(nv, f"{{{NS}}}TpoTraslado").text = str(tpo)
            etree.SubElement(nv, f"{{{NS}}}CantGuia").text    = str(datos["cantidad"])
            if datos["monto"]:
                etree.SubElement(nv, f"{{{NS}}}MntGuia").text = str(datos["monto"])
    else:
        # LibroCV: TotalesPeriodo por tipo de documento
        for tipo_doc in sorted(set(d["tipo"] for d in docs)):
            docs_t = [d for d in docs if d["tipo"] == tipo_doc]
            tot = etree.SubElement(resumen, f"{{{NS}}}TotalesPeriodo")
            etree.SubElement(tot, f"{{{NS}}}TpoDoc").text      = str(tipo_doc)
            etree.SubElement(tot, f"{{{NS}}}TotDoc").text      = str(len(docs_t))
            t_exe = sum(d["exe"] for d in docs_t)
            etree.SubElement(tot, f"{{{NS}}}TotMntExe").text   = str(t_exe)
            etree.SubElement(tot, f"{{{NS}}}TotMntNeto").text  = str(sum(d["neto"] for d in docs_t))

            # ═══ FIX T46 (parte 1/2) ═══════════════════════════
            # TotMntIVA = suma del MntIVA que efectivamente va en el detalle:
            # - iva_uso_comun / iva_no_rec → MntIVA=0 en detalle
            #   (validado por SII SIN reparo en envío 252601730) → se EXCLUYEN
            # - iva_ret_total → MntIVA COMPLETO en detalle
            #   (el SII exige MntIVA = neto × tasa) → se INCLUYE
            # Antes se excluían TODOS los especiales → TotMntIVA=0 en T46
            # → reparo "El Monto IVA No Cuadra"
            tot_mnt_iva = sum(
                d["iva"] for d in docs_t
                if d.get("tipo_especial") not in ("iva_uso_comun", "iva_no_rec")
            )
            etree.SubElement(tot, f"{{{NS}}}TotMntIVA").text   = str(tot_mnt_iva)

            # IVA No Recuperable
            t_nr = sum(d.get("iva_no_rec", 0) for d in docs_t)
            if t_nr:
                inr = etree.SubElement(tot, f"{{{NS}}}TotIVANoRec")
                etree.SubElement(inr, f"{{{NS}}}CodIVANoRec").text    = str(docs_t[0].get("cod_iva_no_rec", 9))
                etree.SubElement(inr, f"{{{NS}}}TotOpIVANoRec").text  = str(sum(1 for d in docs_t if d.get("iva_no_rec", 0)))
                etree.SubElement(inr, f"{{{NS}}}TotMntIVANoRec").text = str(t_nr)
            # IVA Uso Común
            t_uc = sum(d.get("iva_uso_comun", 0) for d in docs_t)
            if t_uc:
                fct = docs_t[0].get("fct_prop", "0.60")
                etree.SubElement(tot, f"{{{NS}}}TotIVAUsoComun").text    = str(t_uc)
                etree.SubElement(tot, f"{{{NS}}}FctProp").text            = fct
                etree.SubElement(tot, f"{{{NS}}}TotCredIVAUsoComun").text = str(round(t_uc * float(fct)))

            # ═══ FIX T46 (parte 2/2) ═══════════════════════════
            # La retención total se informa con TotOpIVARetTotal + TotIVARetTotal
            # (estos tags pasaron el schema del SII en el envío 252601730,
            # que fue LOK - Cuadrado). Se reemplaza TotImpSinCredito, que era
            # parte de las combinaciones que el set rechazó.
            t_ret = sum(d.get("iva_ret_total", 0) for d in docs_t)
            if t_ret:
                etree.SubElement(tot, f"{{{NS}}}TotOpIVARetTotal").text = str(sum(1 for d in docs_t if d.get("iva_ret_total", 0)))
                etree.SubElement(tot, f"{{{NS}}}TotIVARetTotal").text   = str(t_ret)

            etree.SubElement(tot, f"{{{NS}}}TotMntTotal").text = str(sum(d["total"] for d in docs_t))

    # ── Detalle ───────────────────────────────────────────────
    for doc in docs:
        det = etree.SubElement(envio, f"{{{NS}}}Detalle")

        if es_guias:
            etree.SubElement(det, f"{{{NS}}}Folio").text = str(doc["folio"])
            if doc.get("anulado"):
                # Guía anulada: solo Folio + Anulado=2 + FchDoc, SIN montos
                # (igual que el libro aprobado 4841547)
                etree.SubElement(det, f"{{{NS}}}Anulado").text = "2"
                etree.SubElement(det, f"{{{NS}}}FchDoc").text = doc["fecha"]
            else:
                tpo_oper = doc.get("ind_traslado") or 1
                etree.SubElement(det, f"{{{NS}}}TpoOper").text = str(tpo_oper)
                etree.SubElement(det, f"{{{NS}}}FchDoc").text = doc["fecha"]
                if doc["rut"]:
                    etree.SubElement(det, f"{{{NS}}}RUTDoc").text = doc["rut"]
                if doc["razon"]:
                    etree.SubElement(det, f"{{{NS}}}RznSoc").text = doc["razon"]
                if doc["neto"] != 0:
                    etree.SubElement(det, f"{{{NS}}}MntNeto").text = str(doc["neto"])
                if doc["iva"] != 0:
                    etree.SubElement(det, f"{{{NS}}}TasaImp").text = "19"
                    etree.SubElement(det, f"{{{NS}}}IVA").text     = str(doc["iva"])
                if doc["exe"] != 0:
                    etree.SubElement(det, f"{{{NS}}}MntExe").text  = str(doc["exe"])
                etree.SubElement(det, f"{{{NS}}}MntTotal").text = str(doc["total"])
        else:
            etree.SubElement(det, f"{{{NS}}}TpoDoc").text  = str(doc["tipo"])
            etree.SubElement(det, f"{{{NS}}}NroDoc").text  = str(doc["folio"])
            etree.SubElement(det, f"{{{NS}}}TasaImp").text = "19"
            etree.SubElement(det, f"{{{NS}}}FchDoc").text  = doc["fecha"]
            etree.SubElement(det, f"{{{NS}}}RUTDoc").text  = doc["rut"]
            etree.SubElement(det, f"{{{NS}}}RznSoc").text  = doc["razon"]
            if doc["exe"] != 0:
                etree.SubElement(det, f"{{{NS}}}MntExe").text = str(doc["exe"])
            etree.SubElement(det, f"{{{NS}}}MntNeto").text = str(doc["neto"])
            te = doc.get("tipo_especial", "")
            if te == "iva_uso_comun":
                # Validado por SII sin reparo: MntIVA=0, monto en IVAUsoComun
                etree.SubElement(det, f"{{{NS}}}MntIVA").text      = "0"
                etree.SubElement(det, f"{{{NS}}}IVAUsoComun").text = str(doc["iva_uso_comun"])
            elif te == "iva_no_rec":
                # Validado por SII sin reparo: MntIVA=0, monto en IVANoRec
                etree.SubElement(det, f"{{{NS}}}MntIVA").text = "0"
                inr = etree.SubElement(det, f"{{{NS}}}IVANoRec")
                etree.SubElement(inr, f"{{{NS}}}CodIVANoRec").text = str(doc.get("cod_iva_no_rec", 9))
                etree.SubElement(inr, f"{{{NS}}}MntIVANoRec").text = str(doc["iva_no_rec"])
            elif te == "iva_ret_total":
                # ═══ FIX T46 ═══════════════════════════════════
                # El SII exige MntIVA = MntNeto × TasaImp SIEMPRE (reparo
                # LBR-2 si va en 0). La retención se informa ADEMÁS en
                # IVARetTotal. El MntTotal sigue siendo solo el neto
                # (pasó sin reparo): el IVA se declara pero no se paga
                # al proveedor porque el comprador lo retiene.
                etree.SubElement(det, f"{{{NS}}}MntIVA").text      = str(doc["iva"])
                etree.SubElement(det, f"{{{NS}}}IVARetTotal").text = str(doc["iva_ret_total"])
            else:
                # Normal o T56/T61: siempre emitir MntIVA
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

@router.post("/generar-xml", summary="Genera libro (ventas/compras/guías) dinámico desde DTEs en BD")
async def generar_libro_dinamico(
    emisor_id:  int,
    tipo_libro: str,
    natencion:  str,
    periodo:    Optional[str] = None,
    fch_resol:  Optional[str] = None,
    nro_resol:  Optional[str] = None,
    cod_aut_rec: Optional[str] = None,   # Rectificación: código de reemplazo del SII
    db: AsyncSession = Depends(get_db),
):
    tipo_libro = tipo_libro.lower().strip()
    if tipo_libro not in ("ventas", "compras", "guias"):
        raise HTTPException(400, "tipo_libro debe ser: ventas | compras | guias")

    if not periodo:
        hoy = date.today()
        periodo = f"{hoy.year}-{hoy.month:02d}"

    emisor, cert = await _get_emisor_y_cert(emisor_id, db)

    tipos_map = {"ventas": TIPOS_VENTAS, "compras": TIPOS_COMPRAS, "guias": TIPOS_GUIAS}
    tipos = tipos_map[tipo_libro]

    dtes = await _obtener_dtes(emisor_id, tipos, periodo, db)

    if not dtes:
        raise HTTPException(404,
            f"No hay DTEs de tipo {tipos} para el emisor {emisor_id} "
            f"en el período {periodo}. Genera primero los sets de prueba."
        )

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

    xml_str = _construir_libro_xml(
        emisor    = emisor,
        dtes      = dtes,
        tipo_libro = tipo_op,
        tipo_envio_id = libro_id,
        natencion  = natencion,
        periodo    = periodo,
        fch_resol  = fch_resol or "2026-04-19",
        nro_resol  = nro_resol or "0",
        rut_envia  = cert.rut_firmante or emisor.rut,
        cod_aut_rec = cod_aut_rec,
    )

    firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    try:
        xml_firmado = await firma.firmar_libro(xml_str)
    except Exception as e:
        raise HTTPException(500, f"Error al firmar el libro: {e}")

    # Validación XSD post-firma (informativa, no bloquea)
    try:
        from app.services.xsd_validator import validar_xml
        r_val = validar_xml(xml_firmado.encode("ISO-8859-1"))
        if not r_val.valido:
            logger.warning(
                f"Libro {tipo_libro} N°{natencion} NO pasa XSD: "
                + " | ".join(r_val.errores[:3])
            )
        else:
            logger.info(f"Libro {tipo_libro} N°{natencion} XSD OK ({r_val.schema_usado})")
    except Exception as _ve:
        logger.debug(f"Validacion XSD omitida: {_ve}")

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

        # Campos específicos de Guías de Despacho — necesarios para LibroGuías
        ind_traslado   = doc.findtext(f'.//{{{NS}}}IndTraslado')
        tipo_despacho  = doc.findtext(f'.//{{{NS}}}TipoDespacho')

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
            "ind_traslado":   int(ind_traslado) if ind_traslado else None,
            "tipo_despacho":  int(tipo_despacho) if tipo_despacho else None,
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
        self.monto_exe       = d.get("monto_exe",   0)   # exento real (no recalcular)
        self.estado          = "ACEPTADO"
        self.ambiente        = "certificacion"
        self.anulado         = bool(d.get("anulado", False))
        self.ind_traslado    = d.get("ind_traslado")
        self.tipo_despacho   = d.get("tipo_despacho")
        # Campos especiales IVA para LibroCompras
        self.tipo_especial   = d.get("tipo_especial", "")   # iva_uso_comun | iva_no_rec | iva_ret_total
        self.iva_uso_comun   = d.get("iva_uso_comun",  0)
        self.fct_prop        = d.get("fct_prop",  "0.60")
        self.iva_no_rec      = d.get("iva_no_rec",     0)
        self.cod_iva_no_rec  = d.get("cod_iva_no_rec", 9)
        self.iva_ret_total   = d.get("iva_ret_total",  0)


@router.post("/desde-xml", summary="Genera libro desde XMLs de EnvioDTE (sin BD)")
async def generar_libro_desde_xml(
    emisor_id:  int           = Form(...),
    tipo_libro: str           = Form(...),
    natencion:  str           = Form(...),
    periodo:    str           = Form(...),
    archivos:   list[UploadFile] = File(...),
    fch_resol:  str           = Form("2026-04-19"),
    nro_resol:  str           = Form("0"),
    folios_anulados: str      = Form(""),
    cod_aut_rec: str          = Form(""),   # Rectificación: código de reemplazo del SII
    db: AsyncSession = Depends(get_db),
):
    if tipo_libro not in ("ventas", "compras", "guias"):
        raise HTTPException(400, "tipo_libro debe ser: ventas | compras | guias")

    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(404, f"Emisor {emisor_id} no encontrado")

    cert_result = await db.execute(
        select(Certificado).where(Certificado.emisor_id == emisor_id)
    )
    cert = cert_result.scalars().first()
    if not cert:
        raise HTTPException(400, "Sin certificado .p12 cargado")

    # Parsear folios anulados (LibroGuías: "76,77" → {76, 77})
    folios_anulados_set: set[int] = set()
    for f in (folios_anulados or "").split(","):
        f = f.strip()
        if f.isdigit():
            folios_anulados_set.add(int(f))

    # Parsear todos los XMLs subidos, deduplicando por (tipo, folio)
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
            if key not in folios_vistos:
                folios_vistos.add(key)
                d["anulado"] = d["folio"] in folios_anulados_set
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
        rut_envia     = cert.rut_firmante or emisor.rut,
        cod_aut_rec   = cod_aut_rec.strip() or None,
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


class DTEManualInput(BaseModel):
    tipo_dte:        int
    folio:           int
    fecha_emision:   str           # AAAA-MM-DD
    rut_receptor:    str   = "66666666-6"
    nombre_receptor: str   = "Proveedor"
    monto_neto:      int   = 0
    monto_iva:       int   = 0
    monto_total:     int   = 0
    monto_exe:       int   = 0
    # Campos especiales de IVA para LibroCompras
    tipo_especial:   str   = ""    # iva_uso_comun | iva_no_rec | iva_ret_total | ""
    iva_uso_comun:   int   = 0
    fct_prop:        str   = "0.60"
    iva_no_rec:      int   = 0
    # Código de IVA no recuperable (4 = entrega gratuita recibida, 9 = otros)
    cod_iva_no_rec:  int   = 9
    iva_ret_total:   int   = 0

class LibroManualRequest(BaseModel):
    emisor_id:  int
    tipo_libro: str          # ventas | compras | guias
    natencion:  str
    periodo:    str          # AAAA-MM
    dtes:       list[DTEManualInput]
    fch_resol:  str  = "2026-04-19"
    nro_resol:  str  = "0"
    # Rectificación: si el período ya está cerrado (LTC), pedir el código
    # de reemplazo en maullin.sii.cl e informarlo aquí → carátula RECTIFICA
    cod_aut_rec: Optional[str] = None

@router.post("/manual", summary="Genera libro desde datos manuales (para LibroCompras de certificación)")
async def generar_libro_manual(
    body: LibroManualRequest,
    db: AsyncSession = Depends(get_db),
):
    if body.tipo_libro not in ("ventas", "compras", "guias"):
        raise HTTPException(400, "tipo_libro debe ser: ventas | compras | guias")

    emisor, cert = await _get_emisor_y_cert(body.emisor_id, db)

    # Convertir DTEManualInput en _DTEFake para reutilizar _construir_libro_xml
    todos_dtes = []
    for d in body.dtes:
        fake = _DTEFake({
            "tipo_dte":        d.tipo_dte,
            "folio":           d.folio,
            "fecha_emision":   d.fecha_emision,
            "rut_receptor":    d.rut_receptor,
            "nombre_receptor": d.nombre_receptor,
            "monto_neto":      d.monto_neto,
            "monto_iva":       d.monto_iva,
            "monto_total":     d.monto_total,
            "monto_exe":       d.monto_exe,
            # Campos especiales IVA — críticos para LibroCompras
            "tipo_especial":   d.tipo_especial,
            "iva_uso_comun":   d.iva_uso_comun,
            "fct_prop":        d.fct_prop,
            "iva_no_rec":      d.iva_no_rec,
            "cod_iva_no_rec":  d.cod_iva_no_rec,
            "iva_ret_total":   d.iva_ret_total,
        })
        todos_dtes.append(fake)

    todos_dtes.sort(key=lambda x: (x.tipo_dte, x.folio))

    libro_meta = {
        "ventas":  ("VENTA",  "LibroVentas",  "Ventas"),
        "compras": ("COMPRA", "LibroCompras", "Compras"),
        "guias":   ("VENTA",  "LibroGuias",   "Guías"),
    }
    tipo_op, libro_id, label = libro_meta[body.tipo_libro]

    logger.info(
        f"[LIBRO MANUAL] {label} emisor={emisor.rut} "
        f"natencion={body.natencion} dtes={len(todos_dtes)} "
        f"rectifica={'SI' if body.cod_aut_rec else 'NO'}"
    )

    xml_str = _construir_libro_xml(
        emisor        = emisor,
        dtes          = todos_dtes,
        tipo_libro    = tipo_op,
        tipo_envio_id = libro_id,
        natencion     = body.natencion,
        periodo       = body.periodo,
        fch_resol     = body.fch_resol,
        nro_resol     = body.nro_resol,
        rut_envia     = cert.rut_firmante or emisor.rut,
        cod_aut_rec   = body.cod_aut_rec,
    )

    firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    try:
        xml_firmado = await firma.firmar_libro(xml_str)
    except Exception as e:
        raise HTTPException(500, f"Error al firmar el libro: {e}")

    rut_limpio = emisor.rut.replace(".", "").replace("-", "")
    nombre = f"Libro{body.tipo_libro.capitalize()}_{body.natencion}_{rut_limpio}_{body.periodo}.xml"

    return Response(
        content    = xml_firmado.encode("ISO-8859-1"),
        media_type = "application/octet-stream",
        headers    = {
            "Content-Disposition": f'attachment; filename="{nombre}"',
            "X-NroAtencion":       body.natencion,
            "X-Periodo":           body.periodo,
            "X-TipoLibro":         body.tipo_libro,
            "X-DTEs-Incluidos":    str(len(todos_dtes)),
        },
    )


@router.post("/preview-manual", summary="Preview sin firma del libro construido desde datos manuales")
async def preview_libro_manual(
    body: LibroManualRequest,
    db: AsyncSession = Depends(get_db),
):
    """Retorna XML sin firmar para inspección previa al envío."""
    emisor, cert = await _get_emisor_y_cert(body.emisor_id, db)

    todos_dtes = [_DTEFake({
        "tipo_dte": d.tipo_dte, "folio": d.folio,
        "fecha_emision": d.fecha_emision, "rut_receptor": d.rut_receptor,
        "nombre_receptor": d.nombre_receptor, "monto_neto": d.monto_neto,
        "monto_iva": d.monto_iva, "monto_total": d.monto_total, "monto_exe": d.monto_exe,
        # Campos especiales IVA — críticos para LibroCompras
        "tipo_especial": d.tipo_especial, "iva_uso_comun": d.iva_uso_comun,
        "fct_prop": d.fct_prop, "iva_no_rec": d.iva_no_rec,
        "cod_iva_no_rec": d.cod_iva_no_rec, "iva_ret_total": d.iva_ret_total,
    }) for d in body.dtes]
    todos_dtes.sort(key=lambda x: (x.tipo_dte, x.folio))

    libro_meta = {"ventas": ("VENTA","LibroVentas"), "compras": ("COMPRA","LibroCompras"), "guias": ("VENTA","LibroGuias")}
    tipo_op, libro_id = libro_meta.get(body.tipo_libro, ("VENTA", "LibroVentas"))

    xml_str = _construir_libro_xml(
        emisor=emisor, dtes=todos_dtes, tipo_libro=tipo_op, tipo_envio_id=libro_id,
        natencion=body.natencion, periodo=body.periodo,
        fch_resol=body.fch_resol, nro_resol=body.nro_resol,
        rut_envia=cert.rut_firmante or emisor.rut,
        cod_aut_rec=body.cod_aut_rec,
    )

    return Response(
        content    = xml_str.encode("UTF-8"),
        media_type = "application/xml",
        headers    = {"Content-Disposition": f'inline; filename="preview_{body.tipo_libro}_{body.natencion}.xml"'},
    )


@router.post("/preview", summary="Preview del libro XML sin firmar", response_class=Response)
async def preview_libro_xml(
    emisor_id:  int           = Form(...),
    tipo_libro: str           = Form(...),
    natencion:  str           = Form(...),
    periodo:    str           = Form(...),
    archivos:   list[UploadFile] = File(...),
    fch_resol:  str           = Form("2026-04-19"),
    nro_resol:  str           = Form("0"),
    folios_anulados: str      = Form(""),
    cod_aut_rec: str          = Form(""),   # Rectificación: código de reemplazo del SII
    db: AsyncSession = Depends(get_db),
):
    if tipo_libro not in ("ventas", "compras", "guias"):
        raise HTTPException(400, "tipo_libro debe ser: ventas | compras | guias")

    emisor, cert = await _get_emisor_y_cert(emisor_id, db)

    folios_anulados_set: set[int] = {
        int(f.strip()) for f in (folios_anulados or "").split(",")
        if f.strip().isdigit()
    }

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
            if key not in folios_vistos:
                folios_vistos.add(key)
                d["anulado"] = d["folio"] in folios_anulados_set
                todos_dtes.append(_DTEFake(d))

    if not todos_dtes:
        raise HTTPException(404, "No se encontraron DTEs válidos en los XMLs subidos")

    todos_dtes.sort(key=lambda x: (x.tipo_dte, x.folio))
    libro_meta = {
        "ventas":  ("VENTA",  "LibroVentas",  "Ventas"),
        "compras": ("COMPRA", "LibroCompras", "Compras"),
        "guias":   ("VENTA",  "LibroGuias",   "Guías"),
    }
    tipo_op, libro_id, _ = libro_meta[tipo_libro]

    xml_str = _construir_libro_xml(
        emisor        = emisor,
        dtes          = todos_dtes,
        tipo_libro    = tipo_op,
        tipo_envio_id = libro_id,
        natencion     = natencion,
        periodo       = periodo,
        fch_resol     = fch_resol,
        nro_resol     = nro_resol,
        rut_envia     = cert.rut_firmante or emisor.rut,
        cod_aut_rec   = cod_aut_rec.strip() or None,
    )

    return Response(
        content    = xml_str.encode("UTF-8"),
        media_type = "application/xml",
        headers    = {
            "Content-Disposition": f'inline; filename="preview_{tipo_libro}_{natencion}.xml"',
            "X-Preview": "unsigned",
        },
    )
