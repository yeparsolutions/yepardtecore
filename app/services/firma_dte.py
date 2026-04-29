# app/services/firma_dte.py
# ══════════════════════════════════════════════════════════════
# Firma individual de DTEs para SII Chile
#
# REGLA CRÍTICA: El SignedInfo del DTE individual SÍ lleva xmlns:xsi.
# xml_builder construye el elemento <DTE> con nsmap que incluye xmlns:xsi.
# Cuando la <Signature> se agrega dentro del <DTE>, el <SignedInfo> hereda
# xmlns:xsi en su scope de namespaces. El SII al canonicalizar (c14n 1.0)
# el <SignedInfo> para verificar la firma, incluye xmlns:xsi porque está
# en scope desde el <DTE> ancestro.
# Por lo tanto, los bytes que firmamos DEBEN contener xmlns:xsi para que
# SHA1(bytes_firmados) == SHA1(c14n_verificado_por_SII).
# Esto es análogo a firma_sobre.py (EnvioDTE también declara xmlns:xsi).
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


def _signed_info_dte(reference_uri: str, digest_value: str) -> bytes:
    """
    C14N manual del SignedInfo para firma de DTE individual.

    CON xmlns:xsi — xml_builder declara xmlns:xsi en el nsmap del elemento <DTE>.
    Cuando el <Signature> se inserta dentro de <DTE>, el <SignedInfo> hereda
    xmlns:xsi en su scope de namespaces. c14n 1.0 (no-exclusive) incluye TODOS
    los namespaces en scope en el elemento canonicalizado. Por tanto:

        SII calcula: c14n(<SignedInfo>) → bytes CON xmlns:xsi
        Nosotros firmamos: bytes CON xmlns:xsi

    → SHA1 coincide → firma válida.

    Sin xmlns:xsi aquí, SHA1 difiere → DTE-3-505 para todos los folios.

    Elementos con tags expandidos (abrir+cerrar) según C14N 1.0.
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


def _build_signature_block(signed_info_str: str, sig_value: str,
                            rsa_mod: str, rsa_exp: str, cert_der_b64: str) -> str:
    return (
        f'<Signature xmlns="{XMLDSIG_NS}">'
        f'{signed_info_str}'
        f'<SignatureValue>{sig_value}</SignatureValue>'
        f'<KeyInfo>'
        f'<KeyValue><RSAKeyValue>'
        f'<Modulus>{_wrap64(rsa_mod)}</Modulus>'
        f'<Exponent>{rsa_exp}</Exponent>'
        f'</RSAKeyValue></KeyValue>'
        f'<X509Data>'
        f'<X509Certificate>{_wrap64(cert_der_b64)}</X509Certificate>'
        f'</X509Data>'
        f'</KeyInfo>'
        f'</Signature>'
    )


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

        # DigestValue del Documento
        # ROUND-TRIP (idéntico a firma_sobre.py): serializar y re-parsear como
        # elemento raíz antes de calcular c14n.
        #
        # Por qué es necesario:
        #   lxml.etree.tostring(elem, method='c14n') en un elemento NO-raíz
        #   propaga xmlns="" espurios en elementos descendientes (bug conocido de
        #   lxml para árboles de namespace mixto). El SII usa Apache XMLSEC
        #   (Java) cuyo c14n es correcto y NO produce esos xmlns="".
        #   Si calculamos el digest sobre el c14n buggy, el hash difiere del que
        #   el SII computa → DTE-3-505.
        #
        #   Al serializar doc_el a string y re-parsearlo como raíz independiente,
        #   obtenemos un árbol limpio donde lxml produce el c14n correcto (sin
        #   xmlns="" espurios). Ese c14n coincide con el que calcula el SII.
        #
        #   firma_sobre.py usa exactamente este patrón (set_raw→set_standalone)
        #   y el sobre ya pasa. Aquí aplicamos lo mismo al Documento.
        doc_id        = f'DTE-{tipo_dte}-{folio}'
        doc_el        = root.find(f'.//sii:Documento[@ID="{doc_id}"]', ns)
        doc_raw       = etree.tostring(doc_el, encoding='unicode')
        doc_standalone = etree.fromstring(doc_raw.encode('utf-8'))
        doc_c14n      = etree.tostring(doc_standalone, method='c14n', exclusive=False)
        digest_doc    = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        # SignedInfo CON xmlns:xsi (crítico: DTE declara xmlns:xsi en su nsmap,
        # por lo que el SII lo incluye al hacer c14n del SignedInfo)
        si_c14n   = _signed_info_dte(f'#{doc_id}', digest_doc)
        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()

        sig_xml = _build_signature_block(
            si_c14n.decode('utf-8'), firma_b64,
            self._rsa_mod, self._rsa_exp, self._cert_der_b64
        )
        root.append(etree.fromstring(sig_xml.encode()))

        xml_str = etree.tostring(root, encoding='unicode', xml_declaration=False)
        return xml_str.encode('ISO-8859-1')
