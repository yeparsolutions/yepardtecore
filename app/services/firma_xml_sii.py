"""
firma_xml_sii.py — Firma XMLDSig correcta para SII Chile

El SII verifica el digest del <Documento> usando c14n IN-TREE:
- Sin xmlns explícito en el Documento
- Con whitespace heredado del árbol

Este módulo firma el EnvioDTE completo en Python puro,
sin Java, calculando el digest exactamente como el SII lo verifica.
"""
import hashlib, base64, io, re
from lxml import etree
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.backends import default_backend
import textwrap

NS_SII  = "http://www.sii.cl/SiiDte"
NS_DS   = "http://www.w3.org/2000/09/xmldsig#"
C14N_ALG = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
RSA_SHA1 = "http://www.w3.org/2000/09/xmldsig#rsa-sha1"
SHA1_ALG = "http://www.w3.org/2000/09/xmldsig#sha1"


def _c14n_fragment(full_tree_c14n: bytes, start_tag: bytes, end_tag: bytes) -> bytes:
    """Extrae un fragmento del c14n completo del árbol."""
    pos = full_tree_c14n.find(start_tag)
    if pos < 0:
        raise ValueError(f"Tag no encontrado: {start_tag}")
    end = full_tree_c14n.find(end_tag, pos) + len(end_tag)
    return full_tree_c14n[pos:end]


def _digest_sha1_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha1(data).digest()).decode()


def _rsa_sign(private_key, data: bytes) -> bytes:
    return private_key.sign(data, padding.PKCS1v15(), hashes.SHA1())


def _build_signed_info(ref_uri: str, digest_value: str, with_xsi: bool = False) -> str:
    """Construye el SignedInfo XML para calcular su c14n."""
    if with_xsi:
        si_open = f'<SignedInfo xmlns="{NS_DS}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    else:
        si_open = f'<SignedInfo xmlns="{NS_DS}">'
    
    return (
        f'{si_open}'
        f'<CanonicalizationMethod Algorithm="{C14N_ALG}"></CanonicalizationMethod>'
        f'<SignatureMethod Algorithm="{RSA_SHA1}"></SignatureMethod>'
        f'<Reference URI="#{ref_uri}">'
        f'<Transforms><Transform Algorithm="{C14N_ALG}"></Transform></Transforms>'
        f'<DigestMethod Algorithm="{SHA1_ALG}"></DigestMethod>'
        f'<DigestValue>{digest_value}</DigestValue>'
        f'</Reference>'
        f'</SignedInfo>'
    )


