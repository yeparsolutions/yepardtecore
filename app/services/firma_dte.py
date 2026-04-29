# app/services/firma_dte.py
# ══════════════════════════════════════════════════════════════
# Firma individual de DTEs para SII Chile
#
# REGLA CRÍTICA — CANONICALIZACIÓN EN CONTEXTO:
# lxml.etree.tostring(elem, method='c14n') produce resultados DIFERENTES
# dependiendo de si el elemento es raíz o un hijo dentro de un árbol.
# Cuando elem es hijo de un árbol con nsmap={None: SII_NS, 'xsi': XSI_NS},
# lxml produce xmlns="" en elementos profundos que pertenecen a otro namespace
# (p.ej. los hijos de <Reference> dentro del bloque XMLDSIG).
# Esto coincide exactamente con lo que produce Apache XMLSEC (Java) que usa el SII.
#
# POR LO TANTO:
# 1. El DigestValue del Documento se calcula con c14n del elemento dentro del
#    árbol DTE (no con round-trip standalone, que produce un c14n diferente).
# 2. El SignedInfo se construye como árbol lxml dentro de Signature dentro
#    del DTE, se obtiene su c14n desde ahí, y ESE BYTES es lo que se firma.
#
# Ambos métodos garantizan que SHA1(bytes_firmados) == SHA1(c14n_del_SII).
# ══════════════════════════════════════════════════════════════

from cryptography.hazmat.primitives.serialization import pkcs12, load_pem_private_key
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


def _wrap64(s: str) -> str:
    """Envuelve base64 en líneas de 64 chars (CHR-00002 del SII)."""
    clean = s.replace('\n', '').replace(' ', '')
    return '\n' + '\n'.join(textwrap.wrap(clean, 64)) + '\n'


def _rsa_sign_sha1(private_key, data: bytes) -> bytes:
    """Firma RSA+SHA1 eludiendo restricción de OpenSSL nivel alto."""
    digest = hashlib.sha1(data).digest()
    try:
        return private_key.sign_prehash(digest, padding.PKCS1v15())
    except AttributeError:
        return private_key.sign(
            digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA1())
        )


def _build_signed_info_element(parent_sig: etree._Element,
                               reference_uri: str, digest_value: str) -> etree._Element:
    """
    Construye el elemento <SignedInfo> como árbol lxml dentro de <Signature>.

    Al estar dentro del árbol DTE (nsmap={None: SII_NS, xsi: XSI_NS}),
    lxml produce el c14n correcto con xmlns='' en los hijos de <Reference>
    (Transforms, Transform, DigestMethod, DigestValue), que es exactamente
    el mismo output que produce Apache XMLSEC del SII.
    Eso garantiza SHA1(bytes_firmados) == SHA1(c14n_verificado_por_SII).
    """
    si = etree.SubElement(parent_sig, f"{{{XMLDSIG_NS}}}SignedInfo")

    cm = etree.SubElement(si, f"{{{XMLDSIG_NS}}}CanonicalizationMethod")
    cm.set("Algorithm", C14N_ALGORITHM)

    sm = etree.SubElement(si, f"{{{XMLDSIG_NS}}}SignatureMethod")
    sm.set("Algorithm", f"{XMLDSIG_NS}rsa-sha1")

    ref = etree.SubElement(si, f"{{{XMLDSIG_NS}}}Reference")
    ref.set("URI", reference_uri)

    transforms = etree.SubElement(ref, f"{{{XMLDSIG_NS}}}Transforms")
    t = etree.SubElement(transforms, f"{{{XMLDSIG_NS}}}Transform")
    t.set("Algorithm", C14N_ALGORITHM)

    dm = etree.SubElement(ref, f"{{{XMLDSIG_NS}}}DigestMethod")
    dm.set("Algorithm", f"{XMLDSIG_NS}sha1")

    dv = etree.SubElement(ref, f"{{{XMLDSIG_NS}}}DigestValue")
    dv.text = digest_value

    return si


