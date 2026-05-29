# app/services/sii_sender.py
# ══════════════════════════════════════════════════════════════
# Servicio de envio al SII — v2.0
#
# FIX CRÍTICO: re-firma cada DTE DESPUÉS de insertarlo en el
# árbol del EnvioDTE. El DigestValue debe calcularse en el
# contexto del sobre para que el SII pueda verificarlo.
# ══════════════════════════════════════════════════════════════

import logging
import httpx
from app.services.http_client import get_sii_client
from lxml import etree
from datetime import datetime, timezone, timedelta
from app.core.config import settings

logger = logging.getLogger("yepardtecore.dte")

SII_UPLOAD_CERT = "https://maullin.sii.cl/cgi_dte/UPL/DTEUpload"
SII_UPLOAD_PROD = "https://palena.sii.cl/cgi_dte/UPL/DTEUpload"

# Boletas electrónicas usan servidor REST distinto (Instructivo Técnico Boleta 2021)
SII_BOLETA_UPLOAD_CERT = "https://maullin2.sii.cl/boleta.electronica.DTE/ws/ingresarEnvioBOLETA"
SII_BOLETA_UPLOAD_PROD = "https://rahue.sii.cl/boleta.electronica.DTE/ws/ingresarEnvioBOLETA"

# Token semilla para boletas (endpoint REST distinto)
SII_BOLETA_SEMILLA_CERT = "https://maullin2.sii.cl/boleta.electronica.DTE/ws/getEstadoEnvio"
SII_BOLETA_TOKEN_CERT   = "https://maullin2.sii.cl/boleta.electronica.DTE/ws/getToken"
SII_BOLETA_SEMILLA_PROD = "https://rahue.sii.cl/boleta.electronica.DTE/ws/getEstadoEnvio"
SII_BOLETA_TOKEN_PROD   = "https://rahue.sii.cl/boleta.electronica.DTE/ws/getToken"
SII_NS          = "http://www.sii.cl/SiiDte"
XSI_NS          = "http://www.w3.org/2001/XMLSchema-instance"
XMLDSIG_NS      = "http://www.w3.org/2000/09/xmldsig#"
TIPOS_BOLETA    = {39, 41}


