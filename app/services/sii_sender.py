# app/services/sii_sender.py
# ══════════════════════════════════════════════════════════════
# Servicio de envio al SII
#
# ── FIXES v1.3 ─────────────────────────────────────────────
# - EnvioBOLETA incluye xmlns:xsi y xsi:schemaLocation
#   El DTE ya trae xsi desde xml_builder, por lo que al
#   insertar en el sobre el namespace NO hereda nuevamente
#   y los digests se mantienen intactos.
# - STATUS (no ESTADO) para parsear respuesta del SII
# ══════════════════════════════════════════════════════════════

import logging
import httpx
from lxml import etree
from datetime import datetime, timezone
from app.core.config import settings

logger = logging.getLogger("yepardtecore.dte")

SII_UPLOAD_CERT = "https://maullin.sii.cl/cgi_dte/UPL/DTEUpload"
SII_UPLOAD_PROD = "https://palena.sii.cl/cgi_dte/UPL/DTEUpload"
SII_NS          = "http://www.sii.cl/SiiDte"
XSI_NS          = "http://www.w3.org/2001/XMLSchema-instance"
TIPOS_BOLETA    = {39, 41}


class SIISender:

    def __init__(self, ambiente: str = "certificacion"):
        self.ambiente   = ambiente
        self.url_upload = SII_UPLOAD_CERT if ambiente == "certificacion" else SII_UPLOAD_PROD

    @staticmethod
    def limpiar_rut(rut: str) -> str:
        rut    = rut.replace(".", "").strip()
        partes = rut.split("-")
        if len(partes) > 2:
            rut = partes[0] + "-" + partes[1]
        return rut

    def construir_sobre(self, dtes_xml: list[str], rut_emisor: str,
                        rut_enviador: str, firma_service) -> str:
        """
        Construye y firma el sobre EnvioBOLETA o EnvioDTE.

        ⚠️  xsi:schemaLocation se incluye en el root.
            Esto funciona porque los DTEs YA traen xmlns:xsi
            desde xml_builder, entonces al hacer C14N el
            namespace no es "nuevo" en los hijos y el digest
            no cambia.
        """
        NS    = SII_NS
        ahora = datetime.now(timezone.utc)

        rut_emisor   = self.limpiar_rut(rut_emisor)
        rut_enviador = self.limpiar_rut(rut_enviador)

        # Detectar tipos
        tipos_en_sobre: dict[int, int] = {}
        for dte_xml in dtes_xml:
            try:
                # Pasar el string Unicode directamente a lxml (evita conflictos de encoding)
                dte_str = dte_xml
                if dte_str.startswith('<?xml'):
                    dte_str = dte_str[dte_str.index('?>') + 2:].lstrip()
                dte_root = etree.fromstring(dte_str)
                tipo_el  = dte_root.find(f".//{{{NS}}}TipoDTE")
                if tipo_el is not None:
                    t = int(tipo_el.text)
                    tipos_en_sobre[t] = tipos_en_sobre.get(t, 0) + 1
            except Exception:
                pass

        es_boleta = all(t in TIPOS_BOLETA for t in tipos_en_sobre)
        if es_boleta:
            root_tag = f"{{{NS}}}EnvioBOLETA"
            schema   = f"{NS} EnvioBOLETA_v11.xsd"
        else:
            root_tag = f"{{{NS}}}EnvioDTE"
            schema   = f"{NS} EnvioDTE_v10.xsd"

        nsmap    = {None: NS, "xsi": XSI_NS}
        # xsi:schemaLocation DEBE ir ANTES de version — el validador SII lo lee primero
        envio_el = etree.Element(root_tag, attrib={
            f"{{{XSI_NS}}}schemaLocation": schema,
            "version": "1.0",
        }, nsmap=nsmap)

        set_el = etree.SubElement(envio_el, f"{{{NS}}}SetDTE",
                                  attrib={"ID": "SetDoc"})

        caratula = etree.SubElement(set_el, f"{{{NS}}}Caratula",
                                    attrib={"version": "1.0"})
        etree.SubElement(caratula, f"{{{NS}}}RutEmisor").text   = rut_emisor
        etree.SubElement(caratula, f"{{{NS}}}RutEnvia").text    = rut_enviador
        etree.SubElement(caratula, f"{{{NS}}}RutReceptor").text = "60803000-K"

        # FchResol y NroResol deben coincidir con la resolucion real del emisor.
        # Por defecto: NroResol=0 para certificacion (valor exigido por el SII).
        # En produccion se debe configurar segun la resolucion de autorizacion del emisor.
        # FechResol: para certificacion SII exige '2014-08-22' (resolucion estandar moderna).
        # BUG PREVIO: '2003-09-02' no es reconocida por SII moderno -> SCH-00001
        # Para produccion configurar con la fecha de resolucion real del emisor.
        fch_resol = getattr(self, 'fch_resol', '2014-08-22')
        nro_resol = getattr(self, 'nro_resol', '0')
        etree.SubElement(caratula, f"{{{NS}}}FchResol").text     = fch_resol
        etree.SubElement(caratula, f"{{{NS}}}NroResol").text     = nro_resol
        etree.SubElement(caratula, f"{{{NS}}}TmstFirmaEnv").text = (
            ahora.strftime("%Y-%m-%dT%H:%M:%S")
        )

        for tipo, cantidad in tipos_en_sobre.items():
            subtot = etree.SubElement(caratula, f"{{{NS}}}SubTotDTE")
            etree.SubElement(subtot, f"{{{NS}}}TpoDTE").text = str(tipo)
            etree.SubElement(subtot, f"{{{NS}}}NroDTE").text = str(cantidad)

        for i, dte_xml in enumerate(dtes_xml):
            try:
                parser = etree.XMLParser(remove_blank_text=True)
                # Pasar string Unicode directamente (lxml lo acepta sin problemas de encoding)
                dte_str2 = dte_xml
                if dte_str2.startswith('<?xml'):
                    dte_str2 = dte_str2[dte_str2.index('?>') + 2:].lstrip()
                dte_el = etree.fromstring(dte_str2, parser)
                # Agregar newline: tail del DTE anterior apunta al texto
                # que va entre </DTE anterior> y <DTE siguiente>
                if i < len(dtes_xml) - 1:
                    dte_el.tail = "\n"
                set_el.append(dte_el)
            except Exception as e:
                raise ValueError(f"DTE XML invalido: {e}")

        sobre_sin_firma = etree.tostring(envio_el, encoding="unicode")
        return firma_service.firmar_sobre(sobre_sin_firma)

    async def enviar_sobre(self, sobre_xml: str, rut_emisor: str,
                           rut_enviador: str,
                           p12_bytes: bytes = None,
                           password: str = None) -> dict:
        token      = await self._obtener_token(p12_bytes, password)
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

        logger.info(f"[SII ENVIO] rutSender={env_limpio} rutCompany={rut_limpio}")

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(self.url_upload, headers=headers, files=files)

            logger.info(f"[SII RAW] HTTP={response.status_code} body={response.text[:500]}")

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
            async with httpx.AsyncClient(timeout=15.0) as client:
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
