# app/services/firma_dte.py
# ══════════════════════════════════════════════════════════════
# Firma individual de DTEs para SII Chile — v6.0 DEFINITIVO
#
# BUG RAÍZ ENCONTRADO (análisis forense exhaustivo):
#
# ctx.remove(root) cambia el c14n del Documento.
# El DigestValue se calcula DENTRO del ctx, pero el XML final
# se serializa DESPUÉS de sacar el DTE del ctx → los bytes
# son diferentes → DigestValue en el XML ≠ DigestValue real
# del Documento que quedó en el XML → SII rechaza con RFR.
#
# SOLUCIÓN: serializar el DTE mientras todavía está en el ctx.
# Así el DigestValue calculado y los bytes serializados son
# del mismo Documento con el mismo contexto de namespaces.
#
# El about que construir_sobre() re-firme es innecesario —
# el DigestValue calculado dentro del ctx ES el correcto
# porque el ctx tiene el mismo nsmap que el EnvioDTE real.
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
        """Genera TED. FRMT firmado sobre bytes ISO-8859-1 del <DD> sin namespace."""
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

    # ── Firma XMLDSig ─────────────────────────────────────────

    def firmar(self, xml_bytes: bytes, folio: int, tipo_dte: int,
               xml_caf: str, fecha_emision: str, rut_emisor: str,
               monto_total: int, it1_nombre: str = 'PRODUCTO') -> bytes:
        """
        Inserta TED y firma el DTE.

        FIX CRÍTICO v6.0:
        El ctx temporal se usa para que el c14n del Documento incluya
        los xmlns="" correctos. PERO el DTE se serializa MIENTRAS sigue
        dentro del ctx — antes de ctx.remove(). Esto garantiza que los
        bytes serializados son idénticos al contenido sobre el que se
        calculó el DigestValue.

        Sin este fix: ctx.remove(root) cambia el c14n → DigestValue en
        el XML no coincide con el Documento real → SII rechaza con RFR.
        """
        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(xml_bytes, parser)
        ns     = {'sii': SII_NS}

        # 1. Reemplazar TED placeholder con TED real
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

        # 2. Actualizar TmstFirma
        tmst_el = root.find('.//sii:TmstFirma', ns)
        if tmst_el is not None:
            tmst_el.text = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

        # 3. Insertar DTE en ctx con nsmap del EnvioDTE
        #    El c14n del Documento en este contexto es el que verifica el SII
        ctx = etree.Element(
            f'{{{SII_NS}}}_ctx',
            nsmap={None: SII_NS, 'xsi': XSI_NS}
        )
        ctx.append(root)

        # 4. Calcular DigestValue del Documento (in-ctx)
        doc_id  = f'DTE-{tipo_dte}-{folio}'
        doc_el  = root.find(f'.//sii:Documento[@ID="{doc_id}"]', ns)
        doc_c14n = etree.tostring(
            doc_el, method='c14n', exclusive=False, with_comments=False
        )
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        # 5. Construir Signature (in-ctx, Signature con nsmap={None: xmldsig})
        NS   = XMLDSIG_NS
        C14N = C14N_ALGORITHM
        sig_el = etree.SubElement(root, f'{{{NS}}}Signature', nsmap={None: NS})

        si = etree.SubElement(sig_el, f'{{{NS}}}SignedInfo')
        etree.SubElement(si, f'{{{NS}}}CanonicalizationMethod').set('Algorithm', C14N)
        etree.SubElement(si, f'{{{NS}}}SignatureMethod').set('Algorithm', f'{NS}rsa-sha1')
        ref = etree.SubElement(si, f'{{{NS}}}Reference')
        ref.set('URI', f'#{doc_id}')
        tr = etree.SubElement(ref, f'{{{NS}}}Transforms')
        etree.SubElement(tr, f'{{{NS}}}Transform').set('Algorithm', C14N)
        etree.SubElement(ref, f'{{{NS}}}DigestMethod').set('Algorithm', f'{NS}sha1')
        etree.SubElement(ref, f'{{{NS}}}DigestValue').text = digest_doc

        # 6. c14n del SignedInfo in-ctx → bytes que firma RSA-SHA1
        si_c14n   = etree.tostring(si, method='c14n', exclusive=False, with_comments=False)
        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()

        # 7. Completar Signature
        etree.SubElement(sig_el, f'{{{NS}}}SignatureValue').text = firma_b64
        ki     = etree.SubElement(sig_el, f'{{{NS}}}KeyInfo')
        kv     = etree.SubElement(ki, f'{{{NS}}}KeyValue')
        rsa_kv = etree.SubElement(kv, f'{{{NS}}}RSAKeyValue')
        etree.SubElement(rsa_kv, f'{{{NS}}}Modulus').text  = _wrap64(self._rsa_mod)
        etree.SubElement(rsa_kv, f'{{{NS}}}Exponent').text = self._rsa_exp
        x509d  = etree.SubElement(ki, f'{{{NS}}}X509Data')
        etree.SubElement(x509d, f'{{{NS}}}X509Certificate').text = _wrap64(self._cert_der_b64)

        # 8. ── FIX CRÍTICO ──
        #    Serializar MIENTRAS el DTE sigue en el ctx.
        #    Si se hace ctx.remove(root) primero, el c14n cambia
        #    y el DigestValue ya no coincide con el Documento serializado.
        xml_str = etree.tostring(root, encoding='unicode', xml_declaration=False)

        # 9. Ahora sí sacar del ctx (ya no importa)
        ctx.remove(root)

        return xml_str.encode('ISO-8859-1')

    def firmar_dte_en_sobre(self, dte_el: etree._Element) -> None:
        """No-op — compatibilidad con versiones anteriores."""
        pass

    def firmar_en_arbol(self, dte_el: etree._Element, doc_id: str) -> None:
        """
        Re-firma un DTE que ya está en el árbol del EnvioDTE.
        Usado por sii_sender.construir_sobre() para garantizar que
        el DigestValue coincide con el contexto del sobre real.
        """
        NS   = XMLDSIG_NS
        C14N = C14N_ALGORITHM

        doc_el    = dte_el.find(f'{{{SII_NS}}}Documento')
        doc_c14n  = etree.tostring(doc_el, method='c14n', exclusive=False, with_comments=False)
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        sig_el = etree.SubElement(dte_el, f'{{{NS}}}Signature', nsmap={None: NS})
        si = etree.SubElement(sig_el, f'{{{NS}}}SignedInfo')
        etree.SubElement(si, f'{{{NS}}}CanonicalizationMethod').set('Algorithm', C14N)
        etree.SubElement(si, f'{{{NS}}}SignatureMethod').set('Algorithm', f'{NS}rsa-sha1')
        ref = etree.SubElement(si, f'{{{NS}}}Reference')
        ref.set('URI', f'#{doc_id}')
        tr = etree.SubElement(ref, f'{{{NS}}}Transforms')
        etree.SubElement(tr, f'{{{NS}}}Transform').set('Algorithm', C14N)
        etree.SubElement(ref, f'{{{NS}}}DigestMethod').set('Algorithm', f'{NS}sha1')
        etree.SubElement(ref, f'{{{NS}}}DigestValue').text = digest_doc

        si_c14n   = etree.tostring(si, method='c14n', exclusive=False, with_comments=False)
        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()

        etree.SubElement(sig_el, f'{{{NS}}}SignatureValue').text = firma_b64
        ki     = etree.SubElement(sig_el, f'{{{NS}}}KeyInfo')
        kv     = etree.SubElement(ki, f'{{{NS}}}KeyValue')
        rsa_kv = etree.SubElement(kv, f'{{{NS}}}RSAKeyValue')
        etree.SubElement(rsa_kv, f'{{{NS}}}Modulus').text  = _wrap64(self._rsa_mod)
        etree.SubElement(rsa_kv, f'{{{NS}}}Exponent').text = self._rsa_exp
        x509d  = etree.SubElement(ki, f'{{{NS}}}X509Data')
        etree.SubElement(x509d, f'{{{NS}}}X509Certificate').text = _wrap64(self._cert_der_b64)


# Aliases de compatibilidad
FirmadorDTE = FirmaDTE
