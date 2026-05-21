# app/services/xml_builder_boleta.py
# ══════════════════════════════════════════════════════════════
# Constructor XML para Boletas Electrónicas (Tipos 39 y 41)
#
# SEPARADO del xml_builder.py genérico porque las boletas tienen
# reglas de XSD completamente distintas a las facturas.
#
# Analogía: es como separar la receta de un pastel salado de uno
# dulce — comparten horno, pero los ingredientes y el orden son
# muy diferentes.
#
# Diferencias clave vs Factura (Tipo 33):
#   1. IdDoc: IndServicio=3 obligatorio; sin TpoTranVenta ni FmaPago
#   2. Emisor: usa RznSocEmisor y GiroEmisor (no RznSoc/GiroEmis)
#   3. Receptor: sin DirRecep, CmnaRecep, CiudadRecep
#   4. Totales: el IVA se desglosa desde el precio bruto (÷1.19)
#   5. Sobre: debe ser <EnvioBOLETA> (no <EnvioDTE>)
#   6. Referencia: TpoDocRef acepta "SET" como código especial
#
# Referencia XSD: EnvioBOLETA_v11.xsd (SII Chile)
# ══════════════════════════════════════════════════════════════

from lxml import etree
from datetime import date, datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

# Namespace oficial del SII para todos los DTEs
SII_NS = "http://www.sii.cl/SiiDte"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"


# ── Dataclasses de entrada ────────────────────────────────────

@dataclass
class EmisorBoleta:
    """Datos del emisor de la boleta."""
    rut: str
    razon_social: str      # → se mapea a RznSocEmisor (no RznSoc)
    giro: str              # → se mapea a GiroEmisor (no GiroEmis)
    direccion: str
    comuna: str
    ciudad: str
    acteco: str = "620200"
    telefono: str = ""
    correo: str = ""


@dataclass
class ReceptorBoleta:
    """
    Receptor de boleta. Solo exige RUT y razón social.
    No lleva dirección (campo prohibido en EnvioBOLETA_v11.xsd).
    """
    rut: str = "66666666-6"           # Consumidor final por defecto
    razon_social: str = "Consumidor Final"
    correo: str = ""                  # Opcional — para boleta electrónica


@dataclass
class ItemBoleta:
    """
    Ítem de una boleta. El precio_unitario debe ser NETO (sin IVA).
    El motor calcula el IVA automáticamente.

    Analogía: el precio_unitario es el precio antes de que el fisco
    le agregue su parte — nosotros lo recibimos sin IVA y lo
    desglosamos correctamente en el XML.
    """
    nombre: str
    cantidad: float = 1.0
    precio_unitario: float = 0.0      # Precio NETO (sin IVA)
    descuento_pct: float = 0.0
    codigo: str = ""
    unidad: str = ""
    exento: bool = False              # True para ítems no gravados con IVA

    @property
    def monto_item(self) -> int:
        """Monto del ítem después de aplicar descuento."""
        bruto = self.cantidad * self.precio_unitario
        return round(bruto - bruto * (self.descuento_pct / 100))


@dataclass
class ReferenciaBoleta:
    """
    Referencia de una boleta. El SII exige que cada boleta del
    set de certificación incluya TpoDocRef=SET y RazonRef=CASO-N.
    """
    tipo_doc_ref: int           # 801 para "otros documentos de referencia"
    folio_ref: int
    fecha_ref: date
    razon_ref: str = ""
    cod_ref: str = "SET"        # "SET" para sets de prueba SII


@dataclass
class InputBoleta:
    """Datos de entrada completos para generar una boleta."""
    tipo_dte: int                     # 39 = Boleta, 41 = Boleta de Servicios Exenta
    folio: int
    fecha_emision: date
    emisor: EmisorBoleta
    receptor: ReceptorBoleta
    items: list
    referencias: list = field(default_factory=list)
    observacion: str = ""
    descuento_global_pct: float = 0.0


# ── Constructor ───────────────────────────────────────────────

