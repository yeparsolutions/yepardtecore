# app/services/xml_builder.py
from lxml import etree
from datetime import date
from dataclasses import dataclass, field
from typing import Optional, List

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
class ItemDTEInput:
    nombre: str
    precio_unitario: float
    cantidad: float = 1.0
    exento: bool = False
    codigo: str = ""

    @property
    def monto_item(self) -> int:
        return int(round(self.cantidad * self.precio_unitario))

@dataclass
class InputDTE:
    tipo_dte: int
    folio: int
    fecha_emision: date
    emisor: EmisorDTE
    receptor: ReceptorDTE
    items: List[ItemDTEInput]
    ambiente: str = "certificacion"

class XMLBuilder:
    def __init__(self, data: InputDTE):
        self.data = data
        afecto = sum(i.monto_item for i in data.items if not i.exento)
        exento = sum(i.monto_item for i in data.items if i.exento)
        self.monto_neto = afecto
        self.monto_iva = int(round(afecto * 0.19))
        self.monto_total = self.monto_neto + self.monto_iva + exento

    def construir(self) -> bytes:
        schema_file = "EnvioDTE_v10.xsd" if self.data.tipo_dte not in [39, 41] else "EnvioBOLETA_v11.xsd"
        ns_map = {None: SII_NS, 'xsi': XSI_NS}
        
        root = etree.Element("DTE", nsmap=ns_map, version="1.0")
        root.set(f"{{{XSI_NS}}}schemaLocation", f"{SII_NS} {schema_file}")

        # IMPORTANTE: El tag Documento DEBE tener el namespace explícito para que el signer lo vea
        doc_id = f"T{self.data.tipo_dte}F{self.data.folio}"
        documento = etree.SubElement(root, "Documento", ID=doc_id)

        encabezado = etree.SubElement(documento, "Encabezado")
        
        id_doc = etree.SubElement(encabezado, "IdDoc")
        etree.SubElement(id_doc, "TipoDTE").text = str(self.data.tipo_dte)
        etree.SubElement(id_doc, "Folio").text = str(self.data.folio)
        etree.SubElement(id_doc, "FchEmis").text = self.data.fecha_emision.isoformat()
        
        emisor = etree.SubElement(encabezado, "Emisor")
        etree.SubElement(emisor, "RUTEmisor").text = self.data.emisor.rut
        etree.SubElement(emisor, "RznSoc").text = self.data.emisor.razon_social[:100]
        etree.SubElement(emisor, "GiroEmis").text = self.data.emisor.giro[:80]
        
        receptor = etree.SubElement(encabezado, "Receptor")
        etree.SubElement(receptor, "RUTRecep").text = self.data.receptor.rut
        etree.SubElement(receptor, "RznSocRecep").text = self.data.receptor.razon_social[:100]

        totales = etree.SubElement(encabezado, "Totales")
        etree.SubElement(totales, "MntNeto").text = str(self.monto_neto)
        etree.SubElement(totales, "TasaIVA").text = "19"
        etree.SubElement(totales, "IVA").text = str(self.monto_iva)
        etree.SubElement(totales, "MntTotal").text = str(self.monto_total)

        for i, item in enumerate(self.data.items, 1):
            det = etree.SubElement(documento, "Detalle")
            etree.SubElement(det, "NroLinDet").text = str(i)
            etree.SubElement(det, "NmbItem").text = item.nombre[:80]
            etree.SubElement(det, "QtyItem").text = str(item.cantidad)
            etree.SubElement(det, "PrcItem").text = str(round(item.precio_unitario))
            etree.SubElement(det, "MontoItem").text = str(item.monto_item)

        return etree.tostring(root, encoding="ISO-8859-1", xml_declaration=True)
