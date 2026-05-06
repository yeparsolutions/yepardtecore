# app/services/firma_sobre.py  v3.1
# Firma del sobre EnvioDTE para SII Chile
#
# FIX v3.1: c14n in-tree para DigestValue del SetDTE y SignedInfo.
# El SII verifica el SetDTE en contexto del EnvioDTE (in-tree).
# Usar el mismo metodo garantiza que los valores coincidan.

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
C14N_ALGORITHM     = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
ENVELOPED_SIG_ALG  = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"


def _wrap64(s: str) -> str:
    """Formatea base64 en lineas de 64 caracteres."""
    clean = s.replace('\n', '').replace(' ', '')
    return '\n' + '\n'.join(textwrap.wrap(clean, 64)) + '\n'


def _c14n_standalone(el) -> bytes:
    """
    C14N standalone: serializa el elemento con sus namespaces en-scope,
    re-parsea como documento independiente, calcula c14n.
    Este es el metodo que el SII usa para verificar el DigestValue del SetDTE.
    El codigo original que producia EPR usaba este metodo.
    In-tree c14n produce xmlns="" spurios en los elementos dentro del SetDTE
    (causados por los DTE-level Signatures con nsmap XMLDSIG), lo que da
    un DigestValue diferente al que espera el SII.
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
    """Firma el sobre EnvioDTE (referencia al SetDTE#SetDoc)."""

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

        Usa c14n in-tree para DigestValue del SetDTE y para el c14n del
        SignedInfo. El SII verifica usando el mismo metodo, por lo que
        los valores coinciden exactamente.
        """
        NS   = XMLDSIG_NS
        C14N = C14N_ALGORITHM

        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(sobre_xml.encode(), parser)
        ns     = {'sii': SII_NS}

        set_el = root.find(".//sii:SetDTE[@ID='SetDoc']", ns)

        # DigestValue del SetDTE con _c14n_standalone.
        # CRITICO: in-tree c14n del SetDTE produce xmlns="" spurios en los
        # elementos que siguen a los DTE-level Signatures (que tienen nsmap
        # XMLDSIG_NS), dando un DigestValue distinto al que espera el SII.
        # El codigo ORIGINAL que daba EPR (01/05) usaba _c14n_standalone.
        set_c14n   = _c14n_standalone(set_el)
        digest_val = b64encode(hashlib.sha1(set_c14n).digest()).decode()

        # Construir Signature
        sig_el = etree.SubElement(root, f'{{{NS}}}Signature',
                                   nsmap={None: NS})

        si = etree.SubElement(sig_el, f'{{{NS}}}SignedInfo')
        cm = etree.SubElement(si, f'{{{NS}}}CanonicalizationMethod')
        cm.set('Algorithm', C14N)
        sm = etree.SubElement(si, f'{{{NS}}}SignatureMethod')
        sm.set('Algorithm', f'{NS}rsa-sha1')
        ref = etree.SubElement(si, f'{{{NS}}}Reference')
        ref.set('URI', '#SetDoc')
        # enveloped-signature es el Transform correcto para EnvioDTE
        transforms = etree.SubElement(ref, f'{{{NS}}}Transforms')
        transform  = etree.SubElement(transforms, f'{{{NS}}}Transform')
        transform.set('Algorithm', ENVELOPED_SIG_ALG)
        dm = etree.SubElement(ref, f'{{{NS}}}DigestMethod')
        dm.set('Algorithm', f'{NS}sha1')
        dv_el = etree.SubElement(ref, f'{{{NS}}}DigestValue')
        dv_el.text = digest_val

        # c14n STANDALONE del SignedInfo para RSA.
        # CRITICO: el c14n in-tree agrega xmlns="" en Transforms/Transform/etc.
        # porque el sig_el tiene nsmap={None: XMLDSIG_NS} dentro de un root
        # con nsmap={None: SII_NS}. El SII verifica con standalone (sin esos
        # artifacts) -> el in-tree no coincide -> RFR.
        # standalone = etree.tostring(si) captura xmlns en-scope correctamente,
        # re-parsear elimina la contaminacion del arbol padre.
        _si_raw   = etree.tostring(si)
        _si_alone = etree.fromstring(_si_raw)
        si_c14n   = etree.tostring(
            _si_alone, method='c14n', exclusive=False, with_comments=False
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
