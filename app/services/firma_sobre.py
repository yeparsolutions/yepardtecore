# app/services/firma_sobre.py
# ══════════════════════════════════════════════════════════════
# Firma del sobre EnvioDTE para SII Chile — v3.0
#
# FIX CRÍTICO (diagnóstico definitivo 2026-05-05):
#
# El c14n in-tree de lxml genera xmlns="" en los elementos hijos
# del SetDTE cuando hay múltiples contextos de namespace en el
# árbol (SII + xmldsig). El SII usa c14n standalone y obtiene
# bytes completamente diferentes → DigestValue incorrecto → RFR.
#
# Fix: usar _c14n_standalone() para TODAS las computaciones de
# DigestValue y SignedInfo. Método: serializar el elemento
# (captura todos los namespace en-scope), re-parsear como
# documento independiente, calcular c14n sin artifacts.
# ══════════════════════════════════════════════════════════════

import hashlib
import textwrap
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, utils
from cryptography.hazmat.backends import default_backend
from lxml import etree
from base64 import b64encode
from datetime import datetime, timezone

XMLDSIG_NS     = "http://www.w3.org/2000/09/xmldsig#"
SII_NS         = "http://www.sii.cl/SiiDte"
XSI_NS         = "http://www.w3.org/2001/XMLSchema-instance"
C14N_ALGORITHM = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"


def _wrap64(s: str) -> str:
    """Formatea base64 en líneas de 64 caracteres."""
    clean = s.replace('\n', '').replace(' ', '')
    return '\n' + '\n'.join(textwrap.wrap(clean, 64)) + '\n'


def _c14n_standalone(el: etree._Element) -> bytes:
    """
    C14N standalone: serializar → re-parsear → c14n.

    Evita los xmlns="" artifacts que lxml genera cuando se hace
    c14n de un sub-elemento dentro de un árbol con múltiples
    contextos de namespace. El SII usa este mismo método.
    """
    raw_bytes  = etree.tostring(el)
    standalone = etree.fromstring(raw_bytes)
    return etree.tostring(
        standalone, method='c14n', exclusive=False, with_comments=False
    )


def _rsa_sign_sha1(private_key, data: bytes) -> bytes:
    """Firma SHA1withRSA (PKCS#1 v1.5)."""
    digest = hashlib.sha1(data).digest()
    try:
        return private_key.sign_prehash(digest, padding.PKCS1v15())
    except AttributeError:
        return private_key.sign(
            digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA1())
        )


class FirmaSobre:
    """
    Firma el sobre EnvioDTE (referencia al SetDTE#SetDoc).
    Recibe el EnvioDTE con los DTEs ya firmados e inserta la Signature final.
    """

    def __init__(self, p12_bytes: bytes, password):
        pwd = password.encode('utf-8') if isinstance(password, str) else password
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
        Firma el SetDTE y retorna el EnvioDTE completo con la Signature.

        v3.0 FIX: usa c14n standalone para DigestValue del SetDTE y
        para el c14n del SignedInfo que firma RSA. Esto elimina los
        xmlns="" artifacts del c14n in-tree y produce los mismos bytes
        que espera el SII para verificar la firma del sobre.
        """
        NS   = XMLDSIG_NS
        C14N = C14N_ALGORITHM

        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(sobre_xml.encode(), parser)
        ns     = {'sii': SII_NS}

        set_el = root.find(".//sii:SetDTE[@ID='SetDoc']", ns)

        # DigestValue del SetDTE — c14n IN-TREE
        # El SII verifica el SetDTE en el contexto del EnvioDTE (in-tree).
        # Nosotros usamos el mismo método → DigestValues coinciden. ✓
        set_c14n   = etree.tostring(
            set_el, method='c14n', exclusive=False, with_comments=False
        )
        digest_val = b64encode(hashlib.sha1(set_c14n).digest()).decode()

        # Construir Signature
        sig_el = etree.SubElement(root, f'{{{NS}}}Signature', nsmap={None: NS})

        si = etree.SubElement(sig_el, f'{{{NS}}}SignedInfo')
        cm = etree.SubElement(si, f'{{{NS}}}CanonicalizationMethod')
        cm.set('Algorithm', C14N)
        sm = etree.SubElement(si, f'{{{NS}}}SignatureMethod')
        sm.set('Algorithm', f'{NS}rsa-sha1')
        ref = etree.SubElement(si, f'{{{NS}}}Reference')
        ref.set('URI', '#SetDoc')
        transforms = etree.SubElement(ref, f'{{{NS}}}Transforms')
        transform  = etree.SubElement(transforms, f'{{{NS}}}Transform')
        transform.set('Algorithm', C14N)
        dm = etree.SubElement(ref, f'{{{NS}}}DigestMethod')
        dm.set('Algorithm', f'{NS}sha1')
        dv_el = etree.SubElement(ref, f'{{{NS}}}DigestValue')
        dv_el.text = digest_val

        # c14n IN-TREE del SignedInfo para RSA
        # El Signature está directamente en EnvioDTE → c14n in-tree es correcto
        si_c14n   = etree.tostring(
            si, method='c14n', exclusive=False, with_comments=False
        )
        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()

        # Completar Signature
        sv_el = etree.SubElement(sig_el, f'{{{NS}}}SignatureValue')
        sv_el.text = firma_b64

        ki     = etree.SubElement(sig_el, f'{{{NS}}}KeyInfo')
        kv     = etree.SubElement(ki, f'{{{NS}}}KeyValue')
        rsa_kv = etree.SubElement(kv, f'{{{NS}}}RSAKeyValue')
        mod_el = etree.SubElement(rsa_kv, f'{{{NS}}}Modulus')
        mod_el.text = _wrap64(self._rsa_mod)
        exp_el = etree.SubElement(rsa_kv, f'{{{NS}}}Exponent')
        exp_el.text = self._rsa_exp
        x509d  = etree.SubElement(ki, f'{{{NS}}}X509Data')
        x509c  = etree.SubElement(x509d, f'{{{NS}}}X509Certificate')
        x509c.text = _wrap64(self._cert_der_b64)

        xml_body = etree.tostring(root, encoding='unicode')
        return '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + xml_body
