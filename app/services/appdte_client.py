# app/services/appdte_client.py
# ══════════════════════════════════════════════════════════════
# Cliente async para la API de AppDTE (https://apicert.appdte.cl)
#
# Analogía: AppDTE es el "notario certificado" que sabe exactamente
# cómo el SII espera que estén firmados los documentos.
# Nosotros le entregamos el documento y él firma correctamente,
# sin que tengamos que reimplementar el protocolo XMLDSig + C14N.
#
# Endpoints utilizados:
#   POST /api/timbredte   → Inserta TED (Timbre Electrónico)
#   POST /api/firmaxml    → Firma XMLDSig un nodo del XML
#   POST /api/uploaddte   → Envía sobre firmado al SII
# ══════════════════════════════════════════════════════════════

import base64
import logging
import httpx

logger = logging.getLogger("yepardtecore.appdte")

# URLs base según ambiente
APPDTE_URL_CERT = "https://apicert.appdte.cl"
APPDTE_URL_PROD = "https://api.appdte.cl"   # confirmar con AppDTE para producción


def _b64_encode_iso(xml_str: str) -> str:
    """Codifica un string ISO-8859-1 a Base64 (formato que espera AppDTE)."""
    return base64.b64encode(xml_str.encode("iso-8859-1")).decode("utf-8")


def _b64_encode_bytes(data: bytes) -> str:
    """Codifica bytes crudos (ej: archivo .pfx) a Base64."""
    return base64.b64encode(data).decode("utf-8")


def _b64_decode_iso(b64_str: str) -> str:
    """Decodifica Base64 de la respuesta a string ISO-8859-1."""
    return base64.b64decode(b64_str).decode("iso-8859-1")


