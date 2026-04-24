# app/services/firma_digital.py
# ══════════════════════════════════════════════════════════════
# Servicio de Firma Digital para DTE Chile
# Respetando: FRMA en CAF y FRMT en TED (Estándar SII)
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
    """Formato base64 de 64 caracteres por línea."""
    clean = s.replace('\n', '').replace(' ', '')
    return '\n' + '\n'.join(textwrap.wrap(clean, 64)) + '\n'

def _rsa_sign_sha1(private_key, data: bytes) -> bytes:
    """Firma RSA-SHA1 eludiendo políticas restrictivas de OpenSSL."""
    digest = hashlib.sha1(data).digest()
    try:
        return private_key.sign_prehash(digest, padding.PKCS1v15())
    except AttributeError:
        return private_key.sign(digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA1()))

class FirmaDigital:
    def __init__(self, p12_bytes: bytes, password: str):
        pwd_bytes = password.encode("utf-8") if isinstance(password, str) else password
        private_key, certificate, _ = pkcs12.load_key_and_certificates(p12_bytes, pwd_bytes, backend=default_backend())
        self._private_key  = private_key
        self._certificate  = certificate
        self._cert_der_b64 = b64encode(certificate.public_bytes(serialization.Encoding.DER)).decode()
        pub = certificate.public_key().public_numbers()
        self._rsa_mod = b64encode(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")).decode()
        self._rsa_exp = b64encode(pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big")).decode()

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
            # Inyectamos el TED generado con su codificación correcta
            ted_con_enc = b'<?xml version="1.0" encoding="ISO-8859-1"?>' + ted_xml
            parent.insert(idx, etree.fromstring(ted_con_enc))

        xml_con_ted = etree.tostring(root, encoding="unicode")
        xml_firmado = self._firmar_xml(xml_con_ted, f"DTE-{tipo_dte}-{folio}")
        return xml_firmado.encode("ISO-8859-1")

    def _generar_ted(self, folio: int, tipo_dte: int, xml_caf: str,
                      fecha_emision: str, rut_emisor: str, monto_total: int,
                      it1_nombre: str = "PRODUCTO") -> bytes:
        
        # 1. Parsear el CAF y extraer el nodo <CAF> completo (incluye su <FRMA>)
        caf_root = etree.fromstring(xml_caf.encode('ISO-8859-1'))
        rsk_el   = caf_root.find(".//RSASK")
        # Importante: mantenemos el nodo CAF íntegro para el DD
        caf_node = caf_root.find(".//CAF")
        caf_str  = etree.tostring(caf_node, encoding="unicode")

        it1_safe = it1_nombre[:40].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        tsted = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        
        # 2. Construir DD (Datos del Documento) inyectando el CAF con su FRMA
        dd_xml = (
            f"<DD>"
            f"<RE>{rut_emisor}</RE><TD>{tipo_dte}</TD><F>{folio}</F>"
            f"<FE>{fecha_emision}</FE><RR>66666666-6</RR>"
            f"<RSR>CONSUMIDOR FINAL</RSR><MNT>{monto_total}</MNT>"
            f"<IT1>{it1_safe}</IT1>{caf_str}"
            f"<TSTED>{tsted}</TSTED>"
            f"</DD>"
        )

        # 3. Firmar el DD usando la clave privada del CAF para generar el FRMT
        firma_b64 = b64encode(
            self._firmar_rsa_sha1_raw(dd_xml.encode("ISO-8859-1"), rsk_el.text.strip())
        ).decode()

        # 4. Resultado final: TED con DD (que tiene CAF/FRMA) y sellado con FRMT
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

    def _firmar_xml(self, xml_str: str, doc_id: str) -> str:
        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(xml_str.encode(), parser)
        ns     = {"sii": SII_NS}
        doc_el  = root.find(f".//sii:Documento[@ID='{doc_id}']", ns)
        doc_virtual = etree.fromstring(f'<root xmlns="{SII_NS}">{etree.tostring(doc_el, encoding="unicode")}</root>')[0]
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
            f'<KeyInfo><KeyValue><RSAKeyValue><Modulus>{_wrap64(self._rsa_mod)}</Modulus>'
            f'<Exponent>{self._rsa_exp}</Exponent></RSAKeyValue></KeyValue>'
            f'<X509Data><X509Certificate>{_wrap64(self._cert_der_b64)}</X509Certificate></X509Data>'
            f'</KeyInfo></Signature>'
        )

    def firmar_sobre(self, sobre_xml: str) -> str:
        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(sobre_xml.encode(), parser)
        ns     = {"sii": SII_NS}
        set_el  = root.find(".//sii:SetDTE[@ID='SetDoc']", ns)
        set_virtual = etree.fromstring(f'<root xmlns="{SII_NS}">{etree.tostring(set_el, encoding="unicode")}</root>')[0]
        set_c14n   = etree.tostring(set_virtual, method="c14n", exclusive=False)
        digest_val = b64encode(hashlib.sha1(set_c14n).digest()).decode()
        signed_info = self._build_signed_info("#SetDoc", digest_val)
        si_el   = etree.fromstring(signed_info.encode())
        si_c14n = etree.tostring(si_el, method="c14n", exclusive=False)
        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()
        root.append(etree.fromstring(self._build_signature(signed_info, firma_b64).encode()))
        return '<?xml version="1.0" encoding="ISO-8859-1"?>' + etree.tostring(root, encoding="unicode")
