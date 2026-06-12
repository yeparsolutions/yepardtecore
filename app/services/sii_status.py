# app/services/sii_status.py
# ══════════════════════════════════════════════════════════════
# Consulta de estado de envíos al SII — "la ventanilla de seguimiento"
#
# Analogía: cuando dejas una carta certificada en Correos, te dan
# un número de seguimiento (track_id). Correos NUNCA te llama para
# avisarte si llegó — eres tú quien vuelve a la ventanilla y pregunta.
# Este módulo ES esa visita a la ventanilla.
#
# El SII tiene DOS ventanillas distintas según el documento:
#
#   1. DTEs (33/34/52/56/61) → SOAP QueryEstUp.jws (getEstUp)
#      - Certificación: https://maullin.sii.cl/DTEWS/QueryEstUp.jws
#      - Producción:    https://palena.sii.cl/DTEWS/QueryEstUp.jws
#      - Token: el estándar DTE (CrSeed/GetTokenFromSeed de maullin/palena)
#
#   2. Boletas (39/41) → API REST boleta.electronica.envio
#      - Certificación: https://apicert.sii.cl/recursos/v1/boleta.electronica.envio/...
#      - Producción:    https://api.sii.cl/recursos/v1/boleta.electronica.envio/...
#      - Token: el de boletas (maullin2/rahue) — el mismo que usamos
#        para subir el EnvioBOLETA. Se reutiliza el token persistido en BD.
#
# Detalle útil: los track_id de boletas tienen 15 dígitos y los de
# factura/DTE 10 — sirve como detector de respaldo si no viene el tipo.
# ══════════════════════════════════════════════════════════════

import logging
import httpx
import xml.sax.saxutils as saxutils
from lxml import etree

logger = logging.getLogger("yepardtecore.sii_status")

# Tipos que viajan por la ventanilla de boletas
TIPOS_BOLETA = {39, 41}

# Hosts SOAP para consulta de DTEs (QueryEstUp)
QUERYESTUP_CERT = "https://maullin.sii.cl/DTEWS/QueryEstUp.jws"
QUERYESTUP_PROD = "https://palena.sii.cl/DTEWS/QueryEstUp.jws"

# Hosts REST para consulta de boletas
BOLETA_API_CERT = "https://apicert.sii.cl/recursos/v1"
BOLETA_API_PROD = "https://api.sii.cl/recursos/v1"

SOAP_HEADERS = {
    "Content-Type": "text/xml; charset=utf-8",
    "SOAPAction":   '""',
    "User-Agent":   "Mozilla/4.0 (compatible; MSIE 6.0)",
}


