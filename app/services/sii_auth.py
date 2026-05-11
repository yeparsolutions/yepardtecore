# app/services/sii_auth.py
# ══════════════════════════════════════════════════════════════
# Autenticación al SII — Flujo de semilla firmada
#
# El SII no usa usuario/contraseña. Usa este flujo:
#   1. Pedir semilla al SII  → POST SOAP /DTEWS/CrSeed.jws
#   2. Firmar la semilla con xmlsec1 (firma XMLDSig)
#   3. Enviar semilla firmada → POST SOAP /DTEWS/GetTokenFromSeed.jws
#   4. El SII devuelve un TOKEN válido por 1 hora
#
# Analogía: es como un banco que te manda un código SMS
# (semilla), tú lo firmas con tu huella digital (certificado)
# y el banco te da una tarjeta temporal (token) válida 1 hora.
# ══════════════════════════════════════════════════════════════

import httpx
import hashlib
import subprocess
import tempfile
import os
import xml.sax.saxutils as saxutils
from lxml import etree
from datetime import datetime, timezone, timedelta
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.backends import default_backend
from base64 import b64encode

# URLs de autenticación SII
SII_SEMILLA_CERT = "https://maullin.sii.cl/DTEWS/CrSeed.jws"
SII_TOKEN_CERT   = "https://maullin.sii.cl/DTEWS/GetTokenFromSeed.jws"
SII_SEMILLA_PROD = "https://palena.sii.cl/DTEWS/CrSeed.jws"
SII_TOKEN_PROD   = "https://palena.sii.cl/DTEWS/GetTokenFromSeed.jws"

# Headers comunes para todas las llamadas SOAP al SII
SOAP_HEADERS = {
    "Content-Type": "text/xml; charset=utf-8",
    "SOAPAction":   '""',
    "User-Agent":   "Mozilla/4.0 (compatible; MSIE 6.0)",
}