class XMLBuilderBoleta:
    """
    Construye el XML de una boleta electrónica conforme a DTE_v10.xsd
    con las restricciones adicionales de EnvioBOLETA_v11.xsd.

    Analogía: es el molde oficial donde vaciamos los datos del negocio
    para que el SII los reconozca como válidos.
    """

    NAMESPACE = SII_NS

    def __init__(self, datos: InputBoleta):
        self.datos = datos
        self._calcular_totales()

    def _calcular_totales(self):
        """
        Calcula neto, IVA y total según el tipo de ítem.

        Para boletas, el precio_unitario es NETO (sin IVA).
        El IVA (19%) se suma sobre los ítems afectos.
        Los ítems con exento=True no llevan IVA.
        """
        items = self.datos.items
        tipo  = self.datos.tipo_dte

        # Separar montos afectos y exentos
        subtotal_afecto = sum(i.monto_item for i in items if not i.exento)
        subtotal_exento = sum(i.monto_item for i in items if i.exento)

        # Aplicar descuento global solo sobre afectos
        desc = 0
        if self.datos.descuento_global_pct > 0:
            desc = round(subtotal_afecto * self.datos.descuento_global_pct / 100)
        self._desc_global_monto = desc
        monto_afecto_neto = subtotal_afecto - desc

        if tipo == 41:
            # Boleta de Servicios Exenta (tipo 41): todo exento, sin IVA
            self.monto_neto   = 0
            self.monto_iva    = 0
            self.monto_exento = round(subtotal_afecto + subtotal_exento)
            self.monto_total  = self.monto_exento
        else:
            # Boleta Electrónica (tipo 39): afectos llevan IVA 19%
            self.monto_neto   = monto_afecto_neto
            self.monto_iva    = round(monto_afecto_neto * 0.19)
            self.monto_exento = round(subtotal_exento)
            self.monto_total  = self.monto_neto + self.monto_iva + self.monto_exento

    def _sanitizar(self, texto: str, largo: int = 80) -> str:
        """
        Limpia caracteres problemáticos en campos de texto del SII.
        El & (ampersand) causa RFR aunque sea XML válido — el SII
        lo procesa de forma no estándar internamente.
        """
        import re
        reemplazos = {
            '&': ' y ',   # ampersand → causa RFR definitivo
            "'": '',      # comilla simple
            '"': '',      # comilla doble
            '#': '',      # gato
        }
        resultado = texto
        for char, rep in reemplazos.items():
            resultado = resultado.replace(char, rep)
        resultado = re.sub(r'  +', ' ', resultado).strip()
        return resultado[:largo]

    def construir(self) -> bytes:
        """
        Construye el XML completo del DTE de boleta.
        Retorna bytes en codificación ISO-8859-1 (requerido por el SII).

        El XML resultante incluye un placeholder <TED> que
        FirmaDTE.firmar() reemplaza con el TED real firmado.
        """
        d  = self.datos
        NS = self.NAMESPACE

        # Elemento raíz DTE con namespace SII y declaración xsi
        dte_el = etree.Element(
            f"{{{NS}}}DTE",
            attrib={"version": "1.0"},
            nsmap={None: NS, "xsi": XSI_NS}
        )

        # Documento con ID único para la firma XMLDSig
        doc_el = etree.SubElement(
            dte_el, f"{{{NS}}}Documento",
            attrib={"ID": f"DTE-{d.tipo_dte}-{d.folio}"}
        )

        # Construir secciones del documento
        self._build_encabezado(doc_el)
        self._build_detalles(doc_el)

        # Descuento global (si aplica): debe ir DESPUÉS de Detalle, ANTES de Referencia
        if self._desc_global_monto > 0:
            dr = etree.SubElement(doc_el, f"{{{NS}}}DscRcgGlobal")
            etree.SubElement(dr, f"{{{NS}}}NroLinDR").text  = "1"
            etree.SubElement(dr, f"{{{NS}}}TpoMov").text    = "D"
            etree.SubElement(dr, f"{{{NS}}}GlosaDR").text   = "Descuento Global"
            etree.SubElement(dr, f"{{{NS}}}TpoValor").text  = "%"
            etree.SubElement(dr, f"{{{NS}}}ValorDR").text   = f"{d.descuento_global_pct:.2f}"

        # Referencias (obligatorias para el set de certificación)
        for idx, ref in enumerate(d.referencias, start=1):
            self._build_referencia(doc_el, ref, idx)

        # Placeholder TED — será reemplazado por FirmaDTE.firmar()
        ted_el = etree.SubElement(doc_el, f"{{{NS}}}TED", attrib={"version": "1.0"})
        etree.SubElement(ted_el, f"{{{NS}}}DD")

        # Timestamp de firma (será actualizado por FirmaDTE.firmar())
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
        """
        Construye el Encabezado según el orden estricto del XSD:
        IdDoc → Emisor → Receptor → Totales

        DIFERENCIAS CRÍTICAS vs Factura:
        - IdDoc: IndServicio=3 en lugar de TpoTranVenta + FmaPago
        - Emisor: RznSocEmisor y GiroEmisor (no RznSoc y GiroEmis)
        - Receptor: sin dirección
        """
        d  = self.datos
        NS = self.NAMESPACE

        enc   = etree.SubElement(parent, f"{{{NS}}}Encabezado")
        iddoc = etree.SubElement(enc, f"{{{NS}}}IdDoc")

        # ── IdDoc para boleta ─────────────────────────────────
        # Orden XSD: TipoDTE, Folio, FchEmis, [IndServicio], ...
        etree.SubElement(iddoc, f"{{{NS}}}TipoDTE").text = str(d.tipo_dte)
        etree.SubElement(iddoc, f"{{{NS}}}Folio").text   = str(d.folio)
        etree.SubElement(iddoc, f"{{{NS}}}FchEmis").text = d.fecha_emision.strftime("%Y-%m-%d")

        # IndServicio=3 es OBLIGATORIO para boletas (tipo 39 y 41)
        # Significa "Servicio o venta" — lo que el SII usa para identificar boletas
        # Analogía: es la "etiqueta de precio" que le dice al SII que esto ES una boleta
        etree.SubElement(iddoc, f"{{{NS}}}IndServicio").text = "3"

        # ── Emisor para boleta ────────────────────────────────
        # ATENCIÓN: boletas usan RznSocEmisor (no RznSoc) y GiroEmisor (no GiroEmis)
        # Mezclar estos campos causa error cvc-complex-type.2.4.a en validación XSD
        em     = d.emisor
        emisor = etree.SubElement(enc, f"{{{NS}}}Emisor")
        etree.SubElement(emisor, f"{{{NS}}}RUTEmisor").text    = em.rut
        etree.SubElement(emisor, f"{{{NS}}}RznSocEmisor").text = self._sanitizar(em.razon_social)
        etree.SubElement(emisor, f"{{{NS}}}GiroEmisor").text   = self._sanitizar(em.giro, 80)

        # Acteco ANTES de Telefono/Correo (orden XSD EnvioBOLETA_v11)
        etree.SubElement(emisor, f"{{{NS}}}Acteco").text      = em.acteco or "620200"

        # Teléfono y correo son opcionales
        if em.telefono:
            etree.SubElement(emisor, f"{{{NS}}}Telefono").text     = em.telefono
        if em.correo:
            etree.SubElement(emisor, f"{{{NS}}}CorreoEmisor").text = em.correo

        # Dirección del emisor
        etree.SubElement(emisor, f"{{{NS}}}DirOrigen").text   = self._sanitizar(em.direccion)
        etree.SubElement(emisor, f"{{{NS}}}CmnaOrigen").text  = self._sanitizar(em.comuna)
        etree.SubElement(emisor, f"{{{NS}}}CiudadOrigen").text = self._sanitizar(em.ciudad)

        # ── Receptor para boleta ──────────────────────────────
        # CRÍTICO: el receptor de boleta NO lleva dirección.
        # Si se agrega DirRecep/CmnaRecep/CiudadRecep el XSD falla.
        rc       = d.receptor
        receptor = etree.SubElement(enc, f"{{{NS}}}Receptor")
        etree.SubElement(receptor, f"{{{NS}}}RUTRecep").text    = rc.rut.replace(".", "")
        etree.SubElement(receptor, f"{{{NS}}}RznSocRecep").text = self._sanitizar(rc.razon_social)

        # Correo del receptor solo si existe (útil para boleta electrónica)
        if rc.correo:
            etree.SubElement(receptor, f"{{{NS}}}CorreoRecep").text = rc.correo

        # ── Totales ───────────────────────────────────────────
        # Orden XSD para boleta: [MntNeto], [MntExe], [TasaIVA], [IVA], MntTotal
        totales = etree.SubElement(enc, f"{{{NS}}}Totales")

        if self.datos.tipo_dte == 41:
            # Tipo 41 (Boleta Exenta): todo va como MntExe, sin IVA
            etree.SubElement(totales, f"{{{NS}}}MntExe").text = str(self.monto_exento)
        else:
            # Tipo 39 (Boleta afecta): desglosar neto + IVA
            if self.monto_neto > 0:
                etree.SubElement(totales, f"{{{NS}}}MntNeto").text = str(self.monto_neto)
            if self.monto_exento > 0:
                # Caso mixto: algunos ítems afectos, otros exentos
                etree.SubElement(totales, f"{{{NS}}}MntExe").text  = str(self.monto_exento)
            if self.monto_iva > 0:
                # Boletas NO llevan TasaIVA — solo IVA (orden XSD EnvioBOLETA_v11)
                etree.SubElement(totales, f"{{{NS}}}IVA").text = str(self.monto_iva)

        # MntTotal siempre al final — OBLIGATORIO
        etree.SubElement(totales, f"{{{NS}}}MntTotal").text = str(self.monto_total)

    def _build_detalles(self, parent):
        """Agrega los ítems de la boleta al Documento."""
        for idx, item in enumerate(self.datos.items, start=1):
            self._build_detalle(parent, item, idx)

    def _build_detalle(self, parent, item: ItemBoleta, numero_linea: int):
        """
        Construye un elemento <Detalle> según el orden del XSD.
        Orden: NroLinDet, [CdgItem], [IndExe], NmbItem,
               [QtyItem], [UnmdItem], PrcItem, [DescuentoPct],
               [DescuentoMonto], MontoItem
        """
        NS  = self.NAMESPACE
        det = etree.SubElement(parent, f"{{{NS}}}Detalle")
        etree.SubElement(det, f"{{{NS}}}NroLinDet").text = str(numero_linea)

        # Código de ítem (opcional)
        if item.codigo:
            cod = etree.SubElement(det, f"{{{NS}}}CdgItem")
            etree.SubElement(cod, f"{{{NS}}}TpoCodigo").text = "INT1"
            etree.SubElement(cod, f"{{{NS}}}VlrCodigo").text = item.codigo

        # Indicador de exención — va ANTES del nombre según XSD
        if item.exento:
            etree.SubElement(det, f"{{{NS}}}IndExe").text = "1"

        # Nombre del ítem (sanitizado)
        etree.SubElement(det, f"{{{NS}}}NmbItem").text = self._sanitizar(item.nombre, 80)

        # Cantidad
        etree.SubElement(det, f"{{{NS}}}QtyItem").text = f"{item.cantidad:.2f}"

        # Unidad de medida (requerida si el SII lo especifica en el set de prueba)
        if item.unidad:
            etree.SubElement(det, f"{{{NS}}}UnmdItem").text = item.unidad

        # Precio unitario NETO
        etree.SubElement(det, f"{{{NS}}}PrcItem").text = str(round(item.precio_unitario))

        # Descuento si aplica
        if item.descuento_pct > 0:
            etree.SubElement(det, f"{{{NS}}}DescuentoPct").text   = f"{item.descuento_pct:.2f}"
            etree.SubElement(det, f"{{{NS}}}DescuentoMonto").text = str(
                round(item.cantidad * item.precio_unitario * item.descuento_pct / 100)
            )

        # Monto total del ítem (después de descuento)
        etree.SubElement(det, f"{{{NS}}}MontoItem").text = str(item.monto_item)

    def _build_referencia(self, parent, ref: ReferenciaBoleta, numero: int):
        """
        Construye la referencia de la boleta.

        Para el set de certificación, el SII exige:
        - TpoDocRef: tipo de documento referenciado (801 para "set")
        - FolioRef: número del caso (1, 2, 3...)
        - FchRef: fecha del caso
        - RazonRef: texto "CASO-N"

        NOTA: cod_ref="SET" es el código especial que identifica que
        este documento pertenece a un set de prueba. Es un string, no
        un integer, por eso lo manejamos por separado del XSD general.
        """
        NS = self.NAMESPACE
        r  = etree.SubElement(parent, f"{{{NS}}}Referencia")
        etree.SubElement(r, f"{{{NS}}}NroLinRef").text = str(numero)
        etree.SubElement(r, f"{{{NS}}}TpoDocRef").text = str(ref.tipo_doc_ref)

        # FolioRef
        etree.SubElement(r, f"{{{NS}}}FolioRef").text = str(ref.folio_ref)

        # EnvioBOLETA_v11 NO lleva FchRef en Referencia
        # Orden: NroLinRef → TpoDocRef → FolioRef → [CodRef] → [RazonRef]
        if ref.razon_ref:
            etree.SubElement(r, f"{{{NS}}}RazonRef").text = ref.razon_ref[:90]
