# app/services/xml_builder.py
# ══════════════════════════════════════════════════════════════════
# FIXES aplicados en este archivo:
#   [FIX-1] PrcItem=0 explícito cuando precio=0 (todos los tipos)
#           Antes: solo T52 tenía elif, NC/ND con monto=0 no enviaban PrcItem
#           → SII rechazaba: "Los Valores de la Linea X del Detalle No Cuadran"
#   [FIX-2] QtyItem sin decimales cuando cantidad es entera
#           Antes: siempre f"{cantidad:.2f}" → "80.00", "1.00"
#           Ahora: "80", "1" (o "1.5" si genuinamente decimal)
# ══════════════════════════════════════════════════════════════════

from lxml import etree
from datetime import date, datetime, timezone
from dataclasses import dataclass, field

SII_NS = "http://www.sii.cl/SiiDte"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

TIPOS_BOLETA         = {39, 41}
TIPOS_FACTURA_EXENTA = {34}


@dataclass
class EmisorDTE:
    rut: str
    razon_social: str
    giro: str
    direccion: str
    comuna: str
    ciudad: str
    telefono: str = ""
    correo: str = ""
    acteco: str = "620200"


@dataclass
class ReceptorDTE:
    rut: str = "66666666-6"
    razon_social: str = "Consumidor Final"
    giro: str = ""
    direccion: str = ""
    comuna: str = ""
    ciudad: str = ""
    correo: str = ""


@dataclass
class ItemDTE:
    nombre: str
    cantidad: float = 1.0
    precio_unitario: float = 0.0
    descuento_pct: float = 0.0
    codigo: str = ""
    unidad: str = ""
    exento: bool = False

    @property
    def monto_item(self) -> int:
        bruto = self.cantidad * self.precio_unitario
        return round(bruto - bruto * (self.descuento_pct / 100))


@dataclass
class ReferenciaDTE:
    tipo_doc_ref: "str | int"
    folio_ref: "int | str"
    fecha_ref: date
    razon_ref: str = ""
    cod_ref: "str | int" = 0


@dataclass
class InputDTE:
    tipo_dte: int
    folio: int
    fecha_emision: date
    emisor: EmisorDTE
    receptor: ReceptorDTE
    items: list
    ambiente: str = "certificacion"
    referencias: list = field(default_factory=list)
    forma_pago: int = 1
    indicador_traslado: int = 0
    indicador_despacho: int = 0
    observacion: str = ""
    descuento_global_pct: float = 0.0
    descuento_global_monto: int = 0
    forzar_monto_cero: bool = False
    # Descuento global: glosa y alcance
    # glosa_descuento: texto que va en GlosaDR — el SII usa "Porcentaje Variable"
    # desc_global_solo_afectos: True = aplica solo a ítems afectos (sin IndExeDR)
    #                           False = aplica a TODOS los ítems incluyendo exentos (IndExeDR=1)
    glosa_descuento: str = "Porcentaje Variable"
    desc_global_solo_afectos: bool = True


def _reparar_mojibake(texto: str) -> str:
    """
    Repara texto doble-codificado (mojibake). Cuando bytes UTF-8 se leen como
    Latin-1, "ó" (UTF-8: C3 B3) aparece como "Ã³" (dos caracteres). Esto pasa
    cuando en algún punto de la cadena se hizo decode/encode con el encoding
    equivocado. Aquí lo deshacemos: re-codificamos a Latin-1 y decodificamos
    como UTF-8, recuperando el carácter original.
    Analogía: alguien tradujo mal del español al inglés y de vuelta; esto
    revierte ese viaje y recupera la palabra original.
    """
    # Marcadores típicos de mojibake (Ã seguido de otro símbolo)
    if 'Ã' not in texto and 'Â' not in texto:
        return texto  # no hay señales de doble-codificación
    try:
        # El texto tiene caracteres que, re-codificados a Latin-1, dan los
        # bytes UTF-8 originales; decodificarlos como UTF-8 los repara.
        reparado = texto.encode('latin-1').decode('utf-8')
        return reparado
    except (UnicodeEncodeError, UnicodeDecodeError):
        # Si no se puede reparar limpiamente, dejar el texto como está
        return texto


