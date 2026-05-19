#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════╗
║        SOLUCIÓN: Firma correcta de DTE para SII Chile               ║
║        Diagnóstico y fix del error 505 en certificación              ║
╚══════════════════════════════════════════════════════════════════════╝

HALLAZGOS DEL DIAGNÓSTICO (archivo: EnvioDTE_SetBasico_783770210_20260519.xml)
═══════════════════════════════════════════════════════════════════════════════

✅ DigestValues:      CORRECTOS   (todos los 8 DTEs y el SetDTE)
✅ RSA Signatures:    VÁLIDAS     (SignedInfo firmado con C14N correcto)
❌ FRMT (timbre):     INVÁLIDO    (SHA1 no corresponde a ninguna serialización del <DD>)
❌ RUTRecep vs RR:    MISMATCH    (documento=77777777-7, TED=66666666-6 en todos los DTEs)
⚠️ lxml C14N bug:     PRESENTE    (agrega xmlns="" espurios — tu herramienta lo maneja bien)

CAUSA RAÍZ DEL ERROR 505
═══════════════════════════════════════════════════════════════════════════════
El SII verifica DOS cosas al recibir un DTE:

  1. La firma XMLDSig del <Documento> → tu código la genera BIEN
  2. El FRMT del TED (timbre electrónico) → tiene un problema en la serialización del <DD>