class SIISender:

    def __init__(self, ambiente: str = "certificacion"):
        self.ambiente   = ambiente
        self.url_upload      = SII_UPLOAD_CERT if ambiente == "certificacion" else SII_UPLOAD_PROD
        self.url_upload_bol  = SII_BOLETA_UPLOAD_CERT if ambiente == "certificacion" else SII_BOLETA_UPLOAD_PROD
        self.url_token_bol   = SII_BOLETA_TOKEN_CERT  if ambiente == "certificacion" else SII_BOLETA_TOKEN_PROD
        self.url_semilla_bol = SII_BOLETA_SEMILLA_CERT if ambiente == "certificacion" else SII_BOLETA_SEMILLA_PROD

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
        Construye el sobre EnvioDTE SIN firmar y delega a Java
        para que firme todos los DTEs y el SetDTE dentro del contexto DOM correcto.
        """
        NS    = SII_NS
        ahora = datetime.now(timezone.utc)

        rut_emisor   = self.limpiar_rut(rut_emisor)
        rut_enviador = self.limpiar_rut(rut_enviador)

        # Detectar tipos de DTE
        # Detectar tipos con regex para evitar problemas de encoding
        import re as _re
        tipos_en_sobre: dict[int, int] = {}
        for dte_xml in dtes_xml:
            m = _re.search(r'<TipoDTE>(\d+)</TipoDTE>', dte_xml)
            if m:
                t = int(m.group(1))
                tipos_en_sobre[t] = tipos_en_sobre.get(t, 0) + 1

        es_boleta = bool(tipos_en_sobre) and all(t in TIPOS_BOLETA for t in tipos_en_sobre)
        if es_boleta:
            tag         = "EnvioBOLETA"
            schema_name = "EnvioBOLETA_v11.xsd"
        else:
            tag         = "EnvioDTE"
            schema_name = "EnvioDTE_v10.xsd"

        # Resolución del contribuyente — misma para boletas y facturas
        fch_resol = getattr(self, 'fch_resol', '2026-04-19')
        nro_resol = getattr(self, 'nro_resol', '0')
        tmst      = (ahora + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S")

        # Construir SubTotDTE
        subtot = "".join(
            f"<SubTotDTE><TpoDTE>{t}</TpoDTE><NroDTE>{c}</NroDTE></SubTotDTE>"
            for t, c in sorted(tipos_en_sobre.items())
        )

        # Construir DTEs como strings (sin XML declaration)
        dtes_str = []
        for dte_xml in dtes_xml:
            s = dte_xml
            if s.startswith('<?xml'):
                s = s[s.index('?>') + 2:].lstrip()
            dtes_str.append(s.strip())

        # Construir sobre como STRING puro sin firmas
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
        sobre_sin_firmas = (
            f'<?xml version="1.0" encoding="ISO-8859-1"?>\n'
            f'<{tag} xmlns="{NS}" xmlns:xsi="{XSI_NS}" version="1.0" '
            f'xsi:schemaLocation="{NS} {schema_name}">'
            f'{set_str}'
            f'</{tag}>'
        )

        # Java firma todos los DTEs y el SetDTE dentro del árbol completo
        return await firma_service.firmar_sobre(sobre_sin_firmas)


    async def enviar_sobre(self, sobre_xml: str, rut_emisor: str,
                           rut_enviador: str,
                           p12_bytes: bytes = None,
                           password: str = None,
                           auth_p12_bytes: bytes = None,
                           auth_password: str = None) -> dict:
        # Determinar si es envío de boletas (URL y token distintos)
        es_envio_boleta = sobre_xml.strip().find("EnvioBOLETA") > 0 or "EnvioBOLETA" in sobre_xml[:500]

        token_p12 = auth_p12_bytes or p12_bytes
        token_pwd = auth_password or password

        if es_envio_boleta:
            token = await self._obtener_token_boleta(token_p12, token_pwd)
            url_envio = self.url_upload_bol
        else:
            token = await self._obtener_token(token_p12, token_pwd)
            url_envio = self.url_upload

        rut_limpio = self.limpiar_rut(rut_emisor)
        env_limpio = self.limpiar_rut(rut_enviador)
        timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
        nombre     = f"{rut_limpio}_{timestamp}.xml"
        sobre_bytes= sobre_xml.encode("ISO-8859-1")

        headers = {
            "User-Agent": "Mozilla/4.0 (compatible; PROG 1.0; Windows NT 5.0; YeparDTEcore)",
            "Cookie":     f"TOKEN={token}",
        }
        files = {
            "rutSender":  (None, env_limpio),
            "rutCompany": (None, rut_limpio),
            "archivo":    (nombre, sobre_bytes, "text/xml;charset=ISO-8859-1"),
        }

        logger.info(f"[SII ENVIO] {'BOLETA' if es_envio_boleta else 'DTE'} rutSender={env_limpio} rutCompany={rut_limpio} token={token[:8]}...")
        logger.info(f"[SII ENVIO] url={url_envio}")
        logger.info(f"[SII ENVIO] sobre_bytes_len={len(sobre_bytes)}")

        try:
            async with get_sii_client(timeout=30.0) as client:
                response = await client.post(url_envio, headers=headers, files=files)

            logger.info(f"[SII RAW] HTTP={response.status_code} body={response.text[:2000]}")

            if response.status_code != 200:
                return {"track_id": None, "estado": "ERROR_HTTP",
                        "mensaje": f"SII respondio HTTP {response.status_code}",
                        "raw": response.text[:500]}

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
            async with get_sii_client(timeout=15.0) as client:
                response = await client.get(url)
            if response.status_code != 200:
                return {"estado": "ERROR", "mensaje": f"HTTP {response.status_code}"}
            return self._parsear_estado_track(response.text)
        except Exception as e:
            return {"estado": "ERROR", "mensaje": str(e)}

    def _parsear_estado_track(self, response_text: str) -> dict:
        estados_sii = {
            "EPR": ("PENDIENTE",  "Enviado, Pendiente de Revision"),
            "LPR": ("PENDIENTE",  "En proceso de revision"),
            "RCT": ("ACEPTADO",   "Recibido Conforme Total"),
            "RPR": ("REPAROS",    "Aceptado con Reparos"),
            "RFR": ("RECHAZADO",  "Rechazado — revisar errores"),
            "DNK": ("DESCONOCIDO","TrackID no encontrado"),
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
        if p12_bytes and password:
            from app.services.sii_auth import obtener_token_cached
            return await obtener_token_cached(p12_bytes, password, self.ambiente)
        logger.warning("[SII AUTH] Usando token 'prueba' — sin certificado")
        return "prueba"

    async def _obtener_token_boleta(self, p12_bytes: bytes = None,
                                    password: str = None) -> str:
        """
        Token para boletas electrónicas — usa endpoints REST distintos.
        Flujo: GET semilla → firmar → POST token (URLs maullin2/rahue).
        Si falla, cae de vuelta al token DTE normal.
        """
        if p12_bytes and password:
            from app.services.sii_auth import obtener_token_boleta_cached
            try:
                return await obtener_token_boleta_cached(p12_bytes, password, self.ambiente)
            except Exception as e:
                logger.warning(f"[SII AUTH BOLETA] falló ({e}) — usando token DTE como fallback")
                from app.services.sii_auth import obtener_token_cached
                return await obtener_token_cached(p12_bytes, password, self.ambiente)
        logger.warning("[SII AUTH BOLETA] Usando token 'prueba' — sin certificado")
        return "prueba"
