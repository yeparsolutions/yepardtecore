# app/services/firma_digital.py
# ══════════════════════════════════════════════════════════════
# Servicio de Firma Digital para DTE Chile
# Fix: SHA1 via Prehashed para compatibilidad con OpenSSL nivel alto
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
XSI_NS         = "http://www.w3.org/2001/XMLSchema-instance"
C14N_ALGORITHM = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
TIPOS_BOLETA   = {39, 41}

def _wrap64(s: str) -> str:
    """Envuelve base64 en lineas de 64 chars para cumplir CHR-00002 del SII."""
    clean = s.replace('\n', '').replace(' ', '')
    return '\n' + '\n'.join(textwrap.wrap(clean, 64)) + '\n'

def _rsa_sign_sha1(private_key, data: bytes) -> bytes:
    """
    Firma con RSA+SHA1 eludiendo la restricción de OpenSSL nivel alto.
    Calculamos el hash SHA1 con hashlib (Python puro) y luego firmamos
    con sign_prehash, que no pasa por la política de OpenSSL.
    """
    digest = hashlib.sha1(data).digest()
    try:
        # cryptography >= 40 expone sign_prehash directamente
        return private_key.sign_prehash(digest, padding.PKCS1v15())
    except AttributeError:
        # Fallback para versiones anteriores: usar Prehashed
        return private_key.sign(
            digest,
            padding.PKCS1v15(),
            utils.Prehashed(hashes.SHA1()),
        )

