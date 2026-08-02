# -*- coding: utf-8 -*-
"""
Intercambio de información (certificación SII, paso posterior a la Simulación).

El SII envía al postulante un EnvioDTE (documentos "recibidos" en su casilla) y
el contribuyente debe responder con TRES XML firmados con su certificado:

  1. Acuse de Recibo del Envío  → <RespuestaDTE> con <RecepcionEnvio>
  2. Recibo de Mercaderías      → <EnvioRecibos> (cada Recibo firmado + el set firmado)
  3. Resultado Aprobación Com.  → <RespuestaDTE> con <ResultadoDTE>

Todo se firma con el mismo certificado del emisor (XMLDSig, enveloped, RSA-SHA1),
reutilizando el patrón del firmador de libros.
"""
from __future__ import annotations
import base64
import hashlib
from datetime import datetime

from lxml import etree
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12, Encoding

NS_SII = "http://www.sii.cl/SiiDte"
NS_DS  = "http://www.w3.org/2000/09/xmldsig#"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"

DECLARACION_LEY_19983 = (
    "El acuse de recibo que se declara en este acto, de acuerdo a lo "
    "dispuesto en la letra b) del Art. 4, y la letra c) del Art. 5 de la Ley "
    "19.983, acredita que la entrega de mercaderias o servicio(s) prestado(s) "
    "ha(n) sido recibido(s)."
)


def _norm_rut(r: str) -> str:
    """RUT en formato SII: sin puntos, con guión, DV en mayúscula. El SII exige
    el patrón [0-9]+-([0-9]|K) — cualquier punto lo rechaza."""
    if not r:
        return r
    return r.replace(".", "").replace(" ", "").upper()


# ─── Firma genérica (enveloped, RSA-SHA1) ────────────────────────────────────
def _cargar_cert(p12_bytes: bytes, password: str):
    priv, cert, _ = pkcs12.load_key_and_certificates(
        p12_bytes, password.encode() if password else None)
    cert_b64 = base64.b64encode(cert.public_bytes(Encoding.DER)).decode()
    return priv, cert, cert_b64


