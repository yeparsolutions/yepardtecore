# app/services/firma_dte.py
# ══════════════════════════════════════════════════════════════
# Firma individual de DTEs para SII Chile
#
# PRINCIPIO CLAVE:
# El C14N del SignedInfo que firma el código debe ser IDÉNTICO al que
# calcula el SII al verificar. El SII calcula el C14N del SignedInfo
# leyéndolo directamente del XML del DTE (donde el SignedInfo tiene
# xmlns:xsi y xmlns="" en sus hijos por estar en contexto del DTE).
#
# Por eso el proceso es:
# 1. Construir el Signature completo con etree (no string concatenation)
# 2. Insertarlo en el árbol del DTE
# 3. Calcular el C14N del SignedInfo EN ESE CONTEXTO (con todos los xmlns)
# 4. Firmar esos bytes exactos
# 5. Actualizar el SignatureValue en el árbol
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
    """Firma documentos DTE individuales para SII Chile."""

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
        m = re.search(r'(\d{1,2}\.?\d{3}\.?\d{3}-[\dkK])',
                      self._cert.subject.rfc4514_string(), re.I)
        return m.group(1) if m else ''

    # ── TED ──────────────────────────────────────────────────

    def generar_ted(self, folio: int, tipo_dte: int, xml_caf: str,
                    fecha_emision: str, rut_emisor: str, monto_total: int,
                    it1_nombre: str = 'PRODUCTO') -> bytes:
        """Genera el TED (Timbre Electrónico de Documento)."""
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

        # CRÍTICO: incluir xmlns=SiiDte en el TED para que el namespace sea
        # consistente entre la firma y el XML final.
        # Sin esto, lxml serializa TED sin xmlns='' pero lo computa C14N con xmlns='',
        # produciendo un DigestValue diferente al que el SII calcula.
        ted_ns = 'xmlns="http://www.sii.cl/SiiDte"'
        return (
            f'<TED {ted_ns} version="1.0">{dd_xml}'
            f'<FRMT algoritmo="SHA1withRSA">{frmt_b64}</FRMT>'
            f'</TED>'
        ).encode('ISO-8859-1')

    def _firmar_rsa_caf(self, data: bytes, pem_key_str: str) -> bytes:
        if '-----' not in pem_key_str:
            pem_key_str = ('-----BEGIN RSA PRIVATE KEY-----\n'
                           + pem_key_str + '\n-----END RSA PRIVATE KEY-----')
        pk = load_pem_private_key(pem_key_str.encode(), password=None,
                                   backend=default_backend())
        return _rsa_sign_sha1(pk, data)

    # ── XMLDSig ───────────────────────────────────────────────

    def firmar(self, xml_bytes: bytes, folio: int, tipo_dte: int,
               xml_caf: str, fecha_emision: str, rut_emisor: str,
               monto_total: int, it1_nombre: str = 'PRODUCTO') -> bytes:
        """
        Inserta el TED y firma el Documento del DTE.

        FLUJO:
        1. Insertar TED en el DTE
        2. Calcular DigestValue del Documento
        3. Construir el elemento Signature con etree (evita xmlns="" al insertar)
        4. Insertar Signature en el árbol del DTE
        5. Calcular C14N del SignedInfo EN CONTEXTO del DTE
           (esto incluye xmlns:xsi y xmlns="" que el SII también calculará)
        6. Firmar esos bytes exactos
        7. Actualizar el SignatureValue en el árbol
        8. Serializar el DTE firmado
        """
        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(xml_bytes, parser)
        ns     = {'sii': SII_NS}

        # 1. Insertar TED
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

        # 2. DigestValue del Documento
        doc_id  = f'DTE-{tipo_dte}-{folio}'
        doc_el  = root.find(f'.//sii:Documento[@ID="{doc_id}"]', ns)
        doc_c14n   = etree.tostring(doc_el, method='c14n', exclusive=False)
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        # 3. Construir elemento Signature completo con etree
        sig_el = self._build_signature_element(doc_id, digest_doc, placeholder=True)

        # 4. Insertar en el árbol del DTE
        root.append(sig_el)

        # 5. Calcular C14N del SignedInfo EN CONTEXTO del DTE
        #    Aquí el SignedInfo tiene xmlns:xsi (del DTE root) y xmlns="" en
        #    los hijos de Reference — exactamente lo que el SII calculará.
        si_el   = sig_el.find(f"{{{XMLDSIG_NS}}}SignedInfo")
        si_c14n = etree.tostring(si_el, method='c14n', exclusive=False)

        # 6. Firmar esos bytes exactos
        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()

        # 7. Actualizar el SignatureValue en el árbol
        sv_el = sig_el.find(f"{{{XMLDSIG_NS}}}SignatureValue")
        sv_el.text = firma_b64

        # 8. Serializar
        xml_str = etree.tostring(root, encoding='unicode', xml_declaration=False)
        return xml_str.encode('ISO-8859-1')

    def _build_signature_element(self, doc_id: str, digest_value: str,
                                  placeholder: bool = False) -> etree._Element:
        """
        Construye el elemento Signature completo con etree.
        Todos los subelementos están explícitamente en el namespace DSIG.
        """
        ns = XMLDSIG_NS
        sig_el = etree.Element(f"{{{ns}}}Signature", nsmap={None: ns})

        # SignedInfo
        si_el  = etree.SubElement(sig_el, f"{{{ns}}}SignedInfo")
        etree.SubElement(si_el, f"{{{ns}}}CanonicalizationMethod",
                         attrib={"Algorithm": C14N_ALGORITHM})
        etree.SubElement(si_el, f"{{{ns}}}SignatureMethod",
                         attrib={"Algorithm": f"{ns}rsa-sha1"})
        ref_el = etree.SubElement(si_el, f"{{{ns}}}Reference",
                                   attrib={"URI": f"#{doc_id}"})
        trf_el = etree.SubElement(ref_el, f"{{{ns}}}Transforms")
        etree.SubElement(trf_el, f"{{{ns}}}Transform",
                         attrib={"Algorithm": C14N_ALGORITHM})
        etree.SubElement(ref_el, f"{{{ns}}}DigestMethod",
                         attrib={"Algorithm": f"{ns}sha1"})
        dv_el  = etree.SubElement(ref_el, f"{{{ns}}}DigestValue")
        dv_el.text = digest_value

        # SignatureValue (placeholder o real)
        sv_el  = etree.SubElement(sig_el, f"{{{ns}}}SignatureValue")
        sv_el.text = '' if placeholder else None

        # KeyInfo
        ki_el  = etree.SubElement(sig_el, f"{{{ns}}}KeyInfo")
        kv_el  = etree.SubElement(ki_el, f"{{{ns}}}KeyValue")
        rsa_el = etree.SubElement(kv_el, f"{{{ns}}}RSAKeyValue")
        mod_el = etree.SubElement(rsa_el, f"{{{ns}}}Modulus")
        mod_el.text = _wrap64(self._rsa_mod)
        exp_el = etree.SubElement(rsa_el, f"{{{ns}}}Exponent")
        exp_el.text = self._rsa_exp
        x509d  = etree.SubElement(ki_el, f"{{{ns}}}X509Data")
        cert_el = etree.SubElement(x509d, f"{{{ns}}}X509Certificate")
        cert_el.text = _wrap64(self._cert_der_b64)

        return sig_el