def _sanitizar_texto(texto: str, largo: int = 80) -> str:
    """
    Elimina caracteres que el SII rechaza en campos de texto.
    NOTA: & NO se reemplaza — lxml lo escapa como &amp; y el SII lo lee como &,
    comparándolo exactamente con el .txt del set de certificación.
    Si se reemplaza & por ' y ', el nombre cambia y el SII rechaza con
    'El Item No Corresponde a lo Especificado'.
    """
    # Primero reparar cualquier doble-codificación (Ã³ → ó) antes de recortar,
    # para que el SII reciba los acentos correctos y no "caracteres especiales".
    texto = _reparar_mojibake(texto)
    reemplazos = {
        # '&': NO → lxml lo maneja como &amp; correctamente
        "'": '',   # comilla simple
        '"': '',   # comilla doble
        '#': '',   # gato
    }
    resultado = texto
    for char, reemplazo in reemplazos.items():
        resultado = resultado.replace(char, reemplazo)
    import re
    resultado = re.sub(r'  +', ' ', resultado).strip()
    return resultado[:largo]


def _fmt_qty(cantidad: float) -> str:
    """
    [FIX-2] Formatea cantidad SIN decimales si es entera.
    Antes: f'{cantidad:.2f}' → '80.00', '1.00' (visual innecesario)
    Ahora: '80', '1' — o '1.50' si genuinamente tiene decimales.
    El XSD del SII acepta enteros en QtyItem.
    """
    # Si la cantidad es entera (80.0 == 80), retornar sin punto decimal
    if cantidad == int(cantidad):
        return str(int(cantidad))
    # Si tiene decimales reales (ej: 2.5 kg), mantenerlos con 2 cifras
    return f"{cantidad:.2f}"


