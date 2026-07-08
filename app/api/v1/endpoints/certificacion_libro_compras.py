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
    # Set 4919743. Montos exactos del TXT del SII.
    {"tipo": 30, "folio": 234, "fecha": "2026-05-22", "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
     "neto": 18269, "exe": 0, "iva": _iva(18269), "total": 18269 + _iva(18269), "tipo_especial": None},

    {"tipo": 33, "folio": 32, "fecha": "2026-05-22", "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
     "neto": 6059, "exe": 8674, "iva": _iva(6059), "total": 6059 + _iva(6059) + 8674, "tipo_especial": None},

    {"tipo": 30, "folio": 781, "fecha": "2026-05-22", "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
     "neto": 29749, "exe": 0, "iva": 0, "iva_uso_comun": _iva(29749),
     "total": 29749 + _iva(29749), "tipo_especial": "iva_uso_comun"},

    {"tipo": 60, "folio": 451, "fecha": "2026-05-22", "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
     "neto": 2699, "exe": 0, "iva": _iva(2699), "total": 2699 + _iva(2699), "tipo_especial": None},

    {"tipo": 33, "folio": 67, "fecha": "2026-05-22", "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
     "neto": 9826, "exe": 0, "iva": 0, "iva_no_rec": _iva(9826), "cod_iva_no_rec": 4,
     "total": 9826 + _iva(9826), "tipo_especial": "iva_no_rec"},

    {"tipo": 46, "folio": 9, "fecha": "2026-05-22", "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
     "neto": 9474, "exe": 0, "iva": _iva(9474), "iva_ret_total": _iva(9474),
     "otro_imp_cod": 40, "otro_imp_tasa": 19, "otro_imp_monto": _iva(9474),
     "total": 9474, "tipo_especial": "iva_ret_total"},

    {"tipo": 60, "folio": 211, "fecha": "2026-05-22", "rut_doc": RUT_PROV, "razon": "PROVEEDOR SA",
     "neto": 4030, "exe": 0, "iva": _iva(4030), "total": 4030 + _iva(4030), "tipo_especial": None},
]

