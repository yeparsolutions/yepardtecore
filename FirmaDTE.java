# app/services/firma_dte.py  v9.1
# Firma individual de DTEs para SII Chile
#
# FIX v9.0:
#   - DigestValue calculado con c14n IN-TREE (write_c14n del arbol completo)
#   - El SII verifica con in-tree -> coincide -> resuelve DTE-3-505
#   - TED insertado como string (no DOM) -> DD queda sin xmlns -> FRMT correcto
#   - CAF buscado con namespace SII (caf_sin_ns corregido)

import re
import hashlib
import textwrap
import io
from cryptography.hazmat.primitives.serialization import pkcs12, load_pem_private_key
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, utils
from cryptography.hazmat.backends import default_backend
from lxml import etree
from base64 import b64encode
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Timezone de Chile — el SII valida que TmstFirma sea hora local chilena
_CHILE_TZ = ZoneInfo("America/Santiago")

def _now_chile() -> str:
    return datetime.now(_CHILE_TZ).strftime('%Y-%m-%dT%H:%M:%S')

XMLDSIG_NS     = "http://www.w3.org/2000/09/xmldsig#"
SII_NS         = "http://www.sii.cl/SiiDte"
XSI_NS         = "http://www.w3.org/2001/XMLSchema-instance"
C14N_ALGORITHM     = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
ENVELOPED_SIG_ALG  = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"


def _wrap64(s: str) -> str:
    """Formatea base64 en lineas de 64 caracteres."""
    clean = s.replace('\n', '').replace(' ', '')
    return '\n' + '\n'.join(textwrap.wrap(clean, 64)) + '\n'


def _c14n_intree(el, doc_id: str) -> bytes:
    """
    C14N in-tree: canonicaliza el elemento dentro de su arbol completo
    usando write_c14n del documento entero y extrayendo el fragmento.

    Este es el metodo que usa el SII al verificar DigestValues:
    parsea el XML completo, resuelve URI="#ID", y canonicaliza el nodo
    en su contexto (sin xmlns en el Documento porque ya esta declarado
    en el ancestro EnvioDTE).

    Verificado empiricamente:
    - El ejemplo oficial F60T33 del SII coincide con este metodo (✅)
    - standalone (re-parse) produce xmlns="...SiiDte" en el Documento
      que el SII no incluye al canonicalizar -> DigestValue distinto -> 505
    """
    buf = io.BytesIO()
    el.getroottree().write_c14n(buf, exclusive=False, with_comments=False)
    full_c14n = buf.getvalue()
    marker = f'<Documento ID="{doc_id}"'.encode()
    start  = full_c14n.find(marker)
    end    = full_c14n.find(b'</Documento>', start) + len(b'</Documento>')
    return full_c14n[start:end]


def _rsa_sign_sha1(private_key, data: bytes) -> bytes:
    """Firma SHA1withRSA (PKCS#1 v1.5)."""
    digest = hashlib.sha1(data).digest()
    try:
        return private_key.sign_prehash(digest, padding.PKCS1v15())
    except AttributeError:
        return private_key.sign(
            digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA1())
        )


