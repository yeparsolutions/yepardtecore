# app/services/firma_digital.py
# ══════════════════════════════════════════════════════════════
# Servicio de Firma Digital para DTE Chile
#
# FIX DEFINITIVO 2026-04-19:
# El bug era que el SignedInfo se firmaba en contexto standalone
# pero el SII lo verifica en contexto del árbol (EnvioBOLETA>DTE).
# SOLUCIÓN: insertar Signature al árbol ANTES de calcular el C14N.
# ══════════════════════════════════════════════════════════════

from cryptography.hazmat.primitives.serialization import pkcs12, Encoding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from lxml import etree
from base64 import b64encode, b64decode
from datetime import datetime, timezone
import hashlib, re, textwrap

XMLDSIG_NS     = "http://www.w3.org/2000/09/xmldsig#"
SII_NS         = "http://www.sii.cl/SiiDte"
XSI_NS         = "http://www.w3.org/2001/XMLSchema-instance"
C14N_ALGORITHM = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
TIPOS_BOLETA   = {39, 41}

def _wrap64(s: str) -> str:
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
        self._cert_der_b64 = b64encode(certificate.public_bytes(Encoding.DER)).decode()
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

    # ── Firma del DTE individual (emisión standalone) ──────────

    def firmar_dte(self, xml_bytes: bytes, folio: int, tipo_dte: int,
                   xml_caf: str, fecha_emision: str, rut_emisor: str,
                   monto_total: int, it1_nombre: str = "PRODUCTO") -> bytes:
        """
        Firma un DTE y retorna <DTE><Documento/><Signature/></DTE>.
        Usado para emisión individual. Para certificación (set de boletas),
        sii_sender.construir_sobre() llama a firmar_documento_en_arbol().
        """
        D, SII = XMLDSIG_NS, SII_NS
        parser = etree.XMLParser(remove_blank_text=True)
        doc_el = etree.fromstring(xml_bytes, parser)

        ted_xml = self._generar_ted(folio, tipo_dte, xml_caf, fecha_emision,
                                    rut_emisor, monto_total, it1_nombre)
        ns = {"sii": SII}
        ted_ph = doc_el.find(".//sii:TED", ns)
        if ted_ph is not None:
            parent = ted_ph.getparent()
            idx    = list(parent).index(ted_ph)
            parent.remove(ted_ph)
            parent.insert(idx, etree.fromstring(ted_xml))

        dte_el = etree.Element(f"{{{SII}}}DTE")
        dte_el.set("version", "1.0")
        dte_el.append(doc_el)

        self.firmar_documento_en_arbol(dte_el, f"DTE-{tipo_dte}-{folio}")

        return etree.tostring(dte_el, encoding="unicode", xml_declaration=False).encode("ISO-8859-1")

    def firmar_documento_en_arbol(self, dte_el: etree._Element, doc_id: str):
        """
        Firma el <Documento> dentro del <dte_el> YA INSERTADO en el árbol padre.

        El Signature se agrega como hijo de <DTE> (hermano de <Documento>).
        Como el <DTE> ya está en el árbol del sobre, el SignedInfo hereda
        el contexto de namespace correcto (EnvioBOLETA > SetDTE > DTE),
        idéntico al que el SII calculará al verificar.

        Args:
            dte_el: Elemento <DTE> ya insertado en el árbol del sobre
            doc_id: ID del Documento, e.g. "DTE-39-1"
        """
        D, SII = XMLDSIG_NS, SII_NS

        doc_el = dte_el.find(f"{{{SII}}}Documento[@ID='{doc_id}']")
        if doc_el is None:
            raise ValueError(f"No se encontró <Documento ID='{doc_id}'> en el DTE")

        doc_c14n   = etree.tostring(doc_el, method="c14n", exclusive=False)
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        sig_el = etree.SubElement(dte_el, f"{{{D}}}Signature")
        self._build_signed_info_in_tree(sig_el, f"#{doc_id}", digest_doc)

        si_el   = sig_el.find(f"{{{D}}}SignedInfo")
        si_c14n = etree.tostring(si_el, method="c14n", exclusive=False)

        firma_b64 = b64encode(
            self._private_key.sign(si_c14n, padding.PKCS1v15(), hashes.SHA1())
        ).decode()
        self._append_signature_value_and_key(sig_el, firma_b64)

    # ── TED ───────────────────────────────────────────────────

    def _generar_ted(self, folio, tipo_dte, xml_caf, fecha_emision,
                     rut_emisor, monto_total, it1_nombre="PRODUCTO"):
        caf_root = etree.fromstring(xml_caf.encode())
        rsk_el   = caf_root.find(".//RSASK")
        caf_str  = etree.tostring(caf_root.find(".//CAF"), encoding="unicode")
        dd_xml = (
            f"<DD><RE>{rut_emisor}</RE><TD>{tipo_dte}</TD><F>{folio}</F>"
            f"<FE>{fecha_emision}</FE><RR>66666666-6</RR>"
            f"<RSR>CONSUMIDOR FINAL</RSR><MNT>{monto_total}</MNT>"
            f"<IT1>{it1_nombre[:40]}</IT1>{caf_str}"
            f"<TSTED>{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')}</TSTED></DD>"
        )
        firma_b64 = b64encode(
            self._firmar_rsa_sha1_raw(dd_xml.encode("ISO-8859-1"), rsk_el.text.strip())
        ).decode()
        tag = "FRMT" if tipo_dte in TIPOS_BOLETA else "FRMA"
        return (
            f'<TED version="1.0">{dd_xml}'
            f'<{tag} algoritmo="SHA1withRSA">{firma_b64}</{tag}></TED>'
        ).encode("ISO-8859-1")

    def _firmar_rsa_sha1_raw(self, data: bytes, pem_key_str: str) -> bytes:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        if "-----" not in pem_key_str:
            pem_key_str = ("-----BEGIN RSA PRIVATE KEY-----\n"
                           + pem_key_str + "\n-----END RSA PRIVATE KEY-----")
        pk = load_pem_private_key(pem_key_str.encode(), password=None, backend=default_backend())
        return pk.sign(data, padding.PKCS1v15(), hashes.SHA1())

    # ── Firma del sobre ────────────────────────────────────────

    def firmar_sobre(self, sobre_xml: str) -> str:
        """
        Firma el sobre EnvioBOLETA completo (SetDTE).
        Llamar DESPUÉS de que todos los DTEs estén firmados en el árbol.
        """
        D = XMLDSIG_NS
        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(sobre_xml.encode("utf-8"), parser)
        ns     = {"sii": SII_NS}

        set_el     = root.find(".//sii:SetDTE[@ID='SetDoc']", ns)
        set_c14n   = etree.tostring(set_el, method="c14n", exclusive=False)
        digest_val = b64encode(hashlib.sha1(set_c14n).digest()).decode()

        sig_el = etree.SubElement(root, f"{{{D}}}Signature")
        self._build_signed_info_in_tree(sig_el, "#SetDoc", digest_val)

        si_el   = sig_el.find(f"{{{D}}}SignedInfo")
        si_c14n = etree.tostring(si_el, method="c14n", exclusive=False)

        firma_b64 = b64encode(
            self._private_key.sign(si_c14n, padding.PKCS1v15(), hashes.SHA1())
        ).decode()
        self._append_signature_value_and_key(sig_el, firma_b64)

        xml_str = etree.tostring(root, encoding="unicode", xml_declaration=False)
        return '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + xml_str

    # ── Helpers XMLDSig ────────────────────────────────────────

    def _build_signed_info_in_tree(self, sig_el, reference_uri, digest_value):
        D = XMLDSIG_NS
        si = etree.SubElement(sig_el, f"{{{D}}}SignedInfo")
        cm = etree.SubElement(si, f"{{{D}}}CanonicalizationMethod")
        cm.set("Algorithm", C14N_ALGORITHM)
        sm = etree.SubElement(si, f"{{{D}}}SignatureMethod")
        sm.set("Algorithm", f"{D}rsa-sha1")
        ref = etree.SubElement(si, f"{{{D}}}Reference")
        ref.set("URI", reference_uri)
        tr = etree.SubElement(ref, f"{{{D}}}Transforms")
        t  = etree.SubElement(tr, f"{{{D}}}Transform")
        t.set("Algorithm", C14N_ALGORITHM)
        dm = etree.SubElement(ref, f"{{{D}}}DigestMethod")
        dm.set("Algorithm", f"{D}sha1")
        dv = etree.SubElement(ref, f"{{{D}}}DigestValue")
        dv.text = digest_value

    def _append_signature_value_and_key(self, sig_el, firma_b64):
        D = XMLDSIG_NS
        sv = etree.SubElement(sig_el, f"{{{D}}}SignatureValue")
        sv.text = firma_b64
        ki  = etree.SubElement(sig_el, f"{{{D}}}KeyInfo")
        kv  = etree.SubElement(ki, f"{{{D}}}KeyValue")
        rsa = etree.SubElement(kv, f"{{{D}}}RSAKeyValue")
        mod_el = etree.SubElement(rsa, f"{{{D}}}Modulus")
        mod_el.text = _wrap64(self._rsa_mod)
        exp_el = etree.SubElement(rsa, f"{{{D}}}Exponent")
        exp_el.text = self._rsa_exp
        x509d = etree.SubElement(ki, f"{{{D}}}X509Data")
        x509c = etree.SubElement(x509d, f"{{{D}}}X509Certificate")
        x509c.text = _wrap64(self._cert_der_b64)

    # ── Utilidades ─────────────────────────────────────────────

    @staticmethod
    def cargar_desde_base64(cert_b64: str, password: str) -> "FirmaDigital":
        return FirmaDigital(b64decode(cert_b64), password)

    def info_certificado(self) -> dict:
        cert = self._certificate
        return {
            "subject":      cert.subject.rfc4514_string(),
            "emisor":       cert.issuer.rfc4514_string(),
            "valido_hasta": cert.not_valid_after_utc.isoformat(),
            "vigente":      self.esta_vigente,
            "rut":          self.rut_certificado,
        }