def _build_signature_element(ref_uri: str, digest_value: str,
                               sig_value: bytes, cert_der: bytes,
                               pub_key, with_xsi: bool = False) -> etree._Element:
    """Construye el elemento <Signature> completo."""
    sig_b64 = "\n".join(textwrap.wrap(base64.b64encode(sig_value).decode(), 64))
    cert_b64 = "\n".join(textwrap.wrap(base64.b64encode(cert_der).decode(), 64))
    
    pub_numbers = pub_key.public_numbers()
    mod_b64 = base64.b64encode(
        pub_numbers.n.to_bytes((pub_numbers.n.bit_length() + 7) // 8, 'big')
    ).decode()
    exp_b64 = base64.b64encode(
        pub_numbers.e.to_bytes((pub_numbers.e.bit_length() + 7) // 8, 'big')
    ).decode()
    
    si_xml = _build_signed_info(ref_uri, digest_value, with_xsi)
    
    sig_xml = (
        f'<Signature xmlns="{NS_DS}">'
        f'{si_xml}'
        f'<SignatureValue>{sig_b64}</SignatureValue>'
        f'<KeyInfo>'
        f'<KeyValue><RSAKeyValue>'
        f'<Modulus>{mod_b64}</Modulus>'
        f'<Exponent>{exp_b64}</Exponent>'
        f'</RSAKeyValue></KeyValue>'
        f'<X509Data><X509Certificate>{cert_b64}</X509Certificate></X509Data>'
        f'</KeyInfo>'
        f'</Signature>'
    )
    return etree.fromstring(sig_xml.encode('utf-8'))


def firmar_sobre_completo(sobre_xml: str, p12_bytes: bytes, password: str) -> str:
    """
    Firma todos los DTEs y el SetDTE del sobre EnvioDTE.
    
    El digest se calcula usando c14n in-tree — exactamente como el SII lo verifica.
    
    Args:
        sobre_xml: EnvioDTE XML sin firmas (string ISO-8859-1)
        p12_bytes: Certificado PKCS12
        password: Password del certificado
    
    Returns:
        EnvioDTE firmado (string ISO-8859-1)
    """
    # Cargar certificado
    priv_key, cert, _ = pkcs12.load_key_and_certificates(
        p12_bytes, password.encode() if isinstance(password, str) else password,
        default_backend()
    )
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    pub_key = cert.public_key()
    
    # Parsear XML
    parser = etree.XMLParser(remove_blank_text=False)
    tree = etree.parse(io.BytesIO(sobre_xml.encode('ISO-8859-1')), parser)
    root = tree.getroot()
    
    # ── Paso 1: Firmar cada DTE ──────────────────────────────────────────
    for dte_el in root.findall(f".//{{{NS_SII}}}DTE"):
        doc_el = dte_el.find(f"{{{NS_SII}}}Documento")
        if doc_el is None:
            continue
        doc_id = doc_el.get("ID")
        doc_el.set("ID", doc_id)  # asegurar que el atributo esté
        
        # Calcular c14n in-tree del Documento
        full_buf = io.BytesIO()
        tree.write_c14n(full_buf, exclusive=False, with_comments=False)
        full_c14n = full_buf.getvalue()
        
        search = f'<Documento ID="{doc_id}"'.encode()
        pos = full_c14n.find(search)
        if pos < 0:
            raise ValueError(f"Documento {doc_id} no encontrado en c14n")
        end = full_c14n.find(b'</Documento>', pos) + len(b'</Documento>')
        doc_c14n = full_c14n[pos:end]
        
        digest_value = _digest_sha1_b64(doc_c14n)
        
        # Construir SignedInfo y calcular su c14n
        # Para el DTE interno: SignedInfo sin xmlns:xsi
        si_xml = _build_signed_info(doc_id, digest_value, with_xsi=False)
        si_el = etree.fromstring(si_xml.encode('utf-8'))
        si_buf = io.BytesIO()
        etree.ElementTree(si_el).write(si_buf, method="c14n", exclusive=False, with_comments=False)
        si_c14n = si_buf.getvalue()
        
        # Firmar
        sig_value = _rsa_sign(priv_key, si_c14n)
        
        # Agregar Signature al DTE
        sig_el = _build_signature_element(doc_id, digest_value, sig_value, cert_der, pub_key, with_xsi=False)
        dte_el.append(sig_el)
    
    # ── Paso 2: Firmar SetDTE ────────────────────────────────────────────
    set_el = root.find(f"{{{NS_SII}}}SetDTE")
    if set_el is None:
        raise ValueError("SetDTE no encontrado")
    
    # C14N in-tree del SetDTE
    full_buf2 = io.BytesIO()
    tree.write_c14n(full_buf2, exclusive=False, with_comments=False)
    full_c14n2 = full_buf2.getvalue()
    
    pos = full_c14n2.find(b'<SetDTE ')
    end = full_c14n2.find(b'</SetDTE>') + len(b'</SetDTE>')
    set_c14n = full_c14n2[pos:end]
    
    digest_set = _digest_sha1_b64(set_c14n)
    
    # Para el sobre externo: SignedInfo con xmlns:xsi
    si_set_xml = _build_signed_info("SetDoc", digest_set, with_xsi=True)
    si_set_el = etree.fromstring(si_set_xml.encode('utf-8'))
    si_set_buf = io.BytesIO()
    etree.ElementTree(si_set_el).write(si_set_buf, method="c14n", exclusive=False, with_comments=False)
    si_set_c14n = si_set_buf.getvalue()
    
    sig_set_value = _rsa_sign(priv_key, si_set_c14n)
    sig_set_el = _build_signature_element("SetDoc", digest_set, sig_set_value, cert_der, pub_key, with_xsi=True)
    root.append(sig_set_el)
    
    # Serializar
    result = etree.tostring(root, encoding='ISO-8859-1', xml_declaration=True).decode('ISO-8859-1')
    return result


if __name__ == "__main__":
    # Test
    import sys
    if len(sys.argv) < 3:
        print("Uso: python firma_xml_sii.py <sobre_sin_firmas.xml> <cert.pfx> [password]")
        sys.exit(1)
    
    with open(sys.argv[1], 'r', encoding='ISO-8859-1') as f:
        sobre = f.read()
    with open(sys.argv[2], 'rb') as f:
        p12 = f.read()
    pwd = sys.argv[3] if len(sys.argv) > 3 else ""
    
    resultado = firmar_sobre_completo(sobre, p12, pwd)
    print(resultado[:500])
    print("...")
    print(f"✅ Firmado OK — {len(resultado)} chars")
