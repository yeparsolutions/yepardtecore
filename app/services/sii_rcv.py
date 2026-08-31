# -*- coding: utf-8 -*-
"""
Sincronización con el Registro de Compras y Venta (RCV) del SII — trae los
documentos que le llegaron a la empresa como RECEPTORA (sus compras), sin
que el usuario tenga que cargarlos a mano ni importar el XML uno por uno.

IMPORTANTE — esto es ingeniería inversa, no una API oficial y documentada:
El SII no publica un servicio público para "dame todo lo que me llegó este
período". Lo que hay es el portal web que usa cualquier contribuyente para
mirar su RCV a ojo (el mismo que se ve en sii.cl → Mi SII → Registro de
Compras y Venta). Este módulo automatiza esa misma navegación: inicia sesión
con RUT + Clave Tributaria (igual que un usuario real) y llama los mismos
endpoints internos que usa esa pantalla — verificados contra tráfico de red
real (HAR) capturado en agosto de 2026 por el propio cliente, NO contra
documentación del SII (no existe). Consecuencias directas:

  1. La app real resultó ser "consdcvinternetui" (no "consemitidosinternetui"
     como se asumió al principio) — se confirmó viendo un HAR real donde esa
     pantalla devuelve datos reales de compras y ventas con
     respEstado.codRespuesta=0. El nombre del código interno del SII no
     necesariamente coincide con el nombre visible del menú.
  2. El login SÍ está verificado de punta a punta contra el SII real (RUT +
     Clave Tributaria reales, agosto 2026): incluye el paso de login en sí
     (`_login`), el salto "puente" por JavaScript que el SII usa en vez de
     una redirección HTTP normal, y el arranque de la app (obtieneConf,
     aaSessionService/load, consultarParametros, getDatosInicio,
     getDcvEmpresasAutorizadas) antes de poder llamar a getResumen.
  3. TODAVÍA NO VERIFICADO: el detalle a nivel de documento (folio,
     proveedor, fecha de cada factura individual). getResumen del SII solo
     da totales agregados por tipo de documento y estado contable — no lista
     los documentos uno por uno. Falta capturar (en un HAR real) qué llama
     el SII cuando se hace clic en una de esas filas del resumen para ver el
     detalle. Hasta que eso se confirme, `sync_rcv_compras` deliberadamente
     NO inventa documentos individuales (folios falsos romperían la
     deduplicación y el Libro de Compras) — junta los totales por tipo/
     estado y corta ahí con un error claro pidiendo ese último HAR.
  4. El SII puede cambiar esta pantalla interna sin aviso (no es un
     contrato público) y esto dejaría de funcionar de un día para otro. Si
     eso pasa, la carga manual y el importar XML (services/intercambio.py)
     siguen funcionando igual — son la red de seguridad permanente.

Sin estado: como el resto de YeparDTEcore, esta función no persiste nada —
recibe la Clave Tributaria en cada llamada, la usa en memoria para esta
sincronización y la descarta. Quien guarda algo (cifrado) es el backend.
"""
from __future__ import annotations

import logging
import re
import secrets
import uuid
from urllib.parse import urljoin

import httpx

logger = logging.getLogger("yepardtecore.sii_rcv")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

LOGIN_INICIO     = "https://misiir.sii.cl/cgi_misii/siihome.cgi"
LOGIN_POST_URL   = "https://zeusr.sii.cl/cgi_AUT2000/CAutInicio.cgi"
LOGIN_REFERENCIA = "https://misiir.sii.cl/cgi_misii/siihome.cgi"

# CONFIRMADO vía HAR real (agosto 2026): esta es la app real detrás de
# "Registro de Compras y Venta" en Mi SII — no "consemitidosinternetui"
# como se había asumido antes de tener tráfico real para comparar.
RCV_BASE = "https://www4.sii.cl/consdcvinternetui"
RCV_APP  = f"{RCV_BASE}/"
RCV_NS   = "cl.sii.sdi.lob.diii.consdcv.data.api.interfaces.FacadeService"
SETTINGS_NS = "cl.sii.sdi.lob.diii.consdcv.data.impl.SettingsApplicationService"
AUTCONF_NS  = "cl.sii.sdi.ss.aa.api.interfaces.AutConfDataService"

AASESSION_URL = "https://www4.sii.cl/common-1.0/services/aaSessionService/load"
AUTCONF_URL   = "https://www4.sii.cl/common-1.0/services/autConfDataService/obtieneConf"