class SIIAuth:
    """
    Gestiona la autenticación con el SII.

    Uso:
        auth = SIIAuth(p12_bytes, password, ambiente="certificacion")
        token = await auth.obtener_token()
        # Usar token como cookie en envíos: Cookie: TOKEN=<token>
    """

    def __init__(self, p12_bytes: bytes, password: str,
                 ambiente: str = "certificacion"):
        self.ambiente    = ambiente
        self.url_semilla = SII_SEMILLA_CERT if ambiente == "certificacion" else SII_SEMILLA_PROD
        self.url_token   = SII_TOKEN_CERT   if ambiente == "certificacion" else SII_TOKEN_PROD

        # Cargar certificado .p12 (puede ser el de firma o el de autenticacion)
        pwd_bytes = password.encode("utf-8") if isinstance(password, str) else password
        private_key, certificate, _ = pkcs12.load_key_and_certificates(
            p12_bytes, pwd_bytes, backend=default_backend()
        )
        self._private_key  = private_key
        self._certificate  = certificate
        self._p12_bytes    = p12_bytes
        self._password     = password

    # ── Flujo completo ────────────────────────────────────────

    async def obtener_token(self) -> str:
        """
        Ejecuta el flujo completo de autenticación SII.
        Retorna el TOKEN listo para usar en envíos.
        """
        # Paso 1: Pedir semilla al SII
        semilla = await self._pedir_semilla()

        # Paso 2: Firmar la semilla con xmlsec1
        semilla_firmada = self._firmar_semilla(semilla)

        # Paso 3: Obtener token con la semilla firmada
        token = await self._obtener_token_con_semilla(semilla_firmada)

        return token

    # ── Paso 1: Pedir semilla ─────────────────────────────────

    async def _pedir_semilla(self) -> str:
        """
        Solicita una semilla temporal al SII via SOAP.
        La semilla es un número único que expira en pocos minutos.
        """
        soap_body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soapenv:Envelope '
            'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<soapenv:Body>'
            '<getSeed xmlns="http://DefaultNamespace"/>'
            '</soapenv:Body>'
            '</soapenv:Envelope>'
        )

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                self.url_semilla,
                content=soap_body.encode("utf-8"),
                headers=SOAP_HEADERS,
            )

        if response.status_code != 200:
            raise Exception(f"SII no respondió al pedir semilla: HTTP {response.status_code}")

        try:
            # La respuesta SOAP contiene el XML de la semilla escapado dentro
            root        = etree.fromstring(response.content)
            seed_return = root.findtext(".//{http://DefaultNamespace}getSeedReturn")

            if not seed_return:
                raise Exception(f"Respuesta inválida del SII: {response.text[:200]}")

            # El contenido es XML escapado — parsearlo de nuevo
            inner   = etree.fromstring(seed_return.encode("utf-8"))
            estado  = (inner.findtext(".//{http://www.sii.cl/XMLSchema}ESTADO")
                       or inner.findtext(".//ESTADO"))
            semilla = (inner.findtext(".//{http://www.sii.cl/XMLSchema}SEMILLA")
                       or inner.findtext(".//SEMILLA"))

            if estado != "00":
                glosa = inner.findtext(".//GLOSA") or "Error desconocido"
                raise Exception(f"SII rechazó la semilla: {glosa}")

            if not semilla:
                raise Exception("SII no devolvió semilla")

            return semilla.strip()

        except etree.XMLSyntaxError:
            raise Exception(f"Respuesta inválida del SII: {response.text[:200]}")

    # ── Paso 2: Firmar la semilla con Python puro ─────────────

    def _firmar_semilla(self, semilla: str) -> str:
        """
        Firma la semilla usando Python puro (cryptography + lxml).
        Implementa XMLDSig enveloped signature sobre el getToken.
        No depende de xmlsec1 CLI para evitar problemas de compatibilidad.
        """
        import hashlib
        from lxml import etree as ET

        NS = "http://www.w3.org/2000/09/xmldsig#"

        # 1. Construir el documento getToken
        root = ET.fromstring(
            f'<getToken><item><Semilla>{semilla}</Semilla></item></getToken>'
        )

        # 2. Construir el elemento Signature con namespace correcto (sin prefijo ns0)
        sig_el  = ET.SubElement(root, f"{{{NS}}}Signature", nsmap={None: NS})
        si_el   = ET.SubElement(sig_el, f"{{{NS}}}SignedInfo")
        ET.SubElement(si_el, f"{{{NS}}}CanonicalizationMethod").set(
            "Algorithm", "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
        )
        ET.SubElement(si_el, f"{{{NS}}}SignatureMethod").set(
            "Algorithm", f"{NS}rsa-sha1"
        )
        ref_el = ET.SubElement(si_el, f"{{{NS}}}Reference")
        ref_el.set("URI", "")
        trs = ET.SubElement(ref_el, f"{{{NS}}}Transforms")
        ET.SubElement(trs, f"{{{NS}}}Transform").set(
            "Algorithm", f"{NS}enveloped-signature"
        )
        ET.SubElement(ref_el, f"{{{NS}}}DigestMethod").set(
            "Algorithm", f"{NS}sha1"
        )
        dv_el = ET.SubElement(ref_el, f"{{{NS}}}DigestValue")
        sv_el = ET.SubElement(sig_el, f"{{{NS}}}SignatureValue")

        # KeyInfo
        ki_el  = ET.SubElement(sig_el, f"{{{NS}}}KeyInfo")
        kv_el  = ET.SubElement(ki_el,  f"{{{NS}}}KeyValue")
        rsa_el = ET.SubElement(kv_el,  f"{{{NS}}}RSAKeyValue")
        pub    = self._private_key.public_key()
        nums   = pub.public_numbers()

        def int_to_b64(n: int) -> str:
            length = (n.bit_length() + 7) // 8
            return b64encode(n.to_bytes(length, "big")).decode()

        ET.SubElement(rsa_el, f"{{{NS}}}Modulus").text  = int_to_b64(nums.n)
        ET.SubElement(rsa_el, f"{{{NS}}}Exponent").text = int_to_b64(nums.e)
        x509d = ET.SubElement(ki_el, f"{{{NS}}}X509Data")
        ET.SubElement(x509d, f"{{{NS}}}X509Certificate").text = b64encode(
            self._certificate.public_bytes(serialization.Encoding.DER)
        ).decode()

        # 3. Calcular DigestValue
        #    URI="" + enveloped-signature = documento completo SIN el nodo Signature
        #    El Signature ya fue agregado al arbol, lo excluimos para el digest

        import copy, io

        # Copia del root SIN Signature para el digest
        root_for_digest = copy.deepcopy(root)
        sig_in_copy = root_for_digest.find(f"{{{NS}}}Signature")
        if sig_in_copy is not None:
            root_for_digest.remove(sig_in_copy)

        # c14n standalone del documento sin Signature
        root_str  = ET.tostring(root_for_digest)
        root_alone = ET.fromstring(root_str)
        buf = io.BytesIO()
        root_alone.getroottree().write_c14n(buf, exclusive=False, with_comments=False)
        digest_bytes = hashlib.sha1(buf.getvalue()).digest()
        dv_el.text = b64encode(digest_bytes).decode()

        # 4. Calcular SignatureValue sobre SignedInfo c14n standalone
        #    (ahora que DigestValue ya esta relleno en si_el)
        si_raw   = ET.tostring(si_el)
        si_alone = ET.fromstring(si_raw)
        si_c14n  = ET.tostring(si_alone, method="c14n", exclusive=False, with_comments=False)
        sig_raw  = self._private_key.sign(si_c14n, padding.PKCS1v15(), hashes.SHA1())
        sv_el.text = b64encode(sig_raw).decode()

        # 5. Serializar y retornar (sin declaracion XML - va dentro de SOAP)
        return ET.tostring(root, encoding="unicode", xml_declaration=False)

    # ── Paso 3: Obtener token ─────────────────────────────────

    async def _obtener_token_con_semilla(self, semilla_firmada: str) -> str:
        """
        Envía la semilla firmada al SII via SOAP y obtiene el TOKEN.
        El TOKEN es válido aproximadamente 1 hora.
        """
        # El XML firmado va escapado dentro del parámetro pszXml
        soap_body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soapenv:Envelope '
            'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<soapenv:Body>'
            '<getToken xmlns="http://DefaultNamespace">'
            '<pszXml xsi:type="xsd:string">' + saxutils.escape(semilla_firmada) + '</pszXml>'
            '</getToken>'
            '</soapenv:Body>'
            '</soapenv:Envelope>'
        )

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                self.url_token,
                content=soap_body.encode("utf-8"),
                headers=SOAP_HEADERS,
            )

        if response.status_code != 200:
            raise Exception(f"SII no respondió al pedir token: HTTP {response.status_code}")

        try:
            # La respuesta SOAP contiene el XML del token escapado dentro
            root         = etree.fromstring(response.content)
            token_return = root.findtext(".//{http://DefaultNamespace}getTokenReturn")

            if not token_return:
                raise Exception(f"Respuesta inválida del SII: {response.text[:200]}")

            # El contenido es XML escapado — parsearlo de nuevo
            inner  = etree.fromstring(token_return.encode("utf-8"))
            estado = (inner.findtext(".//{http://www.sii.cl/XMLSchema}ESTADO")
                      or inner.findtext(".//ESTADO"))
            token  = (inner.findtext(".//{http://www.sii.cl/XMLSchema}TOKEN")
                      or inner.findtext(".//TOKEN"))

            if estado != "00":
                glosa = inner.findtext(".//GLOSA") or "Error desconocido"
                raise Exception(f"SII rechazó la autenticación: [{estado}] {glosa}")

            if not token:
                raise Exception("SII no devolvió token")

            return token.strip()

        except etree.XMLSyntaxError:
            raise Exception(f"Respuesta inválida del SII: {response.text[:200]}")


