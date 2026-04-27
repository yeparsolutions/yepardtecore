# app/services/firma_digital.py
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
    clean = s.replace('\n', '').replace(' ', '')
    return '\n' + '\n'.join(textwrap.wrap(clean, 64)) + '\n'

def _rsa_sign_sha1(private_key, data: bytes) -> bytes:
    digest = hashlib.sha1(data).digest()
    try:
        return private_key.sign_prehash(digest, padding.PKCS1v15())
    except AttributeError:
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

        ted_bytes = self._generar_ted(folio, tipo_dte, xml_caf, fecha_emision,
                                      rut_emisor, monto_total, it1_nombre)

        ns = {"sii": SII_NS}
        ted_placeholder = root.find(".//sii:TED", ns)
        if ted_placeholder is not None:
            parent = ted_placeholder.getparent()
            idx = list(parent).index(ted_placeholder)
            parent.remove(ted_placeholder)
            ted_con_enc = b'<?xml version="1.0" encoding="ISO-8859-1"?>' + ted_bytes
            parent.insert(idx, etree.fromstring(ted_con_enc))

        xml_con_ted = etree.tostring(root, encoding="unicode")
        xml_firmado = self._firmar_xml(xml_con_ted, f"DTE-{tipo_dte}-{folio}")
        return xml_firmado.encode("ISO-8859-1")

    def _generar_ted(self, folio: int, tipo_dte: int, xml_caf: str,
                      fecha_emision: str, rut_emisor: str, monto_total: int,
                      it1_nombre: str = "PRODUCTO") -> bytes:
        _caf_parser = etree.XMLParser(remove_blank_text=True)
        caf_root = etree.fromstring(xml_caf.encode(), _caf_parser)
        rsk_el   = caf_root.find(".//RSASK")
        caf_str  = etree.tostring(caf_root.find(".//CAF"), encoding="unicode")

        it1_safe = (
            it1_nombre[:40]
            .replace('&', ' y ')
            .replace("'", '')
            .replace('"', '')
            .replace('#', '')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
        ).strip()

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

        return (
            f'<TED version="1.0">{dd_xml}'
            f'<FRMT algoritmo="SHA1withRSA">{firma_b64}</FRMT>'
            f'</TED>'
        ).encode("ISO-8859-1")

    def _firmar_rsa_sha1_raw(self, data: bytes, pem_key_str: str) -> bytes:
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

        doc_el = root.find(f".//sii:Documento[@ID='{doc_id}']", ns)
        doc_c14n   = etree.tostring(doc_el, method="c14n", exclusive=False)
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        # Se firma con el C14N real (incluye xmlns:xsi + xmlns="" espurios)
        # Se escribe en el archivo el string limpio (sin xmlns="")
        # El SII al hacer C14N del archivo produce exactamente el string que se firmó.
        si_para_firmar  = self._signed_info_para_firmar(f"#{doc_id}", digest_doc)
        si_para_archivo = self._signed_info_para_archivo(f"#{doc_id}", digest_doc)

        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_para_firmar)).decode()

        root.append(etree.fromstring(
            self._build_signature(si_para_archivo, firma_b64).encode()
        ))
        return etree.tostring(root, encoding="unicode", xml_declaration=False)

    def _signed_info_para_firmar(self, reference_uri: str, digest_value: str) -> bytes:
        """
        C14N exacto que el SII calcula al verificar: con xmlns:xsi heredado del sobre
        y xmlns="" en los hijos de Reference (Transforms, Transform, DigestMethod,
        DigestValue) porque esos elementos no tienen namespace y deben "cancelar"
        el default namespace xmlns=SiiDte que existe en el árbol padre del sobre.

        Este string se usa SOLO para calcular la firma RSA. Nunca va al archivo.
        """
        sha1_algo = f"{XMLDSIG_NS}sha1"
        rsa_sha1  = f"{XMLDSIG_NS}rsa-sha1"
        return (
            f'<SignedInfo xmlns="{XMLDSIG_NS}" xmlns:xsi="{XSI_NS}">'
            f'<CanonicalizationMethod Algorithm="{C14N_ALGORITHM}"></CanonicalizationMethod>'
            f'<SignatureMethod Algorithm="{rsa_sha1}"></SignatureMethod>'
            f'<Reference URI="{reference_uri}">'
            f'<Transforms xmlns="">'
            f'<Transform xmlns="" Algorithm="{C14N_ALGORITHM}"></Transform>'
            f'</Transforms>'
            f'<DigestMethod xmlns="" Algorithm="{sha1_algo}"></DigestMethod>'
            f'<DigestValue xmlns="">{digest_value}</DigestValue>'
            f'</Reference>'
            f'</SignedInfo>'
        ).encode('utf-8')

    def _signed_info_para_archivo(self, reference_uri: str, digest_value: str) -> str:
        """
        SignedInfo que se escribe en el XML final. Sin xmlns="" para que el
        validador XSD del SII reconozca Transforms/DigestMethod/DigestValue
        como parte del namespace xmldsig#.

        Cuando el SII hace C14N de este elemento dentro del EnvioDTE (que tiene
        xmlns=SiiDte en el root), el proceso de canonización agrega los xmlns=""
        automáticamente — produciendo exactamente el mismo bytes que
        _signed_info_para_firmar. Por eso la firma es válida.
        """
        sha1_algo = f"{XMLDSIG_NS}sha1"
        rsa_sha1  = f"{XMLDSIG_NS}rsa-sha1"
        return (
            f'<SignedInfo xmlns="{XMLDSIG_NS}" xmlns:xsi="{XSI_NS}">'
            f'<CanonicalizationMethod Algorithm="{C14N_ALGORITHM}"></CanonicalizationMethod>'
            f'<SignatureMethod Algorithm="{rsa_sha1}"></SignatureMethod>'
            f'<Reference URI="{reference_uri}">'
            f'<Transforms>'
            f'<Transform Algorithm="{C14N_ALGORITHM}"></Transform>'
            f'</Transforms>'
            f'<DigestMethod Algorithm="{sha1_algo}"></DigestMethod>'
            f'<DigestValue>{digest_value}</DigestValue>'
            f'</Reference>'
            f'</SignedInfo>'
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

        set_el         = root.find(".//sii:SetDTE[@ID='SetDoc']", ns)
        set_raw        = etree.tostring(set_el, encoding="unicode")
        set_standalone = etree.fromstring(set_raw.encode())

        set_c14n   = etree.tostring(set_standalone, method="c14n", exclusive=False)
        digest_val = b64encode(hashlib.sha1(set_c14n).digest()).decode()

        si_para_firmar  = self._signed_info_para_firmar("#SetDoc", digest_val)
        si_para_archivo = self._signed_info_para_archivo("#SetDoc", digest_val)

        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_para_firmar)).decode()

        root.append(etree.fromstring(
            self._build_signature(si_para_archivo, firma_b64).encode()
        ))
        xml_sin_decl = etree.tostring(root, encoding="unicode")
        return '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + xml_sin_decl

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