# Estados contables del RCV que se traen como "compra". Quedan fuera
# RECLAMADO (documentos que la propia empresa objetó — no deberían entrar
# como compra válida) y NO_INCLUIR (documentos marcados para excluir del
# período por el propio contribuyente). Ajustable si hace falta verlos.
ESTADOS_A_TRAER = ("REGISTRO", "PENDIENTE")

# Tope de filas de resumen por sincronización — cortafuego ante una
# respuesta inesperada antes de martillar el SII.
MAX_FILAS_RESUMEN = 200


class SIIRCVError(Exception):
    """Cualquier falla al hablar con el SII: login rechazado, sesión
    bloqueada, o una respuesta que no tiene la forma esperada."""


def _norm_rut(rut_con_formato: str) -> tuple[str, str]:
    """'78.377.021-0' → ('78377021', '0'). Acepta también sin puntos/guión."""
    limpio = re.sub(r"[.\s]", "", rut_con_formato or "").upper()
    if "-" in limpio:
        num, dv = limpio.split("-", 1)
    else:
        num, dv = limpio[:-1], limpio[-1]
    return num, dv


def _gen_conversation_id() -> str:
    return secrets.token_hex(7).upper()[:13]


def _periodo_a_ptributario(periodo: str) -> str:
    """'2026-08' → '202608'. El SII de esta app no usa guión en el período."""
    return re.sub(r"[^0-9]", "", periodo or "")


async def _login(client: httpx.AsyncClient, rut_empresa: str, clave_tributaria: str) -> None:
    """Inicia sesión en el SII igual que lo haría una persona: carga la
    página de 'Mi SII' (recoge las cookies iniciales del portal), y manda el
    formulario de RUT + Clave al endpoint real que usa esa pantalla."""
    rut_num, dv = _norm_rut(rut_empresa)

    try:
        await client.get(LOGIN_INICIO)
    except httpx.RequestError as e:
        raise SIIRCVError(f"No se pudo contactar el portal del SII: {e}") from e

    form = {
        "rutcntr": f"{rut_num}{dv}",
        "rut": rut_num,
        "dv": dv,
        "referencia": LOGIN_REFERENCIA,
        "411": "",
        "clave": clave_tributaria,
    }
    try:
        resp = await client.post(LOGIN_POST_URL, data=form)
    except httpx.RequestError as e:
        raise SIIRCVError(f"No se pudo contactar el login del SII: {e}") from e

    texto = resp.text or ""
    logger.info(
        f"[SII-LOGIN][DIAG] status={resp.status_code} url_final={resp.url} "
        f"largo_body={len(texto)}"
    )
    # El SII no devuelve un 401 — te vuelve a mostrar el formulario de login
    # con un mensaje. Buscamos las señales típicas de rechazo.
    if resp.status_code >= 500:
        raise SIIRCVError(f"El SII respondió con error {resp.status_code} al intentar iniciar sesión.")
    if "IngresoRutClave" in str(resp.url) or re.search(r"clave.{0,20}(incorrect|inv[aá]lid)", texto, re.I):
        raise SIIRCVError(
            "El SII no aceptó el RUT o la Clave Tributaria. Verifica que la "
            "clave esté vigente y sea la misma con la que entras a sii.cl."
        )
    if re.search(r"sala de espera|queue-?it|est[aá]s en la fila", texto, re.I):
        raise SIIRCVError(
            "El SII puso la sesión en una sala de espera virtual (alta demanda "
            "del sitio). Reintenta la sincronización en unos minutos."
        )
    if re.search(r"m[aá]ximo de sesiones autenticadas", texto, re.I):
        # CONFIRMADO en producción: cada intento de sincronización abre una
        # sesión nueva en el SII. Si se acumulan varias sin cerrar, el SII
        # empieza a rechazar logins nuevos con este mensaje — el RUT/Clave
        # están bien, el límite es de sesiones simultáneas sin cerrar.
        raise SIIRCVError(
            "El SII rechazó el login porque hay demasiadas sesiones abiertas "
            "sin cerrar para este RUT (límite de sesiones simultáneas del "
            "propio SII). Espera un rato a que esas sesiones expiren por sí "
            "solas y reintenta — evita sincronizar varias veces seguidas "
            "mientras tanto."
        )

    # ── Completar el login ────────────────────────────────────────────────
    # El SII no redirige por HTTP tras el POST — entrega una página "puente"
    # con un `location.replace('https://misiir.sii.cl/...')` en JavaScript,
    # que el navegador ejecuta para terminar de armar la sesión. httpx no
    # ejecuta JS, así que sin este paso el POST "funciona" (200 OK, ya con
    # las cookies de sesión) pero el SII nunca ve el login como completo. Se
    # extrae la URL del propio script en vez de asumir que siempre es
    # LOGIN_REFERENCIA, por si el SII la cambia.
    m_redirect = re.search(r"location\.replace\(['\"]([^'\"]+)['\"]\)", texto)
    destino_final = m_redirect.group(1) if m_redirect else LOGIN_REFERENCIA
    # Esta página a veces trae una ruta relativa (ej. '/AUT2000/index.html'
    # en la página de "máximo de sesiones") en vez de una URL completa.
    # urljoin la resuelve contra la URL de la respuesta, igual que haría un
    # navegador.
    destino_final = urljoin(str(resp.url), destino_final)
    try:
        resp_final = await client.get(destino_final)
    except httpx.RequestError as e:
        raise SIIRCVError(f"No se pudo completar el login del SII (paso final): {e}") from e
    logger.info(
        f"[SII-LOGIN][DIAG] paso_final destino={destino_final} "
        f"status={resp_final.status_code}"
    )


