# app/services/xml_builder.py
# ══════════════════════════════════════════════════════════════
# Constructor de XML para Documentos Tributarios Electronicos
#
# ── FIXES v1.3 ─────────────────────────────────────────────
# - DTE incluye xmlns:xsi desde que se genera
#   Esto es clave: si xsi no esta en el DTE al momento de
#   firmarlo, y luego se agrega en el sobre, el digest cambia
#   porque C14N no-exclusivo hereda namespaces del padre.
#   Al incluir xsi en el DTE desde el inicio, el digest es
#   consistente tanto dentro como fuera del sobre.
# - Boletas: RznSocEmisor, GiroEmisor, IndServicio=3
# - Facturas: RznSoc, GiroEmis, Acteco, FmaPago
# ══════════════════════════════════════════════════════════════

from lxml import etree
from datetime import date, datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

SII_NS = "http://www.sii.cl/SiiDte"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

TIPOS_DTE = {
    33: "Factura Electronica",
    34: "Factura No Afecta o Exenta",
    39: "Boleta Electronica",
    41: "Boleta Exenta Electronica",
    52: "Guia de Despacho",
    56: "Nota de Debito Electronica",
    61: "Nota de Credito Electronica",
}

TIPOS_BOLETA        = {39, 41}
TIPOS_FACTURA_EXENTA= {34}


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
    unidad: str = "UN"
    exento: bool = False

    @property
    def monto_item(self) -> int:
        bruto = self.cantidad * self.precio_unitario
        return round(bruto - bruto * (self.descuento_pct / 100))


@dataclass
class ReferenciaDTE:
    tipo_doc_ref: int
    folio_ref: int
    fecha_ref: date
    razon_ref: str = ""
    cod_ref: int = 0


