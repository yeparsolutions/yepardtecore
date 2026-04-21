# app/services/firma_digital.py
import logging
from lxml import etree
from signxml import XMLSigner, methods
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.backends import default_backend

# Importes para habilitar algoritmos antiguos si la versión lo permite
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes

logger = logging.getLogger("yepardtecore.firma")

class FirmaDigital:
    def __init__(self, p12_bytes: bytes, password: str):
        try:
            password_bytes = password.encode() if password else None
            self.key, self.cert, self.additional_certs = pkcs12.load_key_and_certificates(
                p12_bytes,
                password_bytes,
                default_backend()
            )
            logger.info("Certificado digital cargado correctamente.")
        except Exception as e:
            logger.error(f"Error al cargar certificado P12: {e}")
            raise ValueError("Contraseña de certificado incorrecta o archivo corrupto.")

    def firmar_dte(self, xml_bytes: bytes, folio: int, tipo_dte: int, fecha_emision=None, rut_emisor=None, monto_total=None) -> bytes:
        """
        Firma un documento individual (DTE) permitiendo SHA1 para el SII.
        """
        try:
            parser = etree.XMLParser(remove_blank_text=True, recover=True)
            root = etree.fromstring(xml_bytes, parser=parser)
            id_referencia = f"T{tipo_dte}F{folio}"
            
            # Configuramos el firmador específicamente para las exigencias del SII
            signer = XMLSigner(
                method=methods.enveloped,
                signature_algorithm="rsa-sha1",
                digest_algorithm="sha1",
                c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
            )

            # Forzamos la firma sobre el nodo con el ID correspondiente
            signed_root = signer.sign(
                root,
                key=self.key,
                cert=self.cert,
                reference_uri=f"#{id_referencia}"
            )
            
            return etree.tostring(signed_root, encoding="ISO-8859-1", xml_declaration=True)
        except Exception as e:
            logger.error(f"Error en firma DTE: {e}")
            # Si sigue fallando por SHA1 aquí, el problema es 100% la versión de cryptography en requirements.txt
            raise RuntimeError(f"Falla en firma individual: {str(e)}")

    def firmar_sobre(self, xml_sobre: bytes) -> bytes:
        """
        Firma el sobre EnvioDTE.
        """
        try:
            parser = etree.XMLParser(remove_blank_text=True, recover=True)
            root = etree.fromstring(xml_sobre, parser=parser)
            
            namespaces = {"sii": "http://www.sii.cl/SiiDte"}
            set_dte_node = root.xpath("//sii:SetDTE", namespaces=namespaces)
            ref_id = set_dte_node[0].attrib["ID"] if set_dte_node and "ID" in set_dte_node[0].attrib else "SetDoc"

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
                reference_uri=f"#{ref_id}"
            )
            return etree.tostring(signed_root, encoding="ISO-8859-1", xml_declaration=True)
        except Exception as e:
            logger.error(f"Error firmando sobre: {e}")
            raise RuntimeError(f"Falla en firma de sobre: {str(e)}")