def _meta(namespace_completo: str, conversation_id: str, *, con_page: bool) -> dict:
    meta = {
        "namespace": namespace_completo,
        "conversationId": conversation_id,
        "transactionId": str(uuid.uuid4()),
    }
    if con_page:
        meta["page"] = None
    return meta


async def _post_arranque(client: httpx.AsyncClient, url: str, namespace: str, conversation_id: str) -> dict:
    """POST liviano para las llamadas de arranque de la app (obtieneConf,
    consultarParametros): no llevan "page" ni "data" en el request, y su
    respuesta no trae "respEstado" — no hay nada que validar más que la
    conexión. Confirmado contra el HAR real: son parte de la secuencia que
    hace el navegador antes de poder pedir datos, pero no son en sí mismas
    la causa de rechazo si fallan — por eso son best-effort (no cortan la
    sincronización si dan error, solo se registra)."""
    body = {"metaData": _meta(namespace, conversation_id, con_page=False)}
    resp = await client.post(url, json=body)
    if not resp.is_success:
        raise SIIRCVError(f"{url} respondió {resp.status_code}.")
    try:
        return resp.json()
    except ValueError:
        return {}


async def _facade_post(client: httpx.AsyncClient, url: str, namespace: str, data: dict, conversation_id: str) -> dict:
    """POST a un método de FacadeService (o equivalente) que sí sigue el
    contrato completo: "page" en metaData, "data" con los parámetros, y
    "respEstado.codRespuesta" en la respuesta (0 = éxito)."""
    body = {"metaData": _meta(namespace, conversation_id, con_page=True), "data": data}
    try:
        resp = await client.post(url, json=body)
    except httpx.RequestError as e:
        raise SIIRCVError(f"No se pudo contactar {url}: {e}") from e

    if not resp.is_success:
        raise SIIRCVError(f"El SII respondió {resp.status_code} en {url}.")
    try:
        payload = resp.json()
    except ValueError:
        raise SIIRCVError(
            f"El SII devolvió una respuesta que no es JSON en {url} — "
            f"probablemente la sesión no quedó autenticada."
        )
    resp_estado = payload.get("respEstado")
    if resp_estado is not None:
        cod = resp_estado.get("codRespuesta")
        if cod not in (0, None):
            msg = resp_estado.get("msgeRespuesta") or "sin detalle"
            logger.warning(f"[SII-RCV][DIAG] Rechazo completo en {url}: {payload!r}")
            raise SIIRCVError(f"El SII rechazó la consulta ({url}): {msg}")
    return payload


