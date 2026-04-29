# app/services/firma_sobre.py
# ══════════════════════════════════════════════════════════════
# Firma del sobre EnvioDTE para SII Chile
#
# REGLA CRÍTICA: El SignedInfo del SetDTE SÍ lleva xmlns:xsi.
# El EnvioDTE tiene xmlns:xsi declarado en el elemento raíz, por lo que
# ese namespace SÍ está en scope cuando el SII verifica la firma del sobre.
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


def _signed_info_sobre(reference_uri: str, digest_value: str) -> bytes:
    """
    C14N manual del SignedInfo para firma del SetDTE.

    CON xmlns:xsi — el EnvioDTE tiene xmlns:xsi en root, por lo que ese
    namespace SÍ está en scope cuando el SII verifica la firma del sobre.
    """
    c14n = C14N_ALGORITHM
    return (
        f'<SignedInfo xmlns="{XMLDSIG_NS}" xmlns:xsi="{XSI_NS}">'
        f'<CanonicalizationMethod Algorithm="{c14n}"></CanonicalizationMethod>'
        f'<SignatureMethod Algorithm="{XMLDSIG_NS}rsa-sha1"></SignatureMethod>'
        f'<Reference URI="{reference_uri}">'
        f'<Transforms><Transform Algorithm="{c14n}"></Transform></Transforms>'
        f'<DigestMethod Algorithm="{XMLDSIG_NS}sha1"></DigestMethod>'
        f'<DigestValue>{digest_value}</DigestValue>'
        f'</Reference>'
        f'</SignedInfo>'
    ).encode('utf-8')


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

        IMPORTANTE: sobre_xml debe ser el EnvioDTE completo con todos los DTEs
        ya insertados, pero SIN la Signature final del sobre.
        """
        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(sobre_xml.encode(), parser)
        ns     = {'sii': SII_NS}

        set_el = root.find(".//sii:SetDTE[@ID='SetDoc']", ns)

        # DigestValue del SetDTE: serializar standalone para C14N limpio
        set_raw        = etree.tostring(set_el, encoding='unicode')
        set_standalone = etree.fromstring(set_raw.encode())
        set_c14n       = etree.tostring(set_standalone, method='c14n', exclusive=False)
        digest_val     = b64encode(hashlib.sha1(set_c14n).digest()).decode()

        # SignedInfo CON xmlns:xsi (crítico para el sobre)
        si_c14n   = _signed_info_sobre('#SetDoc', digest_val)
        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()

        sig_xml = (
            f'<Signature xmlns="{XMLDSIG_NS}">'
            f'{si_c14n.decode("utf-8")}'
            f'<SignatureValue>{firma_b64}</SignatureValue>'
            f'<KeyInfo>'
            f'<KeyValue><RSAKeyValue>'
            f'<Modulus>{_wrap64(self._rsa_mod)}</Modulus>'
            f'<Exponent>{self._rsa_exp}</Exponent>'
            f'</RSAKeyValue></KeyValue>'
            f'<X509Data>'
            f'<X509Certificate>{_wrap64(self._cert_der_b64)}</X509Certificate>'
            f'</X509Data>'
            f'</KeyInfo>'
            f'</Signature>'
        )
        root.append(etree.fromstring(sig_xml.encode()))

        xml_body    = etree.tostring(root, encoding='unicode')
        declaracion = '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
        return declaracion + xml_body
