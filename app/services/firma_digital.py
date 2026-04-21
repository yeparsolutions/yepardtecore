# app/services/firma_digital.py
# ══════════════════════════════════════════════════════════════
# Servicio de Firma Digital para DTE Chile - Versión Certificación
# ══════════════════════════════════════════════════════════════

from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from lxml import etree
from base64 import b64encode, b64decode
import hashlib
import textwrap

# Constantes de Namespaces
XMLDSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
SII_NS     = "http://www.sii.cl/SiiDte"
XSI_NS     = "http://www.w3.org/2001/XMLSchema-instance"

def _wrap64(s: str) -> str:
    """Formatea base64 a 64 caracteres por línea (Estándar SII)."""
    clean = s.replace('\n', '').replace(' ', '')
    return '\n' + '\n'.join(textwrap.wrap(clean, 64)) + '\n'

class FirmaDigital:
    def __init__(self, p12_data: bytes, password: str):
        try:
            # Cargar certificado y llave privada
            password_bytes = password.encode() if password else None
            p12 = pkcs12.load_key_and_certificates(p12_data, password_bytes, default_backend())
            
            self._private_key = p12[0]
            self._certificate = p12[1]
            
            # Extraer el certificado en base64 para el XML
            cert_bytes = self._certificate.public_bytes(serialization.Encoding.DER)
            self._cert_b64 = _wrap64(b64encode(cert_bytes).decode())
            
            # Extraer el módulo de la llave pública (RSA)
            pub_numbers = self._private_key.public_key().public_numbers()
            self._modulus = _wrap64(b64encode(pub_numbers.n.to_bytes((pub_numbers.n.bit_length() + 7) // 8, 'big')).decode())
            self._exponent = b64encode(pub_numbers.e.to_bytes((pub_numbers.e.bit_length() + 7) // 8, 'big')).decode()
            
        except Exception as e:
            raise Exception(f"Error al cargar certificado P12: {str(e)}")

    def firmar_dte(self, xml_bytes: bytes, folio: int, tipo_dte: int, **kwargs) -> bytes:
        """
        Firma un documento DTE individual.
        """
        parser = etree.XMLParser(remove_blank_text=True)
        root = etree.fromstring(xml_bytes, parser)
        
        # El ID del documento debe coincidir con lo que el Builder generó
        doc_id = f"T{tipo_dte}F{folio}"
        documento = root.find("Documento")
        if documento is None or documento.get("ID") != doc_id:
             # Si no tiene ID o no coincide, lo forzamos (necesario para el Reference URI)
             documento.set("ID", doc_id)

        # 1. Calcular Digest del Documento (C14N)
        # Importante: No usar exclusive=True para el SII
        c14n_doc = etree.tostring(documento, method="c14n", exclusive=False)
        digest_val = b64encode(hashlib.sha1(c14n_doc).digest()).decode()

        # 2. Construir SignedInfo
        signed_info = self._build_signed_info(f"#{doc_id}", digest_val)
        
        # 3. Calcular Firma del SignedInfo
        si_element = etree.fromstring(signed_info)
        si_c14n = etree.tostring(si_element, method="c14n", exclusive=False)
        
        signature_value = b64encode(
            self._private_key.sign(si_c14n, padding.PKCS1v15(), hashes.SHA1())
        ).decode()

        # 4. Construir Nodo Signature completo
        signature_xml = self._build_signature_node(signed_info, signature_value)
        root.append(etree.fromstring(signature_xml))

        # El SII requiere declaración XML con ISO-8859-1
        return etree.tostring(root, encoding="ISO-8859-1", xml_declaration=True)

    def _build_signed_info(self, uri: str, digest_val: str) -> str:
        return (
            f'<SignedInfo xmlns="{XMLDSIG_NS}">'
            f'<CanonicalizationMethod Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"></CanonicalizationMethod>'
            f'<SignatureMethod Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1"></SignatureMethod>'
            f'<Reference URI="{uri}">'
            f'<Transforms>'
            f'<Transform Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"></Transform>'
            f'</Transforms>'
            f'<DigestMethod Algorithm="http://www.w3.org/2000/09/xmldsig#sha1"></DigestMethod>'
            f'<DigestValue>{digest_val}</DigestValue>'
            f'</Reference>'
            f'</SignedInfo>'
        )

    def _build_signature_node(self, signed_info: str, signature_val: str) -> str:
        # El SII requiere que el nodo Signature NO tenga prefijos (ds:)
        return (
            f'<Signature xmlns="{XMLDSIG_NS}">'
            f'{signed_info}'
            f'<SignatureValue>{_wrap64(signature_val)}</SignatureValue>'
            f'<KeyInfo>'
            f'<KeyValue>'
            f'<RSAKeyValue>'
            f'<Modulus>{self._modulus}</Modulus>'
            f'<Exponent>{self._exponent}</Exponent>'
            f'</RSAKeyValue>'
            f'</KeyValue>'
            f'<X509Data>'
            f'<X509Certificate>{self._cert_b64}</X509Certificate>'
            f'</X509Data>'
            f'</KeyInfo>'
            f'</Signature>'
        )