async def sync_rcv_compras(rut_empresa: str, clave_tributaria: str, periodo: str) -> list[dict]:
    """
    Trae del SII el resumen de compras del período "AAAA-MM" indicado, para
    `rut_empresa` ("12.345.678-9"), usando su Clave Tributaria.

    IMPORTANTE (ver docstring del módulo, punto 3): esto todavía trae solo
    TOTALES por tipo de documento y estado contable, no el listado
    documento por documento (folio, proveedor). Devuelve esos totales como
    "resumen" — el backend NO debe crear registros de Compra individuales
    con esto todavía; falta confirmar el endpoint de detalle con un HAR real
    de esa pantalla.
    """
    rut_num, dv = _norm_rut(rut_empresa)
    ptributario = _periodo_a_ptributario(periodo)
    conv_id = _gen_conversation_id()

    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www4.sii.cl",
        # Confirmados comparando con un HAR real: el navegador manda Referer
        # apuntando a la SPA y los headers "Sec-Fetch-*" en cada llamada.
        "Referer": RCV_APP,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    async with httpx.AsyncClient(
        headers=headers, timeout=30, follow_redirects=True,
        limits=httpx.Limits(max_keepalive_connections=5),
    ) as client:
        await _login(client, rut_empresa, clave_tributaria)

        try:
            await client.get(RCV_APP)
        except httpx.RequestError as e:
            raise SIIRCVError(f"No se pudo abrir el Registro de Compras y Venta: {e}") from e

        # ── Arranque de la app — CONFIRMADO vía HAR real: el navegador hace
        # esta secuencia exacta antes de poder pedir cualquier dato. Saltarse
        # estos pasos es lo que causaba "Usuario no autorizado" en getResumen
        # aunque el login general al SII ya hubiera funcionado.
        try:
            await client.get(AASESSION_URL)
        except httpx.RequestError as e:
            logger.warning(f"[SII-RCV][DIAG] aaSessionService/load falló (no fatal): {e}")

        try:
            await _post_arranque(client, AUTCONF_URL, f"{AUTCONF_NS}/obtieneConf", conv_id)
        except SIIRCVError as e:
            logger.warning(f"[SII-RCV][DIAG] obtieneConf falló (no fatal): {e}")

        try:
            await _post_arranque(
                client, f"{RCV_BASE}/services/data/settingsService/consultarParametros",
                f"{SETTINGS_NS}/consultarParametros", conv_id,
            )
        except SIIRCVError as e:
            logger.warning(f"[SII-RCV][DIAG] consultarParametros falló (no fatal): {e}")

        # Estos dos SÍ son parte del contrato completo (FacadeService) y si
        # fallan, no tiene sentido seguir — son los que de verdad activan la
        # sesión para esta app específica.
        await _facade_post(
            client, f"{RCV_BASE}/services/data/facadeService/getDatosInicio",
            f"{RCV_NS}/getDatosInicio", {}, conv_id,
        )
        await _facade_post(
            client, f"{RCV_BASE}/services/data/facadeService/getDcvEmpresasAutorizadas",
            f"{RCV_NS}/getDcvEmpresasAutorizadas",
            {"rutAutenticado": rut_num, "dvAutenticado": dv}, conv_id,
        )

        # ── Resumen de compras, por cada estado contable relevante ─────────
        resumen_por_estado: dict[str, dict] = {}
        for estado in ESTADOS_A_TRAER:
            payload = await _facade_post(
                client, f"{RCV_BASE}/services/data/facadeService/getResumen",
                f"{RCV_NS}/getResumen",
                {
                    "rutEmisor": rut_num, "dvEmisor": dv,
                    "ptributario": ptributario, "estadoContab": estado,
                    "operacion": "COMPRA",
                },
                conv_id,
            )
            filas = payload.get("data") or []
            logger.info(f"[SII-RCV] Resumen {estado} {periodo}: {len(filas)} fila(s) — {filas!r}")
            resumen_por_estado[estado] = filas

        total_filas = sum(len(v) for v in resumen_por_estado.values())
        if total_filas == 0:
            # No hay nada que traer — no es un error, es un período sin
            # compras registradas en el RCV.
            return []

        # TODO (bloqueado — falta un HAR real de la pantalla de detalle):
        # getResumen solo da totales por tipo de documento, no folios
        # individuales. Hasta tener el endpoint de detalle confirmado,
        # cortamos acá con un error explícito en vez de inventar documentos
        # con folios falsos (rompería la deduplicación y el Libro de
        # Compras). Los totales ya quedaron en el log de arriba como prueba
        # de que la conexión y la autenticación SÍ están funcionando.
        raise SIIRCVError(
            f"Conexión y autenticación con el SII OK — se encontraron "
            f"{total_filas} fila(s) de resumen para {periodo}, pero todavía "
            f"falta implementar el detalle por documento (folio, proveedor). "
            f"Contacta a soporte con este mensaje para terminar esa parte."
        )
