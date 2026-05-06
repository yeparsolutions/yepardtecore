# app/services/firma_dte.py  v8.0
# Firma individual de DTEs para SII Chile
#
# FIX v8.0:
#   - TED insertado como string (no DOM) -> DD queda sin xmlns -> FRMT correcto
#   - c14n in-tree para DigestValue y SignedInfo (igual que el SII)
#   - CAF buscado con namespace SII (caf_sin_ns corregido)

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
    """Formatea base64 en lineas de 64 caracteres."""
    clean = s.replace('\n', '').replace(' ', '')
    return '\n' + '\n'.join(textwrap.wrap(clean, 64)) + '\n'


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
        return m.group(1) if m else ''

    @property
    def cert_der_b64(self) -> str:
        return self._cert_der_b64

    # ---- Helpers estaticos -------------------------------------------

    @staticmethod
    def _strip_ns(xml_str: str) -> str:
        """Elimina todas las declaraciones de namespace de un string XML."""
        return re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', '', xml_str)

    @staticmethod
    def _caf_sin_ns(xml_caf: str) -> tuple:
        """
        Extrae (caf_str_sin_namespace, rsask_pem) del XML del CAF del SII.

        El CAF del SII viene con xmlns='http://www.sii.cl/SiiDte'.
        Se busca con namespace y luego se elimina para insertar en el DD.
        """
        SII = "http://www.sii.cl/SiiDte"
        parser = etree.XMLParser(remove_blank_text=True)
        root = etree.fromstring(xml_caf.encode(), parser)

        # Buscar CAF con namespace SII (is not None evita FutureWarning de lxml)
        caf_el = root.find(f'.//{{{SII}}}CAF')
        if caf_el is None:
            caf_el = root.find('.//CAF')
        if caf_el is None:
            raise ValueError("CAF no encontrado en el XML del CAF")

        # Buscar RSASK (clave privada del CAF) con o sin namespace
        rsask_el = root.find(f'.//{{{SII}}}RSASK')
        if rsask_el is None:
            rsask_el = root.find('.//RSASK')
        if rsask_el is None:
            raise ValueError("RSASK no encontrado en el CAF")

        # Serializar CAF y eliminar namespace
        caf_str_raw = etree.tostring(caf_el, encoding='unicode')
        caf_str = FirmaDTE._strip_ns(caf_str_raw)

        return caf_str, rsask_el.text.strip()

    # ---- TED ----------------------------------------------------------

    def generar_ted(self, folio: int, tipo_dte: int, xml_caf: str,
                    fecha_emision: str, rut_emisor: str, monto_total: int,
                    it1_nombre: str = 'PRODUCTO',
                    rut_receptor: str = '66666666-6',
                    rsoc_receptor: str = 'CONSUMIDOR FINAL') -> bytes:
        """
        Genera TED. FRMT firmado sobre <DD>...</DD> sin namespace.

        FIX: caf_str limpio de xmlns -> dd_xml firmado coincide
        con lo que el SII lee en el source XML.
        """
        caf_str, rsask_text = FirmaDTE._caf_sin_ns(xml_caf)

        it1_safe = (
            it1_nombre[:40]
            .replace('&', ' y ').replace("'", '').replace('"', '')
            .replace('#', '').replace('<', '&lt;').replace('>', '&gt;')
        ).strip()

        tsted = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

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

        # xmlns="" en TED para que lxml no promueva a SII namespace al insertar.
        # Se elimina en firmar() antes de escribir el source final,
        # dejando <TED version="1.0"><DD>...</DD></TED> sin ninguna declaracion.
        return (
            f'<TED xmlns="" version="1.0">{dd_xml}'
            f'<FRMT algoritmo="SHA1withRSA">{frmt_b64}</FRMT>'
            f'</TED>'
        ).encode('ISO-8859-1')

    def _firmar_rsa_caf(self, data: bytes, pem_key_str: str) -> bytes:
        """Firma datos con la clave privada RSA del CAF."""
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

    # ---- Firma XMLDSig ------------------------------------------------

    def _build_signature(self, parent_el, doc_id: str, digest_doc: str):
        """Construye el elemento Signature y retorna (sig_el, si_el)."""
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
        etree.SubElement(tr, f'{{{NS}}}Transform').set('Algorithm', C14N)
        etree.SubElement(ref, f'{{{NS}}}DigestMethod').set(
            'Algorithm', f'{NS}sha1')
        etree.SubElement(ref, f'{{{NS}}}DigestValue').text = digest_doc

        return sig_el, si

    def _complete_signature(self, sig_el, si_el) -> None:
        """Firma el SignedInfo con c14n in-tree y agrega KeyInfo."""
        NS = XMLDSIG_NS

        # c14n in-tree del SignedInfo (el SII verifica igual)
        si_c14n   = etree.tostring(
            si_el, method='c14n', exclusive=False, with_comments=False
        )
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

    # ---- API publica --------------------------------------------------

    def firmar(self, xml_bytes: bytes, folio: int, tipo_dte: int,
               xml_caf: str, fecha_emision: str, rut_emisor: str,
               monto_total: int, it1_nombre: str = 'PRODUCTO') -> bytes:
        """
        Inserta TED y firma el DTE individual. v8.0

        FIX TED: insercion como string literal (no DOM lxml) para que
        <DD> quede sin xmlns en el source XML. El SII verifica FRMT
        sobre esos bytes exactos.

        FIX DigestValue: c14n in-tree (igual que el SII al verificar).
        """
        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(xml_bytes, parser)
        ns     = {'sii': SII_NS}

        # 1. Generar TED bytes y convertir a string
        ted_bytes_raw = self.generar_ted(
            folio, tipo_dte, xml_caf, fecha_emision,
            rut_emisor, monto_total, it1_nombre
        )
        ted_str = ted_bytes_raw.decode('ISO-8859-1')

        # Quitar xmlns="" del TED: el source queda <TED version="1.0"><DD>...</DD>
        # Al parsear el SII, TED hereda xmlns SII (para c14n correcto),
        # pero el source literal tiene <DD> que el SII usa para verificar FRMT.
        ted_str_limpio = ted_str.replace('<TED xmlns="" ', '<TED ', 1)

        # 2. Actualizar TmstFirma
        tmst_el = root.find('.//sii:TmstFirma', ns)
        if tmst_el is not None:
            tmst_el.text = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

        # 3. Serializar DTE a string e insertar TED como string literal
        xml_str = etree.tostring(root, encoding='unicode', xml_declaration=False)

        # Reemplazar el placeholder TED (cualquier forma con/sin namespace)
        xml_con_ted = re.sub(
            r'<(?:[^:>]+:)?TED(?:\s[^>]*)?(?:/>|>(?:.*?)</(?:[^:>]+:)?TED>)',
            ted_str_limpio,
            xml_str,
            count=1,
            flags=re.DOTALL
        )

        if xml_con_ted == xml_str:
            # Fallback: insertar antes de TmstFirma
            xml_con_ted = xml_str.replace(
                '<TmstFirma>', ted_str_limpio + '<TmstFirma>', 1
            )

        # 4. Re-parsear con TED ya en source.
        #    FIX: pasar el string Unicode directamente (NO .encode('ISO-8859-1'))
        #    Si se pasan bytes ISO-8859-1 sin XML declaration, lxml asume UTF-8
        #    y falla con caracteres espanoles (n~, a', e') -> XMLSyntaxError.
        #    lxml acepta str Unicode sin problemas de codificacion.
        root_final = etree.fromstring(xml_con_ted, parser)

        # 5. DigestValue con c14n in-tree
        doc_id = f'DTE-{tipo_dte}-{folio}'
        doc_el = root_final.find(f'.//sii:Documento[@ID="{doc_id}"]', ns)
        doc_c14n   = etree.tostring(
            doc_el, method='c14n', exclusive=False, with_comments=False
        )
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        # 6. Signature
        sig_el, si_el = self._build_signature(root_final, doc_id, digest_doc)
        self._complete_signature(sig_el, si_el)

        # 7. Serializar final
        xml_out = etree.tostring(root_final, encoding='unicode',
                                  xml_declaration=False)
        return xml_out.encode('ISO-8859-1')

    def firmar_en_arbol(self, dte_el: etree._Element, doc_id: str) -> None:
        """
        Re-firma un DTE ya insertado en el arbol del EnvioDTE.
        Usa c14n in-tree para que DigestValue coincida con la
        verificacion del SII.
        """
        doc_el = dte_el.find(f'{{{SII_NS}}}Documento')

        # DigestValue in-tree (igual que el SII al verificar)
        doc_c14n   = etree.tostring(
            doc_el, method='c14n', exclusive=False, with_comments=False
        )
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        sig_el, si_el = self._build_signature(dte_el, doc_id, digest_doc)
        self._complete_signature(sig_el, si_el)


# Alias de compatibilidad
FirmadorDTE = FirmaDTE