def _keyinfo(sig_el, cert, cert_b64):
    ki = etree.SubElement(sig_el, f"{{{NS_DS}}}KeyInfo")
    nums = cert.public_key().public_numbers()
    n_b = nums.n.to_bytes((nums.n.bit_length() + 7) // 8, "big")
    e_b = nums.e.to_bytes((nums.e.bit_length() + 7) // 8, "big")
    kv = etree.SubElement(ki, f"{{{NS_DS}}}KeyValue")
    rsa = etree.SubElement(kv, f"{{{NS_DS}}}RSAKeyValue")
    etree.SubElement(rsa, f"{{{NS_DS}}}Modulus").text  = base64.b64encode(n_b).decode()
    etree.SubElement(rsa, f"{{{NS_DS}}}Exponent").text = base64.b64encode(e_b).decode()
    x5 = etree.SubElement(ki, f"{{{NS_DS}}}X509Data")
    etree.SubElement(x5, f"{{{NS_DS}}}X509Certificate").text = cert_b64


def firmar_elemento(root, elem_a_firmar, id_ref: str, priv, cert, cert_b64):
    """Firma `elem_a_firmar` (enveloped, ref al #id_ref) y cuelga la <Signature>
    como hijo de `root`. Mismo mecanismo que el firmador de libros del SII."""
    # Digest del elemento (c14n) ANTES de agregar la firma
    c14n = etree.tostring(elem_a_firmar, method="c14n", exclusive=False)
    digest = base64.b64encode(hashlib.sha1(c14n).digest()).decode()

    signed_info_xml = (
        f'<SignedInfo xmlns="{NS_DS}">'
        f'<CanonicalizationMethod Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"/>'
        f'<SignatureMethod Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1"/>'
        f'<Reference URI="#{id_ref}">'
        f'<Transforms><Transform Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature"/></Transforms>'
        f'<DigestMethod Algorithm="http://www.w3.org/2000/09/xmldsig#sha1"/>'
        f'<DigestValue>{digest}</DigestValue>'
        f'</Reference></SignedInfo>'
    )
    si_c14n = etree.tostring(etree.fromstring(signed_info_xml.encode()),
                             method="c14n", exclusive=False)
    sig_val = base64.b64encode(
        priv.sign(si_c14n, padding.PKCS1v15(), hashes.SHA1())).decode()

    sig = etree.SubElement(root, f"{{{NS_DS}}}Signature")
    si  = etree.SubElement(sig, f"{{{NS_DS}}}SignedInfo")
    etree.SubElement(si, f"{{{NS_DS}}}CanonicalizationMethod",
                     Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315")
    etree.SubElement(si, f"{{{NS_DS}}}SignatureMethod",
                     Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1")
    ref = etree.SubElement(si, f"{{{NS_DS}}}Reference", URI=f"#{id_ref}")
    tr  = etree.SubElement(ref, f"{{{NS_DS}}}Transforms")
    etree.SubElement(tr, f"{{{NS_DS}}}Transform",
                     Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature")
    etree.SubElement(ref, f"{{{NS_DS}}}DigestMethod",
                     Algorithm="http://www.w3.org/2000/09/xmldsig#sha1")
    etree.SubElement(ref, f"{{{NS_DS}}}DigestValue").text = digest
    etree.SubElement(sig, f"{{{NS_DS}}}SignatureValue").text = sig_val
    _keyinfo(sig, cert, cert_b64)


def _serializar(root) -> str:
    xml = etree.tostring(root, encoding="ISO-8859-1", xml_declaration=True).decode("ISO-8859-1")
    return xml.replace("<?xml version='1.0' encoding='ISO-8859-1'?>",
                       '<?xml version="1.0" encoding="ISO-8859-1"?>')


# ─── Parseo del EnvioDTE recibido ────────────────────────────────────────────
def parsear_envio_recibido(xml_bytes: bytes) -> dict:
    """Extrae del EnvioDTE que mandó el SII: emisor/receptor del envío, el ID y
    DigestValue del SetDTE (para el acuse), y los datos de cada DTE."""
    root = etree.fromstring(xml_bytes)
    car  = root.find(f".//{{{NS_SII}}}Caratula")
    def _c(tag):
        el = car.find(f"{{{NS_SII}}}{tag}"); return el.text if el is not None else ""

    set_dte = root.find(f".//{{{NS_SII}}}SetDTE")
    set_id  = set_dte.get("ID", "SetDoc") if set_dte is not None else "SetDoc"

    # DigestValue de la firma que referencia al SetDTE (#SetDoc)
    digest_set = ""
    for sig in root.findall(f".//{{{NS_DS}}}Signature"):
        ref = sig.find(f".//{{{NS_DS}}}Reference")
        if ref is not None and ref.get("URI", "").lstrip("#") == set_id:
            dv = sig.find(f".//{{{NS_DS}}}DigestValue")
            digest_set = dv.text if dv is not None else ""
            break

    dtes = []
    for doc in root.findall(f".//{{{NS_SII}}}Documento"):
        enc = doc.find(f"{{{NS_SII}}}Encabezado")
        idd = enc.find(f"{{{NS_SII}}}IdDoc")
        emi = enc.find(f"{{{NS_SII}}}Emisor")
        rec = enc.find(f"{{{NS_SII}}}Receptor")
        tot = enc.find(f"{{{NS_SII}}}Totales")
        def _t(parent, tag):
            el = parent.find(f"{{{NS_SII}}}{tag}"); return el.text if el is not None else ""
        dtes.append({
            "tipo":       _t(idd, "TipoDTE"),
            "folio":      _t(idd, "Folio"),
            "fch_emis":   _t(idd, "FchEmis"),
            "rut_emisor": _norm_rut(_t(emi, "RUTEmisor")),
            "rut_recep":  _norm_rut(_t(rec, "RUTRecep")),
            "mnt_total":  _t(tot, "MntTotal"),
        })
    return {
        "rut_emisor_envio":   _norm_rut(_c("RutEmisor")),
        "rut_receptor_envio": _norm_rut(_c("RutReceptor")),
        "set_id":  set_id,
        "digest":  digest_set,
        "dtes":    dtes,
    }


def _now():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _norm_rut(r: str) -> str:
    """RUT sin puntos, con guión y DV en mayúscula (como exige el esquema SII:
    patrón [0-9]+-[0-9K]). La empresa puede tenerlo guardado con puntos."""
    return (r or "").replace(".", "").replace(" ", "").strip().upper()


def _caratula_resp(parent, rut_responde, rut_recibe, contacto, n_detalles, id_resp="1"):
    car = etree.SubElement(parent, f"{{{NS_SII}}}Caratula", version="1.0")
    etree.SubElement(car, f"{{{NS_SII}}}RutResponde").text = rut_responde
    etree.SubElement(car, f"{{{NS_SII}}}RutRecibe").text   = rut_recibe
    etree.SubElement(car, f"{{{NS_SII}}}IdRespuesta").text = id_resp
    etree.SubElement(car, f"{{{NS_SII}}}NroDetalles").text = str(n_detalles)
    if contacto.get("nombre"):
        etree.SubElement(car, f"{{{NS_SII}}}NmbContacto").text = contacto["nombre"]
    if contacto.get("fono"):
        etree.SubElement(car, f"{{{NS_SII}}}FonoContacto").text = contacto["fono"]
    if contacto.get("mail"):
        etree.SubElement(car, f"{{{NS_SII}}}MailContacto").text = contacto["mail"]
    etree.SubElement(car, f"{{{NS_SII}}}TmstFirmaResp").text = _now()


# ─── 1. Acuse de Recibo del Envío (RespuestaDTE / RecepcionEnvio) ────────────
def generar_acuse_recibo(info, rut_responde, contacto, nombre_envio,
                         p12_bytes, password) -> str:
    priv, cert, cert_b64 = _cargar_cert(p12_bytes, password)
    rut_recibe = info["rut_emisor_envio"]

    root = etree.Element(f"{{{NS_SII}}}RespuestaDTE", nsmap={None: NS_SII, "xsi": NS_XSI}, version="1.0")
    root.set(f"{{{NS_XSI}}}schemaLocation", f"{NS_SII} RespuestaEnvioDTE_v10.xsd")
    res = etree.SubElement(root, f"{{{NS_SII}}}Resultado", ID="Respuesta")
    _caratula_resp(res, rut_responde, rut_recibe, contacto, len(info["dtes"]))

    renv = etree.SubElement(res, f"{{{NS_SII}}}RecepcionEnvio")
    etree.SubElement(renv, f"{{{NS_SII}}}NmbEnvio").text     = nombre_envio
    etree.SubElement(renv, f"{{{NS_SII}}}FchRecep").text     = _now()
    etree.SubElement(renv, f"{{{NS_SII}}}CodEnvio").text     = "1"
    etree.SubElement(renv, f"{{{NS_SII}}}EnvioDTEID").text   = info["set_id"]
    etree.SubElement(renv, f"{{{NS_SII}}}Digest").text       = info["digest"]
    etree.SubElement(renv, f"{{{NS_SII}}}RutEmisor").text    = info["rut_emisor_envio"]
    etree.SubElement(renv, f"{{{NS_SII}}}RutReceptor").text  = rut_responde
    etree.SubElement(renv, f"{{{NS_SII}}}EstadoRecepEnv").text = "0"
    etree.SubElement(renv, f"{{{NS_SII}}}RecepEnvGlosa").text  = "Envio recibido conforme"
    etree.SubElement(renv, f"{{{NS_SII}}}NroDTE").text        = str(len(info["dtes"]))
    for d in info["dtes"]:
        rd = etree.SubElement(renv, f"{{{NS_SII}}}RecepcionDTE")
        etree.SubElement(rd, f"{{{NS_SII}}}TipoDTE").text  = d["tipo"]
        etree.SubElement(rd, f"{{{NS_SII}}}Folio").text    = d["folio"]
        etree.SubElement(rd, f"{{{NS_SII}}}FchEmis").text  = d["fch_emis"]
        etree.SubElement(rd, f"{{{NS_SII}}}RUTEmisor").text = d["rut_emisor"]
        etree.SubElement(rd, f"{{{NS_SII}}}RUTRecep").text  = d["rut_recep"]
        etree.SubElement(rd, f"{{{NS_SII}}}MntTotal").text  = d["mnt_total"]
        # Estado por DTE: 0 = recibido OK; 3 = RUT receptor no corresponde (el
        # DTE viene dirigido a otro RUT, no al nuestro → hay que rechazarlo).
        if d["rut_recep"] == rut_responde:
            estado, glosa = "0", "DTE Recibido OK"
        else:
            estado, glosa = "3", "DTE No Recibido - Error en RUT Receptor"
        etree.SubElement(rd, f"{{{NS_SII}}}EstadoRecepDTE").text = estado
        etree.SubElement(rd, f"{{{NS_SII}}}RecepDTEGlosa").text  = glosa

    firmar_elemento(root, res, "Respuesta", priv, cert, cert_b64)
    return _serializar(root)


# ─── 2. Recibo de Mercaderías (EnvioRecibos / Ley 19.983) ────────────────────
def generar_recibos(info, rut_responde, contacto, recinto, p12_bytes, password) -> str:
    priv, cert, cert_b64 = _cargar_cert(p12_bytes, password)
    rut_recibe = info["rut_emisor_envio"]

    root = etree.Element(f"{{{NS_SII}}}EnvioRecibos", nsmap={None: NS_SII, "xsi": NS_XSI}, version="1.0")
    root.set(f"{{{NS_XSI}}}schemaLocation", f"{NS_SII} Recibos_v10.xsd")
    setr = etree.SubElement(root, f"{{{NS_SII}}}SetRecibos", ID="SetRecibos")
    car = etree.SubElement(setr, f"{{{NS_SII}}}Caratula", version="1.0")
    etree.SubElement(car, f"{{{NS_SII}}}RutResponde").text = rut_responde
    etree.SubElement(car, f"{{{NS_SII}}}RutRecibe").text   = rut_recibe
    if contacto.get("nombre"):
        etree.SubElement(car, f"{{{NS_SII}}}NmbContacto").text = contacto["nombre"]
    if contacto.get("fono"):
        etree.SubElement(car, f"{{{NS_SII}}}FonoContacto").text = contacto["fono"]
    if contacto.get("mail"):
        etree.SubElement(car, f"{{{NS_SII}}}MailContacto").text = contacto["mail"]
    etree.SubElement(car, f"{{{NS_SII}}}TmstFirmaEnv").text = _now()

    for i, d in enumerate(info["dtes"], start=1):
        recibo = etree.SubElement(setr, f"{{{NS_SII}}}Recibo", version="1.0")
        rid = f"Recibo{i}"
        docr = etree.SubElement(recibo, f"{{{NS_SII}}}DocumentoRecibo", ID=rid)
        etree.SubElement(docr, f"{{{NS_SII}}}TipoDoc").text   = d["tipo"]
        etree.SubElement(docr, f"{{{NS_SII}}}Folio").text     = d["folio"]
        etree.SubElement(docr, f"{{{NS_SII}}}FchEmis").text   = d["fch_emis"]
        etree.SubElement(docr, f"{{{NS_SII}}}RUTEmisor").text = d["rut_emisor"]
        etree.SubElement(docr, f"{{{NS_SII}}}RUTRecep").text  = d["rut_recep"]
        etree.SubElement(docr, f"{{{NS_SII}}}MntTotal").text  = d["mnt_total"]
        etree.SubElement(docr, f"{{{NS_SII}}}Recinto").text   = recinto
        etree.SubElement(docr, f"{{{NS_SII}}}RutFirma").text  = rut_responde
        etree.SubElement(docr, f"{{{NS_SII}}}Declaracion").text = DECLARACION_LEY_19983
        etree.SubElement(docr, f"{{{NS_SII}}}TmstFirmaRecibo").text = _now()
        # Cada Recibo se firma sobre su DocumentoRecibo
        firmar_elemento(recibo, docr, rid, priv, cert, cert_b64)

    # Y el set completo se firma sobre SetRecibos
    firmar_elemento(root, setr, "SetRecibos", priv, cert, cert_b64)
    return _serializar(root)


# ─── 3. Resultado Aprobación Comercial (RespuestaDTE / ResultadoDTE) ─────────
def generar_resultado(info, rut_responde, contacto, p12_bytes, password) -> str:
    priv, cert, cert_b64 = _cargar_cert(p12_bytes, password)
    rut_recibe = info["rut_emisor_envio"]

    root = etree.Element(f"{{{NS_SII}}}RespuestaDTE", nsmap={None: NS_SII, "xsi": NS_XSI}, version="1.0")
    root.set(f"{{{NS_XSI}}}schemaLocation", f"{NS_SII} RespuestaEnvioDTE_v10.xsd")
    res = etree.SubElement(root, f"{{{NS_SII}}}Resultado", ID="Resultado")
    _caratula_resp(res, rut_responde, rut_recibe, contacto, len(info["dtes"]))

    for d in info["dtes"]:
        rdoc = etree.SubElement(res, f"{{{NS_SII}}}ResultadoDTE")
        etree.SubElement(rdoc, f"{{{NS_SII}}}TipoDTE").text  = d["tipo"]
        etree.SubElement(rdoc, f"{{{NS_SII}}}Folio").text    = d["folio"]
        etree.SubElement(rdoc, f"{{{NS_SII}}}FchEmis").text  = d["fch_emis"]
        etree.SubElement(rdoc, f"{{{NS_SII}}}RUTEmisor").text = d["rut_emisor"]
        etree.SubElement(rdoc, f"{{{NS_SII}}}RUTRecep").text  = d["rut_recep"]
        etree.SubElement(rdoc, f"{{{NS_SII}}}MntTotal").text  = d["mnt_total"]
        etree.SubElement(rdoc, f"{{{NS_SII}}}CodEnvio").text  = "1"
        etree.SubElement(rdoc, f"{{{NS_SII}}}EstadoDTE").text = "0"
        etree.SubElement(rdoc, f"{{{NS_SII}}}EstadoDTEGlosa").text = "DTE aceptado conforme"

    firmar_elemento(root, res, "Resultado", priv, cert, cert_b64)
    return _serializar(root)


def generar_las_tres(xml_recibido: bytes, rut_responde, contacto, nombre_envio,
                     recinto, p12_bytes, password) -> dict:
    rut_responde = _norm_rut(rut_responde)   # sin puntos (empresa puede tenerlo con puntos)
    info = parsear_envio_recibido(xml_recibido)
    return {
        "acuse_recibo": generar_acuse_recibo(info, rut_responde, contacto, nombre_envio, p12_bytes, password),
        "recibos":      generar_recibos(info, rut_responde, contacto, recinto, p12_bytes, password),
        "resultado":    generar_resultado(info, rut_responde, contacto, p12_bytes, password),
        "dtes":         info["dtes"],
    }