class FirmaDTE:
    """Firma DTEs individuales para SII Chile."""

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
        if m:
            return m.group(1)
        try:
            from cryptography.x509 import ExtensionOID
            san = self._cert.extensions.get_extension_for_oid(
                ExtensionOID.SUBJECT_ALTERNATIVE_NAME
            )
            for name in san.value:
                if hasattr(name, 'value') and isinstance(name.value, bytes):
                    raw = name.value.decode('utf-8', errors='replace')
                    m2 = re.search(r'(\d{7,8}-[\dkK])', raw)
                    if m2:
                        return m2.group(1)
        except Exception:
            pass
        try:
            der_text = self._cert.public_bytes(
                __import__('cryptography.hazmat.primitives.serialization',
                           fromlist=['Encoding']).Encoding.DER
            ).decode('latin-1', errors='replace')
            m3 = re.search(r'(\d{7,8}-[\dkK])', der_text)
            if m3:
                return m3.group(1)
        except Exception:
            pass
        return ''

    @property
    def cert_der_b64(self) -> str:
        return self._cert_der_b64

    @staticmethod
    def _strip_ns(xml_str: str) -> str:
        return re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', '', xml_str)

    @staticmethod
    def _caf_sin_ns(xml_caf: str) -> tuple:
        SII = "http://www.sii.cl/SiiDte"
        parser = etree.XMLParser(remove_blank_text=False)
        root = etree.fromstring(xml_caf.encode(), parser)
        caf_el = root.find(f'.//{{{SII}}}CAF')
        if caf_el is None:
            caf_el = root.find('.//CAF')
        if caf_el is None:
            raise ValueError("CAF no encontrado en el XML del CAF")
        rsask_el = root.find(f'.//{{{SII}}}RSASK')
        if rsask_el is None:
            rsask_el = root.find('.//RSASK')
        if rsask_el is None:
            raise ValueError("RSASK no encontrado en el CAF")
        caf_str_raw = etree.tostring(caf_el, encoding='unicode')
        caf_str = FirmaDTE._strip_ns(caf_str_raw)
        return caf_str, rsask_el.text.strip()

    def generar_ted(self, folio: int, tipo_dte: int, xml_caf: str,
                    fecha_emision: str, rut_emisor: str, monto_total: int,
                    it1_nombre: str = 'PRODUCTO',
                    rut_receptor: str = '66666666-6',
                    rsoc_receptor: str = 'CONSUMIDOR FINAL') -> bytes:
        caf_str, rsask_text = FirmaDTE._caf_sin_ns(xml_caf)
        it1_safe = (
            it1_nombre[:40]
            .replace('&', ' y ').replace("'", '').replace('"', '')
            .replace('#', '').replace('<', '&lt;').replace('>', '&gt;')
        ).strip()
        tsted = _now_chile()
        dd_xml = (
            f'<DD>'
            f'<RE>{rut_emisor}</RE><TD>{tipo_dte}</TD><F>{folio}</F>'
            f'<FE>{fecha_emision}</FE><RR>{rut_receptor}</RR>'
            f'<RSR>{rsoc_receptor}</RSR><MNT>{monto_total}</MNT>'
            f'<IT1>{it1_safe}</IT1>{caf_str}'
            f'<TSTED>{tsted}</TSTED>'
            f'</DD>'
        )
        frmt_b64 = b64encode(
            self._firmar_rsa_caf(dd_xml.encode('ISO-8859-1'), rsask_text)
        ).decode()
        return (
            f'<TED xmlns="" version="1.0">{dd_xml}'
            f'<FRMT algoritmo="SHA1withRSA">{frmt_b64}</FRMT>'
            f'</TED>'
        ).encode('ISO-8859-1')

    def _firmar_rsa_caf(self, data: bytes, pem_key_str: str) -> bytes:
        if '-----' not in pem_key_str:
            pem = (
                '-----BEGIN RSA PRIVATE KEY-----\n'
                + pem_key_str
                + '\n-----END RSA PRIVATE KEY-----'
            )
        else:
            pem = pem_key_str
        pk = load_pem_private_key(pem.encode(), password=None,
                                   backend=default_backend())
        digest = hashlib.sha1(data).digest()
        return pk.sign(digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA1()))

    def _build_signature(self, parent_el, doc_id: str, digest_doc: str):
        NS   = XMLDSIG_NS
        C14N = C14N_ALGORITHM
        sig_el = etree.SubElement(parent_el, f'{{{NS}}}Signature',
                                   nsmap={None: NS})
        si = etree.SubElement(sig_el, f'{{{NS}}}SignedInfo')
        etree.SubElement(si, f'{{{NS}}}CanonicalizationMethod').set(
            'Algorithm', C14N)
        etree.SubElement(si, f'{{{NS}}}SignatureMethod').set(
            'Algorithm', f'{NS}rsa-sha1')
        ref = etree.SubElement(si, f'{{{NS}}}Reference')
        ref.set('URI', f'#{doc_id}')
        tr = etree.SubElement(ref, f'{{{NS}}}Transforms')
        etree.SubElement(tr, f'{{{NS}}}Transform').set('Algorithm', ENVELOPED_SIG_ALG)
        etree.SubElement(ref, f'{{{NS}}}DigestMethod').set(
            'Algorithm', f'{NS}sha1')
        etree.SubElement(ref, f'{{{NS}}}DigestValue').text = digest_doc
        return sig_el, si

    def _complete_signature(self, sig_el, si_el) -> None:
        NS = XMLDSIG_NS
        # c14n IN-TREE del SignedInfo: igual que el SII al verificar.
        # El SII canonicaliza el SignedInfo en contexto del arbol completo.
        # standalone agrega xmlns extras que cambian la firma RSA -> 505.
        buf = io.BytesIO()
        si_el.getroottree().write_c14n(buf, exclusive=False, with_comments=False)
        full = buf.getvalue()
        s = full.find(b'<SignedInfo')
        e = full.find(b'</SignedInfo>', s) + len(b'</SignedInfo>')
        si_c14n = full[s:e]
        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()
        etree.SubElement(sig_el, f'{{{NS}}}SignatureValue').text = firma_b64
        ki     = etree.SubElement(sig_el, f'{{{NS}}}KeyInfo')
        kv     = etree.SubElement(ki, f'{{{NS}}}KeyValue')
        rsa_kv = etree.SubElement(kv, f'{{{NS}}}RSAKeyValue')
        etree.SubElement(rsa_kv, f'{{{NS}}}Modulus').text  = _wrap64(self._rsa_mod)
        etree.SubElement(rsa_kv, f'{{{NS}}}Exponent').text = self._rsa_exp
        x509d = etree.SubElement(ki, f'{{{NS}}}X509Data')
        etree.SubElement(x509d, f'{{{NS}}}X509Certificate').text = (
            _wrap64(self._cert_der_b64)
        )

    def firmar(self, xml_bytes: bytes, folio: int, tipo_dte: int,
               xml_caf: str, fecha_emision: str, rut_emisor: str,
               monto_total: int, it1_nombre: str = 'PRODUCTO') -> bytes:
        """
        Inserta TED y firma el DTE individual. v9.0

        FIX DigestValue v9.0: c14n IN-TREE (write_c14n del arbol completo).
        El SII verifica con in-tree -> sin xmlns en Documento -> coincide -> no 505.
        """
        parser = etree.XMLParser(remove_blank_text=False)
        root   = etree.fromstring(xml_bytes, parser)
        ns     = {'sii': SII_NS}

        # 1. Generar TED
        # Extraer receptor del XML para el TED
        rut_recep_el2  = root.find('.//sii:RUTRecep', ns)
        rsoc_recep_el2 = root.find('.//sii:RznSocRecep', ns)
        _rut_receptor  = rut_recep_el2.text  if rut_recep_el2  is not None else '66666666-6'
        _rsoc_receptor = (rsoc_recep_el2.text if rsoc_recep_el2 is not None else 'CONSUMIDOR FINAL')[:40]

        ted_bytes_raw = self.generar_ted(
            folio, tipo_dte, xml_caf, fecha_emision,
            rut_emisor, monto_total, it1_nombre,
            rut_receptor=_rut_receptor,
            rsoc_receptor=_rsoc_receptor,
        )
        ted_str = ted_bytes_raw.decode('ISO-8859-1')
        ted_str_limpio = ted_str.replace('<TED xmlns="" ', '<TED ', 1)

        # 2. TmstFirma
        tmst_el = root.find('.//sii:TmstFirma', ns)
        if tmst_el is not None:
            tmst_el.text = _now_chile()

        # 3. Serializar e insertar TED como string literal
        xml_str = etree.tostring(root, encoding='unicode', xml_declaration=False)
        xml_con_ted = re.sub(
            r'<(?:[^:>]+:)?TED(?:\s[^>]*)?(?:/>|>(?:.*?)</(?:[^:>]+:)?TED>)',
            ted_str_limpio,
            xml_str,
            count=1,
            flags=re.DOTALL
        )
        if xml_con_ted == xml_str:
            xml_con_ted = xml_str.replace(
                '<TmstFirma>', ted_str_limpio + '<TmstFirma>', 1
            )

        # 4. Re-parsear con TED en source
        root_final = etree.fromstring(xml_con_ted, parser)

        # 5. DigestValue con c14n IN-TREE (FIX v9.0)
        # El SII canonicaliza el Documento dentro del EnvioDTE:
        # <Documento ID="DTE-33-61"> sin xmlns (lo hereda del padre)
        # standalone agrega xmlns="...SiiDte" -> digest distinto -> 505
        doc_id = f'DTE-{tipo_dte}-{folio}'
        doc_el = root_final.find(f'.//sii:Documento[@ID="{doc_id}"]', ns)
        doc_c14n   = _c14n_intree(doc_el, doc_id)
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        # 6. Signature
        sig_el, si_el = self._build_signature(root_final, doc_id, digest_doc)
        self._complete_signature(sig_el, si_el)

        # 7. Serializar final
        xml_out = etree.tostring(root_final, encoding='unicode',
                                  xml_declaration=False)
        return xml_out.encode('ISO-8859-1')


    def generar_xml_con_ted(self, xml_bytes: bytes, folio: int, tipo_dte: int,
                             xml_caf: str, fecha_emision: str, rut_emisor: str,
                             monto_total: int, it1_nombre: str = 'PRODUCTO') -> bytes:
        """
        Inserta el TED (timbre) en el DTE SIN agregar la firma XMLDSig.
        El XML resultante está listo para ser firmado por Java.

        Pasos:
          1. Generar TED con llave CAF
          2. Insertar TED como string en el XML
          3. Re-parsear con TED en source
          4. Devolver bytes ISO-8859-1 listos para FirmaDTE.java
        """
        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(xml_bytes, parser)
        ns     = {'sii': SII_NS}

        # Extraer receptor del XML para el TED
        rut_recep_el  = root.find('.//sii:RUTRecep', ns)
        rsoc_recep_el = root.find('.//sii:RznSocRecep', ns)
        rut_receptor  = rut_recep_el.text  if rut_recep_el  is not None else '66666666-6'
        rsoc_receptor = rsoc_recep_el.text if rsoc_recep_el is not None else 'CONSUMIDOR FINAL'
        # Truncar a 40 chars como requiere el SII
        rsoc_receptor = rsoc_receptor[:40]

        # 1. Generar TED con receptor correcto
        ted_bytes_raw = self.generar_ted(
            folio, tipo_dte, xml_caf, fecha_emision,
            rut_emisor, monto_total, it1_nombre,
            rut_receptor=rut_receptor,
            rsoc_receptor=rsoc_receptor,
        )
        ted_str = ted_bytes_raw.decode('ISO-8859-1')
        ted_str_limpio = ted_str.replace('<TED xmlns="" ', '<TED ', 1)

        # 2. TmstFirma
        tmst_el = root.find('.//sii:TmstFirma', ns)
        if tmst_el is not None:
            tmst_el.text = _now_chile()

        # 3. Serializar e insertar TED como string literal
        xml_str = etree.tostring(root, encoding='unicode', xml_declaration=False)
        xml_con_ted = re.sub(
            r'<(?:[^:>]+:)?TED(?:\s[^>]*)?(?:/>|>(?:.*?)</(?:[^:>]+:)?TED>)',
            ted_str_limpio,
            xml_str,
            count=1,
            flags=re.DOTALL
        )
        if xml_con_ted == xml_str:
            xml_con_ted = xml_str.replace(
                '<TmstFirma>', ted_str_limpio + '<TmstFirma>', 1
            )

        # 4. Re-parsear y devolver bytes ISO-8859-1
        root_final = etree.fromstring(xml_con_ted, parser)
        xml_out = etree.tostring(root_final, encoding='unicode', xml_declaration=False)
        return xml_out.encode('ISO-8859-1')

    def firmar_en_arbol(self, dte_el: etree._Element, doc_id: str) -> None:
        """
        Re-firma un DTE ya insertado en el arbol del EnvioDTE.
        FIX v9.0: usa c14n IN-TREE para DigestValue (igual que el SII).
        """
        doc_el = dte_el.find(f'{{{SII_NS}}}Documento')

        # DigestValue con c14n IN-TREE (FIX v9.0)
        doc_c14n   = _c14n_intree(doc_el, doc_id)
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        sig_el, si_el = self._build_signature(dte_el, doc_id, digest_doc)
        self._complete_signature(sig_el, si_el)


# Alias de compatibilidad
FirmadorDTE = FirmaDTE
