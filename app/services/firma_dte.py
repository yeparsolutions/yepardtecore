# app/services/firma_dte.py
# ══════════════════════════════════════════════════════════════
# Firma individual de DTEs para SII Chile — v5.0 DEFINITIVO.
#
# CAUSA RAÍZ IDENTIFICADA (semanas de análisis):
# El flujo es: xml_builder genera DTE → firma_dte lo firma solo
# → se guarda en BD → sii_sender lo re-parsea e inserta en EnvioDTE.
#
# El DigestValue se calculaba cuando el DTE estaba SOLO.
# El SII verifica el DigestValue con el DTE DENTRO del EnvioDTE.
# En ese contexto, el c14n del Documento incluye xmlns="" en sus
# hijos (IdDoc, Emisor, etc.) porque el namespace default cambia
# al insertar el DTE en el árbol del EnvioDTE.
#
# SOLUCIÓN: exponer _firmar_en_arbol() para que sii_sender pueda
# re-firmar cada DTE DESPUÉS de insertarlo en el EnvioDTE.
# Esto garantiza que DigestValue y SignatureValue sean calculados
# en el mismo contexto que usará el SII para verificar.
# ══════════════════════════════════════════════════════════════

import re
import hashlib
import textwrap
from cryptography.hazmat.primitives.serialization import pkcs12, load_pem_private_key
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, utils
from cryptography.hazmat.backends import default_backend
from lxml import etree
from base64 import b64encode
from datetime import datetime, timezone

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
    """
    Firma documentos DTE individuales para SII Chile.

    Flujo correcto:
        1. xml_builder genera el DTE (bytes ISO-8859-1)
        2. firma_dte.firmar() inserta el TED y firma standalone
           (para guardar en BD — firma provisional)
        3. sii_sender inserta el DTE en el EnvioDTE
        4. sii_sender llama firmar_en_arbol() para re-firmar
           con el DTE ya dentro del sobre (firma definitiva)
    """

    def __init__(self, p12_bytes: bytes, password):
        pwd = password.encode('utf-8') if isinstance(password, str) else password
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

    @property
    def cert_der_b64(self) -> str:
        return self._cert_der_b64

    # ── TED ──────────────────────────────────────────────────

    def generar_ted(self, folio: int, tipo_dte: int, xml_caf: str,
                    fecha_emision: str, rut_emisor: str, monto_total: int,
                    it1_nombre: str = 'PRODUCTO') -> bytes:
        """Genera el TED. El FRMT se firma sobre bytes ISO-8859-1 del <DD> sin namespace."""
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

    # ── Firma en árbol (método clave) ─────────────────────────

    def firmar_en_arbol(self, dte_el: etree._Element, doc_id: str) -> None:
        """
        Firma el <Documento> con el DTE ya insertado en su árbol padre.

        Este método debe llamarse DESPUÉS de que el DTE fue insertado
        en el EnvioDTE. El c14n del Documento y del SignedInfo se calculan
        en ese contexto, produciendo los xmlns="" que el SII espera.

        La Signature anterior (si existe) debe ser removida antes de llamar.
        Inserta la nueva Signature como hija del DTE (hermana de Documento).
        """
        NS   = XMLDSIG_NS
        C14N = C14N_ALGORITHM

        doc_el   = dte_el.find(f'{{{SII_NS}}}Documento')

        # DigestValue: c14n del Documento en el contexto actual del árbol
        doc_c14n   = etree.tostring(doc_el, method='c14n', exclusive=False,
                                     with_comments=False)
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        # Construir Signature con xmldsig como namespace default
        sig_el = etree.SubElement(dte_el, f'{{{NS}}}Signature', nsmap={None: NS})

        si = etree.SubElement(sig_el, f'{{{NS}}}SignedInfo')
        cm = etree.SubElement(si, f'{{{NS}}}CanonicalizationMethod')
        cm.set('Algorithm', C14N)
        sm = etree.SubElement(si, f'{{{NS}}}SignatureMethod')
        sm.set('Algorithm', f'{NS}rsa-sha1')
        ref = etree.SubElement(si, f'{{{NS}}}Reference')
        ref.set('URI', f'#{doc_id}')
        transforms = etree.SubElement(ref, f'{{{NS}}}Transforms')
        transform  = etree.SubElement(transforms, f'{{{NS}}}Transform')
        transform.set('Algorithm', C14N)
        dm = etree.SubElement(ref, f'{{{NS}}}DigestMethod')
        dm.set('Algorithm', f'{NS}sha1')
        dv_el = etree.SubElement(ref, f'{{{NS}}}DigestValue')
        dv_el.text = digest_doc

        # c14n del SignedInfo en el contexto actual → xmlns="" en Transforms etc.
        si_c14n   = etree.tostring(si, method='c14n', exclusive=False,
                                    with_comments=False)
        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()

        sv_el = etree.SubElement(sig_el, f'{{{NS}}}SignatureValue')
        sv_el.text = firma_b64

        ki     = etree.SubElement(sig_el, f'{{{NS}}}KeyInfo')
        kv     = etree.SubElement(ki, f'{{{NS}}}KeyValue')
        rsa_kv = etree.SubElement(kv, f'{{{NS}}}RSAKeyValue')
        mod_el = etree.SubElement(rsa_kv, f'{{{NS}}}Modulus')
        mod_el.text = _wrap64(self._rsa_mod)
        exp_el = etree.SubElement(rsa_kv, f'{{{NS}}}Exponent')
        exp_el.text = self._rsa_exp
        x509d  = etree.SubElement(ki, f'{{{NS}}}X509Data')
        x509c  = etree.SubElement(x509d, f'{{{NS}}}X509Certificate')
        x509c.text = _wrap64(self._cert_der_b64)

    # ── API principal ─────────────────────────────────────────

    def firmar(self, xml_bytes: bytes, folio: int, tipo_dte: int,
               xml_caf: str, fecha_emision: str, rut_emisor: str,
               monto_total: int, it1_nombre: str = 'PRODUCTO') -> bytes:
        """
        Inserta el TED y firma el DTE standalone (para guardar en BD).

        NOTA: Esta firma es provisional. El DigestValue definitivo
        se calcula en sii_sender.construir_sobre() cuando el DTE
        se inserta en el EnvioDTE y se llama firmar_en_arbol().
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

        # Actualizar TmstFirma
        tmst_el = root.find('.//sii:TmstFirma', ns)
        if tmst_el is not None:
            tmst_el.text = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

        # Firma standalone (provisional — será re-firmada en construir_sobre)
        doc_id = f'DTE-{tipo_dte}-{folio}'
        ctx    = etree.Element(f'{{{SII_NS}}}_ctx', nsmap={None: SII_NS, 'xsi': XSI_NS})
        ctx.append(root)
        self.firmar_en_arbol(root, doc_id)
        ctx.remove(root)

        xml_str = etree.tostring(root, encoding='unicode', xml_declaration=False)
        return xml_str.encode('ISO-8859-1')

    def firmar_dte_en_sobre(self, dte_el: etree._Element) -> None:
        """No-op — compatibilidad con versiones anteriores."""
        pass


# Aliases de compatibilidad
FirmadorDTE = FirmaDTE
