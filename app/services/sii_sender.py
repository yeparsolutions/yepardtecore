# app/services/sii_sender.py
# ══════════════════════════════════════════════════════════════
# Servicio de envio al SII — v3.0 (flujo AppDTE)
#
# CAMBIO CRÍTICO v3.0:
#   El sobre se construye con los DTEs ya firmados (tal como
#   llegan de la BD). NO se re-firman dentro del sobre.
#
# Analogía: si cada carta ya tiene su propio sello notarial,
# meterlas en un sobre NO invalida esos sellos. El sobre
# obtiene SU PROPIO sello por separado.
#
# Flujo correcto (replicando AppDTE SDK):
#   1. Cada DTE fue firmado por FirmaDigital.firmar_dte()
#      usando AppDTE → firma XMLDSig standalone sobre Documento
#   2. construir_sobre() ensambla los DTEs firmados SIN tocar
#      sus firmas, agrega la carátula
#   3. FirmaDigital.firmar_sobre() firma el SetDTE vía AppDTE
#   4. enviar_sobre() envía al SII con token de autenticación
# ══════════════════════════════════════════════════════════════

import logging
import httpx
from lxml import etree
from datetime import datetime, timezone, timedelta
from app.core.config import settings

logger = logging.getLogger("yepardtecore.dte")

SII_UPLOAD_CERT = "https://maullin.sii.cl/cgi_dte/UPL/DTEUpload"
SII_UPLOAD_PROD = "https://palena.sii.cl/cgi_dte/UPL/DTEUpload"
SII_NS          = "http://www.sii.cl/SiiDte"
XSI_NS          = "http://www.w3.org/2001/XMLSchema-instance"
XMLDSIG_NS      = "http://www.w3.org/2000/09/xmldsig#"
TIPOS_BOLETA    = {39, 41}


