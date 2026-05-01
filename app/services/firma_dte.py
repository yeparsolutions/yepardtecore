# app/services/firma_dte.py
# ══════════════════════════════════════════════════════════════
# Firma individual de DTEs para SII Chile
#
# ENFOQUE: Usa python-xmlsec (libxmlsec1) para la firma XMLDSig,
# exactamente igual a como .NET usa SignedXml.ComputeSignature().
# La firma del DTE se realiza DENTRO del árbol del EnvioDTE para
# que libxmlsec1 compute el c14n del Documento en el mismo contexto
# de namespace que usará el SII al verificar.
#
# FRMT (timbre TED): firmado con la clave privada del CAF (RSA-SHA1
# sobre los bytes ISO-8859-1 del nodo <DD>).
# ══════════════════════════════════════════════════════════════

from cryptography.hazmat.primitives.serialization import pkcs12, load_pem_private_key
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, utils
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.serialization import Encoding as CryptoEncoding
from cryptography import x509 as cx509
from lxml import etree
from base64 import b64encode, b64decode
from datetime import datetime, timezone
import hashlib
import xmlsec
import textwrap

XMLDSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
SII_NS     = "http://www.sii.cl/SiiDte"
XSI_NS     = "http://www.w3.org/2001/XMLSchema-instance"


def _rsa_sign_sha1(private_key, data: bytes) -> bytes:
    """Firma RSA+SHA1 (para el FRMT del TED)."""
    digest = hashlib.sha1(data).digest()
    try:
        return private_key.sign_prehash(digest, padding.PKCS1v15())
    except AttributeError:
        return private_key.sign(
            digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA1())
        )


