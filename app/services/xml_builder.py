# app/services/xml_builder.py
# Validado contra DTE_v10.xsd oficial SII
# FIXES: orden de elementos segun XSD, DscRcgGlobal en posicion correcta,
#        cod_ref acepta "SET", TpoTranVenta ANTES de FmaPago (orden XSD correcto)

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
    acteco: str = "620200"    # Código actividad económica


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
    tipo_doc_ref: int
    folio_ref: int
    fecha_ref: date
    razon_ref: str = ""
    cod_ref: "str | int" = 0   # "SET" o 1/2/3


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
    observacion: str = ""
    descuento_global_pct: float = 0.0
    descuento_global_monto: int = 0


def _sanitizar_texto(texto: str, largo: int = 80) -> str:
    """
    Elimina caracteres especiales que el SII no acepta en campos de texto.
    El & (ampersand) es el principal culpable del error RFR 'No hay estadísticas'.
    El SII procesa el XML de forma no estándar y falla con estos caracteres.
    Referencia: soporte OML Soluciones - artículo 'Rechazado por error en firma'.
    """
    reemplazos = {
        '&': ' y ',   # & → causa RFR definitivo en SII
        "'": '',      # comilla simple
        '"': '',      # comilla doble
        '#': '',      # gato
    }
    resultado = texto
    for char, reemplazo in reemplazos.items():
        resultado = resultado.replace(char, reemplazo)
    # Limpiar espacios dobles que puedan quedar y truncar
    import re
    resultado = re.sub(r'  +', ' ', resultado).strip()
    return resultado[:largo]


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
            self.monto_iva    = round(monto_afecto * 0.19)
            self.monto_exento = round(subtotal_exento)
            self.monto_total  = self.monto_neto + self.monto_iva + self.monto_exento

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
            self._build_detalle(doc_el, item, idx)

        # DscRcgGlobal: DESPUES de Detalle, ANTES de Referencia (orden XSD)
        if self._desc_global_monto > 0:
            dscto = etree.SubElement(doc_el, f"{{{NS}}}DscRcgGlobal")
            etree.SubElement(dscto, f"{{{NS}}}NroLinDR").text  = "1"
            etree.SubElement(dscto, f"{{{NS}}}TpoMov").text    = "D"
            etree.SubElement(dscto, f"{{{NS}}}GlosaDR").text   = "Descuento Global"
            etree.SubElement(dscto, f"{{{NS}}}TpoValor").text  = "%"
            etree.SubElement(dscto, f"{{{NS}}}ValorDR").text   = f"{d.descuento_global_pct:.2f}"

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

        # Orden XSD: TipoDTE, Folio, FchEmis, [IndNoRebaja], [TipoDespacho],
        # [IndTraslado], [TpoImpresion], [IndServicio], [MntBruto],
        # [TpoTranCompra], [TpoTranVenta], [FmaPago], ...
        etree.SubElement(iddoc, f"{{{NS}}}TipoDTE").text = str(tipo)
        etree.SubElement(iddoc, f"{{{NS}}}Folio").text   = str(d.folio)
        etree.SubElement(iddoc, f"{{{NS}}}FchEmis").text = d.fecha_emision.strftime("%Y-%m-%d")

        if tipo == 52:
            etree.SubElement(iddoc, f"{{{NS}}}IndTraslado").text = str(d.indicador_traslado or 1)

        if es_boleta:
            etree.SubElement(iddoc, f"{{{NS}}}IndServicio").text = "3"
        else:
            # ORDEN CORRECTO segun XSD DTE_v10.xsd (confirmado en lineas 182-194):
            # secuencia obligatoria: ... TpoTranCompra → TpoTranVenta → FmaPago ...
            # BUG PREVIO: FmaPago aparecia ANTES de TpoTranVenta → falla cvc-complex-type.2.4.a
            etree.SubElement(iddoc, f"{{{NS}}}TpoTranVenta").text = "1"
            etree.SubElement(iddoc, f"{{{NS}}}FmaPago").text      = str(d.forma_pago)

        # Emisor: RUTEmisor, RznSoc, GiroEmis, [Telefono], [CorreoEmisor],
        # [Acteco], ..., DirOrigen, CmnaOrigen, CiudadOrigen
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

        # Receptor: RUTRecep, [CdgIntRecep], RznSocRecep, ..., [GiroRecep],
        # [Contacto], [CorreoRecep], DirRecep, CmnaRecep, CiudadRecep
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

        # Totales: MntNeto, [MntExe], ..., [TasaIVA], [IVA], ..., MntTotal
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
            etree.SubElement(totales, f"{{{NS}}}MntNeto").text  = str(self.monto_neto)
            if self.monto_exento > 0:
                etree.SubElement(totales, f"{{{NS}}}MntExe").text = str(self.monto_exento)
            etree.SubElement(totales, f"{{{NS}}}TasaIVA").text  = "19"
            etree.SubElement(totales, f"{{{NS}}}IVA").text      = str(self.monto_iva)

        etree.SubElement(totales, f"{{{NS}}}MntTotal").text = str(self.monto_total)

    def _build_detalle(self, parent, item, numero_linea: int):
        # Orden XSD: NroLinDet, [CdgItem], [IndExe], ..., NmbItem, [DscItem],
        # ..., [QtyItem], ..., [UnmdItem], [PrcItem], [DescuentoPct],
        # [DescuentoMonto], ..., MontoItem
        NS  = self.NAMESPACE
        det = etree.SubElement(parent, f"{{{NS}}}Detalle")
        etree.SubElement(det, f"{{{NS}}}NroLinDet").text = str(numero_linea)

        if item.codigo:
            cod = etree.SubElement(det, f"{{{NS}}}CdgItem")
            etree.SubElement(cod, f"{{{NS}}}TpoCodigo").text = "INT1"
            etree.SubElement(cod, f"{{{NS}}}VlrCodigo").text = item.codigo

        if item.exento:
            etree.SubElement(det, f"{{{NS}}}IndExe").text = "1"

        # Sanitizar: & ' " # causan RFR en SII aunque sean XML válido
        etree.SubElement(det, f"{{{NS}}}NmbItem").text = _sanitizar_texto(item.nombre, 80)
        etree.SubElement(det, f"{{{NS}}}QtyItem").text = f"{item.cantidad:.2f}"

        if item.unidad:
            etree.SubElement(det, f"{{{NS}}}UnmdItem").text = item.unidad

        etree.SubElement(det, f"{{{NS}}}PrcItem").text = str(round(item.precio_unitario))

        if item.descuento_pct > 0:
            etree.SubElement(det, f"{{{NS}}}DescuentoPct").text   = f"{item.descuento_pct:.2f}"
            etree.SubElement(det, f"{{{NS}}}DescuentoMonto").text = str(
                round(item.cantidad * item.precio_unitario * item.descuento_pct / 100)
            )

        etree.SubElement(det, f"{{{NS}}}MontoItem").text = str(item.monto_item)

    def _build_referencia(self, parent, ref, numero: int):
        # Orden XSD: NroLinRef, TpoDocRef, [IndGlobal], FolioRef,
        # [RUTOtr], [FchRef], [CodRef], [RazonRef]
        NS = self.NAMESPACE
        r  = etree.SubElement(parent, f"{{{NS}}}Referencia")
        etree.SubElement(r, f"{{{NS}}}NroLinRef").text = str(numero)
        etree.SubElement(r, f"{{{NS}}}TpoDocRef").text = str(ref.tipo_doc_ref)
        etree.SubElement(r, f"{{{NS}}}FolioRef").text  = str(ref.folio_ref)
        # FchRef es obligatorio segun XSD antes de CodRef
        fecha_ref = ref.fecha_ref.strftime("%Y-%m-%d") if hasattr(ref.fecha_ref, "strftime") else str(ref.fecha_ref)
        etree.SubElement(r, f"{{{NS}}}FchRef").text = fecha_ref
        # CodRef solo acepta valores 1, 2, 3 (XSD xs:positiveInteger enumeration)
        # Las referencias a TpoDocRef=801 (set de prueba) NO llevan CodRef
        if ref.cod_ref not in (0, None, "", "SET") and str(ref.cod_ref) in ("1","2","3"):
            etree.SubElement(r, f"{{{NS}}}CodRef").text = str(ref.cod_ref)
        if ref.razon_ref:
            etree.SubElement(r, f"{{{NS}}}RazonRef").text = ref.razon_ref[:90]
