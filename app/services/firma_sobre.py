# app/services/firma_sobre.py
# ══════════════════════════════════════════════════════════════
# Firma del sobre EnvioDTE para SII Chile
#
# MISMO PRINCIPIO que firma_dte.py:
# 1. Construir Signature con etree
# 2. Insertar en el árbol del EnvioDTE
# 3. Calcular C14N del SignedInfo EN ESE CONTEXTO
# 4. Firmar esos bytes exactos
# ══════════════════════════════════════════════════════════════

from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, utils
from cryptography.hazmat.backends import default_backend
from lxml import etree
from base64 import b64encode, b64decode
import hashlib
import textwrap

XMLDSIG_NS     = "http://www.w3.org/2000/09/xmldsig#"
SII_NS         = "http://www.sii.cl/SiiDte"
XSI_NS         = "http://www.w3.org/2001/XMLSchema-instance"
C14N_ALGORITHM = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"


def _wrap64(s: str) -> str:
    clean = s.replace('\n', '').replace(' ', '')
    return '\n' + '\n'.join(textwrap.wrap(clean, 64)) + '\n'


def _rsa_sign_sha1(private_key, data: bytes) -> bytes:
    digest = hashlib.sha1(data).digest()
    try:
        return private_key.sign_prehash(digest, padding.PKCS1v15())
    except AttributeError:
        return private_key.sign(
            digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA1())
        )


class FirmaSobre:
    """Firma el sobre EnvioDTE (SetDTE) para SII Chile."""

    def __init__(self, p12_bytes: bytes, password: str):
        pwd  = password.encode() if isinstance(password, str) else password
        priv, cert, _ = pkcs12.load_key_and_certificates(
            p12_bytes, pwd, backend=default_backend()
        )
        self._private_key  = priv
        self._cert_der_b64 = b64encode(
            cert.public_bytes(serialization.Encoding.DER)
        ).decode()
        pub = cert.public_key().public_numbers()
        self._rsa_mod = b64encode(
            pub.n.to_bytes((pub.n.bit_length() + 7) // 8, 'big')
        ).decode()
        self._rsa_exp = b64encode(
            pub.e.to_bytes((pub.e.bit_length() + 7) // 8, 'big')
        ).decode()

    def firmar(self, sobre_xml: str) -> str:
        """
        Firma el SetDTE y devuelve el EnvioDTE completo.

        FLUJO (idéntico a FirmaDTE):
        1. Calcular DigestValue del SetDTE
        2. Construir Signature con etree
        3. Insertar en el árbol del EnvioDTE
        4. Calcular C14N del SignedInfo EN CONTEXTO del EnvioDTE
        5. Firmar esos bytes exactos
        6. Actualizar SignatureValue
        7. Serializar
        """
        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(sobre_xml.encode(), parser)
        ns     = {'sii': SII_NS}

        # 1. DigestValue del SetDTE
        set_el = root.find(".//sii:SetDTE[@ID='SetDoc']", ns)
        set_raw        = etree.tostring(set_el, encoding='unicode')
        set_standalone = etree.fromstring(set_raw.encode())
        set_c14n       = etree.tostring(set_standalone, method='c14n', exclusive=False)
        digest_val     = b64encode(hashlib.sha1(set_c14n).digest()).decode()

        # 2. Construir Signature con etree
        sig_el = self._build_signature_element('#SetDoc', digest_val)

        # 3. Insertar en árbol EnvioDTE
        root.append(sig_el)

        # 4. Calcular C14N del SignedInfo EN CONTEXTO del EnvioDTE
        #    (con xmlns:xsi y cualquier xmlns en scope del EnvioDTE)
        si_el   = sig_el.find(f"{{{XMLDSIG_NS}}}SignedInfo")
        si_c14n = etree.tostring(si_el, method='c14n', exclusive=False)

        # 5. Firmar esos bytes exactos
        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()

        # 6. Actualizar SignatureValue
        sv_el = sig_el.find(f"{{{XMLDSIG_NS}}}SignatureValue")
        sv_el.text = firma_b64

        # 7. Serializar
        xml_body    = etree.tostring(root, encoding='unicode')
        declaracion = '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
        return declaracion + xml_body

    def _build_signature_element(self, reference_uri: str,
                                  digest_value: str) -> etree._Element:
        ns = XMLDSIG_NS
        sig_el = etree.Element(f"{{{ns}}}Signature", nsmap={None: ns})

        si_el  = etree.SubElement(sig_el, f"{{{ns}}}SignedInfo")
        etree.SubElement(si_el, f"{{{ns}}}CanonicalizationMethod",
                         attrib={"Algorithm": C14N_ALGORITHM})
        etree.SubElement(si_el, f"{{{ns}}}SignatureMethod",
                         attrib={"Algorithm": f"{ns}rsa-sha1"})
        ref_el = etree.SubElement(si_el, f"{{{ns}}}Reference",
                                   attrib={"URI": reference_uri})
        trf_el = etree.SubElement(ref_el, f"{{{ns}}}Transforms")
        etree.SubElement(trf_el, f"{{{ns}}}Transform",
                         attrib={"Algorithm": C14N_ALGORITHM})
        etree.SubElement(ref_el, f"{{{ns}}}DigestMethod",
                         attrib={"Algorithm": f"{ns}sha1"})
        dv_el  = etree.SubElement(ref_el, f"{{{ns}}}DigestValue")
        dv_el.text = digest_value

        sv_el  = etree.SubElement(sig_el, f"{{{ns}}}SignatureValue")
        sv_el.text = ''

        ki_el  = etree.SubElement(sig_el, f"{{{ns}}}KeyInfo")
        kv_el  = etree.SubElement(ki_el, f"{{{ns}}}KeyValue")
        rsa_el = etree.SubElement(kv_el, f"{{{ns}}}RSAKeyValue")
        mod_el = etree.SubElement(rsa_el, f"{{{ns}}}Modulus")
        mod_el.text = _wrap64(self._rsa_mod)
        exp_el = etree.SubElement(rsa_el, f"{{{ns}}}Exponent")
        exp_el.text = self._rsa_exp
        x509d  = etree.SubElement(ki_el, f"{{{ns}}}X509Data")
        cert_el = etree.SubElement(x509d, f"{{{ns}}}X509Certificate")
        cert_el.text = _wrap64(self._cert_der_b64)

        return sig_el