class FirmadorDTE:
    """
    Firma el nodo <Documento> de un DTE para SII Chile.

    Analogía: es la llave de la firma electrónica.
    El sobre (EnvioDTE) es el sobre físico que la contiene.
    Esta clase solo firma el documento adentro del sobre.
    """

    def __init__(self, p12_bytes: bytes, p12_password):
        # Convertir password a bytes si llega como str
        pwd = p12_password.encode('utf-8') if isinstance(p12_password, str) else p12_password
        self._private_key, self._cert, _ = pkcs12.load_key_and_certificates(
            p12_bytes, pwd, backend=default_backend()
        )
        # PEM de la clave privada (para xmlsec)
        self._pem_private = self._private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()
        )
        # PEM del certificado (para xmlsec)
        self._pem_cert = self._cert.public_bytes(CryptoEncoding.PEM)
        # Atributos de compatibilidad usados por firma_digital.py y firma_sobre.py
        self._cert_der_b64 = b64encode(
            self._cert.public_bytes(CryptoEncoding.DER)
        ).decode()
        pub = self._cert.public_key().public_numbers()
        self._rsa_mod = b64encode(
            pub.n.to_bytes((pub.n.bit_length() + 7) // 8, 'big')
        ).decode()
        self._rsa_exp = b64encode(
            pub.e.to_bytes((pub.e.bit_length() + 7) // 8, 'big')
        ).decode()

    @property
    def rut_certificado(self) -> str:
        """RUT extraído del subject del certificado X.509."""
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

        FIX CRÍTICO — xmlns="" en el elemento raíz <TED>:
        El DTE se construye con xmlns="SiiDte". Cuando lxml serializa un
        elemento sin-namespace dentro de un árbol con namespace heredado,
        NO agrega xmlns="" automáticamente. En el round-trip posterior
        (construir_sobre re-parsea el DTE), lxml lee <TED> sin xmlns="" y
        lo asigna al namespace SiiDte del padre → el DD en el XML final
        queda como {SiiDte}DD. El FRMT fue firmado sobre <DD> sin namespace
        → mismatch → DTE-3-505.
        Solución: incluir xmlns="" explícitamente en el TED.
        """
        # Parsear CAF sin blanks: el CAF del SII viene con saltos de línea.
        # Si no se eliminan, SHA1(dd_xml firmado) ≠ SHA1(DD en archivo) → FRMT inválido.
        caf_parser = etree.XMLParser(remove_blank_text=True)
        caf_root   = etree.fromstring(xml_caf.encode(), caf_parser)
        rsk_el     = caf_root.find('.//RSASK')
        caf_str    = etree.tostring(caf_root.find('.//CAF'), encoding='unicode')

        # Sanear el texto del primer ítem para evitar caracteres inválidos en XML
        it1_safe = (
            it1_nombre[:40]
            .replace('&', ' y ').replace("'", '').replace('"', '')
            .replace('#', '').replace('<', '&lt;').replace('>', '&gt;')
        ).strip()

        tsted  = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
        # El <DD> se firma con la clave RSA del CAF (FRMT).
        # Los bytes a firmar son los bytes ISO-8859-1 del <DD> SIN namespace.
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

        # xmlns="" en <TED> es obligatorio para cancelar el xmlns="SiiDte"
        # heredado del <DTE> padre y evitar que lxml cambie el namespace
        # de TED/DD en sucesivos round-trips.
        return (
            f'<TED xmlns="" version="1.0">{dd_xml}'
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

    # ── XMLDSig usando python-xmlsec ─────────────────────────

    def firmar_dte_en_sobre(self, dte_el: etree._Element) -> None:
        """
        Firma el <Documento> de un DTE que ya está insertado en el EnvioDTE.

        REGLA CRÍTICA (análoga a PreserveWhiteSpace=true en .NET):
        La firma debe hacerse MIENTRAS el DTE está en el árbol del EnvioDTE.
        libxmlsec1 computa el c14n del <Documento> en ese contexto de namespace,
        que es el mismo contexto que usará el SII al verificar.
        Si se firma el DTE de forma standalone, el DigestValue cambia cuando
        se inserta en el EnvioDTE → DTE-3-505.

        Equivale exactamente a:
            signedXml = new SignedXml(xmldocument)  // .NET
            signedXml.ComputeSignature()
        """
        tipo  = dte_el.findtext(f'.//{{{SII_NS}}}TipoDTE')
        folio = dte_el.findtext(f'.//{{{SII_NS}}}Folio')
        doc_id = f'DTE-{tipo}-{folio}'

        # Registrar el ID del Documento para que xmlsec resuelva la referencia
        xmlsec.tree.add_ids(dte_el, ['ID'])

        # Construir la plantilla de Signature (vacía — xmlsec la llenará)
        NS = XMLDSIG_NS
        sig_node = xmlsec.template.create(
            dte_el,
            c14n_method=xmlsec.constants.TransformInclC14N,
            sign_method=xmlsec.constants.TransformRsaSha1
        )
        # Referencia al Documento por su ID
        ref = xmlsec.template.add_reference(
            sig_node,
            digest_method=xmlsec.constants.TransformSha1,
            uri=f'#{doc_id}'
        )
        # Transformación C14N 1.0 (requerida por el SII)
        xmlsec.template.add_transform(ref, xmlsec.constants.TransformInclC14N)

        # KeyInfo con el certificado X.509
        key_info = xmlsec.template.ensure_key_info(sig_node)
        x509_data = etree.SubElement(key_info, f'{{{NS}}}X509Data')
        x509_cert = etree.SubElement(x509_data, f'{{{NS}}}X509Certificate')
        cert_der_b64 = b64encode(self._cert.public_bytes(CryptoEncoding.DER)).decode()
        x509_cert.text = (
            '\n' + '\n'.join(textwrap.wrap(cert_der_b64, 64)) + '\n'
        )

        # Insertar la Signature en el DTE (al final del elemento DTE)
        dte_el.append(sig_node)

        # Firmar: libxmlsec1 computa DigestValue y SignatureValue automáticamente
        key = xmlsec.Key.from_memory(self._pem_private, xmlsec.KeyFormat.PEM)
        key.load_cert_from_memory(self._pem_cert, xmlsec.KeyFormat.CERT_PEM)
        ctx = xmlsec.SignatureContext()
        ctx.key = key
        ctx.sign(sig_node)

    # ── API principal (compatibilidad hacia atrás) ────────────

    def firmar(self, xml_bytes: bytes, folio: int, tipo_dte: int,
               xml_caf: str, fecha_emision: str, rut_emisor: str,
               monto_total: int, it1_nombre: str = 'PRODUCTO') -> bytes:
        """
        Inserta el TED y firma el <Documento> del DTE.

        NOTA: Este método firma el DTE de forma STANDALONE para compatibilidad
        con código que llama a firmar() directamente. El DigestValue correcto
        se obtendrá solo si el DTE se firma dentro del EnvioDTE via
        firmar_dte_en_sobre(). Para el flujo completo, usar firmar_dte_en_sobre().
        """
        parser = etree.XMLParser(remove_blank_text=True)
        root   = etree.fromstring(xml_bytes, parser)
        ns     = {'sii': SII_NS}

        # Insertar TED (con xmlns="" para evitar herencia de namespace SiiDte)
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

        # Actualizar TmstFirma con el timestamp actual
        tmst_el = root.find('.//sii:TmstFirma', ns)
        if tmst_el is not None:
            tmst_el.text = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

        # Firmar el DTE standalone (para compatibilidad)
        xmlsec.tree.add_ids(root, ['ID'])
        self.firmar_dte_en_sobre(root)

        xml_str = etree.tostring(root, encoding='unicode', xml_declaration=False)
        return xml_str.encode('ISO-8859-1')

# Alias de compatibilidad — firma_digital.py importa FirmaDTE
FirmaDTE = FirmadorDTE
