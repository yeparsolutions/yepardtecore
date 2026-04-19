# app/services/firma_digital.py
# ══════════════════════════════════════════════════════════════
# Servicio de Firma Digital para DTE Chile - Versión Final Corregida
# ══════════════════════════════════════════════════════════════

from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
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
            parent.insert(idx, etree.fromstring(ted_xml))

        xml_con_ted = etree.tostring(root, encoding="unicode")
        xml_firmado = self._firmar_xml(xml_con_ted, f"DTE-{tipo_dte}-{folio}")
        return xml_firmado.encode("ISO-8859-1")

    def _generar_ted(self, folio: int, tipo_dte: int, xml_caf: str,
                      fecha_emision: str, rut_emisor: str, monto_total: int,
                      it1_nombre: str = "PRODUCTO") -> bytes:
        caf_root = etree.fromstring(xml_caf.encode())
        rsk_el   = caf_root.find(".//RSASK")
        caf_str  = etree.tostring(caf_root.find(".//CAF"), encoding="unicode")
        
        dd_xml  = (
            f"<DD>"
            f"<RE>{rut_emisor}</RE><TD>{tipo_dte}</TD><F>{folio}</F>"
            f"<FE>{fecha_emision}</FE><RR>66666666-6</RR>"
            f"<RSR>CONSUMIDOR FINAL</RSR><MNT>{monto_total}</MNT>"
            f"<IT1>{it1_nombre[:40]}</IT1>{caf_str}"
            f"<TSTED>{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')}</TSTED>"
            f"</DD>"
        )

        firma_b64 = b64encode(
            self._firmar_rsa_sha1_raw(dd_xml.encode("ISO-8859-1"), rsk_el.text.strip())
        ).decode()

        tag = "FRMT" if tipo_dte in TIPOS_BOLETA else "FRMA"
        return (
            f'<TED version="1.0">{dd_xml}'
            f'<{tag} algoritmo="SHA1withRSA">{firma_b64}</{tag}>'
            f'</TED>'
        ).encode("ISO-8859-1")

    def _firmar_rsa_sha1_raw(self, data: bytes, pem_key_str: str) -> bytes:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        if "-----" not in pem_key_str:
            pem_key_str = "-----BEGIN RSA PRIVATE KEY-----\n" + pem_key_str + "\n-----END RSA PRIVATE KEY-----"
        pk = load_pem_private_key(pem_key_str.encode(), password=None, backend=default_backend())
        return pk.sign(data, padding.PKCS1v15(), hashes.SHA1())

    # ── XMLDSig Corregido ─────────────────────────────────────

    def _firmar_xml(self, xml_str: str, doc_id: str) -> str:
        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(xml_str.encode(), parser)
        ns     = {"sii": SII_NS}

        doc_el = root.find(f".//sii:Documento[@ID='{doc_id}']", ns)

        # Guard: si el Documento no se encuentra, el folio o tipo_dte no coincide
        if doc_el is None:
            ids_encontrados = [
                el.get("ID", "sin-ID")
                for el in root.findall(".//sii:Documento", ns)
            ]
            raise ValueError(
                f"No se encontró <Documento ID='{doc_id}'> en el DTE. "
                f"IDs presentes: {ids_encontrados}"
            )
        doc_raw = etree.tostring(doc_el, encoding="unicode")
        temp_root = etree.fromstring(f'<root xmlns="{SII_NS}">{doc_raw}</root>')
        doc_virtual = temp_root[0]
        
        doc_c14n   = etree.tostring(doc_virtual, method="c14n", exclusive=False)
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        signed_info = self._build_signed_info(f"#{doc_id}", digest_doc)

        # FIX: El SII usa inclusive C14N al verificar, por lo que hereda
        # xmlns:xsi del DTE padre. Envolvemos SignedInfo en un wrapper con
        # el mismo contexto de namespace para que el C14N coincida.
        # Sin este wrapper, el SII calcula un hash diferente → RFR.
        si_wrapper = etree.fromstring(
            f'<w xmlns="{SII_NS}" xmlns:xsi="{XSI_NS}">{signed_info}</w>'
        )
        si_c14n   = etree.tostring(si_wrapper[0], method="c14n", exclusive=False)
        firma_b64 = b64encode(
            self._private_key.sign(si_c14n, padding.PKCS1v15(), hashes.SHA1())
        ).decode()

        root.append(etree.fromstring(self._build_signature(signed_info, firma_b64).encode()))
        return etree.tostring(root, encoding="unicode", xml_declaration=False)

    def _build_signed_info(self, reference_uri: str, digest_value: str) -> str:
        # El SII exige <Transforms> dentro de <Reference> con el algoritmo C14N.
        # Sin este bloque el validador puede rechazar el schema.
        # Fuente: XML de ejemplo oficial del SII (EnvioDTE de prueba).
        return (
            f'<SignedInfo xmlns="{XMLDSIG_NS}">'
            f'<CanonicalizationMethod Algorithm="{C14N_ALGORITHM}"/>'
            f'<SignatureMethod Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1"/>'
            f'<Reference URI="{reference_uri}">'
            f'<Transforms>'
            f'<Transform Algorithm="{C14N_ALGORITHM}"/>'
            f'</Transforms>'
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

    # ── Firma del sobre Corregido ─────────────────────────────

    def firmar_documento_en_arbol(self, dte_el, doc_id: str) -> None:
        """
        Firma el <Documento> dentro de dte_el IN-TREE.
        dte_el DEBE estar ya insertado en el árbol del sobre para que
        la canonicalización C14N herede el contexto de namespace completo
        (EnvioBOLETA > SetDTE > DTE), idéntico al que usa el SII al verificar.
        Analogía: firmar el contrato ya impreso en papel membretado,
        no en una fotocopia en blanco.
        """
        ns = {"sii": SII_NS}
        doc_el = dte_el.find(f"sii:Documento[@ID='{doc_id}']", ns)
        if doc_el is None:
            raise ValueError(f"No se encontró <Documento ID='{doc_id}'> en el DTE")

        # C14N del Documento en contexto real del árbol
        doc_c14n   = etree.tostring(doc_el, method="c14n", exclusive=False)
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        signed_info_str = self._build_signed_info(f"#{doc_id}", digest_doc)

        # Para el C14N del SignedInfo: lo añadimos temporalmente al árbol
        # para que herede el contexto de namespace correcto, luego lo reemplazamos
        sig_temp = etree.fromstring(
            f'<Signature xmlns="{XMLDSIG_NS}">{signed_info_str}</Signature>'
        )
        dte_el.append(sig_temp)
        si_el   = sig_temp.find(f"{{{XMLDSIG_NS}}}SignedInfo")
        si_c14n = etree.tostring(si_el, method="c14n", exclusive=False)
        dte_el.remove(sig_temp)

        firma_b64 = b64encode(
            self._private_key.sign(si_c14n, padding.PKCS1v15(), hashes.SHA1())
        ).decode()

        dte_el.append(etree.fromstring(
            self._build_signature(signed_info_str, firma_b64).encode()
        ))

    def firmar_sobre(self, sobre_xml: str) -> str:
        """
        Firma el EnvioBOLETA completo (nivel sobre).
        El sobre ya contiene los DTEs firmados in-tree por firmar_documento_en_arbol.
        Usamos el mismo enfoque in-tree para el C14N del SetDTE y del SignedInfo.
        """
        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(sobre_xml.encode("utf-8"), parser)
        ns     = {"sii": SII_NS}

        set_el = root.find(".//sii:SetDTE[@ID='SetDoc']", ns)

        # C14N del SetDTE directo del árbol — contexto correcto sin wrappers
        set_c14n   = etree.tostring(set_el, method="c14n", exclusive=False)
        digest_val = b64encode(hashlib.sha1(set_c14n).digest()).decode()

        signed_info_str = self._build_signed_info("#SetDoc", digest_val)

        # C14N del SignedInfo in-tree (temporal en root = EnvioBOLETA)
        sig_temp = etree.fromstring(
            f'<Signature xmlns="{XMLDSIG_NS}">{signed_info_str}</Signature>'
        )
        root.append(sig_temp)
        si_el   = sig_temp.find(f"{{{XMLDSIG_NS}}}SignedInfo")
        si_c14n = etree.tostring(si_el, method="c14n", exclusive=False)
        root.remove(sig_temp)

        firma_b64 = b64encode(
            self._private_key.sign(si_c14n, padding.PKCS1v15(), hashes.SHA1())
        ).decode()

        root.append(etree.fromstring(
            self._build_signature(signed_info_str, firma_b64).encode()
        ))
        xml_str = etree.tostring(root, encoding="unicode", xml_declaration=False)
        return '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + xml_str

    @staticmethod
    def cargar_desde_base64(cert_b64: str, password: str) -> "FirmaDigital":
        return FirmaDigital(b64decode(cert_b64), password)

    def info_certificado(self) -> dict:
        cert = self._certificate
        return {
            "subject": cert.subject.rfc4514_string(),
            "emisor": cert.issuer.rfc4514_string(),
            "valido_hasta": cert.not_valid_after_utc.isoformat(),
            "vigente": self.esta_vigente,
            "rut": self.rut_certificado,
        }
# Update: Parche SII v2.1 - 2026-04-14
