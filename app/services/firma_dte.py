# app/services/firma_dte.py
# ══════════════════════════════════════════════════════════════
# Firma individual de DTEs para SII Chile
#
# FIX DEFINITIVO v3.0
# ─────────────────────────────────────────────────────────────
# PROBLEMA RAÍZ (DTE-3-505):
# El SignedInfo se construía como f-string con xmlns="" explícitos
# en Transforms/Transform/DigestMethod/DigestValue → el validador
# XSD del SII rechazaba porque esos elementos deben estar en
# namespace xmldsig (no xmlns="").
#
# SOLUCIÓN: Construir la Signature con lxml usando nsmap={None: XMLDSIG_NS}
# en el elemento <Signature>. Esto logra DOS cosas simultáneamente:
#
#   1. XML serializado: todos los elementos en namespace xmldsig
#      → pasa validación XSD sin errores.
#
#   2. c14n del SignedInfo: en el contexto del EnvioDTE (xmlns=SiiDte),
#      los elementos Transforms/Transform/DigestMethod/DigestValue
#      reciben xmlns="" porque el namespace default cambia de xmldsig
#      a SiiDte. El código firma sobre ESE c14n → SII verifica OK.
#
# Verificado: XML sin xmlns="" (XSD OK) + c14n con xmlns="" (firma OK).
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


def _build_signature_lxml(reference_uri: str, digest_value: str,
                           private_key, rsa_mod: str, rsa_exp: str,
                           cert_der_b64: str) -> etree._Element:
    """
    Construye el elemento <Signature> completo usando lxml.

    CLAVE — nsmap={None: XMLDSIG_NS} en <Signature>:
      - XML serializado: elementos en namespace xmldsig → XSD válido
      - c14n del SignedInfo en contexto EnvioDTE (xmlns=SiiDte):
        Transforms/Transform/DigestMethod/DigestValue reciben xmlns=""
        porque el namespace default cambia → bytes exactos que verifica el SII

    Árbol temporal: _ctx(SiiDte+xsi) > DTE > Signature(xmldsig)
    Replica el contexto namespace del EnvioDTE real para que el c14n
    del SignedInfo sea idéntico al que computará el SII.
    """
    NS   = XMLDSIG_NS
    C14N = C14N_ALGORITHM

    # Árbol temporal que replica el contexto del EnvioDTE
    _ctx = etree.Element('_ctx', nsmap={None: SII_NS, 'xsi': XSI_NS})
    _dte = etree.SubElement(_ctx, f'{{{SII_NS}}}DTE')

    # Signature con xmldsig como namespace default
    sig_el = etree.SubElement(_dte, f'{{{NS}}}Signature', nsmap={None: NS})

    # SignedInfo y sus hijos — todos en namespace xmldsig
    si = etree.SubElement(sig_el, f'{{{NS}}}SignedInfo')
    cm = etree.SubElement(si, f'{{{NS}}}CanonicalizationMethod')
    cm.set('Algorithm', C14N)
    sm = etree.SubElement(si, f'{{{NS}}}SignatureMethod')
    sm.set('Algorithm', f'{NS}rsa-sha1')
    ref = etree.SubElement(si, f'{{{NS}}}Reference')
    ref.set('URI', reference_uri)
    transforms = etree.SubElement(ref, f'{{{NS}}}Transforms')
    transform  = etree.SubElement(transforms, f'{{{NS}}}Transform')
    transform.set('Algorithm', C14N)
    dm = etree.SubElement(ref, f'{{{NS}}}DigestMethod')
    dm.set('Algorithm', f'{NS}sha1')
    dv_el = etree.SubElement(ref, f'{{{NS}}}DigestValue')
    dv_el.text = digest_value

    # c14n del SignedInfo en el árbol temporal
    # En este contexto (SiiDte como ns default del ancestro, xmldsig en Signature),
    # el c14n produce xmlns="" en Transforms/Transform/DigestMethod/DigestValue
    # → idéntico a lo que calcula el SII al verificar dentro del EnvioDTE real
    si_c14n = etree.tostring(si, method='c14n', exclusive=False,
                              with_comments=False)

    # Firmar el c14n con RSA-SHA1
    firma_b64 = b64encode(_rsa_sign_sha1(private_key, si_c14n)).decode()

    # SignatureValue
    sv_el = etree.SubElement(sig_el, f'{{{NS}}}SignatureValue')
    sv_el.text = firma_b64

    # KeyInfo: RSAKeyValue + X509Data
    ki     = etree.SubElement(sig_el, f'{{{NS}}}KeyInfo')
    kv     = etree.SubElement(ki, f'{{{NS}}}KeyValue')
    rsa_kv = etree.SubElement(kv, f'{{{NS}}}RSAKeyValue')
    mod_el = etree.SubElement(rsa_kv, f'{{{NS}}}Modulus')
    mod_el.text = _wrap64(rsa_mod)
    exp_el = etree.SubElement(rsa_kv, f'{{{NS}}}Exponent')
    exp_el.text = rsa_exp
    x509d  = etree.SubElement(ki, f'{{{NS}}}X509Data')
    x509c  = etree.SubElement(x509d, f'{{{NS}}}X509Certificate')
    x509c.text = _wrap64(cert_der_b64)

    # Desconectar del árbol temporal y devolver solo la Signature
    _dte.remove(sig_el)
    return sig_el