def _build_key_info(parent_sig: etree._Element,
                    rsa_mod: str, rsa_exp: str, cert_der_b64: str) -> None:
    """Construye el bloque <KeyInfo> como árbol lxml dentro de <Signature>."""
    ki = etree.SubElement(parent_sig, f"{{{XMLDSIG_NS}}}KeyInfo")
    kv = etree.SubElement(ki, f"{{{XMLDSIG_NS}}}KeyValue")
    rk = etree.SubElement(kv, f"{{{XMLDSIG_NS}}}RSAKeyValue")
    etree.SubElement(rk, f"{{{XMLDSIG_NS}}}Modulus").text  = _wrap64(rsa_mod)
    etree.SubElement(rk, f"{{{XMLDSIG_NS}}}Exponent").text = rsa_exp
    x5 = etree.SubElement(ki, f"{{{XMLDSIG_NS}}}X509Data")
    etree.SubElement(x5, f"{{{XMLDSIG_NS}}}X509Certificate").text = _wrap64(cert_der_b64)


class FirmaDTE:
    """
    Firma documentos DTE individuales.

    Uso:
        firma = FirmaDTE(p12_bytes, password)
        xml_firmado = firma.firmar(xml_dte_bytes, folio, tipo_dte,
                                    xml_caf, fecha_emision, rut_emisor,
                                    monto_total, it1_nombre)
    """

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

    @property
    def rut_certificado(self) -> str:
        subject = self._cert.subject.rfc4514_string()
        m = re.search(r'(\d{1,2}\.?\d{3}\.?\d{3}-[\dkK])', subject, re.I)
        return m.group(1) if m else ''

    # ── TED ──────────────────────────────────────────────────

    def generar_ted(self, folio: int, tipo_dte: int, xml_caf: str,
                    fecha_emision: str, rut_emisor: str, monto_total: int,
                    it1_nombre: str = 'PRODUCTO') -> bytes:
        """Genera el TED (Timbre Electrónico de Documento)."""
        # Parsear CAF sin blanks para que caf_str sea compacto.
        # El CAF del SII viene con saltos de línea; si no se eliminan,
        # SHA1(dd_xml firmado) ≠ SHA1(DD en archivo) → FRMT inválido.
        caf_parser = etree.XMLParser(remove_blank_text=True)
        caf_root   = etree.fromstring(xml_caf.encode(), caf_parser)
        rsk_el     = caf_root.find('.//RSASK')
        caf_str    = etree.tostring(caf_root.find('.//CAF'), encoding='unicode')

        it1_safe = (
            it1_nombre[:40]
            .replace('&', ' y ').replace("'", '').replace('"', '')
            .replace('#', '').replace('<', '&lt;').replace('>', '&gt;')
        ).strip()

        tsted  = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
        dd_xml = (
            f'<DD>'
            f'<RE>{rut_emisor}</RE><TD>{tipo_dte}</TD><F>{folio}</F>'
            f'<FE>{fecha_emision}</FE><RR>66666666-6</RR>'
            f'<RSR>CONSUMIDOR FINAL</RSR><MNT>{monto_total}</MNT>'
            f'<IT1>{it1_safe}</IT1>{caf_str}'
            f'<TSTED>{tsted}</TSTED>'
            f'</DD>'
        )

        frmt_b64 = b64encode(
            self._firmar_rsa_caf(dd_xml.encode('ISO-8859-1'), rsk_el.text.strip())
        ).decode()

        return (
            f'<TED version="1.0">{dd_xml}'
            f'<FRMT algoritmo="SHA1withRSA">{frmt_b64}</FRMT>'
            f'</TED>'
        ).encode('ISO-8859-1')

    def _firmar_rsa_caf(self, data: bytes, pem_key_str: str) -> bytes:
        if '-----' not in pem_key_str:
            pem_key_str = (
                '-----BEGIN RSA PRIVATE KEY-----\n'
                + pem_key_str +
                '\n-----END RSA PRIVATE KEY-----'
            )
        pk = load_pem_private_key(
            pem_key_str.encode(), password=None, backend=default_backend()
        )
        return _rsa_sign_sha1(pk, data)

    # ── XMLDSig ───────────────────────────────────────────────

    def firmar(self, xml_bytes: bytes, folio: int, tipo_dte: int,
               xml_caf: str, fecha_emision: str, rut_emisor: str,
               monto_total: int, it1_nombre: str = 'PRODUCTO') -> bytes:
        """
        Inserta el TED y firma el Documento del DTE.
        Devuelve el DTE completo firmado en ISO-8859-1.

        FLUJO:
        1. Parsear DTE, insertar TED.
        2. Calcular DigestValue del Documento haciendo c14n DIRECTAMENTE desde
           el árbol (el DTE ya tiene nsmap={None: SII_NS, xsi: XSI_NS}), lo que
           produce xmlns='' en elementos profundos — igual que Apache XMLSEC (SII).
        3. Construir <Signature><SignedInfo>... como árbol lxml dentro del DTE,
           obtener el c14n del <SignedInfo> desde ese árbol, y firmar ESOS bytes.
        4. Completar el bloque <Signature> con <SignatureValue> y <KeyInfo>.
        """
        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(xml_bytes, parser)
        ns     = {'sii': SII_NS}

        # Insertar TED
        ted_bytes = self.generar_ted(
            folio, tipo_dte, xml_caf, fecha_emision,
            rut_emisor, monto_total, it1_nombre
        )
        ph = root.find('.//sii:TED', ns)
        if ph is not None:
            parent = ph.getparent()
            idx    = list(parent).index(ph)
            parent.remove(ph)
            parent.insert(
                idx,
                etree.fromstring(
                    b'<?xml version="1.0" encoding="ISO-8859-1"?>' + ted_bytes
                )
            )

        # ── DigestValue del Documento ────────────────────────────────────────
        # Calculamos c14n del elemento Documento DIRECTAMENTE desde el árbol DTE.
        # El DTE fue construido con nsmap={None: SII_NS, 'xsi': XSI_NS}, por lo
        # que lxml produce xmlns='' en los elementos hijos que no pertenecen al
        # namespace de la DTE (p.ej. los del bloque XMLDSIG en la Signature).
        # Este comportamiento coincide exactamente con Apache XMLSEC (Java) del SII.
        # NO usar round-trip standalone: produce un c14n DIFERENTE (sin xmlns='')
        # que daría un DigestValue distinto al que calcula el SII → DTE-3-505.
        doc_id   = f'DTE-{tipo_dte}-{folio}'
        doc_el   = root.find(f'.//sii:Documento[@ID="{doc_id}"]', ns)
        doc_c14n = etree.tostring(doc_el, method='c14n', exclusive=False)
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        # ── Construir <Signature> como árbol lxml dentro del DTE ─────────────
        # Creamos el elemento <Signature> con nsmap XMLDSIG y lo adjuntamos al
        # root (DTE). Así el <SignedInfo> vive dentro de un árbol con el contexto
        # de namespaces correcto: SII_NS en los ancestros, XMLDSIG en Signature.
        # El c14n de <SignedInfo> desde este árbol incluye xmlns='' en los hijos
        # de <Reference>, que es exactamente lo que calcula el SII → SHA1 coincide.
        sig_el = etree.SubElement(root, f"{{{XMLDSIG_NS}}}Signature",
                                  nsmap={None: XMLDSIG_NS})

        si_el      = _build_signed_info_element(sig_el, f'#{doc_id}', digest_doc)
        si_c14n    = etree.tostring(si_el, method='c14n', exclusive=False)
        firma_b64  = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()

        etree.SubElement(sig_el, f"{{{XMLDSIG_NS}}}SignatureValue").text = firma_b64
        _build_key_info(sig_el, self._rsa_mod, self._rsa_exp, self._cert_der_b64)

        xml_str = etree.tostring(root, encoding='unicode', xml_declaration=False)
        return xml_str.encode('ISO-8859-1')