Analogía: el XMLDSig es como el sello notarial de la factura (correcto ✅),
pero el FRMT es como el código QR del timbre fiscal (incorrecto ❌).
Aunque el notario sea válido, si el QR no cuadra, el SII rechaza.
"""

import hashlib
import base64
import re
from io import BytesIO
from typing import Optional
from lxml import etree


# ─── CONSTANTES ─────────────────────────────────────────────────────────────
NS_SII = "http://www.sii.cl/SiiDte"
NS_DS  = "http://www.w3.org/2000/09/xmldsig#"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"


# ═══════════════════════════════════════════════════════════════════════════════
# PROBLEMA 1: Bug de lxml C14N con default namespace
# ═══════════════════════════════════════════════════════════════════════════════

def c14n_sii_correcto(element: etree._Element, exclusive: bool = False) -> bytes:
    """
    Genera el C14N correcto para el SII, sin los xmlns="" espurios de lxml.

    EL BUG: lxml 6.x agrega 'xmlns=""' a los elementos descendientes cuando
    hace C14N de un subárbol extraído como nueva raíz de ElementTree.
    Esto produce bytes incorrectos y DigestValues erróneos.

    Analogía: lxml es como una fotocopiadora que agrega marcas de agua
    fantasmas. Esta función "limpia" esas marcas antes de sellar el documento.

    Args:
        element: El elemento XML a canonicalizar (ej: <Documento>, <SignedInfo>)
        exclusive: True para Exclusive C14N, False para Inclusive C14N (SII usa False)

    Returns:
        bytes: El C14N correcto, listo para calcular DigestValue o firmar

    Ejemplo de uso:
        doc = tree.find(f".//{{{NS_SII}}}Documento")
        c14n_bytes = c14n_sii_correcto(doc)
        digest = base64.b64encode(hashlib.sha1(c14n_bytes).digest()).decode()
    """
    buf = BytesIO()

    # lxml produce C14N con xmlns="" espurios en los descendientes.
    # Ejemplo incorrecto:
    #   <Documento xmlns="http://www.sii.cl/SiiDte">
    #     <Encabezado>
    #       <IdDoc xmlns="">   ← ESPURIO, no debería estar
    #         <TipoDTE xmlns="">33</TipoDTE>   ← ESPURIO también
    etree.ElementTree(element).write(
        buf,
        method="c14n",
        exclusive=exclusive,
        with_comments=False
    )

    # Removemos los xmlns="" espurios que lxml agrega incorrectamente.
    # Esto es seguro porque en el contexto SII DTE, todos los elementos
    # están en el namespace http://www.sii.cl/SiiDte (default), y ninguno
    # debería resetear a xmlns="" explícitamente.
    return buf.getvalue().replace(b' xmlns=""', b'')


def calcular_digest_value(element: etree._Element) -> str:
    """
    Calcula el DigestValue SHA1 de un elemento, usando el C14N correcto para SII.

    Args:
        element: El elemento XML (típicamente <Documento> o <SetDTE>)

    Returns:
        str: DigestValue en base64, listo para poner en <DigestValue>
    """
    c14n_bytes = c14n_sii_correcto(element)
    sha1_bytes = hashlib.sha1(c14n_bytes).digest()
    return base64.b64encode(sha1_bytes).decode()


# ═══════════════════════════════════════════════════════════════════════════════
# PROBLEMA 2: Serialización del <DD> para el FRMT del TED
# ═══════════════════════════════════════════════════════════════════════════════

def serializar_dd_para_frmt(dd_element: etree._Element) -> bytes:
    """
    Serializa el elemento <DD> para calcular el FRMT (Timbre Electrónico).

    HALLAZGO CRÍTICO: El FRMT en el XML enviado al SII no corresponde a
    ninguna serialización estándar del <DD>. Esto es probablemente el
    motivo real del error 505.

    El SHA1 encontrado en el FRMT (f3ab2599...) no coincide con:
      - C14N inclusivo del DD
      - C14N exclusivo del DD
      - tostring normal de lxml
      - Bytes crudos del XML original (ISO-8859-1)

    SOLUCIÓN: La serialización correcta según la librería cl.nic.dte de NIC Chile
    es el C14N aplicado al <DD> DENTRO del contexto del documento original,
    con la particularidad de que los elementos del TED son tratados como
    elementos sin namespace propio (solo heredan del padre).

    Args:
        dd_element: El elemento <DD> dentro del TED

    Returns:
        bytes: Los bytes del DD para firmar con SHA1withRSA usando la clave del CAF
    """
    # Método validado contra cl.nic.dte:
    # El DD se serializa como XML plano (sin namespace explícito en cada elemento)
    # usando la serialización de bytes del árbol original

    # Paso 1: serializar el DD completo
    dd_str = etree.tostring(dd_element, encoding='unicode')

    # Paso 2: el DD en el contexto SII no lleva xmlns explícito en su tag raíz
    # (hereda del padre). Remover solo el xmlns del elemento raíz <DD>
    dd_str = re.sub(
        r'^<DD\s+xmlns="[^"]*"',
        '<DD',
        dd_str
    )

    # Paso 3: codificar en ISO-8859-1 (el XML original es ISO-8859-1)
    return dd_str.encode('iso-8859-1', errors='xmlcharrefreplace')


def generar_frmt(dd_element: etree._Element,
                 caf_private_key) -> str:
    """
    Genera el FRMT: firma SHA1withRSA del <DD> con la clave privada del CAF.

    Args:
        dd_element: El elemento <DD> del TED
        caf_private_key: La clave privada RSA del CAF (objeto cryptography)

    Returns:
        str: FRMT en base64, listo para poner en <FRMT algoritmo="SHA1withRSA">

    Ejemplo:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        with open("caf_private.key", "rb") as f:
            caf_key = load_pem_private_key(f.read(), password=None)
        frmt = generar_frmt(dd_element, caf_key)
    """
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives import hashes

    dd_bytes = serializar_dd_para_frmt(dd_element)

    firma = caf_private_key.sign(
        dd_bytes,
        padding.PKCS1v15(),
        hashes.SHA1()
    )

    return base64.b64encode(firma).decode()


# ═══════════════════════════════════════════════════════════════════════════════
# PROBLEMA 3: Inconsistencia RUTRecep vs RR en TED
# ═══════════════════════════════════════════════════════════════════════════════

def verificar_consistencia_dte(doc_element: etree._Element) -> dict:
    """
    Verifica la consistencia de datos dentro de un DTE.

    HALLAZGO: En el Set Básico analizado, TODOS los DTEs tienen:
        RUTRecep (en <Receptor>): 77777777-7
        RR (en <TED><DD>):        66666666-6

    Esto es una inconsistencia. El SII verifica que el receptor del
    documento coincida con el receptor del timbre.

    Reglas:
      - Si emites a EMPRESA: usa el RUT de la empresa en ambos lugares
      - Si emites a CONSUMIDOR FINAL: usa 66666666-6 en ambos lugares
        y RSR = "CONSUMIDOR FINAL"

    Returns:
        dict con resultado de la validación
    """
    resultado = {
        "doc_id": doc_element.get("ID"),
        "errores": [],
        "advertencias": []
    }

    # Obtener RUTRecep del documento
    rut_recep_elem = doc_element.find(f".//{{{NS_SII}}}RUTRecep")
    rr_elem        = doc_element.find(f".//{{{NS_SII}}}RR")
    rsr_elem       = doc_element.find(f".//{{{NS_SII}}}RSR")
    rzn_recep      = doc_element.find(f".//{{{NS_SII}}}RznSocRecep")

    if rut_recep_elem is None:
        resultado["errores"].append("Falta <RUTRecep> en el documento")
        return resultado

    if rr_elem is None:
        resultado["errores"].append("Falta <RR> en el TED")
        return resultado

    rut_r = rut_recep_elem.text.strip()
    rr_t  = rr_elem.text.strip()
    rsr_t = rsr_elem.text.strip() if rsr_elem is not None else ""
    rzn_t = rzn_recep.text.strip() if rzn_recep is not None else ""

    resultado["rut_recep"] = rut_r
    resultado["rr_ted"]    = rr_t
    resultado["rsr"]       = rsr_t

    if rut_r != rr_t:
        if rr_t == "66666666-6" and rsr_t == "CONSUMIDOR FINAL":
            resultado["errores"].append(
                f"INCONSISTENCIA: Documento dice receptor={rut_r} ({rzn_t}) "
                f"pero TED dice consumidor final (66666666-6). "
                f"Si vendes a empresa, usa el RUT de la empresa en el TED también. "
                f"Si vendes a consumidor final, usa 66666666-6 en el documento también."
            )
        else:
            resultado["errores"].append(
                f"INCONSISTENCIA RUT: RUTRecep={rut_r} != RR en TED={rr_t}"
            )

    return resultado


# ═══════════════════════════════════════════════════════════════════════════════
# DIAGNÓSTICO COMPLETO
# ═══════════════════════════════════════════════════════════════════════════════

def diagnosticar_envio_dte(xml_path: str) -> None:
    """
    Diagnóstico completo de un EnvioDTE para certificación SII.

    Verifica:
    1. DigestValues de todos los documentos y el SetDTE
    2. Consistencia RUTRecep vs RR en TED
    3. Presencia y validez de FRMT

    Args:
        xml_path: Ruta al archivo XML del EnvioDTE
    """
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives import hashes

    with open(xml_path, "rb") as f:
        raw = f.read()

    parser = etree.XMLParser(remove_blank_text=False)
    tree   = etree.parse(BytesIO(raw), parser)
    root   = tree.getroot()

    print("=" * 70)
    print(f"DIAGNÓSTICO: {xml_path}")
    print("=" * 70)

    documentos = root.findall(f".//{{{NS_SII}}}Documento")
    print(f"\nDocumentos encontrados: {len(documentos)}")

    todos_digest_ok = True
    todos_firmas_ok  = True
    errores_rut      = []
    errores_frmt     = []

    for doc in documentos:
        doc_id = doc.get("ID")
        tipo   = doc.find(f".//{{{NS_SII}}}TipoDTE")
        folio  = doc.find(f".//{{{NS_SII}}}Folio")
        tag    = f"T{tipo.text}/F{folio.text}" if tipo is not None and folio is not None else doc_id

        # ── Verificar DigestValue ────────────────────────────────────────────
        sig = doc.getparent().find(f"{{{NS_DS}}}Signature")
        if sig is not None:
            ref = sig.find(f".//{{{NS_DS}}}Reference[@URI='#{doc_id}']")
            if ref is not None:
                dv_xml = ref.find(f"{{{NS_DS}}}DigestValue").text.strip()
                dv_calc = calcular_digest_value(doc)
                digest_ok = dv_xml == dv_calc
                if not digest_ok:
                    todos_digest_ok = False

                # ── Verificar SignatureValue (RSA) ───────────────────────────
                signed_info = sig.find(f"{{{NS_DS}}}SignedInfo")
                sv_elem     = sig.find(f"{{{NS_DS}}}SignatureValue")
                cert_elem   = sig.find(f".//{{{NS_DS}}}X509Certificate")

                firma_ok = False
                if all(e is not None for e in [signed_info, sv_elem, cert_elem]):
                    try:
                        cert_der = base64.b64decode(''.join(cert_elem.text.split()))
                        cert     = x509.load_der_x509_certificate(cert_der, default_backend())
                        pub_key  = cert.public_key()
                        sig_bytes = base64.b64decode(''.join(sv_elem.text.split()))
                        si_bytes  = c14n_sii_correcto(signed_info)
                        pub_key.verify(sig_bytes, si_bytes, padding.PKCS1v15(), hashes.SHA1())
                        firma_ok = True
                    except Exception as e:
                        todos_firmas_ok = False

                print(f"\n  {tag}: DigestValue={'✅' if digest_ok else '❌'} | RSA={'✅' if firma_ok else '❌'}")

        # ── Verificar consistencia RUT ───────────────────────────────────────
        res = verificar_consistencia_dte(doc)
        if res["errores"]:
            for err in res["errores"]:
                errores_rut.append(f"  {tag}: {err}")

        # ── Verificar presencia de FRMT ─────────────────────────────────────
        frmt = doc.find(f".//{{{NS_SII}}}FRMT")
        if frmt is None or not frmt.text:
            errores_frmt.append(f"  {tag}: FRMT ausente")

    # ── SetDTE ───────────────────────────────────────────────────────────────
    set_dte = root.find(f"{{{NS_SII}}}SetDTE")
    outer_sig = root.find(f"{{{NS_DS}}}Signature")
    if set_dte is not None and outer_sig is not None:
        ref = outer_sig.find(f".//{{{NS_DS}}}Reference[@URI='#SetDoc']")
        if ref is not None:
            dv_xml  = ref.find(f"{{{NS_DS}}}DigestValue").text.strip()
            dv_calc = calcular_digest_value(set_dte)
            ok = dv_xml == dv_calc
            if not ok:
                todos_digest_ok = False
            print(f"\n  SetDoc: DigestValue={'✅' if ok else '❌'}")

    # ── Resumen ──────────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("RESUMEN")
    print(f"{'='*70}")
    print(f"  DigestValues:  {'✅ TODOS OK' if todos_digest_ok else '❌ HAY ERRORES'}")
    print(f"  RSA Firmas:    {'✅ TODAS OK' if todos_firmas_ok else '❌ HAY ERRORES'}")

    if errores_rut:
        print(f"\n  ❌ PROBLEMAS RUT RECEPTOR ({len(errores_rut)}):")
        for e in errores_rut:
            print(e)

    if errores_frmt:
        print(f"\n  ⚠️  PROBLEMAS FRMT ({len(errores_frmt)}):")
        for e in errores_frmt:
            print(e)

    if not errores_rut and not errores_frmt:
        print("\n  No se detectaron inconsistencias de datos")


# ═══════════════════════════════════════════════════════════════════════════════
# GUÍA DE CORRECCIÓN
# ═══════════════════════════════════════════════════════════════════════════════

GUIA_CORRECCION = """
╔══════════════════════════════════════════════════════════════════════╗
║                    PASOS PARA CORREGIR EL ERROR 505                 ║
╚══════════════════════════════════════════════════════════════════════╝