class AppDTEClient:
    """
    Cliente async para la API de AppDTE.

    Delega la firma XMLDSig al servicio Java de AppDTE,
    que ha sido verificado como compatible con el validador del SII.

    Uso:
        client = AppDTEClient(ambiente="certificacion")
        timbrado = await client.timbre_dte(xml_str, caf_xml)
        firmado  = await client.firma_xml(timbrado, pfx_bytes, "pass", "Documento", "DTE-33-65")
        sobre_f  = await client.firma_xml(sobre_xml, pfx_bytes, "pass", "SetDTE", "SetDoc")
    """

    def __init__(self, ambiente: str = "certificacion"):
        # Seleccionar URL base según el ambiente
        self.base_url = APPDTE_URL_CERT if ambiente == "certificacion" else APPDTE_URL_PROD
        self.timeout  = 30.0  # segundos

    # ── Métodos públicos ──────────────────────────────────────────────

    async def timbre_dte(self, xml_iso: str, caf_xml: str) -> str:
        """
        Timbra el DTE insertando el TED (Timbre Electrónico del DTE).

        El TED contiene la firma con la llave privada del CAF, que es
        la que permite al receptor verificar la autenticidad del impreso.

        Args:
            xml_iso: XML del DTE sin timbre (encoding ISO-8859-1)
            caf_xml: XML del CAF entregado por el SII

        Returns:
            XML del DTE con TED insertado (string ISO-8859-1)

        Raises:
            RuntimeError: si AppDTE responde con error o falta xmlResultado
        """
        payload = {
            "xmlBase64": _b64_encode_iso(xml_iso),
            "cafBase64": _b64_encode_iso(caf_xml),
        }
        logger.debug("[AppDTE] timbredte →")
        data = await self._post("/api/timbredte", payload)

        if "xmlResultado" not in data:
            raise RuntimeError(
                f"AppDTE /timbredte: falta 'xmlResultado' en respuesta: {data}"
            )
        return _b64_decode_iso(data["xmlResultado"])

    async def firma_xml(
        self,
        xml_iso:      str,
        pfx_bytes:    bytes,
        password:     str,
        nodo_xml:     str,
        id_referencia: str,
    ) -> str:
        """
        Firma XMLDSig un nodo específico del XML usando la llave del PFX.

        Este método se usa dos veces en el flujo:
          • Firma DTE:    nodo_xml="Documento",  id_referencia="DTE-33-65"
          • Firma sobre:  nodo_xml="SetDTE",     id_referencia="SetDoc"

        El id_referencia debe coincidir exactamente con el atributo ID
        del elemento que se va a firmar en el XML.

        Args:
            xml_iso:       XML a firmar (encoding ISO-8859-1)
            pfx_bytes:     Certificado digital en formato .pfx/.p12 (bytes)
            password:      Clave del certificado
            nodo_xml:      Nombre del elemento XML a firmar
            id_referencia: Valor del atributo ID del elemento a firmar

        Returns:
            XML con firma XMLDSig insertada (string ISO-8859-1)
        """
        payload = {
            "xmlBase64":    _b64_encode_iso(xml_iso),
            "pfxBase64":    _b64_encode_bytes(pfx_bytes),
            "pass_cert":    password,
            "nodo_xml":     nodo_xml,
            "id_referencia": id_referencia,
        }
        logger.debug(f"[AppDTE] firmaxml nodo={nodo_xml} id={id_referencia} →")
        data = await self._post("/api/firmaxml", payload)

        if "xmlFirmado" not in data:
            raise RuntimeError(
                f"AppDTE /firmaxml [{nodo_xml}]: falta 'xmlFirmado': {data}"
            )
        return _b64_decode_iso(data["xmlFirmado"])

    async def upload_dte(
        self,
        sobre_xml_iso: str,
        pfx_bytes:     bytes,
        password:      str,
        rut_emisor:    str,
        rut_enviador:  str,
    ) -> dict:
        """
        Envía el EnvioDTE firmado al SII usando AppDTE como proxy.

        AppDTE obtiene el token SII internamente usando el certificado,
        construye el multipart y envía al endpoint del SII.

        Args:
            sobre_xml_iso: EnvioDTE firmado (string ISO-8859-1)
            pfx_bytes:     Certificado digital (.pfx bytes)
            password:      Clave del certificado
            rut_emisor:    RUT empresa emisora (ej: "78377021-0")
            rut_enviador:  RUT del usuario que envía

        Returns:
            dict con keys: track_id, estado, mensaje
        """
        payload = {
            "xmlBase64":  _b64_encode_iso(sobre_xml_iso),
            "pfxBase64":  _b64_encode_bytes(pfx_bytes),
            "pass_cert":  password,
            "rut_emisor": rut_emisor.replace(".", ""),
            "rut_usuario": rut_enviador.replace(".", ""),
        }
        logger.debug(f"[AppDTE] uploaddte emisor={rut_emisor} →")
        data = await self._post("/api/uploaddte", payload)

        return self._parsear_upload(data)

    # ── Internos ──────────────────────────────────────────────────────

    def _parsear_upload(self, data: dict) -> dict:
        """Normaliza la respuesta de upload a nuestro formato interno."""
        # AppDTE puede retornar diferentes formatos según versión
        track_id = (
            data.get("trackId") or data.get("track_id") or
            data.get("TRACKID") or data.get("trackid")
        )
        estado = str(
            data.get("estado") or data.get("status") or
            data.get("STATUS") or ""
        ).upper()

        if track_id:
            return {
                "track_id": str(track_id),
                "estado":   "RECIBIDO",
                "mensaje":  "Sobre recibido por el SII vía AppDTE",
                "raw":      data,
            }
        return {
            "track_id": None,
            "estado":   "ERROR",
            "mensaje":  str(data),
            "raw":      data,
        }

    async def _post(self, path: str, payload: dict) -> dict:
        """Realiza un POST a la API de AppDTE y retorna el JSON."""
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )

            logger.debug(f"[AppDTE] {path} HTTP {response.status_code}")

            if response.status_code != 200:
                raise RuntimeError(
                    f"AppDTE {path} → HTTP {response.status_code}: "
                    f"{response.text[:400]}"
                )
            return response.json()

        except httpx.TimeoutException:
            raise RuntimeError(
                f"AppDTE {path}: timeout tras {self.timeout}s. "
                "Verificar conectividad con apicert.appdte.cl"
            )
        except httpx.RequestError as e:
            raise RuntimeError(f"AppDTE {path}: error de red: {e}")
