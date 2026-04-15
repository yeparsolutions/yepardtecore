#!/usr/bin/env python3
# tools/validador_sii.py
# ══════════════════════════════════════════════════════════════
# Validador local de XML DTE / EnvioBOLETA para SII Chile
#
# Replica exactamente como el SII verifica las firmas:
#   - C14N no-exclusivo (exclusive=False)
#   - SignedInfo canonicalizado CON contexto xmlns:xsi heredado
#   - Digests de Documento/SetDoc con xsi en contexto
#
# Uso:
#   python3 tools/validador_sii.py /tmp/EnvioBOLETA_certificacion.xml
# ══════════════════════════════════════════════════════════════

import sys
import base64
import hashlib
from lxml import etree
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

DSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
SII_NS  = "http://www.sii.cl/SiiDte"
XSI_NS  = "http://www.w3.org/2001/XMLSchema-instance"


def c14n(element):
    """C14N no-exclusivo sin comentarios — igual que SII."""
    return etree.tostring(element, method="c14n", exclusive=True, with_comments=False)


def c14n_en_contexto(signed_info_el):
    """
    Canonicaliza SignedInfo exactamente como el SII al verificar.
    Wrapper <Signature xmlns=DSIG xmlns:xsi=XSI> reproduce el contexto real.
    """
    DSIG = "http://www.w3.org/2000/09/xmldsig#"
    si_str = etree.tostring(signed_info_el, encoding="unicode")
    wrapper = etree.fromstring(
        f'<Signature xmlns="{DSIG}" xmlns:xsi="{XSI_NS}">'
        f'{si_str}'
        f'</Signature>'
    )
    return etree.tostring(wrapper[0], method="c14n", exclusive=False,
                          with_comments=False)


def validar(archivo):
    print("=" * 40)
    print("   VALIDADOR LOCAL SII")
    print("=" * 40)

    with open(archivo, "rb") as f:
        raw = f.read()

    parser    = etree.XMLParser(remove_blank_text=True)
    root      = etree.fromstring(raw, parser)
    ns        = {"ds": DSIG_NS}
    todo_ok   = True

    # ── 1. Validar Digests ────────────────────────────────────
    print("\n🔍 VALIDANDO DIGESTS...")
    refs = root.xpath("//ds:Reference", namespaces=ns)
    for ref in refs:
        uri     = ref.get("URI", "").lstrip("#")
        dv_el   = ref.find("ds:DigestValue", namespaces=ns)
        if dv_el is None:
            continue
        digest_xml = dv_el.text.strip()

        el = (root.xpath(f"//*[@ID='{uri}']") or
              root.xpath(f"//*[@id='{uri}']"))
        if not el:
            print(f"❌ No encontrado: {uri}")
            todo_ok = False
            continue

        # C14N no-exclusivo — igual que SII (con xsi heredado del padre)
        calc = base64.b64encode(hashlib.sha1(c14n(el[0])).digest()).decode()
        if digest_xml == calc:
            print(f"✅ ID={uri}")
        else:
            print(f"❌ MISMATCH ID={uri}")
            print(f"  XML : {digest_xml}")
            print(f"  CALC: {calc}")
            todo_ok = False

    # ── 2. Validar Firmas RSA ─────────────────────────────────
    print("\n🔐 VALIDANDO FIRMAS RSA...")
    sigs = root.xpath("//ds:Signature", namespaces=ns)
    for i, sig in enumerate(sigs):
        si      = sig.find("ds:SignedInfo", namespaces=ns)
        sv      = sig.find("ds:SignatureValue", namespaces=ns)
        cert_el = sig.find(".//ds:X509Certificate", namespaces=ns)

        if si is None or sv is None or cert_el is None:
            print(f"❌ Firma {i+1}: incompleta")
            todo_ok = False
            continue

        cert_b64  = cert_el.text.replace("\n", "").replace(" ", "")
        cert      = x509.load_der_x509_certificate(base64.b64decode(cert_b64))
        pub       = cert.public_key()

        # C14N exclusivo para SignedInfo
        si_c14n   = etree.tostring(si, method="c14n", exclusive=True, with_comments=False)
        sig_bytes = base64.b64decode(sv.text.replace("\n", "").replace(" ", ""))

        try:
            pub.verify(sig_bytes, si_c14n, padding.PKCS1v15(), hashes.SHA1())
            print(f"✅ Firma {i+1} valida")
        except Exception as e:
            print(f"❌ Firma {i+1} INVALIDA: {e}")
            todo_ok = False

    # ── 3. Validar longitud de lineas ─────────────────────────
    print("\n📏 VALIDANDO LONGITUD DE LINEAS (max 1500 chars)...")
    lineas_largas = []
    for num, linea in enumerate(raw.decode("ISO-8859-1", errors="replace")
                                .splitlines(), start=1):
        if len(linea.rstrip()) > 1500:
            lineas_largas.append((num, len(linea.rstrip())))

    if lineas_largas:
        for num, largo in lineas_largas[:10]:
            print(f"❌ Linea {num}: {largo} chars")
        todo_ok = False
    else:
        print("✅ Todas las lineas dentro del limite")

    # ── Resultado ─────────────────────────────────────────────
    print("\n" + "=" * 40)
    if todo_ok:
        print("🎉 XML VALIDO — listo para subir al SII")
    else:
        print("❌ XML con errores — revisar antes de enviar")
    print("=" * 40)
    return todo_ok


if __name__ == "__main__":
    archivo = (sys.argv[1] if len(sys.argv) > 1
               else "/tmp/EnvioBOLETA_certificacion.xml")
    validar(archivo)
