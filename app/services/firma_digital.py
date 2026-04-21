# app/services/firma_digital.py
# ══════════════════════════════════════════════════════════════
# Servicio de Firma Electrónica - Versión Compatible con Railway
# ══════════════════════════════════════════════════════════════

import logging
from lxml import etree
import signxml
from signxml import XMLSigner, methods
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.backends import default_backend

# Forzamos a signxml a usar la arquitectura moderna para evitar el error de pyOpenSSL
signxml.access_control_allow_dns_lookups = False

logger = logging.getLogger("yepardtecore.firma")

class FirmaDigital:
    def __init__(self, p12_bytes: bytes, password: str):
        """
        Carga el certificado P12 usando cryptography (evita pyOpenSSL).
        """
        try:
            # pkcs12.load_key_and_certificates devuelve (private_key, certificate, additional_certificates)
            self.key, self.cert, self.additional_certs = pkcs12.load_key_and_certificates(
                p12_bytes,
                password.encode() if password else None,
                default_backend()
            )
            
            if not self.key or not self.cert:
                raise ValueError("El archivo P12 no contiene una llave o certificado válido.")

            logger.info("Certificado digital cargado exitosamente.")

        except Exception as e:
            logger.error(f"Error al cargar certificado P12: {e}")
            raise ValueError(f"No se pudo abrir el certificado digital. Verifique la contraseña y el archivo.")

    def firmar_dte(self, xml_bytes: bytes, folio: int, tipo_dte: int, xml_caf: str = None) -> bytes:
        """
        Firma un documento individual (DTE).
        """
        try:
            parser = etree.XMLParser(remove_blank_text=True, recover=True)
            root = etree.fromstring(xml_bytes, parser=parser)

            # ID de referencia siguiendo el estándar del SII
            id_referencia = f"T{tipo_dte}F{folio}"
            
            signer = XMLSigner(
                method=methods.enveloped,
                signature_algorithm="rsa-sha1",
                digest_algorithm="sha1",
                c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
            )

            signed_root = signer.sign(
                root,
                key=self.key,
                cert=self.cert,
                reference_uri=f"#{id_referencia}"
            )

            return etree.tostring(signed_root, encoding="ISO-8859-1", xml_declaration=True)

        except Exception as e:
            logger.error(f"Error firmando DTE {tipo_dte} folio {folio}: {e}")
            raise RuntimeError(f"Falla en firma de documento individual: {str(e)}")

    def firmar_sobre(self, xml_sobre: bytes) -> bytes:
        """
        Firma el sobre electrónico (EnvioDTE) para el envío al SII.
        """
        try:
            parser = etree.XMLParser(remove_blank_text=True, recover=True)
            root = etree.fromstring(xml_sobre, parser=parser)

            signer = XMLSigner(
                method=methods.enveloped,
                signature_algorithm="rsa-sha1",
                digest_algorithm="sha1",
                c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
            )

            # Identificar el nodo SetDTE para la referencia
            # El prefijo suele ser del namespace del SII
            namespaces = {"sii": "http://www.sii.cl/SiiDte"}
            set_dte_node = root.xpath("//sii:SetDTE", namespaces=namespaces)
            
            ref_id = "SetDoc"
            if set_dte_node and "ID" in set_dte_node[0].attrib:
                ref_id = set_dte_node[0].attrib["ID"]

            signed_root = signer.sign(
                root,
                key=self.key,
                cert=self.cert,
                reference_uri=f"#{ref_id}"
            )

            return etree.tostring(signed_root, encoding="ISO-8859-1", xml_declaration=True)

        except Exception as e:
            logger.error(f"Error al firmar el sobre de envío: {e}")
            raise RuntimeError(f"No se pudo realizar la firma del sobre (EnvioDTE): {str(e)}")
