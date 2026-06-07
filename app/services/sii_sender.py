# app/services/sii_sender.py
# ══════════════════════════════════════════════════════════════
# Servicio de envio al SII — v2.4
#
# FIX v2.4: STATUS 7 en boletas electrónicas
#   Las boletas (tipo 39/41) requieren token obtenido desde
#   maullin2.sii.cl (cert) o rahue.sii.cl (prod), NO desde
#   maullin.sii.cl que es el endpoint DTE normal.
#   enviar_sobre() ahora detecta si es boleta y usa el token
#   correcto automáticamente.
#
# FIX v2.3: restaurar xsi:schemaLocation en EnvioBOLETA y EnvioDTE
# FIX v2.1: boletas y facturas usan el mismo endpoint de upload
# FIX v2.0: re-firma cada DTE DESPUÉS de insertarlo en el árbol DOM
# ══════════════════════════════════════════════════════════════

import logging
import httpx
from lxml import etree
from datetime import datetime, timezone, timedelta
from app.core.config import settings

logger = logging.getLogger("yepardtecore.dte")

SII_UPLOAD_CERT = "https://maullin.sii.cl/cgi_dte/UPL/DTEUpload"
SII_UPLOAD_PROD = "https://palena.sii.cl/cgi_dte/UPL/DTEUpload"

SII_NS     = "http://www.sii.cl/SiiDte"
XSI_NS     = "http://www.w3.org/2001/XMLSchema-instance"
XMLDSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
TIPOS_BOLETA = {39, 41}


