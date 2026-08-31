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
real capturado en agosto de 2026, NO contra documentación del SII (no
existe). Dos consecuencias directas:

  1. El login (`_login`) es la única parte que no pudimos verificar con una
     ejecución real de punta a punta — por buenas razones no se probó con
     una clave tributaria real durante el desarrollo. Los nombres de campo
     y la URL sí están confirmados leyendo el HTML real del formulario de
     login (ver commit), pero conviene probarlo primero con un período que
     ya sabemos que tiene datos.
  2. El SII puede cambiar esta pantalla interna sin aviso (no es un
     contrato público) y esto dejaría de funcionar de un día para otro. Si
     eso pasa, la carga manual y el importar XML (services/intercambio.py)
     siguen funcionando igual — son la red de seguridad permanente.

Sin estado: como el resto de YeparDTEcore, esta función no persiste nada —
recibe la Clave Tributaria en cada llamada, la usa en memoria para esta
sincronización y la descarta. Quien guarda algo (cifrado) es el backend.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import uuid
from datetime import datetime

import httpx

logger = logging.getLogger("yepardtecore.sii_rcv")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

LOGIN_INICIO   = "https://misiir.sii.cl/cgi_misii/siihome.cgi"
LOGIN_POST_URL = "https://zeusr.sii.cl/cgi_AUT2000/CAutInicio.cgi"
LOGIN_REFERENCIA = "https://misiir.sii.cl/cgi_misii/siihome.cgi"
RCV_BASE = "https://www4.sii.cl/consemitidosinternetui"
RCV_APP  = f"{RCV_BASE}/"
RCV_NS   = "cl.sii.sdi.lob.diii.consemitidos.data.api.interfaces.FacadeService"

# Operación 2 = documentos donde la empresa es RECEPTORA (compras).
# Operación 1 = documentos donde es EMISORA (ventas) — no se usa acá.
OPERACION_COMPRAS = 2

# Tope de documentos por sincronización — cortafuego ante una respuesta
# inesperada (o un período con un volumen anormal) antes de martillar el SII.
MAX_DOCUMENTOS = 500


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


def _meta(namespace_metodo: str, conversation_id: str) -> dict:
    return {
        "namespace": f"{RCV_NS}/{namespace_metodo}",
        "conversationId": conversation_id,
        "transactionId": str(uuid.uuid4()),
        "page": None,
    }


