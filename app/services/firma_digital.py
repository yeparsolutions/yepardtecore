# app/services/firma_digital.py
import logging
from lxml import etree
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.backends import default_backend

# Importación diferida para evitar fallos en el arranque del servidor
try:
    from signxml import XMLSigner, methods
except ImportError:
    XMLSigner = None
    methods = None

logger = logging.getLogger("yepardtecore.firma")

class FirmaDigital:
    def __init__(self, p12_bytes: bytes, password: str):
        try:
            self.key, self.cert, self.additional_certs = pkcs12.load_key_and_certificates(
                p12_bytes,
                password.encode() if password else None,
                default_backend()
            )
            logger.info("Certificado cargado correctamente.")
        except Exception as e:
            logger.error(f"Error al cargar P12: {e}")
            raise ValueError("Contraseña de certificado incorrecta o archivo corrupto.")

    def firmar_dte(self, xml_bytes: bytes, folio: int, tipo_dte: int, xml_caf: str = None) -> bytes:
        if XMLSigner is None:
            raise RuntimeError("La librería signxml no se cargó correctamente debido a dependencias faltantes.")
            
        try:
            parser = etree.XMLParser(remove_blank_text=True, recover=True)
            root = etree.fromstring(xml_bytes, parser=parser)
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
            logger.error(f"Error firmando DTE: {e}")
            raise RuntimeError(f"Falla en firma individual: {e}")

    def firmar_sobre(self, xml_sobre: bytes) -> bytes:
        if XMLSigner is None:
            raise RuntimeError("La librería signxml no pudo iniciar.")

        try:
            parser = etree.XMLParser(remove_blank_text=True, recover=True)
            root = etree.fromstring(xml_sobre, parser=parser)
            
            signer = XMLSigner(
                method=methods.enveloped,
                signature_algorithm="rsa-sha1",
                digest_algorithm="sha1",
                c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
            )

            namespaces = {"sii": "http://www.sii.cl/SiiDte"}
            set_dte_node = root.xpath("//sii:SetDTE", namespaces=namespaces)
            ref_id = set_dte_node[0].attrib["ID"] if set_dte_node and "ID" in set_dte_node[0].attrib else "SetDoc"

            signed_root = signer.sign(
                root,
                key=self.key,
                cert=self.cert,
                reference_uri=f"#{ref_id}"
            )
            return etree.tostring(signed_root, encoding="ISO-8859-1", xml_declaration=True)
        except Exception as e:
            logger.error(f"Error firmando sobre: {e}")
            raise RuntimeError(f"Falla en firma de sobre: {e}")