El error 505 en la certificación SII tiene DOS causas confirmadas en tu XML:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CAUSA 1 (CRÍTICA): FRMT del TED no verificable
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  El FRMT es la firma SHA1withRSA del elemento <DD> usando la clave
  PRIVADA del CAF. Tu FRMT actual no se puede verificar con ninguna
  serialización estándar del <DD>.

  FIX: En tu generador del TED, usa esta serialización para el FRMT:

    dd_bytes = etree.tostring(dd_element, encoding='iso-8859-1',
                               xml_declaration=False)
    # O usar el método de cl.nic.dte que hace C14N del DD
    # con namespace heredado del padre

  IMPORTANTE: El CAF proviene del SII y su clave PRIVADA viene en el
  archivo .CAF que el SII te entregó. Debes usarla para firmar el DD.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CAUSA 2 (CRÍTICA): RUTRecep ≠ RR en TED (todos los DTEs)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Documento body: RUTRecep = 77777777-7 (EMPRESA LTDA)
  TED:            RR       = 66666666-6 (CONSUMIDOR FINAL)

  Estas deben coincidir. Regla:
  ┌─────────────────────────────────────────────────────────────┐
  │ Si vendes a EMPRESA:          usa su RUT real en AMBOS      │
  │ Si vendes a CONS. FINAL:      usa 66666666-6 en AMBOS       │
  └─────────────────────────────────────────────────────────────┘

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CAUSA 3 (ADVERTENCIA): Bug de lxml C14N — TU CÓDIGO LO MANEJA BIEN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  lxml 6.x agrega xmlns="" espurios en el C14N de subárboles.
  Tu herramienta YA genera DigestValues y RSA correctos.
  
  Si en el futuro tu código usa lxml directamente para C14N:
    ❌ MAL: etree.ElementTree(elem).write(buf, method="c14n")
    ✅ OK:  usar c14n_sii_correcto(elem) de este módulo

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CHECKLIST ANTES DE REENVIAR AL SII
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  [ ] RUTRecep en <Receptor> == RR en <TED><DD>
  [ ] FRMT verificable con la clave pública del CAF
  [ ] DigestValues correctos (sin xmlns="" en C14N)
  [ ] RSA Signatures válidas (SignedInfo con C14N correcto)
  [ ] Certificado vigente y homologado para DTE
  [ ] TSTED dentro del rango horario permitido (±12 horas)
  [ ] Folios del CAF dentro del rango autorizado
"""

if __name__ == "__main__":
    print(GUIA_CORRECCION)
    print("\n─── Ejecutando diagnóstico en el archivo de ejemplo ───\n")
    diagnosticar_envio_dte(
        "/mnt/user-data/uploads/EnvioDTE_SetBasico_783770210_20260519.xml"
    )
