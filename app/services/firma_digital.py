# app/services/firma_digital.py
# ══════════════════════════════════════════════════════════════
# Servicio de Firma Digital para DTE Chile - VERSIÓN CORREGIDA
# ══════════════════════════════════════════════════════════════

from cryptography.hazmat.primitives.serialization import pkcs12
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
C14N_ALGORITHM = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"

def _wrap64(s: str) -> str:
    clean = s.replace('\n', '').replace(' ', '')
    return '\n' + '\n'.join(textwrap.wrap(clean, 64)) + '\n'

def _rsa_sign_sha1(private_key, data: bytes) -> bytes:
    digest = hashlib.sha1(data).digest()
    try:
        return private_key.sign_prehash(digest, padding.PKCS1v15())
    except AttributeError:
        return private_key.sign(digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA1()))

class FirmaDigital:
    def __init__(self, p12_bytes: bytes, password: str):
        pwd_bytes = password.encode("utf-8") if isinstance(password, str) else password
        try:
            private_key, certificate, _ = pkcs12.load_key_and_certificates(
                p12_bytes, pwd_bytes, backend=default_backend()
            )
        except Exception as e:
            raise ValueError(f"Certificado inválido: {e}")

        self._private_key  = private_key
        self._certificate  = certificate
        self._cert_der_b64 = b64encode(certificate.public_bytes(serialization.Encoding.DER)).decode()
        pub = certificate.public_key().public_numbers()
        self._rsa_mod = b64encode(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")).decode()
        self._rsa_exp = b64encode(pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big")).decode()

    def firmar_dte(self, xml_bytes: bytes, folio: int, tipo_dte: int, 
                   xml_caf: str, fecha_emision: str, rut_emisor: str, 
                   monto_total: int, it1_nombre: str = "PRODUCTO") -> bytes:
        
        # 1. Generar el TED como string
        ted_xml_str = self._generar_ted(folio, tipo_dte, xml_caf, fecha_emision, 
                                        rut_emisor, monto_total, it1_nombre).decode("ISO-8859-1")

        # 2. Normalizar el XML base a string
        xml_str = xml_bytes.decode("ISO-8859-1") if isinstance(xml_bytes, bytes) else xml_bytes

        # CORRECCIÓN TAG MISMATCH: Usamos un reemplazo literal más seguro
        # Esto asegura que <TED/> sea reemplazado por el bloque completo sin romper el árbol
        if "<TED/>" in xml_str:
            xml_con_ted = xml_str.replace("<TED/>", ted_xml_str)
        else:
            # Si el placeholder tiene espacios o es largo
            xml_con_ted = re.sub(r'<TED\s*/>', ted_xml_str, xml_str)
            if xml_con_ted == xml_str:
                xml_con_ted = re.sub(r'<TED>.*?</TED>', ted_xml_str, xml_str, flags=re.DOTALL)

        # 3. Firmar el XML (Documento)
        xml_firmado = self._firmar_xml(xml_con_ted, f"DTE-{tipo_dte}-{folio}")
        return xml_firmado.encode("ISO-8859-1")

    def _generar_ted(self, folio: int, tipo_dte: int, xml_caf: str, 
                      fecha_emision: str, rut_emisor: str, monto_total: int, 
                      it1_nombre: str = "PRODUCTO") -> bytes:
        
        caf_root = etree.fromstring(xml_caf.encode())
        rsk_el   = caf_root.find(".//RSASK")
        # Extraemos el CAF sin namespaces para evitar herencia
        caf_el = caf_root.find(".//CAF")
        caf_str = etree.tostring(caf_el, encoding="unicode").replace(' xmlns="http://www.sii.cl/SiiDte"', '')

        # Limpieza rigurosa de caracteres especiales
        it1_safe = it1_nombre[:40].replace('&', ' y ').replace("'", "").replace('"', "").replace('#', "").strip()

        tsted = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        
        # Construcción manual del DD (Datos del Documento) para asegurar que NO tenga namespaces
        dd_xml = (
            f"<DD>"
            f"<RE>{rut_emisor}</RE><TD>{tipo_dte}</TD><F>{folio}</F>"
            f"<FE>{fecha_emision}</FE><RR>66666666-6</RR>"
            f"<RSR>CONSUMIDOR FINAL</RSR><MNT>{monto_total}</MNT>"
            f"<IT1>{it1_safe}</IT1>{caf_str}"
            f"<TSTED>{tsted}</TSTED>"
            f"</DD>"
        )

        firma_b64 = b64encode(
            self._firmar_rsa_sha1_raw(dd_xml.encode("ISO-8859-1"), rsk_el.text.strip())
        ).decode()

        # Retornamos el TED con xmlns="" para resetear el namespace heredado
        return (
            f'<TED version="1.0" xmlns="">{dd_xml}'
            f'<FRMT algoritmo="SHA1withRSA">{firma_b64}</FRMT>'
            f'</TED>'
        ).encode("ISO-8859-1")

    def _firmar_xml(self, xml_str: str, doc_id: str) -> str:
        parser = etree.XMLParser(remove_blank_text=True, recover=True)
        root = etree.fromstring(xml_str.encode("ISO-8859-1"), parser)
        ns = {"sii": SII_NS}

        doc_el = root.find(f".//sii:Documento[@ID='{doc_id}']", ns)
        if doc_el is None: raise ValueError(f"No se encontró el Documento ID {doc_id}")

        # Canonicalización del Documento
        doc_c14n = etree.tostring(doc_el, method="c14n", exclusive=False)
        digest_doc = b64encode(hashlib.sha1(doc_c14n).digest()).decode()

        signed_info_xml = self._build_signed_info(f"#{doc_id}", digest_doc)
        
        # Firma del SignedInfo en contexto
        temp_sig = etree.fromstring(f'<Signature xmlns="{XMLDSIG_NS}">{signed_info_xml}</Signature>')
        root.append(temp_sig)
        si_en_doc = temp_sig.find(f"{{{XMLDSIG_NS}}}SignedInfo")
        si_c14n = etree.tostring(si_en_doc, method="c14n", exclusive=False)
        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()
        root.remove(temp_sig)

        # Agregar Signature final
        root.append(etree.fromstring(self._build_signature(signed_info_xml, firma_b64)))
        return etree.tostring(root, encoding="unicode")

    def _firmar_rsa_sha1_raw(self, data: bytes, pem_key_str: str) -> bytes:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        if "-----" not in pem_key_str:
            pem_key_str = f"-----BEGIN RSA PRIVATE KEY-----\n{pem_key_str}\n-----END RSA PRIVATE KEY-----"
        pk = load_pem_private_key(pem_key_str.encode(), password=None, backend=default_backend())
        return _rsa_sign_sha1(pk, data)

    def _build_signed_info(self, reference_uri: str, digest_value: str) -> str:
        return (
            f'<SignedInfo xmlns="{XMLDSIG_NS}">'
            f'<CanonicalizationMethod Algorithm="{C14N_ALGORITHM}"/>'
            f'<SignatureMethod Algorithm="{XMLDSIG_NS}rsa-sha1"/>'
            f'<Reference URI="{reference_uri}">'
            f'<Transforms><Transform Algorithm="{C14N_ALGORITHM}"/></Transforms>'
            f'<DigestMethod Algorithm="{XMLDSIG_NS}sha1"/>'
            f'<DigestValue>{digest_value}</DigestValue>'
            f'</Reference></SignedInfo>'
        )

    def _build_signature(self, signed_info: str, signature_value: str) -> str:
        return (
            f'<Signature xmlns="{XMLDSIG_NS}">'
            f'{signed_info}'
            f'<SignatureValue>{signature_value}</SignatureValue>'
            f'<KeyInfo><KeyValue><RSAKeyValue>'
            f'<Modulus>{_wrap64(self._rsa_mod)}</Modulus>'
            f'<Exponent>{self._rsa_exp}</Exponent>'
            f'</RSAKeyValue></KeyValue>'
            f'<X509Data><X509Certificate>{_wrap64(self._cert_der_b64)}</X509Certificate></X509Data>'
            f'</KeyInfo></Signature>'
        )

    def firmar_sobre(self, sobre_xml: str) -> str:
        parser = etree.XMLParser(remove_blank_text=True)
        root = etree.fromstring(sobre_xml.encode("ISO-8859-1"), parser)
        ns = {"sii": SII_NS}

        set_el = root.find(".//sii:SetDTE[@ID='SetDoc']", ns)
        set_c14n = etree.tostring(set_el, method="c14n", exclusive=False)
        digest_val = b64encode(hashlib.sha1(set_c14n).digest()).decode()

        signed_info = self._build_signed_info("#SetDoc", digest_val)

        temp_sig = etree.fromstring(f'<Signature xmlns="{XMLDSIG_NS}">{signed_info}</Signature>')
        root.append(temp_sig)
        si_en_doc = temp_sig.find(f"{{{XMLDSIG_NS}}}SignedInfo")
        si_c14n = etree.tostring(si_en_doc, method="c14n", exclusive=False)
        firma_b64 = b64encode(_rsa_sign_sha1(self._private_key, si_c14n)).decode()
        root.remove(temp_sig)

        root.append(etree.fromstring(self._build_signature(signed_info, firma_b64)))
        return '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + etree.tostring(root, encoding="unicode")
