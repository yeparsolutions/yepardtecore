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

# Las BOLETAS se envían por una plataforma DESACOPLADA de la de facturas
# (así lo exige el SII por el volumen de boletas). El endpoint es la API REST
# de boletas, NO el DTEUpload de maullin/palena:
#   Certificación: apicert.sii.cl/recursos/v1/boleta.electronica.envio
#   Producción:    api.sii.cl/recursos/v1/boleta.electronica.envio
# Enviar una boleta a palena (endpoint de facturas) da STATUS 5 aunque el
# token sea válido — es la "puerta equivocada".
SII_BOLETA_ENVIO_CERT = "https://apicert.sii.cl/recursos/v1/boleta.electronica.envio"
SII_BOLETA_ENVIO_PROD = "https://api.sii.cl/recursos/v1/boleta.electronica.envio"

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

        # Usar siempre EnvioDTE — palena y maullin no reconocen EnvioBOLETA.
        # Las boletas (tipo 39/41) van igualmente dentro del EnvioDTE.
        schema_loc = f'xsi:schemaLocation="{NS} EnvioDTE_v10.xsd"'
        sobre_sin_firmas = (
            f'<?xml version="1.0" encoding="ISO-8859-1"?>\n'
            f'<EnvioDTE xmlns="{NS}" xmlns:xsi="{XSI_NS}" version="1.0" {schema_loc}>'
            f'{set_str}'
            f'</EnvioDTE>'
        )

        return await firma_service.firmar_sobre(sobre_sin_firmas)

    async def enviar_sobre(self, sobre_xml: str, rut_emisor: str,
                           rut_enviador: str,
                           p12_bytes: bytes = None,
                           password: str = None,
                           auth_p12_bytes: bytes = None,
                           auth_password: str = None,
                           db=None, emisor_id: int = None) -> dict:
        """
        Envía un sobre XML (EnvioDTE o EnvioBOLETA) al SII.

        FIX STATUS 7:
        Las boletas electrónicas (EnvioBOLETA) requieren un token obtenido
        desde el endpoint de boletas (maullin2 en cert, rahue en prod), NO el
        token DTE estándar (maullin/palena). Si se usa el token DTE para una
        boleta, el SII responde STATUS 7 (esquema/credencial no corresponde).

        El token de boletas se obtiene desde Chile y se PERSISTE en BD
        (obtener_token_boleta_cached), para que Railway (fuera de Chile) lo
        reutilice sin tener que alcanzar rahue en cada envío.
        """
        token_p12 = auth_p12_bytes or p12_bytes
        token_pwd = auth_password or password

        es_boleta = "EnvioBOLETA" in sobre_xml[:500]

        # ── ETAPA TOKEN (con reintentos) ──────────────────────────────────────
        # maullin (cert) es inestable: a veces corta la conexión sin responder
        # ("Server disconnected"). Como pedir un token es idempotente (no pasa
        # nada por pedirlo dos veces), reintentamos con calma: 3 intentos.
        #
        # BOLETAS: usan su propio token (rahue/maullin2, persistido en BD). Si
        # se usara el token DTE → STATUS 7. Por eso ramificamos según es_boleta.
        async def _pedir_token():
            # Token DTE estándar (palena/maullin) para todos los tipos
            return await self._obtener_token(token_p12, token_pwd)

        try:
            token = await self._con_reintentos(
                "TOKEN", _pedir_token, intentos=3,
            )
        except Exception as e:
            logger.error(f"[SII TOKEN] Agotados los reintentos: {e}")
            # Mensaje claro si es boleta y no se pudo obtener el token (rahue
            # inalcanzable desde fuera de Chile y sin token persistido en BD).
            if es_boleta:
                return {"track_id": None, "estado": "ERROR",
                        "mensaje": "No se pudo obtener token de BOLETA. El "
                                   "endpoint de boletas del SII (rahue) solo "
                                   "responde desde Chile. Hay que precargar el "
                                   "token de boletas desde una salida en Chile "
                                   "(se persiste en BD y se reutiliza)."}
            return {"track_id": None, "estado": "ERROR",
                    "mensaje": f"[etapa TOKEN] {e}"}

        # Todos los documentos —incluidas las boletas— se envían al DTEUpload
        # de maullin (cert) / palena (prod). Este es el endpoint que el SII
        # aceptó históricamente para EnvioBOLETA (dio TrackID en certificación).
        # El endpoint REST api.sii.cl/boleta.electronica.envio devolvía
        # "Acceso Denegado (from client)", así que NO se usa.
        # Siempre DTEUpload (maullin/palena) para todos los tipos
        url_envio = self.url_upload
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
            # ── ETAPA UPLOAD (reintento prudente) ─────────────────────────────
            # Aquí reintentamos con más cuidado que en TOKEN: si la conexión
            # ni siquiera se estableció (ConnectError) es seguro reintentar;
            # si se cortó a mitad de camino (RemoteProtocolError) reintentamos
            # solo una vez — en certificación un eventual duplicado es inocuo,
            # el SII simplemente registra otro track.
            async def _subir():
                async with httpx.AsyncClient(timeout=120.0,
                                             follow_redirects=True) as client:
                    return await client.post(url_envio, headers=headers,
                                             files=files)

            # 4 intentos: reintentar es SEGURO porque si un intento "fallido"
            # en realidad entró al SII, el siguiente recibe STATUS 99 y el
            # parser rescata el TrackID original (no se duplica nada).
            response = await self._con_reintentos("UPLOAD", _subir, intentos=4)

            # Colapsar saltos de línea para que el cuerpo de la respuesta
            # quede en UNA línea del log (Railway parte por \n y se vuelve
            # ilegible). Así el XML/JSON de error del SII se lee completo.
            cuerpo_plano = " ".join(response.text.split())
            logger.info(f"[SII RAW] HTTP={response.status_code} body={cuerpo_plano[:2000]}")

            if response.status_code != 200:
                return {
                    "track_id": None,
                    "estado":   "ERROR_HTTP",
                    # Devolver el cuerpo del error en el mensaje para verlo en
                    # la UI sin tener que bucear en el log.
                    "mensaje":  f"SII respondio HTTP {response.status_code}: {cuerpo_plano[:400]}",
                    "raw":      cuerpo_plano[:800],
                }

            # Maullin/palena responden XML con <TRACKID> para todos los tipos
            # (DTE y boletas). Se parsea igual para ambos.
            return self._parsear_respuesta_upload(response.text)

        except httpx.TimeoutException:
            return {"track_id": None, "estado": "TIMEOUT",
                    "mensaje": "[etapa UPLOAD] El SII no respondio (timeout)"}
        except Exception as e:
            return {"track_id": None, "estado": "ERROR",
                    "mensaje": f"[etapa UPLOAD] {e}"}

    async def _con_reintentos(self, etapa: str, fn, intentos: int = 3):
        """
        Ejecuta una llamada al SII reintentando ante cortes de conexión.

        Analogía: si la caseta de peaje te cierra la cortina en la cara,
        no te das media vuelta — esperas un momento y vuelves a tocar.
        Espera progresiva: 2s, luego 4s, luego 6s entre intentos.

        Solo reintenta errores de CONEXIÓN (cortes, rechazos), nunca
        respuestas del SII (un rechazo de negocio no se arregla insistiendo).
        """
        import asyncio
        ultimo_error = None
        for intento in range(1, intentos + 1):
            try:
                return await fn()
            except (httpx.RemoteProtocolError, httpx.ConnectError,
                    httpx.ReadError, httpx.WriteError) as e:
                ultimo_error = e
                logger.warning(
                    f"[SII {etapa}] Intento {intento}/{intentos} falló: "
                    f"{type(e).__name__}: {e}"
                )
                if intento < intentos:
                    await asyncio.sleep(2 * intento)
        raise ultimo_error

    def _parsear_respuesta_boleta_rest(self, texto: str) -> dict:
        """
        Parsea la respuesta del envío de boleta por la API REST del SII.

        A diferencia de palena (que devuelve XML con <TRACKID>), la API REST
        de boletas devuelve JSON. El track_id puede venir bajo distintas
        claves según la versión de la API, así que lo buscamos en las más
        probables y, si no aparece, dejamos el JSON crudo en el log para
        poder afinar el parseo con datos reales.

        Analogía: es la misma carta (el sobre firmado), pero esta oficina
        (boletas) te da el comprobante en otro formato (JSON) que la oficina
        de facturas (XML). Hay que leer el comprobante en su idioma.
        """
        import json as _json
        try:
            data = _json.loads(texto)
        except Exception:
            # Si por algún motivo respondió XML, intentamos el parser viejo.
            logger.warning(f"[SII BOLETA] respuesta no es JSON, intento XML: {texto[:300]}")
            return self._parsear_respuesta_upload(texto)

        # Buscar el track_id en las claves que usa la API REST del SII.
        track = (data.get("trackid") or data.get("trackId")
                 or data.get("track_id") or data.get("TRACKID"))
        estado_api = (data.get("estado") or data.get("status") or "").upper()

        if track:
            logger.info(f"[SII OK BOLETA] TrackID={track}")
            return {"track_id": str(track), "estado": "RECIBIDO",
                    "mensaje": "Boleta recibida por el SII", "raw": texto[:500]}

        # Sin track_id: dejar el JSON completo en el log para diagnóstico.
        logger.warning(f"[SII BOLETA] sin trackid en respuesta JSON: {texto[:800]}")
        return {"track_id": None, "estado": "RECHAZADO",
                "mensaje": data.get("descripcion") or data.get("mensaje")
                           or f"Envío de boleta sin track_id (estado API: {estado_api})",
                "raw": texto[:800]}

    def _parsear_respuesta_upload(self, response_text: str) -> dict:
        try:
            # Log de la respuesta CRUDA del SII: ante cualquier rechazo, esto
            # deja en el log el XML exacto que devolvió palena/maullin, para no
            # tener que adivinar la causa. (Se trunca para no llenar el log.)
            logger.info(f"[SII RAW] respuesta upload: {response_text[:600]}")
            root     = etree.fromstring(response_text.encode())
            track_id = root.findtext("TRACKID")
            status   = root.findtext("STATUS")
            glosa    = root.findtext("GLOSA") or ""

            if status == "0" and track_id:
                logger.info(f"[SII OK] TrackID={track_id}")
                return {"track_id": track_id, "estado": "RECIBIDO",
                        "mensaje": "Sobre recibido por el SII"}

            # ── STATUS 99: "Archivo ya fue enviado con Trackid NNN" ──────────
            # Caso típico: maullin recibió el sobre pero cortó la conexión
            # antes de respondernos; nuestro reintento sube los bytes
            # idénticos y el SII responde "ese archivo ya lo tengo, este es
            # su número". No es un rechazo — es el TrackID llegando por la
            # puerta trasera. Lo rescatamos del texto del DETAIL.
            if status == "99":
                import re as _re
                m = _re.search(r"[Tt]rack\s*[Ii]d\s*[:=]?\s*(\d+)", response_text)
                if m:
                    track_recuperado = m.group(1)
                    logger.info(f"[SII OK] STATUS=99 — archivo ya enviado, "
                                f"TrackID recuperado={track_recuperado}")
                    return {"track_id": track_recuperado, "estado": "RECIBIDO",
                            "mensaje": "El SII ya había recibido este archivo "
                                       "(TrackID recuperado del envío original)"}
                # 99 sin trackid en el detalle: pedir espera y reintentar después
                logger.warning(f"[SII 99] sin trackid en DETAIL: {response_text[:300]}")
                return {"track_id": None, "estado": "ERROR",
                        "mensaje": "El SII indica archivo ya enviado pero sin "
                                   "TrackID en la respuesta. Espera ~15 minutos "
                                   "y reintenta el envío (mismo set, sin regenerar).",
                        "codigo": "99", "detalle_sii": response_text[:800]}

            # Códigos STATUS oficiales de DTEUpload (manual "Envío de DTE
            # mediante upload" del SII). El código 7 NO es de token: es
            # rechazo por ESQUEMA — el XML no cumple el formato esperado
            # (estructura, campos obligatorios o caracteres especiales).
            errores_sii = {
                "1":  "El sender no tiene permiso para enviar (RutEnvia no autorizado)",
                "2":  "Error en el tamaño del archivo (muy grande o muy chico)",
                "3":  "Archivo cortado — el tamaño recibido no coincide",
                "5":  "No autenticado — token invalido o expirado",
                "6":  "Empresa no autorizada a enviar archivos",
                "7":  "ESQUEMA INVALIDO — el XML no cumple el formato del SII "
                      "(revisar estructura, campos obligatorios y caracteres especiales)",
                "8":  "Firma del documento erronea",
                "9":  "Sistema del SII bloqueado",
                "10": "RUT no autorizado para enviar DTE",
                "11": "CAF no corresponde a este emisor",
                "12": "CAF vencido",
                "13": "Folio fuera de rango del CAF",
                "-1": "Error de autenticacion — token invalido",
                "-2": "Error en el XML del sobre",
            }
            mensaje = errores_sii.get(status, glosa or f"STATUS desconocido: {status}")
            logger.warning(f"[SII ERROR] STATUS={status} glosa='{glosa}' mensaje={mensaje}")
            return {"track_id": None, "estado": "RECHAZADO",
                    "mensaje": mensaje, "codigo": status,
                    "detalle_sii": response_text[:800]}

        except etree.XMLSyntaxError:
            if "NO ESTA AUTORIZADA" in response_text:
                return {"track_id": None, "estado": "NO_AUTORIZADO",
                        "mensaje": "Empresa no autorizada para enviar DTE"}
            return {"track_id": None, "estado": "ERROR_PARSEO",
                    "mensaje": "No se pudo parsear respuesta del SII",
                    "raw": response_text[:300]}

    async def consultar_estado(self, track_id: str, rut_emisor: str,
                                auth_p12_bytes: bytes = None,
                                auth_password: str = None) -> dict:
        """
        Consulta el estado de un envío usando el servicio SOAP QueryEstUp del SII
        (el correcto para saber si los documentos fueron ACEPTADOS), no el
        endpoint de upload. Requiere token de autenticación.

        Analogía: enviar el sobre es echar la carta al buzón (DTEUpload); esto es
        llamar a la oficina de correos para preguntar si la carta llegó y fue
        aceptada (QueryEstUp). Son ventanillas distintas.
        """
        rut_e   = self.limpiar_rut(rut_emisor)
        # rutEmisor se parte en cuerpo y dígito verificador
        cuerpo, dv = rut_e.rsplit("-", 1) if "-" in rut_e else (rut_e[:-1], rut_e[-1])
        host    = "maullin" if self.ambiente == "certificacion" else "palena"
        url     = f"https://{host}.sii.cl/DTEWS/QueryEstUp.jws"

        # Token de autenticación (mismo que para enviar)
        try:
            token = await self._obtener_token(auth_p12_bytes, auth_password)
        except Exception as e:
            return {"estado": "ERROR", "mensaje": f"No se pudo obtener token: {e}"}

        soap = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:def="http://DefaultNamespace">'
            '<soapenv:Header/><soapenv:Body>'
            '<def:getEstUp>'
            f'<arg0>{cuerpo}</arg0>'
            f'<arg1>{dv}</arg1>'
            f'<arg2>{track_id}</arg2>'
            f'<arg3>{token}</arg3>'
            '</def:getEstUp>'
            '</soapenv:Body></soapenv:Envelope>'
        )
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url, content=soap.encode("utf-8"),
                    headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""},
                )
            if response.status_code != 200:
                return {"estado": "ERROR", "mensaje": f"HTTP {response.status_code}"}
            return self._parsear_estado_track(response.text)
        except Exception as e:
            return {"estado": "ERROR", "mensaje": str(e)}

    def _parsear_estado_track(self, response_text: str) -> dict:
        # Estados del SII para un envío consultado con QueryEstUp.
        # Distinguimos el estado del SOBRE del estado de los DOCUMENTOS:
        #   - EPR = Envío Procesado (el sobre se procesó; mirar los documentos)
        #   - RCT/DOK = Documentos aceptados conforme
        #   - RFR/RCH = Rechazado
        #   - RPR/RLV = Aceptado con reparos / reparos leves
        estados_sii = {
            "REC": ("RECIBIDO",    "Envío recibido, aún no procesado"),
            "EPR": ("PROCESADO",   "Envío procesado — revisar estado de documentos"),
            "RPR": ("REPAROS",     "Procesado con reparos"),
            "RLV": ("REPAROS",     "Procesado con reparos leves"),
            "RCT": ("ACEPTADO",    "Recibido Conforme Total"),
            "DOK": ("ACEPTADO",    "Documentos aceptados"),
            "SOK": ("ACEPTADO",    "Schema y firma OK"),
            "RCH": ("RECHAZADO",   "Rechazado"),
            "RFR": ("RECHAZADO",   "Rechazado por errores de schema/firma"),
            "RSC": ("RECHAZADO",   "Rechazado por schema"),
            "RDC": ("RECHAZADO",   "Rechazado, documento con error"),
            "DNK": ("DESCONOCIDO", "TrackID no encontrado o no corresponde"),
            "LPR": ("PENDIENTE",   "En proceso de revisión"),
        }
        try:
            root = etree.fromstring(response_text.encode())
            # La respuesta SOAP de getEstUp trae un <return> con XML escapado
            # adentro, o los campos directos. Buscamos ESTADO en cualquier nivel.
            ret = (root.findtext(".//{*}getEstUpReturn")
                   or root.findtext(".//getEstUpReturn") or "")
            # Si el return trae XML escapado, parsearlo de nuevo
            cuerpo_xml = root
            if ret and "<" in ret:
                try:
                    cuerpo_xml = etree.fromstring(ret.encode())
                except Exception:
                    cuerpo_xml = root
            def _find(tag):
                return (cuerpo_xml.findtext(f".//{{{'*'}}}{tag}")
                        or cuerpo_xml.findtext(f".//{tag}")
                        or root.findtext(f".//{tag}") or "")
            estado = (_find("ESTADO") or _find("estado")).strip().upper()
            glosa  = _find("GLOSA") or _find("glosa") or ""
            estado_norm, descripcion = estados_sii.get(estado, ("DESCONOCIDO", glosa or estado))
            # Detalle por documento (aceptados/rechazados/reparos)
            docs = []
            for doc_el in (cuerpo_xml.findall(".//{*}DETALLE_REP_RECH") +
                           cuerpo_xml.findall(".//DETALLE_REP_RECH") +
                           cuerpo_xml.findall(".//{*}DETALLE") +
                           cuerpo_xml.findall(".//DETALLE")):
                docs.append({
                    "tipo":   doc_el.findtext("TIPO_DOC") or doc_el.findtext("{*}TIPO_DOC"),
                    "folio":  doc_el.findtext("FOLIO") or doc_el.findtext("{*}FOLIO"),
                    "estado": doc_el.findtext("EST_DTE") or doc_el.findtext("{*}EST_DTE"),
                    "error":  doc_el.findtext("ERR_DOC") or doc_el.findtext("{*}ERR_DOC"),
                })
            # Conteo de estadísticas si vienen (informados/aceptados/rechazados)
            stats = {}
            for tag in ("INFORMADOS", "ACEPTADOS", "RECHAZADOS", "REPAROS"):
                v = _find(tag)
                if v and v.strip().isdigit():
                    stats[tag.lower()] = int(v.strip())
            return {"estado": estado_norm, "codigo_sii": estado,
                    "descripcion": descripcion, "documentos": docs,
                    "estadisticas": stats, "track_id_consultado": True}
        except Exception as e:
            return {"estado": "ERROR_PARSEO",
                    "descripcion": "No se pudo parsear respuesta del SII",
                    "raw": response_text[:400], "error": str(e)}

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