def _construir_libro_xml(emisor: Emisor, rut_envia: str, natencion: str,
                          periodo: str, tmst: str, fch_resol: str = "2026-04-19",
                          docs_override=None, cod_aut_rec: str | None = None) -> str:
    # El período tributario DEBE corresponder al mes de los documentos del
    # libro, no al mes en que se genera. Los documentos del set son de mayo
    # (2026-05-22), así que derivamos el período de su fecha y NO del parámetro
    # (que llega con el mes actual). Si el período del SII no calza con las
    # fechas de los documentos, el libro se repara. Analogía: el libro de mayo
    # lleva la fecha de mayo aunque lo armes en junio.
    docs = docs_override if docs_override is not None else DOCUMENTOS
    if docs:
        fecha_doc = docs[0].get("fecha", "")
        if len(fecha_doc) >= 7:
            periodo = fecha_doc[:7]
    root = etree.Element(f"{{{NS}}}LibroCompraVenta",
        nsmap={None: NS, "xsi": "http://www.w3.org/2001/XMLSchema-instance"},
        attrib={"version": "1.0",
                "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation":
                    f"{NS} LibroCV_v10.xsd"})
    envio = etree.SubElement(root, f"{{{NS}}}EnvioLibro")
    envio.set("ID", "LibroCompras")

    car = etree.SubElement(envio, f"{{{NS}}}Caratula")
    _limpiar = lambda r: r.replace(".", "").strip() if r else r
    etree.SubElement(car, f"{{{NS}}}RutEmisorLibro").text   = _limpiar(emisor.rut)
    etree.SubElement(car, f"{{{NS}}}RutEnvia").text          = _limpiar(rut_envia)
    etree.SubElement(car, f"{{{NS}}}PeriodoTributario").text = periodo
    etree.SubElement(car, f"{{{NS}}}FchResol").text          = fch_resol
    etree.SubElement(car, f"{{{NS}}}NroResol").text          = "0"
    etree.SubElement(car, f"{{{NS}}}TipoOperacion").text     = "COMPRA"
    # FIX REEMPLAZO (2026-07-07): la propia confirmación del SII al
    # entregar el CodAutRec dice literalmente "Tipo: ESPECIAL" — el
    # código de reemplazo NO cambia el TipoLibro a RECTIFICA, solo
    # autoriza reenviar un libro ESPECIAL que ya está "Cuadrado". Es
    # como un duplicado de entrada al cine: sigue siendo la misma
    # función (ESPECIAL), solo trae un sello de reemplazo (CodAutRec)
    # pegado encima. TipoLibro se mantiene ESPECIAL siempre; lo único
    # que cambia es si <CodAutRec> viene o no.
    etree.SubElement(car, f"{{{NS}}}TipoLibro").text = "ESPECIAL"
    etree.SubElement(car, f"{{{NS}}}TipoEnvio").text         = "TOTAL"
    etree.SubElement(car, f"{{{NS}}}FolioNotificacion").text = natencion
    if cod_aut_rec:
        # CodAutRec va AL FINAL de la Carátula según el <xs:sequence> del
        # XSD oficial (después de FolioNotificacion) — igual que con
        # OtrosImp/IVARetTotal, el orden en XML es parte del contrato.
        etree.SubElement(car, f"{{{NS}}}CodAutRec").text = cod_aut_rec

    resumen = etree.SubElement(envio, f"{{{NS}}}ResumenPeriodo")
    # FIX REPARO "Numero de Lineas de Resumen No Cuadra" (2026-07-07):
    # antes agrupábamos SOLO por Tipo de Documento, mezclando en una
    # misma línea de resumen documentos normales con documentos de casos
    # especiales del MISMO tipo (ej: Folio 234, Factura normal Tipo 30,
    # se sumaba junto con Folio 781, Factura CON IVA Uso Común, también
    # Tipo 30). El problema: el "Factor de Proporcionalidad" del IVA Uso
    # Común aplica solo a ESA porción — mezclarla con una factura normal
    # en la misma línea de resumen no tiene sentido matemático.
    #
    # Analogía: es como sumar en una sola casilla del balance las ventas
    # "de contado" y las "a crédito con descuento" solo porque ambas son
    # facturas — cada una necesita su propia fila, aunque compartan el
    # mismo tipo de documento.
    #
    # Ahora agrupamos por (tipo, tipo_especial): cada caso especial recibe
    # su PROPIA línea de resumen, separada de los documentos normales del
    # mismo tipo. El orden de clasificación pone primero los "normales"
    # (tipo_especial=None) y luego los especiales, para mantener un orden
    # estable y predecible.
    claves = sorted(
        set((d["tipo"], d.get("tipo_especial")) for d in docs),
        key=lambda k: (k[0], k[1] or "")
    )
    for tipo_doc, te_grupo in claves:
        dt = [d for d in docs if d["tipo"] == tipo_doc and d.get("tipo_especial") == te_grupo]
        tot = etree.SubElement(resumen, f"{{{NS}}}TotalesPeriodo")
        etree.SubElement(tot, f"{{{NS}}}TpoDoc").text     = str(tipo_doc)
        etree.SubElement(tot, f"{{{NS}}}TotDoc").text     = str(len(dt))
        etree.SubElement(tot, f"{{{NS}}}TotMntExe").text  = str(sum(d["exe"] for d in dt))
        etree.SubElement(tot, f"{{{NS}}}TotMntNeto").text = str(sum(d["neto"] for d in dt))

        # FIX REPARO LBR-3 (2026-07-06): TotMntIVA debe coincidir con la
        # suma de MntIVA que REALMENTE aparece en cada Detalle — no con una
        # regla genérica de "todo tipo_especial se excluye".
        #
        # Analogía: el Resumen es una caja que suma boleta por boleta; si
        # una boleta cambia de monto, hay que sumarla de nuevo con el
        # monto nuevo, no seguir tratándola como si aportara $0 porque
        # antes aportaba $0.
        #
        # Solo DOS de los tres casos especiales llevan MntIVA=0 en el
        # detalle (iva_uso_comun, iva_no_rec) → esos SÍ se excluyen.
        # iva_ret_total (T46) ya NO va en 0 (fix del 2026-07-06: el SII
        # exige MntIVA = Neto×19% siempre) → debe SUMARSE igual que un
        # documento normal.
        _tot_mnt_iva = sum(
            d["iva"] for d in dt
            if d.get("tipo_especial") not in ("iva_uso_comun", "iva_no_rec")
        )
        etree.SubElement(tot, f"{{{NS}}}TotMntIVA").text  = str(_tot_mnt_iva)

        # FIX REPARO 1: TotIVANoRec informa el IVA no recuperable por separado
        t_nr = sum(d.get("iva_no_rec", 0) for d in dt)
        if t_nr:
            inr = etree.SubElement(tot, f"{{{NS}}}TotIVANoRec")
            # Código de IVA no recuperable del documento (4 = entrega gratuita
            # recibida, para la factura 67 del set). Se toma del dict, no fijo.
            cod_nr = next(d.get("cod_iva_no_rec", 1) for d in dt if d.get("iva_no_rec", 0))
            etree.SubElement(inr, f"{{{NS}}}CodIVANoRec").text    = str(cod_nr)
            etree.SubElement(inr, f"{{{NS}}}TotOpIVANoRec").text  = str(sum(1 for d in dt if d.get("iva_no_rec", 0)))
            etree.SubElement(inr, f"{{{NS}}}TotMntIVANoRec").text = str(t_nr)

        t_uc = sum(d.get("iva_uso_comun", 0) for d in dt)
        if t_uc:
            etree.SubElement(tot, f"{{{NS}}}TotIVAUsoComun").text    = str(t_uc)
            etree.SubElement(tot, f"{{{NS}}}FctProp").text            = FCT_PROP
            etree.SubElement(tot, f"{{{NS}}}TotCredIVAUsoComun").text = str(round(t_uc * float(FCT_PROP)))

        # FIX ESQUEMA (2026-07-06): en XML, a diferencia de un diccionario
        # de Python, el ORDEN de los elementos importa cuando el XSD define
        # una <xs:sequence> — es como una fila para el banco: si te saltas
        # el orden de los números, no importa que lleves el papel correcto,
        # igual te rechazan. El XSD exige TotOtrosImp ANTES que
        # TotOpIVARetTotal/TotIVARetTotal — nosotros los escribíamos al
        # revés, y eso causó el rechazo "ESQUEMA INVALIDO".
        t_otro = sum(d.get("otro_imp_monto", 0) for d in dt if d.get("tipo_especial") == "iva_ret_total")
        if t_otro:
            toi = etree.SubElement(tot, f"{{{NS}}}TotOtrosImp")
            etree.SubElement(toi, f"{{{NS}}}CodImp").text    = "40"
            etree.SubElement(toi, f"{{{NS}}}TotMntImp").text = str(t_otro)
            etree.SubElement(toi, f"{{{NS}}}TotCredImp").text = str(t_otro)

        # Va DESPUÉS de TotOtrosImp en la secuencia del XSD (confirmado
        # arriba). Mismo dato, solo cambia dónde se escribe en el XML.
        t_ret = sum(d.get("iva_ret_total", 0) for d in dt)
        if t_ret:
            etree.SubElement(tot, f"{{{NS}}}TotOpIVARetTotal").text = str(sum(1 for d in dt if d.get("iva_ret_total", 0)))
            etree.SubElement(tot, f"{{{NS}}}TotIVARetTotal").text   = str(t_ret)

        etree.SubElement(tot, f"{{{NS}}}TotMntTotal").text = str(sum(d["total"] for d in dt))

    for doc in docs:
        det = etree.SubElement(envio, f"{{{NS}}}Detalle")
        etree.SubElement(det, f"{{{NS}}}TpoDoc").text  = str(doc["tipo"])
        etree.SubElement(det, f"{{{NS}}}NroDoc").text  = str(doc["folio"])
        etree.SubElement(det, f"{{{NS}}}TasaImp").text = "19"
        etree.SubElement(det, f"{{{NS}}}FchDoc").text  = doc["fecha"]
        etree.SubElement(det, f"{{{NS}}}RUTDoc").text  = doc["rut_doc"]
        etree.SubElement(det, f"{{{NS}}}RznSoc").text  = doc["razon"][:50]
        # FIX DOCREF (2026-07-08): <TpoDocRef>/<FolioDocRef> van EXACTAMENTE
        # aquí según la secuencia del XSD (después de RznSoc, antes de los
        # montos) — verificado línea por línea contra LibroCV_v10.xsd.
        # Sirven para que una Nota de Crédito diga a qué factura descuenta
        # (ej. Folio 451 descuenta la Factura 234). Son opcionales
        # (minOccurs=0): si el documento no referencia nada, simplemente
        # no se escriben, sin afectar la validez del esquema.
        if doc.get("tipo_doc_ref") and doc.get("folio_doc_ref"):
            etree.SubElement(det, f"{{{NS}}}TpoDocRef").text   = str(doc["tipo_doc_ref"])
            etree.SubElement(det, f"{{{NS}}}FolioDocRef").text = str(doc["folio_doc_ref"])
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
            # FIX ESQUEMA (2026-07-06): el envío volvió "RECHAZADO ESQUEMA
            # INVALIDO". Revisando el XSD oficial línea por línea, el
            # <xs:sequence> exige este orden exacto: ... Ley18211,
            # OtrosImp, MntSinCred, IVARetTotal, IVARetParcial ... —
            # es decir, <OtrosImp> va ANTES de <IVARetTotal>, y nosotros
            # los escribíamos al revés. En XML el orden es parte del
            # contrato (a diferencia de un dict en Python, donde el orden
            # de las llaves no importa) — por eso pasaba de ser un
            # "reparo de contenido" a un rechazo de forma.
            etree.SubElement(det, f"{{{NS}}}MntIVA").text = str(doc["iva"])
            oi = etree.SubElement(det, f"{{{NS}}}OtrosImp")
            etree.SubElement(oi, f"{{{NS}}}CodImp").text  = str(doc["otro_imp_cod"])
            etree.SubElement(oi, f"{{{NS}}}TasaImp").text = str(doc["otro_imp_tasa"])
            etree.SubElement(oi, f"{{{NS}}}MntImp").text  = str(doc["otro_imp_monto"])
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
    natencion: Optional[str] = "4919743",
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