class SIIStatusChecker:
    """
    Consulta el estado de un envío (track_id) en el SII.

    Uso:
        checker = SIIStatusChecker(ambiente="certificacion")
        # Para DTEs (factura, guía, NC/ND):
        r = await checker.consultar_envio_dte(rut_emisor, track_id, token_dte)
        # Para boletas:
        r = await checker.consultar_envio_boleta(rut_emisor, track_id, token_boleta)

    Ambas devuelven un dict normalizado:
        {
          "estado":      "ACEPTADO" | "RECHAZADO" | "REPAROS" | "PENDIENTE" | "ERROR" | "DESCONOCIDO",
          "codigo_sii":  "EPR" / "REC" / "-11" / ...,
          "glosa":       texto del SII,
          "informados":  int, "aceptados": int, "rechazados": int, "reparos": int,
          "raw":         primeros caracteres de la respuesta (para diagnóstico),
        }
    """

    def __init__(self, ambiente: str = "certificacion"):
        self.ambiente   = ambiente
        es_cert         = ambiente == "certificacion"
        self.url_soap   = QUERYESTUP_CERT if es_cert else QUERYESTUP_PROD
        self.url_boleta = BOLETA_API_CERT if es_cert else BOLETA_API_PROD

    # ── Helpers de RUT ────────────────────────────────────────

    @staticmethod
    def split_rut(rut: str) -> tuple[str, str]:
        """'76123456-7' → ('76123456', '7'). Tolera puntos y mayúsculas."""
        rut    = rut.replace(".", "").strip().upper()
        partes = rut.split("-")
        if len(partes) >= 2:
            return partes[0], partes[1]
        # Sin guión: el último carácter es el dígito verificador
        return rut[:-1], rut[-1]

    # ── Ventanilla 1: DTEs vía SOAP QueryEstUp ────────────────

    async def consultar_envio_dte(self, rut_emisor: str, track_id: str,
                                  token: str) -> dict:
        """
        Consulta SOAP getEstUp(Rut, Dv, TrackId, Token).
        El Rut consultado debe ser el del EMISOR del envío (la empresa),
        y el token debe haberse obtenido con un certificado autorizado
        a consultar por esa empresa (el e-Sign registrado funciona).
        """
        rut_num, dv = self.split_rut(rut_emisor)

        # El namespace SOAP es la propia URL del servicio (estilo RPC del SII)
        soap_body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<SOAP-ENV:Envelope '
            'xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            'SOAP-ENV:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<SOAP-ENV:Body>'
            f'<m:getEstUp xmlns:m="{self.url_soap}">'
            f'<Rut xsi:type="xsd:string">{rut_num}</Rut>'
            f'<Dv xsi:type="xsd:string">{dv}</Dv>'
            f'<TrackId xsi:type="xsd:string">{track_id}</TrackId>'
            f'<Token xsi:type="xsd:string">{saxutils.escape(token)}</Token>'
            '</m:getEstUp>'
            '</SOAP-ENV:Body>'
            '</SOAP-ENV:Envelope>'
        )

        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.post(self.url_soap,
                                         content=soap_body.encode("utf-8"),
                                         headers=SOAP_HEADERS)
        except Exception as e:
            return self._error(f"Sin conexión con QueryEstUp: {e}")

        if resp.status_code != 200:
            return self._error(f"QueryEstUp respondió HTTP {resp.status_code}",
                               raw=resp.text[:300])

        logger.info(f"[ESTADO DTE] track={track_id} raw={resp.text[:400]}")

        # La respuesta SOAP trae el XML del SII ESCAPADO dentro de getEstUpReturn
        # (como una carta dentro de otro sobre — hay que abrir ambos)
        try:
            root = etree.fromstring(resp.content)
            inner_str = None
            for el in root.iter():
                # El tag es getEstUpReturn, con namespace variable según ambiente
                if el.tag.endswith("getEstUpReturn") and el.text:
                    inner_str = el.text
                    break
            if not inner_str:
                return self._error("SII no devolvió getEstUpReturn",
                                   raw=resp.text[:300])
            inner = etree.fromstring(inner_str.encode("utf-8"))
        except etree.XMLSyntaxError:
            return self._error("Respuesta SOAP no parseable", raw=resp.text[:300])

        return self._normalizar_respuesta_sii(inner, raw=inner_str[:400])

    # ── Ventanilla 2: Boletas vía API REST ────────────────────

    async def consultar_envio_boleta(self, rut_emisor: str, track_id: str,
                                     token_boleta: str) -> dict:
        """
        Consulta REST del estado de un EnvioBOLETA:
          GET {base}/boleta.electronica.envio/{rut}-{dv}-{track_id}
          Cookie: TOKEN={token_boleta}

        IMPORTANTE: el token debe ser el de BOLETAS (maullin2/rahue),
        el mismo que se usó para subir el sobre. Si maullin2 no es
        accesible desde el servidor, se reutiliza el token persistido
        en la BD (campo Certificado.token_boleta).
        """
        rut_num, dv = self.split_rut(rut_emisor)
        url = f"{self.url_boleta}/boleta.electronica.envio/{rut_num}-{dv}-{track_id}"

        headers = {
            "Cookie":     f"TOKEN={token_boleta}",
            "User-Agent": "Mozilla/4.0 (compatible; PROG 1.0; YeparDTEcore)",
            "Accept":     "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
        except Exception as e:
            return self._error(f"Sin conexión con API boletas: {e}")

        logger.info(f"[ESTADO BOLETA] track={track_id} HTTP={resp.status_code} "
                    f"raw={resp.text[:400]}")

        if resp.status_code == 401:
            return self._error("Token de boleta inválido o expirado — "
                               "reenviar un sobre para refrescarlo",
                               codigo="401", raw=resp.text[:300])
        if resp.status_code == 404:
            return {"estado": "DESCONOCIDO", "codigo_sii": "404",
                    "glosa": "TrackID no encontrado en el SII",
                    "informados": 0, "aceptados": 0, "rechazados": 0,
                    "reparos": 0, "raw": resp.text[:300]}
        if resp.status_code != 200:
            return self._error(f"API boletas respondió HTTP {resp.status_code}",
                               raw=resp.text[:300])

        # La API REST de boletas normalmente devuelve JSON con estado +
        # estadística — pero el SII a veces contesta XML (SII:RESPUESTA) o
        # una página de error. Plan A: JSON. Plan B: XML. Plan C: mostrar
        # el cuerpo crudo para diagnóstico en vez de un error opaco.
        try:
            data = resp.json()
        except Exception:
            try:
                inner = etree.fromstring(resp.content)
                return self._normalizar_respuesta_sii(inner, raw=resp.text[:400])
            except Exception:
                return self._error(
                    "Respuesta del SII no es JSON ni XML — ver campo raw",
                    raw=resp.text[:400],
                )

        codigo = str(data.get("estado", "")).strip()
        glosa  = data.get("glosa", "") or data.get("descripcion", "") or ""

        # Sumar los contadores por tipo (estadistica es lista por TipoDTE)
        informados = aceptados = rechazados = reparos = 0
        for fila in (data.get("estadistica") or []):
            informados += int(fila.get("informados", 0) or 0)
            aceptados  += int(fila.get("aceptados",  0) or 0)
            rechazados += int(fila.get("rechazados", 0) or 0)
            reparos    += int(fila.get("reparos",    0) or 0)

        estado = self._clasificar(codigo, aceptados, rechazados, reparos)
        return {"estado": estado, "codigo_sii": codigo, "glosa": glosa,
                "informados": informados, "aceptados": aceptados,
                "rechazados": rechazados, "reparos": reparos,
                "detalle": data.get("detalle_rep_rech"),
                "raw": resp.text[:400]}

    # ── Normalización común ───────────────────────────────────

    def _normalizar_respuesta_sii(self, inner: etree._Element, raw: str = "") -> dict:
        """
        Convierte el XML SII:RESPUESTA (de QueryEstUp) al dict normalizado.
        Estructura: RESP_HDR (TRACKID/ESTADO/GLOSA) + RESP_BODY
        (TIPO_DOCTO/INFORMADOS/ACEPTADOS/RECHAZADOS/REPAROS).
        """
        def txt(tag: str) -> str:
            # Busca el tag con y sin namespace SII
            return (inner.findtext(f".//{{http://www.sii.cl/XMLSchema}}{tag}")
                    or inner.findtext(f".//{tag}") or "")

        codigo = txt("ESTADO").strip()
        glosa  = txt("GLOSA").strip()

        def num(tag: str) -> int:
            try:
                return int(txt(tag) or 0)
            except ValueError:
                return 0

        informados = num("INFORMADOS")
        aceptados  = num("ACEPTADOS")
        rechazados = num("RECHAZADOS")
        reparos    = num("REPAROS")

        estado = self._clasificar(codigo, aceptados, rechazados, reparos)
        return {"estado": estado, "codigo_sii": codigo, "glosa": glosa,
                "informados": informados, "aceptados": aceptados,
                "rechazados": rechazados, "reparos": reparos, "raw": raw}

    @staticmethod
    def _clasificar(codigo: str, aceptados: int, rechazados: int,
                    reparos: int) -> str:
        """
        Traduce el código SII a un estado simple para la app.

        Analogía del semáforo:
          verde    (ACEPTADO)  → el SII procesó y aceptó los documentos
          amarillo (REPAROS / PENDIENTE) → aceptado con observaciones, o aún en cola
          rojo     (RECHAZADO) → el sobre o los documentos fueron rechazados
        """
        # Envío ya procesado: el veredicto está en los contadores
        if codigo in {"EPR", "DOK"}:
            if rechazados > 0:
                return "RECHAZADO"
            if reparos > 0:
                return "REPAROS"
            if aceptados > 0:
                return "ACEPTADO"
            return "PENDIENTE"   # procesado pero sin detalle aún

        # Etapas intermedias: el sobre va avanzando por la cinta
        if codigo in {"REC", "SOK", "SDK", "CRT", "FOK", "PDR", "VOF", "EPR2"}:
            return "PENDIENTE"

        # Rechazos del sobre completo (schema, firma, carátula)
        if codigo in {"RSC", "RCT", "RFR", "RCH", "RPT"}:
            return "RECHAZADO"

        # Errores de token / sesión
        if codigo in {"001", "002", "003"}:
            return "ERROR"

        # Códigos negativos = error de consulta en el SII
        if codigo.startswith("-"):
            return "ERROR"

        return "DESCONOCIDO"

    @staticmethod
    def _error(mensaje: str, codigo: str = "", raw: str = "") -> dict:
        return {"estado": "ERROR", "codigo_sii": codigo, "glosa": mensaje,
                "informados": 0, "aceptados": 0, "rechazados": 0,
                "reparos": 0, "raw": raw}