class SIISender:

    def __init__(self, ambiente: str = "certificacion"):
        self.ambiente   = ambiente
        self.url_upload = SII_UPLOAD_CERT if ambiente == "certificacion" else SII_UPLOAD_PROD

    @staticmethod
    def limpiar_rut(rut: str) -> str:
        """Elimina puntos y normaliza el RUT para envío al SII."""
        rut    = rut.replace(".", "").strip()
        partes = rut.split("-")
        if len(partes) > 2:
            rut = partes[0] + "-" + partes[1]
        return rut

    async def construir_sobre(
        self,
        dtes_xml:     list[str],
        rut_emisor:   str,
        rut_enviador: str,
        firma_service,
    ) -> str:
        """
        Construye el EnvioDTE con los DTEs ya firmados y firma el sobre.

        IMPORTANTE: Los DTEs llegan pre-firmados por AppDTE (firma XMLDSig
        standalone sobre el nodo Documento). Se insertan tal como están.
        NO se re-firma dentro del árbol del sobre.

        Flujo:
          1. Detectar tipos de DTE para armar carátula
          2. Insertar cada DTE firmado en el SetDTE
          3. Serializar el sobre SIN firma
          4. Llamar a firma_service.firmar_sobre() → AppDTE firma SetDTE

        Args:
            dtes_xml:     Lista de XML firmados (strings ISO-8859-1)
            rut_emisor:   RUT del emisor
            rut_enviador: RUT del usuario que envía (del certificado)
            firma_service: Instancia de FirmaDigital

        Returns:
            EnvioDTE firmado (string con declaración XML ISO-8859-1)
        """
        NS    = SII_NS
        ahora = datetime.now(timezone.utc)

        rut_emisor   = self.limpiar_rut(rut_emisor)
        rut_enviador = self.limpiar_rut(rut_enviador)

        # ── Detectar tipos de DTE para SubTotDTE en carátula ──────────
        tipos_en_sobre: dict[int, int] = {}
        for dte_xml in dtes_xml:
            try:
                dte_str = dte_xml
                if dte_str.startswith("<?xml"):
                    dte_str = dte_str[dte_str.index("?>") + 2:].lstrip()
                dte_root = etree.fromstring(dte_str.encode("iso-8859-1"))
                tipo_el  = dte_root.find(f".//{{{NS}}}TipoDTE")
                if tipo_el is not None:
                    t = int(tipo_el.text)
                    tipos_en_sobre[t] = tipos_en_sobre.get(t, 0) + 1
            except Exception:
                pass

        # ── Seleccionar tipo de sobre (EnvioDTE vs EnvioBOLETA) ───────
        es_boleta = all(t in TIPOS_BOLETA for t in tipos_en_sobre)
        if es_boleta:
            root_tag = f"{{{NS}}}EnvioBOLETA"
            schema   = f"{NS} EnvioBOLETA_v11.xsd"
        else:
            root_tag = f"{{{NS}}}EnvioDTE"
            schema   = f"{NS} EnvioDTE_v10.xsd"

        # ── Construir árbol del sobre ─────────────────────────────────
        nsmap    = {None: NS, "xsi": XSI_NS}
        envio_el = etree.Element(root_tag, attrib={
            f"{{{XSI_NS}}}schemaLocation": schema,
            "version": "1.0",
        }, nsmap=nsmap)

        set_el = etree.SubElement(envio_el, f"{{{NS}}}SetDTE",
                                  attrib={"ID": "SetDoc"})

        # Carátula — datos del envío
        caratula = etree.SubElement(set_el, f"{{{NS}}}Caratula",
                                    attrib={"version": "1.0"})
        etree.SubElement(caratula, f"{{{NS}}}RutEmisor").text   = rut_emisor
        etree.SubElement(caratula, f"{{{NS}}}RutEnvia").text    = rut_enviador
        etree.SubElement(caratula, f"{{{NS}}}RutReceptor").text = "60803000-K"

        fch_resol = getattr(self, "fch_resol", "2026-04-19")
        nro_resol = getattr(self, "nro_resol", "0")
        etree.SubElement(caratula, f"{{{NS}}}FchResol").text     = fch_resol
        etree.SubElement(caratula, f"{{{NS}}}NroResol").text     = nro_resol
        etree.SubElement(caratula, f"{{{NS}}}TmstFirmaEnv").text = (
            (ahora + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S")
        )

        # SubTotDTE: resumen de cantidades por tipo
        for tipo, cantidad in tipos_en_sobre.items():
            subtot = etree.SubElement(caratula, f"{{{NS}}}SubTotDTE")
            etree.SubElement(subtot, f"{{{NS}}}TpoDTE").text = str(tipo)
            etree.SubElement(subtot, f"{{{NS}}}NroDTE").text = str(cantidad)

        # ── Insertar DTEs pre-firmados tal como están ─────────────────
        # NO se re-firma aquí. Cada DTE ya tiene su Signature XMLDSig
        # calculada standalone por AppDTE. Remover/re-firmar la rompería.
        parser = etree.XMLParser(remove_blank_text=True)
        for i, dte_xml in enumerate(dtes_xml):
            try:
                dte_str2 = dte_xml
                if dte_str2.startswith("<?xml"):
                    dte_str2 = dte_str2[dte_str2.index("?>") + 2:].lstrip()

                # Parsear el DTE (con su Signature) y agregarlo al sobre
                dte_el = etree.fromstring(
                    dte_str2.encode("iso-8859-1"), parser
                )
                if i < len(dtes_xml) - 1:
                    dte_el.tail = "\n"
                set_el.append(dte_el)

            except Exception as e:
                raise ValueError(f"DTE XML inválido (índice {i}): {e}")

        # ── Serializar sobre sin firma ─────────────────────────────────
        sobre_sin_firma = etree.tostring(envio_el, encoding="unicode")

        # ── Firmar SetDTE vía AppDTE ───────────────────────────────────
        return await firma_service.firmar_sobre(sobre_sin_firma)

    async def enviar_sobre(
        self,
        sobre_xml:     str,
        rut_emisor:    str,
        rut_enviador:  str,
        p12_bytes:     bytes = None,
        password:      str   = None,
        auth_p12_bytes: bytes = None,
        auth_password:  str  = None,
    ) -> dict:
        """
        Envía el EnvioDTE firmado al SII.

        Obtiene el token de autenticación usando el certificado digital
        y envía el sobre via multipart al endpoint del SII.
        """
        token      = await self._obtener_token(p12_bytes, password,
                                               auth_p12_bytes, auth_password)
        rut_limpio = self.limpiar_rut(rut_emisor)
        env_limpio = self.limpiar_rut(rut_enviador)
        timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
        nombre     = f"{rut_limpio}_{timestamp}.xml"
        sobre_bytes = sobre_xml.encode("ISO-8859-1")

        headers = {
            "User-Agent": "Mozilla/4.0 (compatible; PROG 1.0; Windows NT 5.0; YeparDTEcore)",
            "Cookie":     f"TOKEN={token}",
        }
        files = {
            "rutSender":  (None, env_limpio),
            "rutCompany": (None, rut_limpio),
            "archivo":    (nombre, sobre_bytes, "text/xml;charset=ISO-8859-1"),
        }

        logger.info(f"[SII ENVIO] rutSender={env_limpio} rutCompany={rut_limpio}")

        try:
            logger.info(f"[SII UPLOAD] Enviando a {self.url_upload}")
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(self.url_upload, headers=headers, files=files)

            logger.info(f"[SII RAW] HTTP={response.status_code} body={response.text[:500]}")

            if response.status_code != 200:
                return {
                    "track_id": None,
                    "estado":   "ERROR_HTTP",
                    "mensaje":  f"SII respondió HTTP {response.status_code}",
                    "raw":      response.text[:500],
                }
            return self._parsear_respuesta_upload(response.text)

        except httpx.TimeoutException:
            logger.error("[SII UPLOAD] TIMEOUT — SII no respondió en 60 segundos")
            return {"track_id": None, "estado": "TIMEOUT",
                    "mensaje":  "El SII no respondió en 60 segundos"}
        except httpx.RemoteProtocolError as e:
            logger.error(f"[SII UPLOAD] RemoteProtocolError: {e}")
            return {"track_id": None, "estado": "ERROR",
                    "mensaje":  f"Server disconnected: {e}"}
        except Exception as e:
            logger.error(f"[SII UPLOAD] Exception: {type(e).__name__}: {e}")
            return {"track_id": None, "estado": "ERROR", "mensaje": str(e)}

    def _parsear_respuesta_upload(self, response_text: str) -> dict:
        """Parsea la respuesta XML del SII al subir el sobre."""
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
                "1":  "Error de autenticación — token inválido",
                "2":  "Error en el XML del sobre",
                "3":  "RUT del emisor no coincide",
                "5":  "No autorizado para este RUT",
                "10": "RUT no autorizado para enviar DTE",
                "11": "CAF no corresponde a este emisor",
                "12": "CAF vencido",
                "13": "Folio fuera de rango del CAF",
                "-1": "Error de autenticación — token inválido",
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
        """Consulta el estado de un envío por TrackID."""
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
        """Parsea la respuesta de consulta de estado por TrackID."""
        estados_sii = {
            "EPR": ("PENDIENTE",   "Enviado, Pendiente de Revisión"),
            "LPR": ("PENDIENTE",   "En proceso de revisión"),
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

    async def _obtener_token(
        self,
        p12_bytes:      bytes = None,
        password:       str   = None,
        auth_p12_bytes: bytes = None,
        auth_password:  str   = None,
    ) -> str:
        """Obtiene el token de autenticación del SII."""
        if p12_bytes and password:
            from app.services.sii_auth import obtener_token_cached
            return await obtener_token_cached(
                p12_bytes, password, self.ambiente,
                auth_p12_bytes=auth_p12_bytes,
                auth_password=auth_password,
            )
        logger.warning("[SII AUTH] Sin certificado — usando token 'prueba'")
        return "prueba"
