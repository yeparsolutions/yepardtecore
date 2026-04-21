# app/services/firma_digital.py
# ══════════════════════════════════════════════════════════════
# Servicio de Firma Electrónica - Estándar XMLDSig (SII Chile)
# ══════════════════════════════════════════════════════════════

import logging
from lxml import etree
from signxml import XMLSigner, methods
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.backends import default_backend

logger = logging.getLogger("yepardtecore.firma")

class FirmaDigital:
    def __init__(self, p12_bytes: bytes, password: str):
        """
        Carga el certificado P12 para realizar firmas digitales.
        """
        try:
            # Extraer llave privada y certificado del archivo P12
            self.key, self.cert, self.additional_certs = pkcs12.load_key_and_certificates(
                p12_bytes,
                password.encode() if password else None,
                default_backend()
            )
            
            # Extraer RUT del certificado para logs (opcional)
            self.rut_certificado = None
            if self.cert:
                subject = self.cert.subject.rfc4514_string()
                # El RUT suele venir en el campo serialNumber o dentro del CN
                logger.info(f"Certificado cargado correctamente: {subject}")

        except Exception as e:
            logger.error(f"Error al cargar certificado P12: {e}")
            raise ValueError(f"No se pudo abrir el certificado digital. Verifique la contraseña.")

    def firmar_dte(self, xml_bytes: bytes, folio: int, tipo_dte: int, xml_caf: str = None) -> bytes:
        """
        Firma un documento individual (DTE).
        """
        try:
            parser = etree.XMLParser(remove_blank_text=True, recover=True)
            root = etree.fromstring(xml_bytes, parser=parser)

            # El ID que se firma debe coincidir con el ID del nodo Documento
            # Ej: ID="T33F5" (Tipo 33, Folio 5)
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
        MÉTODO CORREGIDO: Firma el sobre electrónico (EnvioDTE)
        Este es el sello final que envuelve a todos los DTEs.
        """
        try:
            # Configurar el parser para preservar la estructura exacta
            parser = etree.XMLParser(remove_blank_text=True, recover=True)
            root = etree.fromstring(xml_sobre, parser=parser)

            # El SII exige que el sobre se firme referenciando el ID del SetDTE (usualmente 'SetDoc')
            signer = XMLSigner(
                method=methods.enveloped,
                signature_algorithm="rsa-sha1",
                digest_algorithm="sha1",
                c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
            )

            # Buscamos el nodo SetDTE para confirmar su ID
            set_dte_node = root.find(".//{http://www.sii.cl/SiiDte}SetDTE")
            ref_id = "SetDoc" # Valor por defecto
            if set_dte_node is not None and "ID" in set_dte_node.attrib:
                ref_id = set_dte_node.attrib["ID"]

            signed_root = signer.sign(
                root,
                key=self.key,
                cert=self.cert,
                reference_uri=f"#{ref_id}"
            )

            # El SII requiere codificación ISO-8859-1
            return etree.tostring(signed_root, encoding="ISO-8859-1", xml_declaration=True)

        except Exception as e:
            logger.error(f"Error al firmar el sobre de envío: {e}")
            raise RuntimeError(f"No se pudo realizar la firma del sobre (EnvioDTE): {str(e)}")
