# app/services/firma_dte.py
# ══════════════════════════════════════════════════════════════
# Firma individual de DTEs para SII Chile — v7.0
#
# FIX CRÍTICO (diagnóstico definitivo 2026-05-05):
#
# lxml genera 653+ ocurrencias de xmlns="" cuando se calcula el
# c14n de un sub-elemento "in-tree" dentro de un árbol con
# múltiples contextos de namespace (SII + xmldsig).
#
# El SII verifica los DigestValues usando c14n "standalone":
#   1. Serializar el elemento (tostring)
#   2. Re-parsear como documento independiente (fromstring)
#   3. Calcular c14n del standalone
#
# Esto produce una salida limpia sin xmlns="", que es exactamente
# lo que espera el SII. Con el c14n in-tree (método anterior),
# los bytes eran completamente diferentes → DigestValue incorrecto
# → RFR (Rechazado por Error en Firma).
#
# Analogía: es como firmar una fotocopia del documento, pero el
# SII verifica el original. Con standalone c14n, firmamos el
# original que el SII también ve.
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
    """Formatea un string base64 en líneas de 64 caracteres."""
    clean = s.replace('\n', '').replace(' ', '')
    return '\n' + '\n'.join(textwrap.wrap(clean, 64)) + '\n'


def _c14n_standalone(el: etree._Element) -> bytes:
    """
    Calcula el c14n de un elemento de forma standalone.

    Problema: lxml genera xmlns="" artifacts cuando se hace c14n
    de un sub-elemento dentro de un árbol con múltiples namespaces.
    El SII verifica sin esos artifacts.

    Solución: serializar el elemento (incluye todos los namespace
    declarations en-scope), re-parsear como documento independiente,
    luego calcular c14n sobre ese standalone.

    Analogía: es como sacar una fotocopia del elemento exactamente
    como aparece, sin que le afecten los marcos del árbol padre.
    """
    # Serializar con todos los namespace en-scope heredados
    raw_bytes = etree.tostring(el)
    # Re-parsear como documento independiente (sin contexto del árbol)
    standalone = etree.fromstring(raw_bytes)
    # C14N del standalone: sin xmlns="" artifacts
    return etree.tostring(
        standalone, method='c14n', exclusive=False, with_comments=False
    )


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

    @staticmethod
    def _strip_ns(xml_str: str) -> str:
        """
        Elimina TODAS las declaraciones de namespace de un string XML.

        CRÍTICO para el TED: el DD y sus elementos hijos (incluyendo CAF)
        deben estar SIN namespace en el XML final. El SII verifica el FRMT
        sobre los bytes exactos del DD sin namespace.
        """
        return re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', '', xml_str)

    @staticmethod
    def _caf_sin_ns(xml_caf: str) -> tuple:
        """
        Extrae (caf_str_sin_namespace, rsask_pem) del XML del CAF.
        """
        SII = "http://www.sii.cl/SiiDte"
        parser = etree.XMLParser(remove_blank_text=True)
        root = etree.fromstring(xml_caf.encode(), parser)

        caf_el = (
            root.find(f'.//{{{SII}}}CAF') or
            root.find('.//CAF')
        )
        if caf_el is None:
            raise ValueError("CAF no encontrado en el XML del CAF")

        rsask_el = (
            root.find(f'.//{{{SII}}}RSASK') or
            root.find('.//RSASK')
        )
        if rsask_el is None:
            raise ValueError("RSASK no encontrado en el CAF")

        caf_str_raw    = etree.tostring(caf_el, encoding='unicode')
        caf_str_limpio = FirmaDTE._strip_ns(caf_str_raw)
        rsask_text     = rsask_el.text.strip()
        return caf_str_limpio, rsask_text

    def generar_ted(self, folio: int, tipo_dte: int, xml_caf: str,
                    fecha_emision: str, rut_emisor: str, monto_total: int,
                    it1_nombre: str = 'PRODUCTO') -> bytes:
        """
        Genera TED firmado con la clave RSA del CAF.
        El FRMT cubre los bytes ISO-8859-1 del DD sin namespace.
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
            f'<FE>{fecha_emision}</FE><RR>66666666-6</RR>'
            f'<RSR>CONSUMIDOR FINAL</RSR><MNT>{monto_total}</MNT>'
            f'<IT1>{it1_safe}</IT1>{caf_str}'
            f'<TSTED>{tsted}</TSTED>'
            f'</DD>'
        )

        frmt_b64 = b64encode(
            self._firmar_rsa_caf(dd_xml.encode('ISO-8859-1'), rsask_text)
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

    def _build_signature(
        self,
        parent_el: etree._Element,
        doc_id: str,
        digest_doc: str,
    ) -> etree._Element:
        """
        Construye el elemento Signature completo.
        Retorna el SignedInfo para que el caller lo firme con RSA.
        """
        NS   = XMLDSIG_NS
        C14N = C14N_ALGORITHM

        sig_el = etree.SubElement(parent_el, f'{{{NS}}}Signature', nsmap={None: NS})

        si = etree.SubElement(sig_el, f'{{{NS}}}SignedInfo')
        etree.SubElement(si, f'{{{NS}}}CanonicalizationMethod').set('Algorithm', C14N)
        etree.SubElement(si, f'{{{NS}}}SignatureMethod').set('Algorithm', f'{NS}rsa-sha1')
        ref = etree.SubElement(si, f'{{{NS}}}Reference')
        ref.set('URI', f'#{doc_id}')
        tr = etree.SubElement(ref, f'{{{NS}}}Transforms')
        etree.SubElement(tr, f'{{{NS}}}Transform').set('Algorithm', C14N)
        etree.SubElement(ref, f'{{{NS}}}DigestMethod').set('Algorithm', f'{NS}sha1')
        etree.SubElement(ref, f'{{{NS}}}DigestValue').text = digest_doc

        return sig_el, si

    def _complete_signature(self, sig_el, si_el) -> None:
        """Firma el SignedInfo y completa KeyInfo con el certificado."""
        NS = XMLDSIG_NS

        # c14n IN-TREE del SignedInfo para RSA (el SII verifica igual)
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
        x509d  = etree.SubElement(ki, f'{{{NS}}}X509Data')
        etree.SubElement(x509d, f'{{{NS}}}X509Certificate').text = _wrap64(self._cert_der_b64)

    def firmar(self, xml_bytes: bytes, folio: int, tipo_dte: int,
               xml_caf: str, fecha_emision: str, rut_emisor: str,
               monto_total: int, it1_nombre: str = 'PRODUCTO') -> bytes:
        """
        Inserta TED y firma el DTE individual. v8.0

        FIX TED NAMESPACE (bug raíz del DTE-3-505):
          El FRMT se firma sobre <DD>...</DD> (sin namespace).
          Si el TED se inserta vía DOM lxml, el DD hereda xmlns del padre
          (SII namespace) y el SII al verificar FRMT encuentra bytes distintos.
          SOLUCIÓN: insertar el TED como string literal en el XML serializado
          (igual que LibreDTE PHP) → el source XML queda con <DD>...</DD>
          y el SII verifica FRMT sobre esos bytes exactos. ✓

        FIX DIGESTVALUE (bug del _c14n_standalone):
          _c14n_standalone daba resultados distintos al c14n in-tree.
          El SII verifica el Documento in-tree → usa c14n in-tree.
          Nosotros también debemos usar c14n in-tree para que coincidan. ✓
        """
        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(xml_bytes, parser)
        ns     = {'sii': SII_NS}

        # 1. Generar TED (dd_xml firmado sin namespace, ted como string)
        ted_bytes_str = self.generar_ted(
            folio, tipo_dte, xml_caf, fecha_emision,
            rut_emisor, monto_total, it1_nombre
        )
        ted_str = ted_bytes_str.decode('ISO-8859-1')
        # ted_str es: <TED xmlns="" version="1.0"><DD>...</DD><FRMT>...</FRMT></TED>
        # Quitar xmlns="" del TED para que en el XML quede solo <TED version="1.0">
        # → al parsear el SII, TED hereda xmlns SII (para c14n) pero el source
        #   literal tiene <DD>...</DD> que el SII usa para verificar FRMT
        ted_str_limpio = ted_str.replace('<TED xmlns="" ', '<TED ', 1)

        # 2. Actualizar TmstFirma ANTES de serializar
        tmst_el = root.find('.//sii:TmstFirma', ns)
        if tmst_el is not None:
            tmst_el.text = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

        # 3. Serializar el DTE como string y hacer STRING INSERTION del TED
        #    (no DOM insertion — así el DD queda sin namespace en el source)
        xml_str = etree.tostring(root, encoding='unicode', xml_declaration=False)

        # Buscar el placeholder del TED en el string serializado
        # El placeholder puede tener namespace: <TED ...> o <TED xmlns="...">
        ted_nuevo = re.sub(
            r'<(?:[^:>]+:)?TED(?:\s[^>]*)?(?:/>|>(?:.*?)</(?:[^:>]+:)?TED>)',
            ted_str_limpio,
            xml_str,
            count=1,
            flags=re.DOTALL
        )

        if ted_nuevo == xml_str:
            # Si no había placeholder TED en el source, insertar antes de TmstFirma
            ted_nuevo = xml_str.replace(
                '<TmstFirma>',
                ted_str_limpio + '<TmstFirma>',
                1
            )

        # 4. Re-parsear el XML con el TED como string insertado
        #    Ahora el Documento tiene el TED en su árbol, lxml lo pone en SII
        #    namespace (herencia del padre) → c14n in-tree coincide con SII ✓
        root_final = etree.fromstring(ted_nuevo.encode('ISO-8859-1'), parser)

        # 5. DigestValue con C14N IN-TREE (el SII también usa in-tree c14n)
        doc_id = f'DTE-{tipo_dte}-{folio}'
        doc_el = root_final.find(f'.//sii:Documento[@ID="{doc_id}"]', ns)
        doc_c14n   = etree.tostring(
            doc_el, method='c14n', exclusive=False, with_comments=False
        )
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        # 6. Construir Signature y firmarla
        sig_el, si_el = self._build_signature(root_final, doc_id, digest_doc)
        self._complete_signature(sig_el, si_el)

        # 7. Serializar DTE firmado final
        xml_out = etree.tostring(root_final, encoding='unicode', xml_declaration=False)
        return xml_out.encode('ISO-8859-1')

    def firmar_en_arbol(self, dte_el: etree._Element, doc_id: str) -> None:
        """
        Re-firma un DTE que ya está insertado en el árbol del EnvioDTE.

        v8.0 FIX: usa c14n IN-TREE (no standalone).
        Cuando el DTE está dentro del EnvioDTE, el c14n in-tree del Documento
        produce los mismos xmlns-artifacts que el SII obtiene al verificar
        (porque el SII también parsea el EnvioDTE y hace c14n in-tree).
        Ambos lados tienen los mismos artifacts → DigestValues coinciden. ✓
        """
        doc_el = dte_el.find(f'{{{SII_NS}}}Documento')

        # DigestValue IN-TREE — igual que el SII al verificar
        doc_c14n   = etree.tostring(
            doc_el, method='c14n', exclusive=False, with_comments=False
        )
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        sig_el, si_el = self._build_signature(dte_el, doc_id, digest_doc)
        self._complete_signature(sig_el, si_el)


# Alias de compatibilidad
FirmadorDTE = FirmaDTE