class XMLBuilder:

    NAMESPACE = SII_NS

    def __init__(self, datos: InputDTE):
        self.datos = datos
        self._calcular_totales()

    def _calcular_totales(self):
        tipo  = self.datos.tipo_dte
        items = self.datos.items

        subtotal_afecto = sum(i.monto_item for i in items if not i.exento)
        subtotal_exento = sum(i.monto_item for i in items if i.exento)

        desc = 0
        if self.datos.descuento_global_pct > 0:
            desc = round(subtotal_afecto * self.datos.descuento_global_pct / 100)
        elif self.datos.descuento_global_monto > 0:
            desc = self.datos.descuento_global_monto
        self._desc_global_monto = desc
        monto_afecto = subtotal_afecto - desc

        if tipo in TIPOS_BOLETA:
            if monto_afecto > 0:
                neto = round(monto_afecto / 1.19)
                iva  = monto_afecto - neto
                self.monto_neto = neto
                self.monto_iva  = iva
            else:
                self.monto_neto = 0
                self.monto_iva  = 0
            self.monto_exento = round(subtotal_exento)
            self.monto_total  = round(monto_afecto + subtotal_exento)
        elif tipo in TIPOS_FACTURA_EXENTA:
            self.monto_neto   = 0
            self.monto_iva    = 0
            self.monto_exento = round(monto_afecto + subtotal_exento)
            self.monto_total  = self.monto_exento
        else:
            self.monto_neto   = round(monto_afecto)
            # IVA calculado ítem por ítem (igual que el SII) para evitar diferencias
            # de redondeo de 1 peso cuando hay múltiples ítems afectos.
            # round(sum) puede diferir de sum(round) por acumulación de fracciones.
            items_afectos = [i for i in items if not i.exento]
            if len(items_afectos) > 1 and desc == 0:
                # Sin descuento global: IVA = suma del IVA por ítem
                self.monto_iva = sum(round(i.monto_item * 0.19) for i in items_afectos)
            elif len(items_afectos) > 1 and desc > 0:
                # Con descuento global: aplicar proporción del descuento a cada ítem
                fct = 1 - (desc / subtotal_afecto) if subtotal_afecto else 1
                self.monto_iva = sum(round(i.monto_item * fct * 0.19) for i in items_afectos)
            else:
                self.monto_iva = round(monto_afecto * 0.19)
            self.monto_exento = round(subtotal_exento)
            self.monto_total  = self.monto_neto + self.monto_iva + self.monto_exento

        if self.datos.forzar_monto_cero:
            self.monto_neto   = 0
            self.monto_iva    = 0
            self.monto_exento = 0
            self.monto_total  = 0

    def construir(self) -> bytes:
        d  = self.datos
        NS = self.NAMESPACE

        dte_el = etree.Element(
            f"{{{NS}}}DTE",
            attrib={"version": "1.0"},
            nsmap={None: NS, "xsi": XSI_NS}
        )
        doc_el = etree.SubElement(
            dte_el, f"{{{NS}}}Documento",
            attrib={"ID": f"DTE-{d.tipo_dte}-{d.folio}"}
        )

        self._build_encabezado(doc_el)

        for idx, item in enumerate(d.items, start=1):
            # Filtrar ítems decorativos (solo guiones) que vienen del txt del SII
            if item.nombre and set(item.nombre.strip()) == {'-'}:
                continue
            self._build_detalle(doc_el, item, idx,
                                forzar_monto_cero=d.forzar_monto_cero)

        if self._desc_global_monto > 0:
            dscto = etree.SubElement(doc_el, f"{{{NS}}}DscRcgGlobal")
            etree.SubElement(dscto, f"{{{NS}}}NroLinDR").text = "1"
            etree.SubElement(dscto, f"{{{NS}}}TpoMov").text   = "D"
            # GlosaDR: texto del .txt del SII — por defecto "Porcentaje Variable"
            glosa = _sanitizar_texto(d.glosa_descuento or "Porcentaje Variable", 45)
            etree.SubElement(dscto, f"{{{NS}}}GlosaDR").text  = glosa
            etree.SubElement(dscto, f"{{{NS}}}TpoValor").text = "%"
            # ValorDR sin decimales innecesarios: 24 no 24.00
            pct = d.descuento_global_pct
            etree.SubElement(dscto, f"{{{NS}}}ValorDR").text  = (
                str(int(pct)) if pct == int(pct) else f"{pct:.2f}"
            )
            # IndExeDR=1 solo cuando el descuento aplica también a ítems exentos
            # Si es "ITEMES AFECTOS" (solo afectos), NO se incluye IndExeDR
            if not d.desc_global_solo_afectos:
                etree.SubElement(dscto, f"{{{NS}}}IndExeDR").text = "1"

        for idx, ref in enumerate(d.referencias, start=1):
            self._build_referencia(doc_el, ref, idx)

        ted_el = etree.SubElement(doc_el, f"{{{NS}}}TED", attrib={"version": "1.0"})
        etree.SubElement(ted_el, f"{{{NS}}}DD")

        etree.SubElement(doc_el, f"{{{NS}}}TmstFirma").text = (
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        )

        return etree.tostring(
            dte_el,
            xml_declaration=True,
            encoding="ISO-8859-1",
            pretty_print=False,
        )

    def _build_encabezado(self, parent):
        d         = self.datos
        NS        = self.NAMESPACE
        tipo      = d.tipo_dte
        es_boleta = tipo in TIPOS_BOLETA

        enc   = etree.SubElement(parent, f"{{{NS}}}Encabezado")
        iddoc = etree.SubElement(enc, f"{{{NS}}}IdDoc")

        etree.SubElement(iddoc, f"{{{NS}}}TipoDTE").text = str(tipo)
        etree.SubElement(iddoc, f"{{{NS}}}Folio").text   = str(d.folio)
        etree.SubElement(iddoc, f"{{{NS}}}FchEmis").text = d.fecha_emision.strftime("%Y-%m-%d")

        if tipo == 52:
            if d.indicador_despacho:
                etree.SubElement(iddoc, f"{{{NS}}}TipoDespacho").text = str(d.indicador_despacho)
            etree.SubElement(iddoc, f"{{{NS}}}IndTraslado").text = str(d.indicador_traslado or 1)

        if es_boleta:
            etree.SubElement(iddoc, f"{{{NS}}}IndServicio").text = "3"
        elif tipo in (33, 34):
            etree.SubElement(iddoc, f"{{{NS}}}FmaPago").text = str(d.forma_pago)

        em     = d.emisor
        emisor = etree.SubElement(enc, f"{{{NS}}}Emisor")
        etree.SubElement(emisor, f"{{{NS}}}RUTEmisor").text = em.rut

        if es_boleta:
            etree.SubElement(emisor, f"{{{NS}}}RznSocEmisor").text = (em.razon_social or "").strip()
            etree.SubElement(emisor, f"{{{NS}}}GiroEmisor").text   = em.giro[:80]
        else:
            etree.SubElement(emisor, f"{{{NS}}}RznSoc").text   = (em.razon_social or "").strip()
            etree.SubElement(emisor, f"{{{NS}}}GiroEmis").text = em.giro[:80]
            if em.telefono:
                etree.SubElement(emisor, f"{{{NS}}}Telefono").text     = em.telefono
            if em.correo:
                etree.SubElement(emisor, f"{{{NS}}}CorreoEmisor").text = em.correo
            etree.SubElement(emisor, f"{{{NS}}}Acteco").text = em.acteco or "620200"

        etree.SubElement(emisor, f"{{{NS}}}DirOrigen").text    = (em.direccion or "").strip()
        etree.SubElement(emisor, f"{{{NS}}}CmnaOrigen").text   = (em.comuna or "").strip()
        etree.SubElement(emisor, f"{{{NS}}}CiudadOrigen").text = (em.ciudad or "").strip()

        rc       = d.receptor
        receptor = etree.SubElement(enc, f"{{{NS}}}Receptor")
        etree.SubElement(receptor, f"{{{NS}}}RUTRecep").text    = rc.rut.replace(".", "")
        etree.SubElement(receptor, f"{{{NS}}}RznSocRecep").text = rc.razon_social
        if rc.giro:
            etree.SubElement(receptor, f"{{{NS}}}GiroRecep").text = rc.giro[:40]
        if rc.correo:
            etree.SubElement(receptor, f"{{{NS}}}CorreoRecep").text = rc.correo
        if not es_boleta:
            etree.SubElement(receptor, f"{{{NS}}}DirRecep").text    = rc.direccion or "S/D"
            etree.SubElement(receptor, f"{{{NS}}}CmnaRecep").text   = rc.comuna or "S/C"
            etree.SubElement(receptor, f"{{{NS}}}CiudadRecep").text = rc.ciudad or "S/C"

        totales = etree.SubElement(enc, f"{{{NS}}}Totales")

        if tipo in TIPOS_FACTURA_EXENTA:
            etree.SubElement(totales, f"{{{NS}}}MntExe").text  = str(self.monto_exento)
        elif es_boleta:
            if self.monto_neto > 0:
                etree.SubElement(totales, f"{{{NS}}}MntNeto").text = str(self.monto_neto)
            if self.monto_exento > 0:
                etree.SubElement(totales, f"{{{NS}}}MntExe").text  = str(self.monto_exento)
            if self.monto_iva > 0:
                etree.SubElement(totales, f"{{{NS}}}TasaIVA").text = "19"
                etree.SubElement(totales, f"{{{NS}}}IVA").text     = str(self.monto_iva)
        else:
            if d.forzar_monto_cero:
                pass  # Solo MntTotal=0, sin otros campos
            else:
                # MntNeto solo si hay afectos (NC/ND exenta lo tiene en 0)
                if self.monto_neto > 0:
                    etree.SubElement(totales, f"{{{NS}}}MntNeto").text  = str(self.monto_neto)
                if self.monto_exento > 0:
                    etree.SubElement(totales, f"{{{NS}}}MntExe").text = str(self.monto_exento)
                # TasaIVA e IVA solo cuando hay IVA real — NO para NC/ND de docs exentos
                if self.monto_iva > 0:
                    etree.SubElement(totales, f"{{{NS}}}TasaIVA").text  = "19"
                    etree.SubElement(totales, f"{{{NS}}}IVA").text      = str(self.monto_iva)

        etree.SubElement(totales, f"{{{NS}}}MntTotal").text = str(self.monto_total)

    def _build_detalle(self, parent, item, numero_linea: int, forzar_monto_cero: bool = False):
        NS  = self.NAMESPACE
        det = etree.SubElement(parent, f"{{{NS}}}Detalle")
        etree.SubElement(det, f"{{{NS}}}NroLinDet").text = str(numero_linea)

        # ── CodRef=2 (corrige texto / giro): formato mínimo ──────────────────
        # El SII solo acepta NroLinDet + NmbItem + MontoItem=0.
        # IndExe, QtyItem, PrcItem NO deben ir — el SII los rechaza en este contexto.
        if forzar_monto_cero:
            etree.SubElement(det, f"{{{NS}}}NmbItem").text = _sanitizar_texto(item.nombre, 80)
            etree.SubElement(det, f"{{{NS}}}MontoItem").text = "0"
            return
        # ─────────────────────────────────────────────────────────────────────

        if item.codigo:
            cod = etree.SubElement(det, f"{{{NS}}}CdgItem")
            etree.SubElement(cod, f"{{{NS}}}TpoCodigo").text = "INT1"
            etree.SubElement(cod, f"{{{NS}}}VlrCodigo").text = item.codigo

        # IndExe=1 solo para ítems exentos dentro de documentos AFECTOS (T33, T56, T61)
        # Para T34 (Factura Exenta), el documento completo es exento — IndExe en ítems
        # es redundante y el validador SII lo rechaza con "Los Valores No Cuadran"
        if item.exento and self.datos.tipo_dte not in TIPOS_FACTURA_EXENTA:
            etree.SubElement(det, f"{{{NS}}}IndExe").text = "1"

        etree.SubElement(det, f"{{{NS}}}NmbItem").text = _sanitizar_texto(item.nombre, 80)

        # [FIX-2] QtyItem sin decimales cuando la cantidad es entera
        etree.SubElement(det, f"{{{NS}}}QtyItem").text = _fmt_qty(item.cantidad)

        if item.unidad:
            etree.SubElement(det, f"{{{NS}}}UnmdItem").text = item.unidad

        # PrcItem: solo incluir cuando el precio es mayor que 0
        # El XSD del SII define PrcItem con minInclusive=0.000001 → PrcItem=0 es inválido
        # Para T52 traslado interno (precio=0) y otros casos sin precio → omitir PrcItem
        if round(item.precio_unitario) > 0:
            etree.SubElement(det, f"{{{NS}}}PrcItem").text = str(round(item.precio_unitario))

        if item.descuento_pct > 0:
            etree.SubElement(det, f"{{{NS}}}DescuentoPct").text   = f"{item.descuento_pct:.2f}"
            etree.SubElement(det, f"{{{NS}}}DescuentoMonto").text = str(
                round(item.cantidad * item.precio_unitario * item.descuento_pct / 100)
            )

        etree.SubElement(det, f"{{{NS}}}MontoItem").text = str(item.monto_item)

    def _build_referencia(self, parent, ref, numero: int):
        NS = self.NAMESPACE
        es_set = str(ref.tipo_doc_ref).upper() == "SET"

        r = etree.SubElement(parent, f"{{{NS}}}Referencia")
        etree.SubElement(r, f"{{{NS}}}NroLinRef").text = str(numero)
        etree.SubElement(r, f"{{{NS}}}TpoDocRef").text = str(ref.tipo_doc_ref)

        if es_set:
            etree.SubElement(r, f"{{{NS}}}FolioRef").text = str(ref.folio_ref)
            fecha_ref = ref.fecha_ref.strftime("%Y-%m-%d") if hasattr(ref.fecha_ref, "strftime") else str(ref.fecha_ref)
            etree.SubElement(r, f"{{{NS}}}FchRef").text = fecha_ref
            if ref.razon_ref:
                etree.SubElement(r, f"{{{NS}}}RazonRef").text = ref.razon_ref[:90]
        else:
            etree.SubElement(r, f"{{{NS}}}FolioRef").text = str(ref.folio_ref)
            fecha_ref = ref.fecha_ref.strftime("%Y-%m-%d") if hasattr(ref.fecha_ref, "strftime") else str(ref.fecha_ref)
            etree.SubElement(r, f"{{{NS}}}FchRef").text = fecha_ref
            if ref.cod_ref not in (0, None, "") and str(ref.cod_ref) in ("1","2","3"):
                etree.SubElement(r, f"{{{NS}}}CodRef").text = str(ref.cod_ref)
            if ref.razon_ref:
                etree.SubElement(r, f"{{{NS}}}RazonRef").text = ref.razon_ref[:90]