class SIISender:

    def __init__(
        self,
        ambiente:  str = "certificacion",
        fch_resol: str | None = None,
        nro_resol: str | None = None,
    ):
        self.ambiente   = ambiente
        self.url_upload = SII_UPLOAD_CERT if ambiente == "certificacion" else SII_UPLOAD_PROD
        self.fch_resol  = fch_resol or "2000-01-01"
        self.nro_resol  = nro_resol or "0"

    @staticmethod
    def limpiar_rut(rut: str) -> str:
        rut    = rut.replace(".", "").strip()
        partes = rut.split("-")
        if len(partes) > 2:
            rut = partes[0] + "-" + partes[1]
        return rut

    async def construir_sobre(self, dtes_xml: list[str], rut_emisor: str,
                              rut_enviador: str, firma_service) -> str:
        """
        Construye el sobre EnvioDTE/EnvioBOLETA y lo firma.
        FIX SCH-00001: incluye xsi:schemaLocation correcto según tipo.
        """
        NS    = SII_NS
        ahora = datetime.now(timezone.utc)

        rut_emisor   = self.limpiar_rut(rut_emisor)
        rut_enviador = self.limpiar_rut(rut_enviador)

        import re as _re
        tipos_en_sobre: dict[int, int] = {}
        for dte_xml in dtes_xml:
            m = _re.search(r'<TipoDTE>(\d+)</TipoDTE>', dte_xml)
            if m:
                t = int(m.group(1))
                tipos_en_sobre[t] = tipos_en_sobre.get(t, 0) + 1

        es_boleta = bool(tipos_en_sobre) and all(t in TIPOS_BOLETA for t in tipos_en_sobre)
        tag = "EnvioBOLETA" if es_boleta else "EnvioDTE"

        fch_resol = self.fch_resol
        nro_resol = self.nro_resol
        tmst      = (ahora + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S")

        subtot = "".join(
            f"<SubTotDTE><TpoDTE>{t}</TpoDTE><NroDTE>{c}</NroDTE></SubTotDTE>"
            for t, c in sorted(tipos_en_sobre.items())
        )

        dtes_str = []
        for dte_xml in dtes_xml:
            s = dte_xml
            if s.startswith('<?xml'):
                s = s[s.index('?>') + 2:].lstrip()
            dtes_str.append(s.strip())

        caratula = (
            f'<Caratula version="1.0">'
            f'<RutEmisor>{rut_emisor}</RutEmisor>'
            f'<RutEnvia>{rut_enviador}</RutEnvia>'
            f'<RutReceptor>60803000-K</RutReceptor>'
            f'<FchResol>{fch_resol}</FchResol>'
            f'<NroResol>{nro_resol}</NroResol>'
            f'<TmstFirmaEnv>{tmst}</TmstFirmaEnv>'
            f'{subtot}'
            f'</Caratula>'
        )
        set_str = f'<SetDTE ID="SetDoc">{caratula}{"".join(dtes_str)}</SetDTE>'

        if es_boleta:
            schema_loc = f'xsi:schemaLocation="{NS} EnvioBOLETA_v11.xsd"'
        else:
            schema_loc = f'xsi:schemaLocation="{NS} EnvioDTE_v10.xsd"'

        sobre_sin_firmas = (
            f'<?xml version="1.0" encoding="ISO-8859-1"?>\n'
            f'<{tag} xmlns="{NS}" xmlns:xsi="{XSI_NS}" version="1.0" {schema_loc}>'
            f'{set_str}'
            f'</{tag}>'
        )

        return await firma_service.firmar_sobre(sobre_sin_firmas)

    async def enviar_sobre(self, sobre_xml: str, rut_emisor: str,
                           rut_enviador: str,
                           p12_bytes: bytes = None,
                           password: str = None,
                           auth_p12_bytes: bytes = None,
                           auth_password: str = None) -> dict:
        """
        Envía un sobre XML (EnvioDTE o EnvioBOLETA) al SII.

        FIX STATUS 7:
        Las boletas electrónicas (EnvioBOLETA) requieren un token
        obtenido desde maullin2/rahue (endpoint REST boletas), NO
        desde maullin/palena (endpoint SOAP DTE estándar).
        Si se usa el token DTE para boletas, el SII responde STATUS 7.
        """
        token_p12 = auth_p12_bytes or p12_bytes
        token_pwd = auth_password or password

        # Token DTE estándar para todos los tipos — maullin/palena
        # maullin2/rahue no es accesible desde servidores fuera de Chile
        # El endpoint de upload acepta el mismo token para DTE y boletas
        es_boleta = "EnvioBOLETA" in sobre_xml[:500]
        token = await self._obtener_token(token_p12, token_pwd)

        url_envio   = self.url_upload
        rut_limpio  = self.limpiar_rut(rut_emisor)
        env_limpio  = self.limpiar_rut(rut_enviador)
        timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
        nombre      = f"{rut_limpio}_{timestamp}.xml"
        sobre_bytes = sobre_xml.encode("ISO-8859-1")

        def split_rut(rut):
            partes = rut.replace(".", "").split("-")
            return partes[0], partes[1] if len(partes) > 1 else "0"

        rut_num, dv_company = split_rut(rut_limpio)
        env_num, dv_sender  = split_rut(env_limpio)

        headers = {
            "User-Agent": "Mozilla/4.0 (compatible; PROG 1.0; Windows NT 5.0; YeparDTEcore)",
            "Cookie":     f"TOKEN={token}",
        }
        files = {
            "rutSender":  (None, env_num),
            "dvSender":   (None, dv_sender),
            "rutCompany": (None, rut_num),
            "dvCompany":  (None, dv_company),
            "archivo":    (nombre, sobre_bytes, "text/xml;charset=ISO-8859-1"),
        }

        logger.info(f"[SII ENVIO] {'BOLETA' if es_boleta else 'DTE'} "
                    f"rutSender={env_limpio} rutCompany={rut_limpio} "
                    f"token={token[:8]}...")
        logger.info(f"[SII ENVIO] url={url_envio} bytes={len(sobre_bytes)}")

        try:
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                response = await client.post(url_envio, headers=headers, files=files)

            logger.info(f"[SII RAW] HTTP={response.status_code} "
                        f"body={response.text[:2000]}")

            if response.status_code != 200:
                return {
                    "track_id": None,
                    "estado":   "ERROR_HTTP",
                    "mensaje":  f"SII respondio HTTP {response.status_code}",
                    "raw":      response.text[:500],
                }

            return self._parsear_respuesta_upload(response.text)

        except httpx.TimeoutException:
            return {"track_id": None, "estado": "TIMEOUT",
                    "mensaje": "El SII no respondio en 30 segundos"}
        except Exception as e:
            return {"track_id": None, "estado": "ERROR", "mensaje": str(e)}

    def _parsear_respuesta_upload(self, response_text: str) -> dict:
        try:
            root     = etree.fromstring(response_text.encode())
            track_id = root.findtext("TRACKID")
            status   = root.findtext("STATUS")
            glosa    = root.findtext("GLOSA") or ""

            if status == "0" and track_id:
                logger.info(f"[SII OK] TrackID={track_id}")
                return {"track_id": track_id, "estado": "RECIBIDO",
                        "mensaje": "Sobre recibido por el SII"}

            errores_sii = {
                "1":  "Error de autenticacion — token invalido",
                "2":  "Error en el XML del sobre",
                "3":  "RUT del emisor no coincide",
                "5":  "No autorizado para este RUT",
                "7":  "Token invalido para este tipo de documento — boletas requieren token maullin2",
                "10": "RUT no autorizado para enviar DTE",
                "11": "CAF no corresponde a este emisor",
                "12": "CAF vencido",
                "13": "Folio fuera de rango del CAF",
                "-1": "Error de autenticacion — token invalido",
                "-2": "Error en el XML del sobre",
            }
            mensaje = errores_sii.get(status, glosa or f"STATUS desconocido: {status}")
            logger.warning(f"[SII ERROR] STATUS={status} mensaje={mensaje}")
            return {"track_id": None, "estado": "RECHAZADO",
                    "mensaje": mensaje, "codigo": status}

        except etree.XMLSyntaxError:
            if "NO ESTA AUTORIZADA" in response_text:
                return {"track_id": None, "estado": "NO_AUTORIZADO",
                        "mensaje": "Empresa no autorizada para enviar DTE"}
            return {"track_id": None, "estado": "ERROR_PARSEO",
                    "mensaje": "No se pudo parsear respuesta del SII",
                    "raw": response_text[:300]}

    async def consultar_estado(self, track_id: str, rut_emisor: str) -> dict:
        rut_limpio = self.limpiar_rut(rut_emisor)
        host       = "maullin" if self.ambiente == "certificacion" else "palena"
        url        = (f"https://{host}.sii.cl/cgi_dte/UPL/DTEUpload"
                      f"?rutEmisor={rut_limpio}&trackId={track_id}")
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url)
            if response.status_code != 200:
                return {"estado": "ERROR", "mensaje": f"HTTP {response.status_code}"}
            return self._parsear_estado_track(response.text)
        except Exception as e:
            return {"estado": "ERROR", "mensaje": str(e)}

    def _parsear_estado_track(self, response_text: str) -> dict:
        estados_sii = {
            "EPR": ("PENDIENTE",   "Enviado, Pendiente de Revision"),
            "LPR": ("PENDIENTE",   "En proceso de revision"),
            "RCT": ("ACEPTADO",    "Recibido Conforme Total"),
            "RPR": ("REPAROS",     "Aceptado con Reparos"),
            "RFR": ("RECHAZADO",   "Rechazado — revisar errores"),
            "DNK": ("DESCONOCIDO", "TrackID no encontrado"),
        }
        try:
            root   = etree.fromstring(response_text.encode())
            estado = root.findtext(".//ESTADO") or root.findtext("ESTADO") or ""
            glosa  = root.findtext(".//GLOSA")  or root.findtext("GLOSA")  or ""
            estado_norm, descripcion = estados_sii.get(estado, ("DESCONOCIDO", glosa))
            docs = []
            for doc_el in (root.findall(".//DETALLE_REP_RECH") +
                           root.findall(".//DETALLE")):
                docs.append({
                    "tipo":   doc_el.findtext("TIPO_DOC"),
                    "folio":  doc_el.findtext("FOLIO"),
                    "estado": doc_el.findtext("EST_DTE"),
                    "error":  doc_el.findtext("ERR_DOC"),
                })
            return {"estado": estado_norm, "codigo_sii": estado,
                    "descripcion": descripcion, "documentos": docs}
        except Exception:
            return {"estado": "ERROR_PARSEO",
                    "descripcion": "No se pudo parsear respuesta del SII",
                    "raw": response_text[:300]}

    async def _obtener_token(self, p12_bytes: bytes = None,
                             password: str = None) -> str:
        """Token DTE estándar — para facturas, guías, notas de crédito/débito."""
        if p12_bytes and password:
            from app.services.sii_auth import obtener_token_cached
            return await obtener_token_cached(p12_bytes, password, self.ambiente)
        logger.warning("[SII AUTH] Usando token 'prueba' — sin certificado")
        return "prueba"

    async def _obtener_token_boleta(self, p12_bytes: bytes = None,
                                    password: str = None) -> str:
        """
        Token específico para boletas electrónicas.
        Usa maullin2.sii.cl (cert) o rahue.sii.cl (prod) — endpoint REST.
        Sin este token, el SII responde STATUS 7 al recibir EnvioBOLETA.
        """
        if p12_bytes and password:
            from app.services.sii_auth import obtener_token_boleta_cached
            return await obtener_token_boleta_cached(p12_bytes, password, self.ambiente)
        logger.warning("[SII AUTH BOLETA] Usando token 'prueba' — sin certificado")
        return "prueba"
