# app/services/xml_builder.py
# ══════════════════════════════════════════════════════════════
# Constructor de XML para DTE - Optimizado para Certificación SII
# ══════════════════════════════════════════════════════════════

from lxml import etree
from datetime import date
from dataclasses import dataclass, field
from typing import Optional, List

# Namespaces oficiales del SII
SII_NS = "http://www.sii.cl/SiiDte"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

@dataclass
class EmisorDTE:
    rut: str
    razon_social: str
    giro: str
    direccion: str
    comuna: str
    ciudad: str
    acteco: Optional[int] = None

@dataclass
class ReceptorDTE:
    rut: str
    razon_social: str
    giro: str = "Particular"
    direccion: str = "Ciudad"
    comuna: str = "Santiago"
    ciudad: str = "Santiago"

@dataclass
class ItemDTE:
    nombre: str
    precio_unitario: float
    cantidad: float = 1.0
    exento: bool = False
    unidad: str = "UN"
    descuento_pct: float = 0.0
    codigo: str = ""

    @property
    def monto_item(self) -> int:
        # El SII exige montos enteros
        total = self.cantidad * self.precio_unitario
        if self.descuento_pct > 0:
            total -= (total * self.descuento_pct / 100)
        return int(round(total))

@dataclass
class ReferenciaDTE:
    tipo_doc_ref: str
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
    items: List[ItemDTE]
    ambiente: str = "certificacion"
    referencias: List[ReferenciaDTE] = field(default_factory=list)

class XMLBuilder:
    def __init__(self, data: InputDTE):
        self.data = data
        self.monto_neto = 0
        self.monto_iva = 0
        self.monto_total = 0
        self._calcular_totales()

    def _calcular_totales(self):
        # Separar afecto de exento
        afecto = sum(item.monto_item for item in self.data.items if not item.exento)
        exento = sum(item.monto_item for item in self.data.items if item.exento)
        
        # En Chile, el IVA (19%) se calcula sobre el neto afecto
        # Para Boletas, el precio suele venir con IVA, aquí asumimos Neto para el motor
        self.monto_neto = afecto
        self.monto_iva = int(round(afecto * 0.19))
        self.monto_total = self.monto_neto + self.monto_iva + exento

    def construir(self) -> bytes:
        # Definir el esquema correcto según el tipo
        # SCH-00001 fix: El nombre del .xsd debe ser exacto
        schema_file = "EnvioDTE_v10.xsd" if self.data.tipo_dte not in [39, 41] else "EnvioBOLETA_v11.xsd"
        schema_location = f"{SII_NS} {schema_file}"

        # Configuración de Namespaces
        ns_map = {
            None: SII_NS,
            'xsi': XSI_NS
        }

        # Nodo Raíz DTE
        root = etree.Element("DTE", nsmap=ns_map, version="1.0")
        root.set(f"{{{XSI_NS}}}schemaLocation", schema_location)

        # Documento e ID (Necesario para la firma)
        doc_id = f"T{self.data.tipo_dte}F{self.data.folio}"
        documento = etree.SubElement(root, "Documento", ID=doc_id)

        # Encabezado
        encabezado = etree.SubElement(documento, "Encabezado")
        
        # 1. IdDoc
        id_doc = etree.SubElement(encabezado, "IdDoc")
        etree.SubElement(id_doc, "TipoDTE").text = str(self.data.tipo_dte)
        etree.SubElement(id_doc, "Folio").text = str(self.data.folio)
        etree.SubElement(id_doc, "FchEmis").text = self.data.fecha_emision.isoformat()
        
        # Indicador de servicio para boletas
        if self.data.tipo_dte in [39, 41]:
            etree.SubElement(id_doc, "IndServicio").text = "3" # Boleta de servicios

        # 2. Emisor
        emisor = etree.SubElement(encabezado, "Emisor")
        etree.SubElement(emisor, "RUTEmisor").text = self.data.emisor.rut
        etree.SubElement(emisor, "RznSoc").text = self.data.emisor.razon_social[:100]
        etree.SubElement(emisor, "GiroEmis").text = self.data.emisor.giro[:80]
        if self.data.emisor.acteco:
            etree.SubElement(emisor, "Acteco").text = str(self.data.emisor.acteco)
        etree.SubElement(emisor, "DirOrigin").text = self.data.emisor.direccion[:70]
        etree.SubElement(emisor, "CmnaOrigin").text = self.data.emisor.comuna[:20]
        etree.SubElement(emisor, "CiudadOrigin").text = self.data.emisor.ciudad[:20]

        # 3. Receptor
        receptor = etree.SubElement(encabezado, "Receptor")
        etree.SubElement(receptor, "RUTRecep").text = self.data.receptor.rut
        etree.SubElement(receptor, "RznSocRecep").text = self.data.receptor.razon_social[:100]
        etree.SubElement(receptor, "GiroRecep").text = self.data.receptor.giro[:40]
        etree.SubElement(receptor, "DirRecep").text = self.data.receptor.direccion[:70]
        etree.SubElement(receptor, "CmnaRecep").text = self.data.receptor.comuna[:20]
        etree.SubElement(receptor, "CiudadRecep").text = self.data.receptor.ciudad[:20]

        # 4. Totales
        totales = etree.SubElement(encabezado, "Totales")
        etree.SubElement(totales, "MntNeto").text = str(self.monto_neto)
        etree.SubElement(totales, "TasaIVA").text = "19"
        etree.SubElement(totales, "IVA").text = str(self.monto_iva)
        etree.SubElement(totales, "MntTotal").text = str(self.monto_total)

        # 5. Detalles (Items)
        for i, item in enumerate(self.data.items, 1):
            detalle = etree.SubElement(documento, "Detalle")
            etree.SubElement(detalle, "NroLinDet").text = str(i)
            etree.SubElement(detalle, "NmbItem").text = item.nombre[:80]
            etree.SubElement(detalle, "QtyItem").text = str(item.cantidad)
            etree.SubElement(detalle, "PrcItem").text = str(round(item.precio_unitario))
            etree.SubElement(detalle, "MontoItem").text = str(item.monto_item)

        # 6. Referencias
        for i, ref in enumerate(self.data.referencias, 1):
            referencia = etree.SubElement(documento, "Referencia")
            etree.SubElement(referencia, "NroLinRef").text = str(i)
            etree.SubElement(referencia, "TpoDocRef").text = str(ref.tipo_doc_ref)
            etree.SubElement(referencia, "FolioRef").text = str(ref.folio_ref)
            etree.SubElement(referencia, "FchRef").text = ref.fecha_ref.isoformat()
            if ref.razon_ref:
                etree.SubElement(referencia, "RazonRef").text = ref.razon_ref[:90]

        # Retornar como bytes en ISO-8859-1 (Requisito SII)
        return etree.tostring(root, encoding="ISO-8859-1", xml_declaration=True, pretty_print=False)