@dataclass
class InputDTE:
    tipo_dte: int
    folio: int
    fecha_emision: date
    emisor: EmisorDTE
    receptor: ReceptorDTE
    items: list[ItemDTE]
    ambiente: str = "certificacion"
    referencias: list[ReferenciaDTE] = field(default_factory=list)
    forma_pago: int = 1
    indicador_traslado: int = 0
    observacion: str = ""
    descuento_global_pct: float = 0.0
    descuento_global_monto: int = 0


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
            if subtotal_exento > 0 and monto_afecto > 0:
                neto = round(monto_afecto / 1.19)
                iva  = monto_afecto - neto
                self.monto_neto   = neto
                self.monto_iva    = iva
                self.monto_exento = round(subtotal_exento)
            else:
                self.monto_neto   = 0
                self.monto_iva    = 0
                self.monto_exento = round(subtotal_exento)
            self.monto_total = round(monto_afecto + subtotal_exento)
        elif tipo in TIPOS_FACTURA_EXENTA:
            self.monto_neto   = 0
            self.monto_iva    = 0
            self.monto_exento = round(monto_afecto + subtotal_exento)
            self.monto_total  = self.monto_exento
        else:
            self.monto_neto   = round(monto_afecto)
            self.monto_iva    = round(monto_afecto * 0.19)
            self.monto_exento = round(subtotal_exento)
            self.monto_total  = self.monto_neto + self.monto_iva + self.monto_exento

    def construir(self) -> bytes:
        d  = self.datos
        NS = self.NAMESPACE

        # ⚠️  FIX v1.3: incluir xsi en el DTE desde el inicio
        # Esto garantiza que el digest sea consistente cuando el
        # DTE se inserta en el sobre (que tambien tiene xsi)
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
            self._build_detalle(doc_el, item, idx)

        for idx, ref in enumerate(d.referencias, start=1):
            self._build_referencia(doc_el, ref, idx)

        ted_el = etree.SubElement(doc_el, f"{{{NS}}}TED",
                                  attrib={"version": "1.0"})
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

        if es_boleta:
            etree.SubElement(iddoc, f"{{{NS}}}IndServicio").text = "3"
        else:
            etree.SubElement(iddoc, f"{{{NS}}}FmaPago").text = str(d.forma_pago)
            if tipo == 52:
                etree.SubElement(iddoc, f"{{{NS}}}IndTraslado").text = str(d.indicador_traslado)
            etree.SubElement(iddoc, f"{{{NS}}}TpoTranVenta").text = "1"

        em     = d.emisor
        emisor = etree.SubElement(enc, f"{{{NS}}}Emisor")
        etree.SubElement(emisor, f"{{{NS}}}RUTEmisor").text = em.rut

        if es_boleta:
            etree.SubElement(emisor, f"{{{NS}}}RznSocEmisor").text = em.razon_social
            etree.SubElement(emisor, f"{{{NS}}}GiroEmisor").text   = em.giro[:80]
        else:
            etree.SubElement(emisor, f"{{{NS}}}RznSoc").text   = em.razon_social
            etree.SubElement(emisor, f"{{{NS}}}GiroEmis").text = em.giro[:80]
            if em.telefono:
                etree.SubElement(emisor, f"{{{NS}}}Telefono").text     = em.telefono
            if em.correo:
                etree.SubElement(emisor, f"{{{NS}}}CorreoEmisor").text = em.correo
            etree.SubElement(emisor, f"{{{NS}}}Acteco").text    = "620100"

        etree.SubElement(emisor, f"{{{NS}}}DirOrigen").text    = em.direccion
        etree.SubElement(emisor, f"{{{NS}}}CmnaOrigen").text   = em.comuna
        etree.SubElement(emisor, f"{{{NS}}}CiudadOrigen").text = em.ciudad

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

        if self._desc_global_monto > 0:
            dscto = etree.SubElement(enc, f"{{{NS}}}DscRcgGlobal")
            etree.SubElement(dscto, f"{{{NS}}}NroLinDR").text  = "1"
            etree.SubElement(dscto, f"{{{NS}}}TpoMov").text    = "D"
            etree.SubElement(dscto, f"{{{NS}}}GlosaDR").text   = "Descuento Global"
            etree.SubElement(dscto, f"{{{NS}}}TpoValor").text  = "%"
            etree.SubElement(dscto, f"{{{NS}}}ValorDR").text   = f"{d.descuento_global_pct:.2f}"

        totales = etree.SubElement(enc, f"{{{NS}}}Totales")

        if tipo in TIPOS_FACTURA_EXENTA:
            etree.SubElement(totales, f"{{{NS}}}MntExe").text = str(self.monto_exento)
        elif es_boleta:
            if self.monto_neto > 0:
                etree.SubElement(totales, f"{{{NS}}}MntNeto").text = str(self.monto_neto)
            if self.monto_exento > 0:
                etree.SubElement(totales, f"{{{NS}}}MntExe").text  = str(self.monto_exento)
            if self.monto_iva > 0:
                etree.SubElement(totales, f"{{{NS}}}IVA").text     = str(self.monto_iva)
        else:
            etree.SubElement(totales, f"{{{NS}}}MntNeto").text  = str(self.monto_neto)
            if self.monto_exento > 0:
                etree.SubElement(totales, f"{{{NS}}}MntExe").text = str(self.monto_exento)
            etree.SubElement(totales, f"{{{NS}}}TasaIVA").text  = "19"
            etree.SubElement(totales, f"{{{NS}}}IVA").text      = str(self.monto_iva)

        etree.SubElement(totales, f"{{{NS}}}MntTotal").text = str(self.monto_total)

    def _build_detalle(self, parent, item: ItemDTE, numero_linea: int):
        NS  = self.NAMESPACE
        det = etree.SubElement(parent, f"{{{NS}}}Detalle")
        etree.SubElement(det, f"{{{NS}}}NroLinDet").text = str(numero_linea)
        if item.codigo:
            cod = etree.SubElement(det, f"{{{NS}}}CdgItem")
            etree.SubElement(cod, f"{{{NS}}}TpoCodigo").text = "INT1"
            etree.SubElement(cod, f"{{{NS}}}VlrCodigo").text = item.codigo
        if item.exento:
            etree.SubElement(det, f"{{{NS}}}IndExe").text    = "1"
        etree.SubElement(det, f"{{{NS}}}NmbItem").text       = item.nombre[:80]
        etree.SubElement(det, f"{{{NS}}}QtyItem").text       = f"{item.cantidad:.2f}"
        if item.unidad:
            etree.SubElement(det, f"{{{NS}}}UnmdItem").text  = item.unidad
        etree.SubElement(det, f"{{{NS}}}PrcItem").text       = str(round(item.precio_unitario))
        if item.descuento_pct > 0:
            etree.SubElement(det, f"{{{NS}}}DescuentoPct").text   = f"{item.descuento_pct:.2f}"
            etree.SubElement(det, f"{{{NS}}}DescuentoMonto").text = str(
                round(item.cantidad * item.precio_unitario * item.descuento_pct / 100)
            )
        etree.SubElement(det, f"{{{NS}}}MontoItem").text = str(item.monto_item)

    def _build_referencia(self, parent, ref: ReferenciaDTE, numero: int):
        NS = self.NAMESPACE
        r  = etree.SubElement(parent, f"{{{NS}}}Referencia")
        etree.SubElement(r, f"{{{NS}}}NroLinRef").text  = str(numero)
        etree.SubElement(r, f"{{{NS}}}TpoDocRef").text  = str(ref.tipo_doc_ref)
        etree.SubElement(r, f"{{{NS}}}FolioRef").text   = str(ref.folio_ref)
        if ref.cod_ref:
            etree.SubElement(r, f"{{{NS}}}CodRef").text   = str(ref.cod_ref)
        if ref.razon_ref:
            etree.SubElement(r, f"{{{NS}}}RazonRef").text = ref.razon_ref[:90]
