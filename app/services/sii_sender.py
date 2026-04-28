# app/services/sii_sender.py
# ══════════════════════════════════════════════════════════════
# Servicio de envio al SII (MODO MANUAL)
#
# - NO realiza envio HTTP al SII
# - Solo construye el sobre y devuelve el XML
# - Evita error 502 mientras trabajas en certificación manual
# ══════════════════════════════════════════════════════════════

import logging
from lxml import etree
from datetime import datetime, timezone

logger = logging.getLogger("yepardtecore.dte")

SII_NS = "http://www.sii.cl/SiiDte"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
TIPOS_BOLETA = {39, 41}


class SIISender:

    def __init__(self, ambiente: str = "certificacion"):
        self.ambiente = ambiente

    @staticmethod
    def limpiar_rut(rut: str) -> str:
        rut = rut.replace(".", "").strip()
        partes = rut.split("-")
        if len(partes) > 2:
            rut = partes[0] + "-" + partes[1]
        return rut

    def construir_sobre(self, dtes_xml: list[str], rut_emisor: str,
                        rut_enviador: str, firma_service) -> str:

        NS = SII_NS
        ahora = datetime.now(timezone.utc)

        rut_emisor = self.limpiar_rut(rut_emisor)
        rut_enviador = self.limpiar_rut(rut_enviador)

        tipos_en_sobre = {}
        for dte_xml in dtes_xml:
            try:
                dte_str = dte_xml
                if dte_str.startswith('<?xml'):
                    dte_str = dte_str[dte_str.index('?>') + 2:].lstrip()
                dte_root = etree.fromstring(dte_str)
                tipo_el = dte_root.find(f".//{{{NS}}}TipoDTE")
                if tipo_el is not None:
                    t = int(tipo_el.text)
                    tipos_en_sobre[t] = tipos_en_sobre.get(t, 0) + 1
            except Exception:
                pass

        es_boleta = all(t in TIPOS_BOLETA for t in tipos_en_sobre)

        if es_boleta:
            root_tag = f"{{{NS}}}EnvioBOLETA"
            schema = f"{NS} EnvioBOLETA_v11.xsd"
        else:
            root_tag = f"{{{NS}}}EnvioDTE"
            schema = f"{NS} EnvioDTE_v10.xsd"

        nsmap = {None: NS, "xsi": XSI_NS}

        envio_el = etree.Element(root_tag, attrib={
            f"{{{XSI_NS}}}schemaLocation": schema,
            "version": "1.0",
        }, nsmap=nsmap)

        set_el = etree.SubElement(envio_el, f"{{{NS}}}SetDTE",
                                 attrib={"ID": "SetDoc"})

        caratula = etree.SubElement(set_el, f"{{{NS}}}Caratula",
                                   attrib={"version": "1.0"})

        etree.SubElement(caratula, f"{{{NS}}}RutEmisor").text = rut_emisor
        etree.SubElement(caratula, f"{{{NS}}}RutEnvia").text = rut_enviador
        etree.SubElement(caratula, f"{{{NS}}}RutReceptor").text = "60803000-K"

        etree.SubElement(caratula, f"{{{NS}}}FchResol").text = "2026-04-19"
        etree.SubElement(caratula, f"{{{NS}}}NroResol").text = "0"
        etree.SubElement(caratula, f"{{{NS}}}TmstFirmaEnv").text = (
            ahora.strftime("%Y-%m-%dT%H:%M:%S")
        )

        for tipo, cantidad in tipos_en_sobre.items():
            subtot = etree.SubElement(caratula, f"{{{NS}}}SubTotDTE")
            etree.SubElement(subtot, f"{{{NS}}}TpoDTE").text = str(tipo)
            etree.SubElement(subtot, f"{{{NS}}}NroDTE").text = str(cantidad)

        for i, dte_xml in enumerate(dtes_xml):
            try:
                parser = etree.XMLParser(remove_blank_text=True)
                dte_str2 = dte_xml
                if dte_str2.startswith('<?xml'):
                    dte_str2 = dte_str2[dte_str2.index('?>') + 2:].lstrip()
                dte_el = etree.fromstring(dte_str2, parser)

                if i < len(dtes_xml) - 1:
                    dte_el.tail = "\n"

                set_el.append(dte_el)

            except Exception as e:
                raise ValueError(f"DTE XML invalido: {e}")

        sobre_sin_firma = etree.tostring(envio_el, encoding="unicode")

        # Firma del sobre (esto SÍ se mantiene)
        sobre_firmado = firma_service.firmar_sobre(sobre_sin_firma)

        return sobre_firmado

    async def enviar_sobre(self, sobre_xml: str, rut_emisor: str,
                           rut_enviador: str,
                           p12_bytes: bytes = None,
                           password: str = None) -> dict:
        """
        MODO MANUAL:
        NO se envía al SII.
        Solo devuelve el XML generado.
        """

        logger.warning("[SII] MODO MANUAL - no se envía al SII")

        return {
            "track_id": None,
            "estado": "GENERADO",
            "mensaje": "XML generado correctamente (modo manual)",
            "xml": sobre_xml
        }