# ── Cache de token en memoria ─────────────────────────────────
# Evita pedir un token nuevo en cada envío — se reutiliza por 55 min

_token_cache: dict[str, dict] = {}


async def obtener_token_cached(p12_bytes: bytes, password: str,
                                ambiente: str = "certificacion",
                                auth_p12_bytes: bytes = None,
                                auth_password: str = None) -> str:
    """
    Obtiene el token SII con cache en memoria.
    Si auth_p12_bytes está presente, lo usa para autenticarse (en vez de p12_bytes).
    Esto permite usar un certificado homologado SII para auth y otro para firmar DTEs.
    """
    # Usar certificado de auth si existe, si no usar el de firma
    token_p12   = auth_p12_bytes if auth_p12_bytes else p12_bytes
    token_pwd   = auth_password  if auth_password  else password

    cache_key = f"{ambiente}_{hash(token_p12)}"
    ahora     = datetime.now(timezone.utc)

    if cache_key in _token_cache:
        cached          = _token_cache[cache_key]
        tiempo_restante = (cached["expira"] - ahora).total_seconds()
        if tiempo_restante > 60:
            return cached["token"]

    auth  = SIIAuth(token_p12, token_pwd, ambiente)
    token = await auth.obtener_token()

    _token_cache[cache_key] = {
        "token":  token,
        "expira": ahora + timedelta(minutes=55),
    }

    return token
