# app/services/firma_digital.py
# ══════════════════════════════════════════════════════════════
# Servicio de Firma Digital para DTE Chile - VERSIÓN FINAL FIX ENCODING
# ══════════════════════════════════════════════════════════════

from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, utils
from cryptography.hazmat.backends import default_backend
from lxml import etree
from base64 import b64encode, b64decode
from datetime import datetime, timezone
import hashlib
import re
import textwrap

XMLDSIG_NS     = "http://www.w3.org/2000/09/xmldsig#"
SII_NS         = "http://www.sii.cl/SiiDte"
C14N_ALGORITHM = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"

def _wrap64(s: str) -> str:
    clean = s.replace('\n', '').replace(' ', '')
    return '\n' + '\n'.join(textwrap.wrap(clean, 64)) + '\n'

def _rsa_sign_sha1(private_key, data: bytes) -> bytes:
    digest = hashlib.sha1(data).digest()
    try:
        return private_key.sign_prehash(digest, padding.PKCS1v15())
    except AttributeError:
        return private_key.sign(digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA1()))

class FirmaDigital:
    def __init__(self, p12_bytes: bytes, password: str):
        pwd_bytes = password.encode("utf-8") if isinstance(password, str) else password
        try:
            private_key, certificate, _ = pkcs12.load_key_and_certificates(
                p12_bytes, pwd_bytes, backend=default_backend()
            )
        except Exception as e:
            raise ValueError(f"Certificado inválido: {e}")

        self._private_key  = private_key
        self._certificate  = certificate
        self._cert_der_b64 = b64encode(certificate.public_bytes(serialization.Encoding.DER)).decode()
        pub = certificate.public_key().public_numbers()
        self._rsa_mod = b64encode(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")).decode()
        self._rsa_exp = b64encode(pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big")).decode()

    @property
    def rut_certificado(self) -> str:
        subject = self._certificate.subject.rfc4514_string()
        match = re.search(r"(\d{1,2}\.?\d{3}\.?\d{3}-[\dkK])", subject, re.IGNORECASE)
        return match.group(1).replace(".", "") if match else ""

    @property
    def vigente_hasta(self) -> datetime:
        return self._certificate.not_valid_after_utc

    @property
    def esta_vigente(self) -> bool:
        return datetime.now(timezone.utc) < self.vigente_hasta

    def firmar_dte(self, xml_bytes: bytes, folio: int, tipo_dte: int,
                   xml_caf: str, fecha_emision: str, rut_emisor: str,
                   monto_total: int, it1_nombre: str = "PRODUCTO") -> bytes:

        ted_xml_str = self._generar_ted(folio, tipo_dte, xml_caf, fecha_emision,
                                        rut_emisor, monto_total, it1_nombre).decode("ISO-8859-1")

        xml_str = xml_bytes.decode("ISO-8859-1") if isinstance(xml_bytes, bytes) else xml_bytes

        if "<TED/>" in xml_str:
            xml_con_ted = xml_str.replace("<TED/>", ted_xml_str)
        else:
            xml_con_ted = re.sub(r'<TED\s*/>', ted_xml_str, xml_str)
            if xml_con_ted == xml_str:
                xml_con_ted = re.sub(r'<TED>.*?</TED>', ted_xml_str, xml_str, flags=re.DOTALL)

        xml_firmado = self._firmar_xml(xml_con_ted, f"DTE-{tipo_dte}-{folio}")
        return xml_firmado.encode("ISO-8859-1")

    def _generar_ted(self, folio: int, tipo_dte: int, xml_caf: str,
                      fecha_emision: str, rut_emisor: str, monto_total: int,
                      it1_nombre: str = "PRODUCTO") -> bytes:
        
        # Forzar parsing del CAF con encoding correcto
        caf_root = etree.fromstring(xml_caf.encode("ISO-8859-1") if isinstance(xml_caf, str) else xml_caf)
        rsk_el   = caf_root.find(".//RSASK")
        caf_el   = caf_root.find(".//CAF")
        caf_str  = etree.tostring(caf_el, encoding="unicode").replace(f' xmlns="{SII_NS}"', '')

        it1_safe = it1_nombre[:40].replace('&', ' y ').replace("'", '').replace('"', '').replace('#', '').strip()
        tsted = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        
        dd_xml = (
            f"<DD>"
            f"<RE>{rut_emisor}</RE><TD>{tipo_dte}</TD><F>{folio}</F>"
            f"<FE>{fecha_emision}</FE><RR>66666666-6</RR>"
            f"<RSR>CONSUMIDOR FINAL</RSR><MNT>{monto_total}</MNT>"
            f"<IT1>{it1_safe}</IT1>{caf_str}"
            f"<TSTED>{tsted}</TSTED>"
            f"</DD>"
        )

        firma_b64 = b64encode(self._firmar_rsa_sha1_raw(dd_xml.encode("ISO-8859-1"), rsk_el.text.strip())).decode()

        return (
            f'<TED version="1.0" xmlns="">{dd_xml}'
            f'<FRMT algoritmo="SHA1withRSA">{firma_b64}</FRMT>'
            f'</TED>'
        ).encode("ISO-8859-1")

    def _firmar_xml(self, xml_str: str, doc_id: str) -> str:
        # FIX: Forzar al parser a tratar la entrada como ISO-8859-1
        parser = etree.XMLParser(remove_blank_text=True, recover=True, encoding="ISO-8859-1")
        root = etree.fromstring(xml_str.encode("ISO-8859-1"), parser)
        ns = {"sii": SII_NS}

        doc_el = root.find(f".//sii:Documento[@ID='{doc_id}']", ns)
        doc_c14n = etree.tostring(doc_el, method="c14n", exclusive=False)
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        si_xml = self._build_signed_info(f"#{doc_id}", digest_doc)
        temp_sig = etree.fromstring(f'<Signature xmlns="{XMLDSIG_NS}">{si_xml}</Signature>')
        root.append(temp_sig)
        si_en_doc = temp_sig.find(f"{{{XMLDSIG_NS}}}SignedInfo")
        si_c14n = etree.tostring(si_en_doc, method="c14n", exclusive=False)
        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()
        root.remove(temp_sig)

        root.append(etree.fromstring(self._build_signature(si_xml, firma_b64)))
        return etree.tostring(root, encoding="unicode")

    def _firmar_rsa_sha1_raw(self, data: bytes, pem_key_str: str) -> bytes:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        if "-----" not in pem_key_str:
            pem_key_str = f"-----BEGIN RSA PRIVATE KEY-----\n{pem_key_str}\n-----END RSA PRIVATE KEY-----"
        pk = load_pem_private_key(pem_key_str.encode(), password=None, backend=default_backend())
        return _rsa_sign_sha1(pk, data)

    def _build_signed_info(self, reference_uri: str, digest_value: str) -> str:
        return (
            f'<SignedInfo xmlns="{XMLDSIG_NS}">'
            f'<CanonicalizationMethod Algorithm="{C14N_ALGORITHM}"/>'
            f'<SignatureMethod Algorithm="{XMLDSIG_NS}rsa-sha1"/>'
            f'<Reference URI="{reference_uri}">'
            f'<Transforms><Transform Algorithm="{C14N_ALGORITHM}"/></Transforms>'
            f'<DigestMethod Algorithm="{XMLDSIG_NS}sha1"/>'
            f'<DigestValue>{digest_value}</DigestValue>'
            f'</Reference></SignedInfo>'
        )

    def _build_signature(self, signed_info: str, signature_value: str) -> str:
        return (
            f'<Signature xmlns="{XMLDSIG_NS}">'
            f'{signed_info}'
            f'<SignatureValue>{signature_value}</SignatureValue>'
            f'<KeyInfo><KeyValue><RSAKeyValue>'
            f'<Modulus>{_wrap64(self._rsa_mod)}</Modulus>'
            f'<Exponent>{self._rsa_exp}</Exponent>'
            f'</RSAKeyValue></KeyValue>'
            f'<X509Data><X509Certificate>{_wrap64(self._cert_der_b64)}</X509Certificate></X509Data>'
            f'</KeyInfo></Signature>'
        )

    def firmar_sobre(self, sobre_xml: str) -> str:
        # FIX: Forzar al parser a tratar la entrada como ISO-8859-1 en el sobre
        parser = etree.XMLParser(remove_blank_text=True, encoding="ISO-8859-1")
        root = etree.fromstring(sobre_xml.encode("ISO-8859-1"), parser)
        ns = {"sii": SII_NS}

        set_el = root.find(".//sii:SetDTE[@ID='SetDoc']", ns)
        set_c14n = etree.tostring(set_el, method="c14n", exclusive=False)
        digest_val = b64encode(hashlib.sha1(set_c14n).digest()).decode()

        si_xml = self._build_signed_info("#SetDoc", digest_val)
        temp_sig = etree.fromstring(f'<Signature xmlns="{XMLDSIG_NS}">{si_xml}</Signature>')
        root.append(temp_sig)
        si_en_doc = temp_sig.find(f"{{{XMLDSIG_NS}}}SignedInfo")
        si_c14n = etree.tostring(si_en_doc, method="c14n", exclusive=False)
        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()
        root.remove(temp_sig)

        root.append(etree.fromstring(self._build_signature(si_xml, firma_b64)))
        
        return '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + etree.tostring(root, encoding="unicode")

    @staticmethod
    def cargar_desde_base64(cert_b64: str, password: str) -> "FirmaDigital":
        return FirmaDigital(b64decode(cert_b64), password)
