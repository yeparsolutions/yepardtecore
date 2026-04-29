# app/services/firma_sobre.py
# ══════════════════════════════════════════════════════════════
# Firma del sobre EnvioDTE para SII Chile
#
# REGLA CRÍTICA — CANONICALIZACIÓN EN CONTEXTO:
# lxml.etree.tostring(elem, method='c14n') en un elemento NO-raíz produce
# xmlns='' en elementos profundos. Apache XMLSEC (Java) del SII produce el
# mismo output. El round-trip standalone los elimina → digest diferente → RCH.
#
# POR LO TANTO:
# 1. DigestValue del SetDTE: c14n directo desde el árbol del EnvioDTE.
# 2. SignedInfo: construido como árbol lxml dentro de Signature dentro del
#    EnvioDTE; el c14n se obtiene desde ese árbol y ESE BYTES se firma.
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


def _build_signed_info_element(parent_sig: etree._Element,
                               reference_uri: str, digest_value: str) -> etree._Element:
    """
    Construye el elemento <SignedInfo> como árbol lxml dentro de <Signature>.
    Al estar dentro del árbol EnvioDTE (nsmap={None: SII_NS, xsi: XSI_NS}),
    lxml produce el c14n correcto con xmlns='' en los hijos de <Reference>,
    idéntico al output de Apache XMLSEC del SII.
    """
    si = etree.SubElement(parent_sig, f"{{{XMLDSIG_NS}}}SignedInfo")

    cm = etree.SubElement(si, f"{{{XMLDSIG_NS}}}CanonicalizationMethod")
    cm.set("Algorithm", C14N_ALGORITHM)

    sm = etree.SubElement(si, f"{{{XMLDSIG_NS}}}SignatureMethod")
    sm.set("Algorithm", f"{XMLDSIG_NS}rsa-sha1")

    ref = etree.SubElement(si, f"{{{XMLDSIG_NS}}}Reference")
    ref.set("URI", reference_uri)

    transforms = etree.SubElement(ref, f"{{{XMLDSIG_NS}}}Transforms")
    t = etree.SubElement(transforms, f"{{{XMLDSIG_NS}}}Transform")
    t.set("Algorithm", C14N_ALGORITHM)

    dm = etree.SubElement(ref, f"{{{XMLDSIG_NS}}}DigestMethod")
    dm.set("Algorithm", f"{XMLDSIG_NS}sha1")

    dv = etree.SubElement(ref, f"{{{XMLDSIG_NS}}}DigestValue")
    dv.text = digest_value

    return si


class FirmaSobre:
    """
    Firma el sobre EnvioDTE (SetDTE).

    Recibe el EnvioDTE XML con los DTEs ya firmados e inserta la firma del sobre.
    """

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
        Firma el SetDTE y devuelve el EnvioDTE completo como string ISO-8859-1.

        FLUJO:
        1. Calcular DigestValue del SetDTE haciendo c14n DIRECTAMENTE desde
           el árbol del EnvioDTE (nsmap={None: SII_NS, xsi: XSI_NS}).
           lxml produce xmlns='' en elementos profundos — igual que Apache XMLSEC.
           NO usar round-trip standalone: produce un c14n diferente → RCH.
        2. Construir <Signature><SignedInfo>... como árbol lxml dentro del EnvioDTE,
           obtener c14n del <SignedInfo> desde ese árbol, firmar ESOS bytes.
        3. Completar <Signature> con <SignatureValue> y <KeyInfo>.
        """
        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(sobre_xml.encode(), parser)
        ns     = {'sii': SII_NS}

        set_el = root.find(".//sii:SetDTE[@ID='SetDoc']", ns)

        # ── DigestValue del SetDTE ────────────────────────────────────────────
        # c14n directo desde el árbol (con xmlns='' en hijos profundos).
        # NO round-trip standalone: produce digest diferente al del SII.
        set_c14n   = etree.tostring(set_el, method='c14n', exclusive=False)
        digest_val = b64encode(hashlib.sha1(set_c14n).digest()).decode()

        # ── Construir <Signature> como árbol lxml dentro del EnvioDTE ────────
        sig_el = etree.SubElement(root, f"{{{XMLDSIG_NS}}}Signature",
                                  nsmap={None: XMLDSIG_NS})

        si_el     = _build_signed_info_element(sig_el, '#SetDoc', digest_val)
        si_c14n   = etree.tostring(si_el, method='c14n', exclusive=False)
        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()

        etree.SubElement(sig_el, f"{{{XMLDSIG_NS}}}SignatureValue").text = firma_b64

        ki = etree.SubElement(sig_el, f"{{{XMLDSIG_NS}}}KeyInfo")
        kv = etree.SubElement(ki, f"{{{XMLDSIG_NS}}}KeyValue")
        rk = etree.SubElement(kv, f"{{{XMLDSIG_NS}}}RSAKeyValue")
        etree.SubElement(rk, f"{{{XMLDSIG_NS}}}Modulus").text  = _wrap64(self._rsa_mod)
        etree.SubElement(rk, f"{{{XMLDSIG_NS}}}Exponent").text = self._rsa_exp
        x5 = etree.SubElement(ki, f"{{{XMLDSIG_NS}}}X509Data")
        etree.SubElement(x5, f"{{{XMLDSIG_NS}}}X509Certificate").text = _wrap64(self._cert_der_b64)

        xml_body    = etree.tostring(root, encoding='unicode')
        declaracion = '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
        return declaracion + xml_body
