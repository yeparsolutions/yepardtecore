# app/services/firma_digital.py
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from lxml import etree
from base64 import b64encode
import hashlib
import textwrap

SII_NS = "http://www.sii.cl/SiiDte"
XMLDSIG_NS = "http://www.w3.org/2000/09/xmldsig#"

def _wrap64(s: str) -> str:
    clean = s.replace('\n', '').replace(' ', '')
    return '\n' + '\n'.join(textwrap.wrap(clean, 64)) + '\n'

class FirmaDigital:
    def __init__(self, p12_data: bytes, password: str):
        p12 = pkcs12.load_key_and_certificates(p12_data, password.encode() if password else None, default_backend())
        self._private_key = p12[0]
        self._certificate = p12[1]
        cert_bytes = self._certificate.public_bytes(serialization.Encoding.DER)
        self._cert_b64 = _wrap64(b64encode(cert_bytes).decode())
        pub_numbers = self._private_key.public_key().public_numbers()
        self._modulus = _wrap64(b64encode(pub_numbers.n.to_bytes((pub_numbers.n.bit_length() + 7) // 8, 'big')).decode())
        self._exponent = b64encode(pub_numbers.e.to_bytes((pub_numbers.e.bit_length() + 7) // 8, 'big')).decode()

    def firmar_dte(self, xml_bytes: bytes, folio: int, tipo_dte: int, **kwargs) -> bytes:
        parser = etree.XMLParser(remove_blank_text=True, recover=True)
        root = etree.fromstring(xml_bytes, parser)
        
        # BUSQUEDA ROBUSTA: Intenta encontrar Documento con o sin namespace
        documento = root.find(".//{http://www.sii.cl/SiiDte}Documento")
        if documento is None:
            documento = root.find(".//Documento")
        
        if documento is None:
            # Si aún es None, el XML viene mal formado desde el builder
            raise ValueError("No se encontró el nodo <Documento> en el XML generado")

        doc_id = f"T{tipo_dte}F{folio}"
        documento.set("ID", doc_id)

        # Proceso de firma
        c14n_doc = etree.tostring(documento, method="c14n", exclusive=False)
        digest_val = b64encode(hashlib.sha1(c14n_doc).digest()).decode()

        signed_info = (
            f'<SignedInfo xmlns="{XMLDSIG_NS}">'
            f'<CanonicalizationMethod Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"></CanonicalizationMethod>'
            f'<SignatureMethod Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1"></SignatureMethod>'
            f'<Reference URI="#{doc_id}">'
            f'<Transforms><Transform Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"></Transform></Transforms>'
            f'<DigestMethod Algorithm="http://www.w3.org/2000/09/xmldsig#sha1"></DigestMethod>'
            f'<DigestValue>{digest_val}</DigestValue>'
            f'</Reference></SignedInfo>'
        )
        
        si_el = etree.fromstring(signed_info)
        si_c14n = etree.tostring(si_el, method="c14n", exclusive=False)
        sig_val = b64encode(self._private_key.sign(si_c14n, padding.PKCS1v15(), hashes.SHA1())).decode()

        signature_xml = (
            f'<Signature xmlns="{XMLDSIG_NS}">{signed_info}'
            f'<SignatureValue>{_wrap64(sig_val)}</SignatureValue>'
            f'<KeyInfo><KeyValue><RSAKeyValue><Modulus>{self._modulus}</Modulus>'
            f'<Exponent>{self._exponent}</Exponent></RSAKeyValue></KeyValue>'
            f'<X509Data><X509Certificate>{self._cert_b64}</X509Certificate></X509Data>'
            f'</KeyInfo></Signature>'
        )
        root.append(etree.fromstring(signature_xml))
        return etree.tostring(root, encoding="ISO-8859-1", xml_declaration=True)