def _fecha_a_iso(fecha_ddmmaaaa: str | None) -> str | None:
    """'04/07/2026' → '2026-07-04'. Si no calza el formato, la deja tal cual
    para no perder el dato — mejor una fecha rara que un documento perdido."""
    if not fecha_ddmmaaaa:
        return None
    try:
        return datetime.strptime(fecha_ddmmaaaa, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return fecha_ddmmaaaa


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
    # DIAGNÓSTICO TEMPORAL: el login es la única parte que nunca se probó de
    # punta a punta contra el SII real. "Usuario no autorizado" en getResumen
    # con el RUT correcto sugiere que la sesión no quedó realmente autenticada
    # aunque el POST haya devuelto 200 — y que nuestras señales de rechazo
    # (_login más abajo) no están detectando el mensaje real del SII. Este log
    # muestra la URL final y el arranque del HTML para ver qué contestó de
    # verdad, sin tener que adivinar. Se puede quitar una vez confirmado.
    logger.info(
        f"[SII-LOGIN][DIAG] status={resp.status_code} url_final={resp.url} "
        f"largo_body={len(texto)} inicio_body={texto[:400]!r}"
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


async def _rcv_post(client: httpx.AsyncClient, metodo: str, data: dict, conversation_id: str) -> dict:
    body = {"metaData": _meta(metodo, conversation_id), "data": data}
    try:
        resp = await client.post(f"{RCV_BASE}/services/data/facadeService/{metodo}", json=body)
    except httpx.RequestError as e:
        raise SIIRCVError(f"No se pudo contactar el RCV del SII ({metodo}): {e}") from e

    if not resp.is_success:
        raise SIIRCVError(f"El RCV del SII respondió {resp.status_code} en {metodo}.")
    try:
        payload = resp.json()
    except ValueError:
        raise SIIRCVError(
            f"El RCV del SII devolvió una respuesta que no es JSON en {metodo} "
            f"— probablemente la sesión no quedó autenticada."
        )
    cod = (payload.get("respEstado") or {}).get("codRespuesta")
    if cod not in (0, None):
        msg = (payload.get("respEstado") or {}).get("msgeRespuesta") or "sin detalle"
        raise SIIRCVError(f"El SII rechazó la consulta ({metodo}): {msg}")
    return payload


async def sync_rcv_compras(rut_empresa: str, clave_tributaria: str, periodo: str) -> list[dict]:
    """
    Trae del SII los documentos recibidos (compras) del período "AAAA-MM"
    indicado, para `rut_empresa` ("12.345.678-9"), usando su Clave
    Tributaria. Devuelve una lista de dicts listos para guardar como
    `Compra` en el backend — no persiste nada acá.
    """
    rut_num, dv = _norm_rut(rut_empresa)
    conv_id = _gen_conversation_id()

    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www4.sii.cl",
    }
    async with httpx.AsyncClient(
        headers=headers, timeout=30, follow_redirects=True,
        limits=httpx.Limits(max_keepalive_connections=5),
    ) as client:
        await _login(client, rut_empresa, clave_tributaria)

        # Visita la app antes de llamarla — mismo orden que seguiría un
        # navegador real y evita depender solo de las cookies del login.
        try:
            await client.get(RCV_APP)
        except httpx.RequestError as e:
            raise SIIRCVError(f"No se pudo abrir el Registro de Compras y Venta: {e}") from e

        resumen = await _rcv_post(client, "getResumen", {
            "periodo": periodo, "rutContribuyente": rut_num,
            "dvContribuyente": dv, "operacion": OPERACION_COMPRAS,
        }, conv_id)

        filas_resumen = ((resumen.get("data") or {}).get("resumenDte")) or []
        documentos: list[dict] = []

        for fila in filas_resumen:
            tipo_doc = fila.get("tipoDoc")
            if not tipo_doc or not fila.get("totalDoc"):
                continue

            detalle_tipo = await _rcv_post(client, "getDetalleRecibidos", {
                "tipoDoc": str(tipo_doc), "rut": rut_num, "dv": dv,
                "periodo": periodo, "operacion": OPERACION_COMPRAS,
                "derrCodigo": str(tipo_doc), "refNCD": "0",
            }, conv_id)

            for fila_doc in ((detalle_tipo.get("dataResp") or {}).get("detalles")) or []:
                if len(documentos) >= MAX_DOCUMENTOS:
                    logger.warning(f"[SII-RCV] Tope de {MAX_DOCUMENTOS} documentos alcanzado en {periodo}, se corta.")
                    break

                # OJO: en esta respuesta el SII reutiliza los campos
                # "rutReceptor/dvReceptor/rznSocRecep" para el PROVEEDOR
                # cuando operacion=2 (son compras, no ventas) — es un
                # nombre heredado de la pantalla de "emitidos", no un error
                # nuestro. getDetalleDTERecibidos sí los llama correctamente
                # rutEmisor/rznSocEmisor.
                dhdr_codigo = fila_doc.get("dhdrCodigo")
                folio = fila_doc.get("folio")
                rut_prov_num = fila_doc.get("rutReceptor")
                dv_prov = fila_doc.get("dvReceptor")

                razon_proveedor = fila_doc.get("rznSocRecep") or ""
                if dhdr_codigo and folio and rut_prov_num:
                    try:
                        detalle_doc = await _rcv_post(client, "getDetalleDTERecibidos", {
                            "dhdrCodigo": dhdr_codigo, "rut": rut_num, "dv": dv,
                            "folio": folio, "tipoDoc": str(tipo_doc),
                            "rutDoc": rut_prov_num, "dvDoc": dv_prov,
                        }, conv_id)
                        dd = detalle_doc.get("detalleDte") or {}
                        if dd.get("rznSocEmisor"):
                            razon_proveedor = dd["rznSocEmisor"]
                    except SIIRCVError as e:
                        # No aborta la sincronización completa por un documento
                        # puntual que falle — sigue con el resto y avisa.
                        logger.warning(f"[SII-RCV] No se pudo ampliar el detalle de folio {folio} tipo {tipo_doc}: {e}")
                    await asyncio.sleep(0.25)

                documentos.append({
                    "tipo_dte": int(tipo_doc),
                    "folio": int(folio) if folio else None,
                    "fecha": _fecha_a_iso(fila_doc.get("fechaEmision")),
                    "proveedor_rut": f"{rut_prov_num}-{dv_prov}" if rut_prov_num else "",
                    "proveedor_razon": razon_proveedor,
                    "monto_neto": int(fila_doc.get("mntNeto") or 0),
                    "monto_exento": int(fila_doc.get("mntExento") or 0),
                    "monto_iva": int(fila_doc.get("mntIva") or 0),
                    "monto_total": int(fila_doc.get("mntTotal") or 0),
                })

            if len(documentos) >= MAX_DOCUMENTOS:
                break

        return documentos
