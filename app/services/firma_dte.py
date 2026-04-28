# app/services/firma_dte.py
# ══════════════════════════════════════════════════════════════
# Firma individual de DTEs para SII Chile (versión robusta)
# ══════════════════════════════════════════════════════════════

from cryptography.hazmat.primitives.serialization import pkcs12, load_pem_private_key
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, utils
from cryptography.hazmat.backends import default_backend
from lxml import etree
from base64 import b64encode
from datetime import datetime, timezone
import hashlib
import re
import textwrap

XMLDSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
SII_NS     = "http://www.sii.cl/SiiDte"

C14N = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"


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


class FirmaDTE:

    def __init__(self, p12_bytes: bytes, password: str):
        pwd = password.encode() if isinstance(password, str) else password
        priv, cert, _ = pkcs12.load_key_and_certificates(
            p12_bytes, pwd, backend=default_backend()
        )

        self._private_key  = priv
        self._cert         = cert
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

    # ✅ FIX: requerido por tu app
    @property
    def rut_certificado(self) -> str:
        subject = self._cert.subject.rfc4514_string()
        m = re.search(r'(\d{1,2}\.?\d{3}\.?\d{3}-[\dkK])', subject, re.I)
        return m.group(1) if m else ''

    # ──────────────────────────────────────────────────────────
    # TED
    # ──────────────────────────────────────────────────────────

    def generar_ted(self, folio, tipo_dte, xml_caf, fecha_emision,
                    rut_emisor, monto_total, it1_nombre='PRODUCTO'):

        parser = etree.XMLParser(remove_blank_text=True)
        caf_root = etree.fromstring(xml_caf.encode(), parser)

        rsk = caf_root.find('.//RSASK').text.strip()
        caf_str = etree.tostring(
            caf_root.find('.//CAF'),
            encoding='unicode'
        )

        it1_safe = (
            it1_nombre[:40]
            .replace('&', ' y ')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
        )

        tsted = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

        dd = (
            f'<DD><RE>{rut_emisor}</RE><TD>{tipo_dte}</TD>'
            f'<F>{folio}</F><FE>{fecha_emision}</FE>'
            f'<RR>66666666-6</RR><RSR>CONSUMIDOR FINAL</RSR>'
            f'<MNT>{monto_total}</MNT><IT1>{it1_safe}</IT1>'
            f'{caf_str}<TSTED>{tsted}</TSTED></DD>'
        )

        frmt = b64encode(
            self._firmar_caf(dd.encode('ISO-8859-1'), rsk)
        ).decode()

        return etree.fromstring(
            f'<TED version="1.0">{dd}'
            f'<FRMT algoritmo="SHA1withRSA">{frmt}</FRMT>'
            f'</TED>'.encode('ISO-8859-1')
        )

    def _firmar_caf(self, data, pem):
        if '-----' not in pem:
            pem = f'-----BEGIN RSA PRIVATE KEY-----\n{pem}\n-----END RSA PRIVATE KEY-----'

        key = load_pem_private_key(
            pem.encode(), password=None, backend=default_backend()
        )

        return _rsa_sign_sha1(key, data)

    # ──────────────────────────────────────────────────────────
    # FIRMA XMLDSIG
    # ──────────────────────────────────────────────────────────

    def firmar(self, xml_bytes, folio, tipo_dte,
               xml_caf, fecha_emision, rut_emisor,
               monto_total, it1_nombre='PRODUCTO'):

        parser = etree.XMLParser(remove_blank_text=True)
        root = etree.fromstring(xml_bytes, parser)

        ns = {'sii': SII_NS}

        # Insertar TED
        ted = self.generar_ted(
            folio, tipo_dte, xml_caf,
            fecha_emision, rut_emisor,
            monto_total, it1_nombre
        )

        ph = root.find('.//sii:TED', ns)
        parent = ph.getparent()
        idx = list(parent).index(ph)
        parent.remove(ph)
        parent.insert(idx, ted)

        # Documento
        doc_id = f'DTE-{tipo_dte}-{folio}'
        doc = root.find(f'.//sii:Documento[@ID="{doc_id}"]', ns)

        # Digest
        doc_c14n = etree.tostring(doc, method="c14n", exclusive=False)
        digest = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        # SignedInfo (DOM real)
        ds = f"{{{XMLDSIG_NS}}}"

        signed_info = etree.Element(ds + "SignedInfo", nsmap={None: XMLDSIG_NS})

        etree.SubElement(
            signed_info, ds + "CanonicalizationMethod",
            Algorithm=C14N
        )

        etree.SubElement(
            signed_info, ds + "SignatureMethod",
            Algorithm=XMLDSIG_NS + "rsa-sha1"
        )

        ref = etree.SubElement(
            signed_info, ds + "Reference",
            URI=f"#{doc_id}"
        )

        transforms = etree.SubElement(ref, ds + "Transforms")

        etree.SubElement(
            transforms, ds + "Transform",
            Algorithm=XMLDSIG_NS + "enveloped-signature"
        )

        etree.SubElement(
            transforms, ds + "Transform",
            Algorithm=C14N
        )

        etree.SubElement(
            ref, ds + "DigestMethod",
            Algorithm=XMLDSIG_NS + "sha1"
        )

        etree.SubElement(ref, ds + "DigestValue").text = digest

        # Canonicalizar
        si_c14n = etree.tostring(signed_info, method="c14n", exclusive=False)

        # Firmar
        signature_value = b64encode(
            _rsa_sign_sha1(self._private_key, si_c14n)
        ).decode()

        # Signature
        signature = etree.Element(ds + "Signature", nsmap={None: XMLDSIG_NS})
        signature.append(signed_info)

        etree.SubElement(signature, ds + "SignatureValue").text = signature_value

        key_info = etree.SubElement(signature, ds + "KeyInfo")
        key_value = etree.SubElement(key_info, ds + "KeyValue")
        rsa = etree.SubElement(key_value, ds + "RSAKeyValue")

        etree.SubElement(rsa, ds + "Modulus").text = _wrap64(self._rsa_mod)
        etree.SubElement(rsa, ds + "Exponent").text = self._rsa_exp

        x509 = etree.SubElement(key_info, ds + "X509Data")
        etree.SubElement(x509, ds + "X509Certificate").text = _wrap64(self._cert_der_b64)

        # Insertar firma
        doc.append(signature)

        return etree.tostring(
            root,
            encoding="ISO-8859-1",
            xml_declaration=False
        )