class FirmaDigital:
    def __init__(self, p12_bytes: bytes, password: str):
        pwd_bytes = password.encode("utf-8") if isinstance(password, str) else password
        try:
            private_key, certificate, _ = pkcs12.load_key_and_certificates(
                p12_bytes, pwd_bytes, backend=default_backend()
            )
        except Exception as e:
            raise ValueError(f"No se pudo cargar el certificado .p12: {e}")

        self._private_key  = private_key
        self._certificate  = certificate
        self._cert_der_b64 = b64encode(certificate.public_bytes(serialization.Encoding.DER)).decode()

        pub = certificate.public_key().public_numbers()
        self._rsa_mod = b64encode(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")).decode()
        self._rsa_exp = b64encode(pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big")).decode()

    @property
    def rut_certificado(self) -> str:
        subject = self._certificate.subject.rfc4514_string()
        match   = re.search(r"(\d{1,2}\.?\d{3}\.?\d{3}-[\dkK])", subject, re.IGNORECASE)
        return match.group(1) if match else ""

    @property
    def vigente_hasta(self) -> datetime:
        return self._certificate.not_valid_after_utc

    @property
    def esta_vigente(self) -> bool:
        return datetime.now(timezone.utc) < self.vigente_hasta

    # ── Firma del DTE ─────────────────────────────────────────

    def firmar_dte(self, xml_bytes: bytes, folio: int, tipo_dte: int,
                   xml_caf: str, fecha_emision: str, rut_emisor: str,
                   monto_total: int, it1_nombre: str = "PRODUCTO") -> bytes:

        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(xml_bytes, parser)

        ted_xml = self._generar_ted(folio, tipo_dte, xml_caf, fecha_emision,
                                    rut_emisor, monto_total, it1_nombre)

        ns = {"sii": SII_NS}
        ted_placeholder = root.find(".//sii:TED", ns)
        if ted_placeholder is not None:
            parent = ted_placeholder.getparent()
            idx = list(parent).index(ted_placeholder)
            parent.remove(ted_placeholder)
            ted_con_enc = b'<?xml version="1.0" encoding="ISO-8859-1"?>' + ted_xml
            parent.insert(idx, etree.fromstring(ted_con_enc))

        xml_con_ted = etree.tostring(root, encoding="unicode")
        xml_firmado = self._firmar_xml(xml_con_ted, f"DTE-{tipo_dte}-{folio}")
        return xml_firmado.encode("ISO-8859-1")

    def _generar_ted(self, folio: int, tipo_dte: int, xml_caf: str,
                      fecha_emision: str, rut_emisor: str, monto_total: int,
                      it1_nombre: str = "PRODUCTO") -> bytes:
        caf_root = etree.fromstring(xml_caf.encode())
        rsk_el   = caf_root.find(".//RSASK")
        caf_str  = etree.tostring(caf_root.find(".//CAF"), encoding="unicode")

        it1_safe = it1_nombre[:40].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        # Orden del ejemplo SII: DD(con TSTED al final) → FRMA
        # FRMA es la firma del DD por el emisor (para todos los tipos)
        # FRMT solo aplica para boletas (tipos 39/41)
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

        firma_b64 = b64encode(
            self._firmar_rsa_sha1_raw(dd_xml.encode("ISO-8859-1"), rsk_el.text.strip())
        ).decode()

        # El XSD exige FRMT para todos los tipos de DTE (facturas y boletas)
        return (
            f'<TED version="1.0">{dd_xml}'
            f'<FRMT algoritmo="SHA1withRSA">{firma_b64}</FRMT>'
            f'</TED>'
        ).encode("ISO-8859-1")

    def _firmar_rsa_sha1_raw(self, data: bytes, pem_key_str: str) -> bytes:
        """Firma con la clave privada del CAF (clave RSA del SII para el TED)."""
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        if "-----" not in pem_key_str:
            pem_key_str = "-----BEGIN RSA PRIVATE KEY-----\n" + pem_key_str + "\n-----END RSA PRIVATE KEY-----"
        pk = load_pem_private_key(pem_key_str.encode(), password=None, backend=default_backend())
        return _rsa_sign_sha1(pk, data)

    # ── XMLDSig ───────────────────────────────────────────────

    def _firmar_xml(self, xml_str: str, doc_id: str) -> str:
        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(xml_str.encode(), parser)
        ns     = {"sii": SII_NS}

        doc_el  = root.find(f".//sii:Documento[@ID='{doc_id}']", ns)
        doc_raw = etree.tostring(doc_el, encoding="unicode")
        temp_root   = etree.fromstring(f'<root xmlns="{SII_NS}">{doc_raw}</root>')
        doc_virtual = temp_root[0]

        doc_c14n   = etree.tostring(doc_virtual, method="c14n", exclusive=False)
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        signed_info = self._build_signed_info(f"#{doc_id}", digest_doc)

        si_el   = etree.fromstring(signed_info.encode())
        si_c14n = etree.tostring(si_el, method="c14n", exclusive=False)
        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()

        root.append(etree.fromstring(self._build_signature(signed_info, firma_b64).encode()))
        return etree.tostring(root, encoding="unicode", xml_declaration=False)

    def _build_signed_info(self, reference_uri: str, digest_value: str) -> str:
        return (
            f'<SignedInfo xmlns="{XMLDSIG_NS}">'
            f'<CanonicalizationMethod Algorithm="{C14N_ALGORITHM}"/>'
            f'<SignatureMethod Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1"/>'
            f'<Reference URI="{reference_uri}">'
            f'<Transforms><Transform Algorithm="{C14N_ALGORITHM}"/></Transforms>'
            f'<DigestMethod Algorithm="http://www.w3.org/2000/09/xmldsig#sha1"/>'
            f'<DigestValue>{digest_value}</DigestValue>'
            f'</Reference></SignedInfo>'
        )

    def _build_signature(self, signed_info: str, signature_value: str) -> str:
        return (
            f'<Signature xmlns="{XMLDSIG_NS}">'
            f'{signed_info}'
            f'<SignatureValue>{signature_value}</SignatureValue>'
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

    # ── Firma del sobre ───────────────────────────────────────

    def firmar_sobre(self, sobre_xml: str) -> str:
        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(sobre_xml.encode(), parser)
        ns     = {"sii": SII_NS}

        set_el  = root.find(".//sii:SetDTE[@ID='SetDoc']", ns)
        set_raw = etree.tostring(set_el, encoding="unicode")
        temp_root   = etree.fromstring(f'<root xmlns="{SII_NS}">{set_raw}</root>')
        set_virtual = temp_root[0]

        set_c14n   = etree.tostring(set_virtual, method="c14n", exclusive=False)
        digest_val = b64encode(hashlib.sha1(set_c14n).digest()).decode()

        signed_info = self._build_signed_info("#SetDoc", digest_val)

        si_el   = etree.fromstring(signed_info.encode())
        si_c14n = etree.tostring(si_el, method="c14n", exclusive=False)
        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()

        root.append(etree.fromstring(self._build_signature(signed_info, firma_b64).encode()))
        xml_sin_decl = etree.tostring(root, encoding="unicode")
        declaracion  = '<?xml version="1.0" encoding="ISO-8859-1"?>'
        return declaracion + xml_sin_decl

    @staticmethod
    def cargar_desde_base64(cert_b64: str, password: str) -> "FirmaDigital":
        return FirmaDigital(b64decode(cert_b64), password)

    def info_certificado(self) -> dict:
        cert = self._certificate
        return {
            "subject": cert.subject.rfc4514_string(),
            "emisor":  cert.issuer.rfc4514_string(),
            "valido_hasta": cert.not_valid_after_utc.isoformat(),
            "vigente": self.esta_vigente,
            "rut":     self.rut_certificado,
        }
