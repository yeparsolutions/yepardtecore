# app/services/firma_digital.py
# ══════════════════════════════════════════════════════════════
# Servicio de Firma Electrónica - Versión XMLSIG (Sin dependencias rotas)
# ══════════════════════════════════════════════════════════════

import logging
import base64
from lxml import etree
import xmlsig
from cryptography.hazmat.primitives.serialization import pkcs12, Encoding
from cryptography.hazmat.backends import default_backend

logger = logging.getLogger("yepardtecore.firma")

class FirmaDigital:
    def __init__(self, p12_bytes: bytes, password: str):
        """
        Carga el certificado P12 usando cryptography para evitar errores de OpenSSL.
        """
        try:
            # Extraer llave privada y certificado
            self.key, self.cert, self.additional_certs = pkcs12.load_key_and_certificates(
                p12_bytes,
                password.encode() if password else None,
                default_backend()
            )
            logger.info("Certificado digital cargado correctamente.")
        except Exception as e:
            logger.error(f"Error al cargar certificado P12: {e}")
            raise ValueError(f"No se pudo abrir el certificado digital. Verifique la contraseña.")

    def firmar_dte(self, xml_bytes: bytes, folio: int, tipo_dte: int, xml_caf: str = None) -> bytes:
        """
        Firma un documento individual (DTE) siguiendo el estándar del SII.
        """
        try:
            parser = etree.XMLParser(remove_blank_text=True, recover=True)
            root = etree.fromstring(xml_bytes, parser=parser)
            
            # ID de referencia requerido por el SII (ej: T33F10)
            id_referencia = f"T{tipo_dte}F{folio}"
            
            # Configurar contexto de firma
            signer = xmlsig.SignContext()
            signer.private_key = self.key
            
            # Crear estructura de la firma
            signature_node = xmlsig.template.create(
                xmlsig.constants.TransformEnveloped,
                xmlsig.constants.TransformC14N,
                xmlsig.constants.MethodRsaSha1
            )
            
            # Añadir referencia al ID del documento
            ref = xmlsig.template.add_reference(
                signature_node, 
                xmlsig.constants.TransformSha1, 
                uri=f"#{id_referencia}"
            )
            xmlsig.template.add_transform(ref, xmlsig.constants.TransformEnveloped)
            
            # Añadir el certificado público al nodo KeyInfo (Obligatorio para el SII)
            xmlsig.template.add_key_info_cert(signature_node)
            
            root.append(signature_node)
            signer.sign(signature_node)
            
            # Inyectar el certificado en formato Base64 dentro de X509Certificate
            self._inyectar_x509_certificate(signature_node)

            return etree.tostring(root, encoding="ISO-8859-1", xml_declaration=True)
            
        except Exception as e:
            logger.error(f"Error en firma DTE {tipo_dte} folio {folio}: {e}")
            raise RuntimeError(f"Falla en firma de documento individual: {str(e)}")

    def firmar_sobre(self, xml_sobre: bytes) -> bytes:
        """
        Firma el sobre electrónico EnvioDTE (referencia al ID del SetDTE).
        """
        try:
            parser = etree.XMLParser(remove_blank_text=True, recover=True)
            root = etree.fromstring(xml_sobre, parser=parser)
            
            # Buscar el ID del SetDTE (por defecto SetDoc)
            namespaces = {"sii": "http://www.sii.cl/SiiDte"}
            set_dte_node = root.xpath("//sii:SetDTE", namespaces=namespaces)
            ref_id = set_dte_node[0].attrib["ID"] if set_dte_node and "ID" in set_dte_node[0].attrib else "SetDoc"

            signer = xmlsig.SignContext()
            signer.private_key = self.key
            
            signature_node = xmlsig.template.create(
                xmlsig.constants.TransformEnveloped,
                xmlsig.constants.TransformC14N,
                xmlsig.constants.MethodRsaSha1
            )
            
            ref = xmlsig.template.add_reference(
                signature_node, 
                xmlsig.constants.TransformSha1, 
                uri=f"#{ref_id}"
            )
            xmlsig.template.add_transform(ref, xmlsig.constants.TransformEnveloped)
            xmlsig.template.add_key_info_cert(signature_node)
            
            root.append(signature_node)
            signer.sign(signature_node)
            
            self._inyectar_x509_certificate(signature_node)

            return etree.tostring(root, encoding="ISO-8859-1", xml_declaration=True)
        except Exception as e:
            logger.error(f"Error al firmar sobre de envío: {e}")
            raise RuntimeError(f"No se pudo realizar la firma del sobre (EnvioDTE): {str(e)}")

    def _inyectar_x509_certificate(self, signature_node):
        """Método auxiliar para cumplir con el formato X509Certificate del SII."""
        ki_node = signature_node.find("{http://www.w3.org/2000/09/xmldsig#}KeyInfo")
        x509_data = ki_node.find("{http://www.w3.org/2000/09/xmldsig#}X509Data")
        if x509_data is not None:
            x509_cert = etree.SubElement(x509_data, "{http://www.w3.org/2000/09/xmldsig#}X509Certificate")
            cert_der = self.cert.public_bytes(Encoding.DER)
            x509_cert.text = base64.b64encode(cert_der).decode()