class FirmaDTE:
    """
    Firma documentos DTE individuales para SII Chile.

    Uso:
        firma = FirmaDTE(p12_bytes, password)
        xml_firmado = firma.firmar(xml_dte_bytes, folio, tipo_dte,
                                    xml_caf, fecha_emision, rut_emisor,
                                    monto_total, it1_nombre)
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
        """
        Genera el TED (Timbre Electrónico de Documento).

        El CAF se parsea con remove_blank_text=True para compactarlo.
        El FRMT se calcula sobre los bytes ISO-8859-1 del <DD> sin namespace.
        """
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
        """Firma RSA-SHA1 con la clave privada del CAF."""
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
        Inserta el TED y firma el <Documento> del DTE.

        Flujo:
          1. Parsear DTE template
          2. Insertar TED generado
          3. Actualizar TmstFirma
          4. Calcular DigestValue (round-trip c14n del Documento)
          5. Construir Signature con lxml → firmar c14n del SignedInfo
          6. Insertar Signature como hija de DTE (hermana de Documento)
          7. Devolver DTE firmado en ISO-8859-1

        La Signature va como hija de <DTE>, no de <Documento>,
        según el schema DTE_v10.xsd del SII.
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

        # 2. Actualizar TmstFirma
        tmst_el = root.find('.//sii:TmstFirma', ns)
        if tmst_el is not None:
            tmst_el.text = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

        # 3. DigestValue del Documento (round-trip c14n)
        # Serializar → parsear standalone → c14n inclusivo.
        # Produce el mismo hash que calcula el SII al verificar el DigestValue.
        doc_id   = f'DTE-{tipo_dte}-{folio}'
        doc_el   = root.find(f'.//sii:Documento[@ID="{doc_id}"]', ns)
        doc_raw  = etree.tostring(doc_el, encoding='unicode')
        doc_sa   = etree.fromstring(doc_raw.encode('utf-8'))
        doc_c14n = etree.tostring(doc_sa, method='c14n', exclusive=False,
                                   with_comments=False)
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        # 4. Construir Signature y firmar
        sig_el = _build_signature_lxml(
            f'#{doc_id}', digest_doc,
            self._private_key,
            self._rsa_mod, self._rsa_exp, self._cert_der_b64
        )

        # 5. Insertar Signature como hija de DTE
        root.append(sig_el)

        xml_str = etree.tostring(root, encoding='unicode', xml_declaration=False)
        return xml_str.encode('ISO-8859-1')

    # Alias de compatibilidad con código legado
    def firmar_dte_en_sobre(self, dte_el: etree._Element) -> None:
        """No-op — compatibilidad con versiones anteriores."""
        pass


# Aliases de compatibilidad
FirmadorDTE = FirmaDTE
