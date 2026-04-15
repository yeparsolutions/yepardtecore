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

        # Cargar certificado .p12
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

    # ── Paso 2: Firmar la semilla con xmlsec1 ─────────────────

    def _firmar_semilla(self, semilla: str) -> str:
        """
        Firma la semilla usando xmlsec1 — herramienta oficial para XMLDSig.
        xmlsec1 rellena automáticamente DigestValue, SignatureValue,
        Modulus, Exponent y X509Certificate en el template XML.
        """
        # Template XML con placeholders que xmlsec1 rellena automáticamente
        template = (
            '<?xml version="1.0"?>'
            '<getToken>'
            f'<item><Semilla>{semilla}</Semilla></item>'
            '<Signature xmlns="http://www.w3.org/2000/09/xmldsig#">'
            '<SignedInfo>'
            '<CanonicalizationMethod Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"/>'
            '<SignatureMethod Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1"/>'
            '<Reference URI="">'
            '<Transforms><Transform Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature"/></Transforms>'
            '<DigestMethod Algorithm="http://www.w3.org/2000/09/xmldsig#sha1"/>'
            '<DigestValue/>'
            '</Reference>'
            '</SignedInfo>'
            '<SignatureValue/>'
            '<KeyInfo>'
            '<KeyValue><RSAKeyValue><Modulus/><Exponent/></RSAKeyValue></KeyValue>'
            '<X509Data><X509Certificate/></X509Data>'
            '</KeyInfo>'
            '</Signature>'
            '</getToken>'
        )

        # Crear archivos temporales para xmlsec1
        path_in   = None
        path_key  = None
        path_cert = None
        path_out  = None

        try:
            # Archivo XML con semilla
            with tempfile.NamedTemporaryFile(suffix=".xml", delete=False, mode="w") as f:
                f.write(template)
                path_in = f.name

            # Archivo clave privada PEM
            with tempfile.NamedTemporaryFile(suffix=".pem", delete=False, mode="wb") as f:
                f.write(self._private_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL,
                    serialization.NoEncryption()
                ))
                path_key = f.name

            # Archivo certificado PEM
            with tempfile.NamedTemporaryFile(suffix=".pem", delete=False, mode="wb") as f:
                f.write(self._certificate.public_bytes(serialization.Encoding.PEM))
                path_cert = f.name

            # Archivo de salida firmado
            path_out = path_in + "_firmado.xml"

            # Firmar con xmlsec1
            result = subprocess.run(
                ["xmlsec1", "--sign",
                 "--privkey-pem", f"{path_key},{path_cert}",
                 "--output", path_out,
                 path_in],
                capture_output=True, text=True, timeout=15
            )

            if result.returncode != 0:
                raise Exception(f"xmlsec1 error: {result.stderr}")

            # Leer y retornar el XML firmado
            return open(path_out).read()

        finally:
            # Limpiar archivos temporales siempre
            for p in [path_in, path_key, path_cert, path_out]:
                if p:
                    try:
                        os.unlink(p)
                    except Exception:
                        pass

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
                                ambiente: str = "certificacion") -> str:
    """
    Obtiene el token SII con cache en memoria.
    Si el token tiene menos de 55 minutos, lo reutiliza.
    Si expiró, pide uno nuevo automáticamente.
    """
    cache_key = f"{ambiente}_{hash(p12_bytes)}"
    ahora     = datetime.now(timezone.utc)

    # Verificar si hay token válido en cache
    if cache_key in _token_cache:
        cached          = _token_cache[cache_key]
        tiempo_restante = (cached["expira"] - ahora).total_seconds()
        if tiempo_restante > 60:
            return cached["token"]

    # Pedir token nuevo al SII
    auth  = SIIAuth(p12_bytes, password, ambiente)
    token = await auth.obtener_token()

    # Guardar en cache con expiración de 55 minutos
    _token_cache[cache_key] = {
        "token":  token,
        "expira": ahora + timedelta(minutes=55),
    }

    return token
