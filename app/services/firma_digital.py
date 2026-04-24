from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, utils
from cryptography.hazmat.backends import default_backend
from lxml import etree
from base64 import b64encode, b64decode
from datetime import datetime, timezone
import hashlib
import re

XMLDSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
SII_NS     = "http://www.sii.cl/SiiDte"
C14N       = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"

TIPOS_BOLETA = {39, 41}


def _rsa_sign_sha1(private_key, data: bytes) -> bytes:
    digest = hashlib.sha1(data).digest()
    return private_key.sign(
        digest,
        padding.PKCS1v15(),
        utils.Prehashed(hashes.SHA1())
    )


class FirmaDigital:

    def __init__(self, p12_bytes: bytes, password: str):
        private_key, certificate, _ = pkcs12.load_key_and_certificates(
            p12_bytes, password.encode(), backend=default_backend()
        )

        self._private_key = private_key
        self._certificate = certificate

        self._cert_b64 = b64encode(
            certificate.public_bytes(serialization.Encoding.DER)
        ).decode()

        pub = certificate.public_key().public_numbers()
        self._modulus = b64encode(pub.n.to_bytes((pub.n.bit_length()+7)//8, "big")).decode()
        self._exp     = b64encode(pub.e.to_bytes((pub.e.bit_length()+7)//8, "big")).decode()

    # ─────────────────────────────
    # TED
    # ─────────────────────────────

    def generar_ted(self, folio, tipo_dte, caf_xml, fecha, rut_emisor, monto, item):
        caf = etree.fromstring(caf_xml.encode())
        key = caf.find(".//RSASK").text.strip()

        caf_str = etree.tostring(caf.find(".//CAF"), encoding="unicode")

        tsted = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        dd = (
            f"<DD>"
            f"<RE>{rut_emisor}</RE>"
            f"<TD>{tipo_dte}</TD>"
            f"<F>{folio}</F>"
            f"<FE>{fecha}</FE>"
            f"<RR>66666666-6</RR>"
            f"<RSR>CONSUMIDOR FINAL</RSR>"
            f"<MNT>{monto}</MNT>"
            f"<IT1>{item[:40]}</IT1>"
            f"{caf_str}"
            f"<TSTED>{tsted}</TSTED>"
            f"</DD>"
        )

        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        if "BEGIN" not in key:
            key = f"-----BEGIN RSA PRIVATE KEY-----\n{key}\n-----END RSA PRIVATE KEY-----"

        pk = load_pem_private_key(key.encode(), password=None)

        firma = b64encode(_rsa_sign_sha1(pk, dd.encode("ISO-8859-1"))).decode()

        tag = "FRMT" if tipo_dte in TIPOS_BOLETA else "FRMA"

        return f'<TED xmlns="{SII_NS}" version="1.0">{dd}<{tag} algoritmo="SHA1withRSA">{firma}</{tag}></TED>'

    # ─────────────────────────────
    # FIRMA DTE
    # ─────────────────────────────

    def firmar_dte(self, xml_bytes, folio, tipo_dte, caf, fecha, rut, monto, item):

        root = etree.fromstring(xml_bytes)

        ted_xml = self.generar_ted(folio, tipo_dte, caf, fecha, rut, monto, item)

        ns = {"sii": SII_NS}
        ted = root.find(".//sii:TED", ns)

        parent = ted.getparent()
        parent.replace(ted, etree.fromstring(ted_xml.encode()))

        return self._firmar_xml(root, f"#DTE-{tipo_dte}-{folio}")

    # ─────────────────────────────
    # XMLDSIG
    # ─────────────────────────────

    def _firmar_xml(self, root, uri):

        doc = root.find(".//*[@ID]")
        c14n = etree.tostring(doc, method="c14n")

        digest = b64encode(hashlib.sha1(c14n).digest()).decode()

        signed_info = f"""
<SignedInfo xmlns="{XMLDSIG_NS}">
  <CanonicalizationMethod Algorithm="{C14N}"/>
  <SignatureMethod Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1"/>
  <Reference URI="{uri}">
    <Transforms>
      <Transform Algorithm="{C14N}"/>
    </Transforms>
    <DigestMethod Algorithm="http://www.w3.org/2000/09/xmldsig#sha1"/>
    <DigestValue>{digest}</DigestValue>
  </Reference>
</SignedInfo>
"""

        si = etree.fromstring(signed_info.encode())
        si_c14n = etree.tostring(si, method="c14n")

        firma = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()

        signature = f"""
<Signature xmlns="{XMLDSIG_NS}">
{signed_info}
<SignatureValue>{firma}</SignatureValue>
<KeyInfo>
<KeyValue>
<RSAKeyValue>
<Modulus>{self._modulus}</Modulus>
<Exponent>{self._exp}</Exponent>
</RSAKeyValue>
</KeyValue>
<X509Data>
<X509Certificate>{self._cert_b64}</X509Certificate>
</X509Data>
</KeyInfo>
</Signature>
"""

        root.append(etree.fromstring(signature.encode()))
        return etree.tostring(root, encoding="ISO-8859-1")

    # ─────────────────────────────
    # FIRMA SOBRE
    # ─────────────────────────────

    def firmar_sobre(self, xml: str) -> str:

        root = etree.fromstring(xml.encode())
        ns = {"sii": SII_NS}

        setdte = root.find(".//sii:SetDTE", ns)

        c14n = etree.tostring(setdte, method="c14n")
        digest = b64encode(hashlib.sha1(c14n).digest()).decode()

        signed_info = f"""
<SignedInfo xmlns="{XMLDSIG_NS}">
  <CanonicalizationMethod Algorithm="{C14N}"/>
  <SignatureMethod Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1"/>
  <Reference URI="#SetDoc">
    <Transforms>
      <Transform Algorithm="{C14N}"/>
    </Transforms>
    <DigestMethod Algorithm="http://www.w3.org/2000/09/xmldsig#sha1"/>
    <DigestValue>{digest}</DigestValue>
  </Reference>
</SignedInfo>
"""

        si = etree.fromstring(signed_info.encode())
        firma = b64encode(_rsa_sign_sha1(self._private_key, etree.tostring(si, method="c14n"))).decode()

        signature = f"""
<Signature xmlns="{XMLDSIG_NS}">
{signed_info}
<SignatureValue>{firma}</SignatureValue>
<KeyInfo>
<X509Data>
<X509Certificate>{self._cert_b64}</X509Certificate>
</X509Data>
</KeyInfo>
</Signature>
"""

        root.append(etree.fromstring(signature.encode()))

        return '<?xml version="1.0" encoding="ISO-8859-1"?>' + etree.tostring(root, encoding="unicode")
