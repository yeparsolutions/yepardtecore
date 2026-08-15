# app/api/v1/endpoints/pagos.py
# ══════════════════════════════════════════════════════════════
# Endpoints de cobro con Mercado Pago (Checkout Pro)
#
#   POST /v1/pagos/crear-preferencia  — crea preferencia MP y
#                                       devuelve la URL de pago
#   POST /v1/pagos/webhook            — MP notifica el resultado
#   GET  /v1/pagos/estado/{emisor_id} — consulta estado del pago
#
# Flujo:
#   1. Desarrollador se registra → verifica email (OTP)
#   2. Frontend llama crear-preferencia → recibe init_point (URL MP)
#   3. Desarrollador paga en MP
#   4. MP llama webhook → Core activa la cuenta
#   5. Frontend consulta estado → si pagado, muestra API key
#
# Analogía: es la caja registradora — el cliente llega, hace el
# pedido (preferencia), paga en la caja (MP), y el mozo le trae
# el producto (API key) cuando el sistema confirma el cobro.
# ══════════════════════════════════════════════════════════════

import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import get_db
from app.models.emisor import Emisor
from app.core.security import verificar_token

logger = logging.getLogger("yepardtecore.pagos")
router = APIRouter(prefix="/pagos", tags=["Pagos"])

def _verificar_secreto(request: Request, header: str = "X-Admin-Secret") -> None:
    """
    Verifica un secreto compartido (panel admin / cron). Falla CERRADO:
    si el secreto no está configurado en el entorno, rechaza. Antes, con el
    default vacío, un header vacío igualaba a "" y abría el panel a cualquiera.
    Comparación en tiempo constante para no filtrar el secreto por timing.
    """
    provisto = request.headers.get(header, "")
    esperado = settings.MP_WEBHOOK_SECRET or ""
    if not esperado or not hmac.compare_digest(provisto, esperado):
        raise HTTPException(403, "No autorizado")


# Precio anual: se muestra como base + IVA; MP cobra el TOTAL con IVA.
PRECIO_NETO       = 150000                     # base sin IVA
MONTO_SUSCRIPCION = round(PRECIO_NETO * 1.19)  # 178.500 — total que cobra MP
DIAS_SUSCRIPCION  = 365


# ── Crear preferencia de pago ─────────────────────────────────

class CrearPreferenciaInput(BaseModel):
    emisor_id: int
    email:     str   # email del pagador (para MP)
    plan:      str | None = None   # developer|partner|business|scale (default developer)


@router.post("/crear-preferencia")
async def crear_preferencia(
    datos: CrearPreferenciaInput,
    db: AsyncSession = Depends(get_db),
):
    """
    Crea una preferencia de pago en Mercado Pago y retorna
    la URL de pago (init_point) para redirigir al usuario.
    """
    if not settings.MP_ACCESS_TOKEN:
        raise HTTPException(500, "MP_ACCESS_TOKEN no configurado en Railway. Agrega la variable de entorno.")

    emisor = await db.get(Emisor, datos.emisor_id)
    if not emisor:
        raise HTTPException(404, "Cuenta no encontrada")

    # Bloquear solo si la suscripción está VIGENTE (pagada Y con fin en el futuro).
    # Si ya venció, dejar renovar aunque estado_pago todavía diga "pagado" — así
    # la renovación funciona al instante, sin depender de que el job diario haya
    # corrido. Una cuenta permanente (fin en 2300) queda siempre vigente.
    _fin = emisor.suscripcion_fin
    if _fin is not None and getattr(_fin, "tzinfo", None) is None:
        _fin = _fin.replace(tzinfo=timezone.utc)
    vigente = (emisor.estado_pago == "pagado"
               and _fin is not None and _fin > datetime.now(timezone.utc))
    if vigente:
        return {
            "ok":     True,
            "pagado": True,
            "mensaje": "Esta cuenta ya tiene una suscripción activa.",
        }

    # Plan elegido → precio y nombre (Developer por defecto)
    from app.services.planes_dtecore import plan_info, precio_total, PLAN_DEFAULT
    _plan  = (datos.plan or PLAN_DEFAULT).lower()
    _pinfo = plan_info(_plan)
    _monto = precio_total(_plan)

    # Construir la preferencia para Checkout Pro
    preferencia = {
        "items": [
            {
                "title":       f"YeparDTEcore — Plan {_pinfo['nombre']} ({emisor.nombre_app})",
                "quantity":    1,
                "unit_price":  _monto,
                "currency_id": "CLP",
                "description": f"Plan {_pinfo['nombre']} — hasta {_pinfo['apps']} app(s) por 1 año",
            }
        ],
        "payer": {
            "email": datos.email,
        },
        "back_urls": {
            "success": f"{settings.APP_BASE_URL}/onboarding?pago=exitoso&emisor={datos.emisor_id}",
            "failure": f"{settings.APP_BASE_URL}/onboarding?pago=fallido&emisor={datos.emisor_id}",
            "pending": f"{settings.APP_BASE_URL}/onboarding?pago=pendiente&emisor={datos.emisor_id}",
        },
        "auto_return":       "approved",
        "external_reference": f"{datos.emisor_id}:{_plan}",   # emisor_id:plan para el webhook
        "notification_url":  f"{settings.APP_BASE_URL}/v1/pagos/webhook",
        "statement_descriptor": "YEPAR DTECORE",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.mercadopago.com/checkout/preferences",
            json=preferencia,
            headers={
                "Authorization": f"Bearer {settings.MP_ACCESS_TOKEN}",
                "Content-Type":  "application/json",
            },
            timeout=15,
        )

    if resp.status_code not in (200, 201):
        logger.error(f"[MP] Error creando preferencia: {resp.status_code} {resp.text}")
        raise HTTPException(502, f"Error en Mercado Pago: {resp.text[:200]}")

    data = resp.json()
    logger.info(
        f"[MP] Preferencia creada para emisor {datos.emisor_id}: "
        f"id={data.get('id')} init_point={data.get('init_point', '')[:60]}"
    )

    return {
        "ok":              True,
        "pagado":          False,
        "preference_id":   data.get("id"),
        "init_point":      data.get("init_point"),      # producción
        "sandbox_init_point": data.get("sandbox_init_point"),  # pruebas
    }


# ── Webhook de Mercado Pago ───────────────────────────────────

@router.post("/webhook")
async def webhook_mp(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    MP llama este endpoint cuando hay una actualización de pago.
    Valida la firma, consulta el pago y activa la cuenta si fue aprobado.
    """
    body = await request.body()

    # ── Validar firma HMAC del webhook ────────────────────────
    # MP firma cada notificación con el MP_WEBHOOK_SECRET.
    # Si no coincide, alguien está enviando requests falsos.
    x_signature  = request.headers.get("x-signature", "")
    x_request_id = request.headers.get("x-request-id", "")

    # Falla CERRADO: sin secreto configurado o sin firma en el request, se
    # rechaza. Antes, si faltaba x-signature se SALTABA la validación entera
    # y la notificación se procesaba igual (webhook falsificable).
    if not settings.MP_WEBHOOK_SECRET:
        logger.error("[MP-WEBHOOK] MP_WEBHOOK_SECRET no configurado — request rechazado")
        raise HTTPException(503, "Webhook no configurado")

    if not x_signature:
        logger.warning("[MP-WEBHOOK] Falta la firma x-signature — request rechazado")
        raise HTTPException(400, "Falta firma del webhook")

    ts = ""
    v1 = ""
    for part in x_signature.split(","):
        k, _, v = part.partition("=")
        if k.strip() == "ts":   ts = v.strip()
        if k.strip() == "v1":   v1 = v.strip()

    manifest = f"id:{request.query_params.get('data.id', '')};request-id:{x_request_id};ts:{ts};"
    # .hexdigest(): antes se comparaba el OBJETO hmac.HMAC contra el string v1,
    # que nunca coincide — la firma legítima quedaba rota y solo pasaba el
    # camino sin firma. Ahora comparamos el hex contra el v1 hex que manda MP.
    esperado = hmac.new(
        settings.MP_WEBHOOK_SECRET.encode(),
        manifest.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(esperado, v1):
        logger.warning("[MP-WEBHOOK] Firma inválida — request rechazado")
        raise HTTPException(400, "Firma inválida")

    # ── Procesar la notificación ──────────────────────────────
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    tipo   = payload.get("type") or request.query_params.get("type", "")
    data_id = (
        (payload.get("data") or {}).get("id")
        or request.query_params.get("data.id")
    )

    logger.info(f"[MP-WEBHOOK] tipo={tipo} data_id={data_id}")

    # Solo procesar notificaciones de pagos
    if tipo not in ("payment", "merchant_order"):
        return {"ok": True, "ignorado": True}

    if not data_id:
        return {"ok": True, "sin_data_id": True}

    # ── Consultar el pago en MP ───────────────────────────────
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.mercadopago.com/v1/payments/{data_id}",
            headers={"Authorization": f"Bearer {settings.MP_ACCESS_TOKEN}"},
            timeout=10,
        )

    if resp.status_code != 200:
        logger.error(f"[MP-WEBHOOK] Error consultando pago {data_id}: {resp.status_code}")
        return {"ok": False, "error": "No se pudo consultar el pago"}

    pago = resp.json()
    estado      = pago.get("status")           # approved, pending, rejected
    referencia  = pago.get("external_reference")  # emisor_id que pusimos
    monto       = pago.get("transaction_amount", 0)

    logger.info(
        f"[MP-WEBHOOK] Pago {data_id}: estado={estado} "
        f"referencia={referencia} monto={monto}"
    )

    if estado != "approved":
        return {"ok": True, "estado": estado, "accion": "sin_cambio"}

    # ── Activar la cuenta ─────────────────────────────────────
    if not referencia:
        logger.warning("[MP-WEBHOOK] Pago aprobado sin external_reference")
        return {"ok": True, "advertencia": "sin referencia"}

    # external_reference viene como "emisor_id" o "emisor_id:plan"
    _ref_partes = str(referencia).split(":")
    try:
        emisor_id = int(_ref_partes[0])
    except (ValueError, IndexError):
        logger.warning(f"[MP-WEBHOOK] external_reference inválido: {referencia}")
        return {"ok": True, "advertencia": "referencia inválida"}
    plan_pagado = _ref_partes[1].lower() if len(_ref_partes) > 1 and _ref_partes[1] else None

    res = await db.execute(select(Emisor).where(Emisor.id == emisor_id))
    emisor = res.scalar_one_or_none()

    if not emisor:
        logger.error(f"[MP-WEBHOOK] Emisor {emisor_id} no encontrado")
        return {"ok": False, "error": "Emisor no encontrado"}

    ahora = datetime.now(timezone.utc)
    # Renovación: si aún está vigente, extender DESDE su fecha de fin (no perder
    # los días que le quedan al renovar anticipado); si ya venció, partir de hoy.
    _fin_actual = emisor.suscripcion_fin
    if _fin_actual is not None and getattr(_fin_actual, "tzinfo", None) is None:
        _fin_actual = _fin_actual.replace(tzinfo=timezone.utc)
    base = _fin_actual if (_fin_actual and _fin_actual > ahora) else ahora
    emisor.estado_pago        = "pagado"
    emisor.ambiente           = "produccion"   # al pagar, se habilita producción
    if plan_pagado:
        emisor.plan = plan_pagado              # registrar el plan cobrado
    emisor.suscripcion_inicio = ahora
    emisor.suscripcion_fin    = base + timedelta(days=DIAS_SUSCRIPCION)
    await db.commit()

    logger.info(
        f"[MP-WEBHOOK] ✓ Emisor {emisor_id} ({emisor.nombre_app}) activado. "
        f"Suscripción hasta {emisor.suscripcion_fin.date()}"
    )

    # Enviar API key por email al desarrollador
    if emisor.correo and emisor.api_key:
        try:
            from app.services.email_service import enviar_email
            asunto = "Tu API key de YeparDTEcore está lista"
            html = f"""
            <!DOCTYPE html>
            <html lang="es">
            <head><meta charset="UTF-8"></head>
            <body style="font-family:'DM Sans',Arial,sans-serif;background:#f8f9fa;margin:0;padding:40px 20px;">
              <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:16px;
                          padding:36px;border:1px solid #e0e0e0;">
                <img src="https://app.yepardtecore.cl/static/logo-300x130.png"
                     alt="YeparDTEcore" style="height:36px;margin-bottom:28px;">
                <h2 style="font-size:1.4rem;color:#1a1a1a;margin-bottom:10px;">
                  ¡Tu suscripción está activa!
                </h2>
                <p style="color:#4a4a4a;margin-bottom:16px;line-height:1.6;">
                  Tu pago fue confirmado. Aquí está tu API key para integrar
                  <strong>{emisor.nombre_app}</strong> con YeparDTEcore:
                </p>
                <div style="background:#0a0a0a;color:#4cff91;border-radius:10px;
                            padding:16px;font-family:monospace;font-size:0.85rem;
                            word-break:break-all;margin:16px 0;">
                  {emisor.api_key}
                </div>
                <p style="color:#6c757d;font-size:0.85rem;line-height:1.6;">
                  Úsala en el header <code>X-API-Key</code> en cada llamada.<br>
                  Documentación: <a href="https://app.yepardtecore.cl/api/docs">app.yepardtecore.cl/api/docs</a>
                </p>
                <hr style="border:none;border-top:1px solid #eee;margin:28px 0;">
                <p style="font-size:0.75rem;color:#999;">
                  YeparDTEcore · Yepar Solutions SpA<br>
                  Suscripción válida hasta {emisor.suscripcion_fin.strftime('%d/%m/%Y')}
                </p>
              </div>
            </body>
            </html>
            """
            await enviar_email(emisor.correo, asunto, html)
            logger.info(f"[MP-WEBHOOK] API key enviada por email a {emisor.correo}")
        except Exception as e:
            logger.error(f"[MP-WEBHOOK] Error enviando email: {e}")

    return {"ok": True, "estado": "activado", "emisor_id": emisor_id}


# ── Buscar emisor por email (fallback si se pierde el emisorId) ──────────────

@router.get("/buscar-por-email")
async def buscar_por_email(
    email: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Fallback: si el frontend perdió el emisorId al volver de MP,
    lo recupera por el email del desarrollador.
    """
    from app.models.usuario import Usuario
    res = await db.execute(
        select(Usuario).where(Usuario.email == email.lower().strip())
    )
    usuario = res.scalar_one_or_none()
    if not usuario or not usuario.emisor_id:
        raise HTTPException(404, "No encontrado")
    return {"emisor_id": usuario.emisor_id}


# ── Consultar estado de pago ──────────────────────────────────

def _verificar_jwt_emisor(request: Request, emisor_id: int) -> None:
    """
    estado_pago y dashboard devuelven la API key del emisor en la
    respuesta — sin esto, cualquiera que supiera un emisor_id pagado
    podía pedir su API key sin ninguna credencial (el hallazgo más
    grave de la revisión, incluso peor que subir un certificado ajeno,
    porque la API key es la llave maestra para todo lo demás).
    Usa el mismo JWT que el desarrollador ya recibió al registrarse
    (registro_desarrollador) — no rompe el flujo, el frontend ya lo
    guarda desde el paso 1.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Falta el token de sesión (Authorization: Bearer ...)")
    payload = verificar_token(auth[len("Bearer "):])
    if not payload or payload.get("emisor_id") != emisor_id:
        raise HTTPException(status_code=403, detail="No autorizado para este emisor")


@router.get("/estado/{emisor_id}")
async def estado_pago(
    emisor_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    El frontend consulta este endpoint después de que MP redirige
    al usuario de vuelta al sitio. Si está pagado, devuelve la API key.
    """
    _verificar_jwt_emisor(request, emisor_id)
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(404, "Cuenta no encontrada")

    if emisor.estado_pago == "pagado":
        return {
            "ok":      True,
            "pagado":  True,
            "api_key": emisor.api_key,
            "nombre_app":       emisor.nombre_app,
            "suscripcion_fin":  emisor.suscripcion_fin.isoformat() if emisor.suscripcion_fin else None,
        }

    return {
        "ok":     True,
        "pagado": False,
        "estado": emisor.estado_pago,
    }


# ── Dashboard del desarrollador ───────────────────────────────

# ══════════════════════════════════════════════════════════════
# MONITOR DE SERVIDORES DEL SII (cron cada 5 min)
# Chequea los servidores que YeparDTEcore realmente usa y que son
# alcanzables desde el server. Nota: rahue/maullin2 (boletas host viejo)
# NO resuelven por DNS fuera de Chile, por eso monitoreamos la API REST
# de boletas (api/apicert.sii.cl), que es la puerta real de producción.
# ══════════════════════════════════════════════════════════════
SII_SERVIDORES = [
    ("maullin",     "Maullín · DTE certificación", "https://maullin.sii.cl/DTEWS/CrSeed.jws"),
    ("palena",      "Palena · DTE producción",     "https://palena.sii.cl/DTEWS/CrSeed.jws"),
    ("boleta_cert", "Boletas · certificación",     "https://apicert.sii.cl/recursos/v1/boleta.electronica.semilla"),
    ("boleta_prod", "Boletas · producción",        "https://api.sii.cl/recursos/v1/boleta.electronica.semilla"),
]
SII_LATENCIA_LENTA_MS = 3500


async def ejecutar_check_sii(db: AsyncSession) -> list:
    """
    Chequea cada servidor del SII, mide latencia y registra caídas.
    La comparten el endpoint /check-sii-health y el monitor interno (main.py).
    """
    from app.models.sii_health import SIIHealth, SIIIncidente
    import time as _time

    ahora = datetime.now(timezone.utc)
    resultados = []
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        for clave, nombre, url in SII_SERVIDORES:
            estado, latencia = "caido", None
            t0 = _time.perf_counter()
            try:
                resp = await client.get(url)
                latencia = int((_time.perf_counter() - t0) * 1000)
                estado = "ok" if resp.status_code < 500 else "caido"
                if estado == "ok" and latencia > SII_LATENCIA_LENTA_MS:
                    estado = "lento"
            except Exception:
                latencia = int((_time.perf_counter() - t0) * 1000)
                estado = "caido"

            row = await db.get(SIIHealth, clave)
            if not row:
                row = SIIHealth(servidor=clave, nombre=nombre, url=url)
                db.add(row)
            row.nombre, row.url = nombre, url
            row.estado, row.latencia_ms, row.ultimo_check = estado, latencia, ahora

            estaba_caido = row.caido_desde is not None
            if estado == "caido":
                if not estaba_caido:
                    row.caido_desde = ahora
            else:
                row.ultimo_ok = ahora
                if estaba_caido:
                    inicio = row.caido_desde
                    if getattr(inicio, "tzinfo", None) is None:
                        inicio = inicio.replace(tzinfo=timezone.utc)
                    db.add(SIIIncidente(
                        servidor=clave, nombre=nombre, inicio=inicio, fin=ahora,
                        duracion_seg=int((ahora - inicio).total_seconds()),
                    ))
                    row.caido_desde = None
            resultados.append({"servidor": clave, "estado": estado, "latencia_ms": latencia})

    await db.commit()
    return resultados


@router.post("/check-sii-health")
async def check_sii_health(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Dispara el chequeo manualmente o desde un cron externo (X-Cron-Secret).
    El monitor interno de main.py ya lo corre solo cada 5 min; este endpoint
    queda como respaldo y para probarlo a mano.
    """
    _verificar_secreto(request, "X-Cron-Secret")
    resultados = await ejecutar_check_sii(db)
    return {"ok": True, "checked_at": datetime.now(timezone.utc).isoformat(),
            "resultados": resultados}


@router.get("/dashboard/{emisor_id}")
async def dashboard(
    emisor_id: int,
    request: Request,
    dias: int = 30,
    mes: str | None = None,   # "YYYY-MM" para ver un mes específico (tiene prioridad sobre dias)
    db: AsyncSession = Depends(get_db),
):
    """
    Dashboard del desarrollador, enfocado en la SALUD de la conexión con el SII
    (no en listar sus documentos, que el dev ya tiene en su app): estado de los
    servidores del SII con latencia e historial de caídas, señal de vida de la
    integración, tasa de aceptación, y volumen de DTE para costos (rango o mes).
    """
    _verificar_jwt_emisor(request, emisor_id)
    from app.models.dte import DTE
    from app.models.sii_health import SIIHealth, SIIIncidente
    from sqlalchemy import func
    from datetime import datetime, timezone, timedelta

    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(404, "Cuenta no encontrada")

    ahora = datetime.now(timezone.utc)

    # ── Ventana de tiempo: por mes (YYYY-MM) o por rango de días ───────────────
    modo = "mes" if mes else "rango"
    if mes:
        try:
            y, m = int(mes[:4]), int(mes[5:7])
            desde = datetime(y, m, 1, tzinfo=timezone.utc)
            hasta = datetime(y + (m // 12), (m % 12) + 1, 1, tzinfo=timezone.utc)
        except Exception:
            mes, modo = None, "rango"
    if not mes:
        dias = max(1, min(int(dias or 30), 365))
        desde = ahora - timedelta(days=dias)
        hasta = ahora + timedelta(days=1)

    TIPO_LBL = {33: "Factura", 34: "F.Exenta", 39: "Boleta", 41: "B.Exenta",
                52: "Guía", 56: "N.Débito", 61: "N.Crédito"}
    EXITO = {"ACEPTADO", "ACEPTADO_CON_REPAROS", "RECIBIDO"}
    ERROR = {"RECHAZADO", "ERROR", "ERROR_HTTP", "ERROR_PARSEO",
             "NO_AUTORIZADO", "TIMEOUT", "DESCONOCIDO"}

    # ── Suscripción ───────────────────────────────────────────────────────────
    dias_restantes = porcentaje_tiempo = None
    fin = emisor.suscripcion_fin
    if fin is not None and getattr(fin, "tzinfo", None) is None:
        fin = fin.replace(tzinfo=timezone.utc)
    vigente = (emisor.estado_pago == "pagado" and fin is not None and fin > ahora)
    if fin:
        dias_restantes = max(0, (fin - ahora).days)
        if emisor.suscripcion_inicio:
            inicio = emisor.suscripcion_inicio
            if getattr(inicio, "tzinfo", None) is None:
                inicio = inicio.replace(tzinfo=timezone.utc)
            total_dias = (fin - inicio).days or 365
            porcentaje_tiempo = min(100, max(0, round((ahora - inicio).days / total_dias * 100)))

    # ── Estado de los servidores del SII (leído del monitor de fondo) ──────────
    sii_servers, sii_incidentes = [], []
    monitor_activo = False
    try:
        rows = (await db.execute(select(SIIHealth))).scalars().all()
        by_key = {r.servidor: r for r in rows}
        for clave, nombre, url in SII_SERVIDORES:
            r = by_key.get(clave)
            if r and r.ultimo_check:
                monitor_activo = True
                uc = r.ultimo_check
                if getattr(uc, "tzinfo", None) is None:
                    uc = uc.replace(tzinfo=timezone.utc)
                # Si el último chequeo es muy viejo (>20 min), el monitor no corre
                fresco = (ahora - uc).total_seconds() < 20 * 60
                cd = r.caido_desde
                if cd is not None and getattr(cd, "tzinfo", None) is None:
                    cd = cd.replace(tzinfo=timezone.utc)
                sii_servers.append({
                    "servidor": clave, "nombre": r.nombre or nombre,
                    "estado": r.estado if fresco else "desconocido",
                    "latencia_ms": r.latencia_ms,
                    "ultimo_check": uc.strftime("%d/%m %H:%M"),
                    "caido_desde": cd.strftime("%d/%m %H:%M") if cd else None,
                    "fresco": fresco,
                })
            else:
                sii_servers.append({
                    "servidor": clave, "nombre": nombre, "estado": "desconocido",
                    "latencia_ms": None, "ultimo_check": None,
                    "caido_desde": None, "fresco": False,
                })
        res_inc = await db.execute(
            select(SIIIncidente).order_by(SIIIncidente.inicio.desc()).limit(8))
        for i in res_inc.scalars().all():
            mins = round((i.duracion_seg or 0) / 60)
            sii_incidentes.append({
                "nombre": i.nombre,
                "inicio": i.inicio.strftime("%d/%m %H:%M") if i.inicio else "",
                "fin":    i.fin.strftime("%d/%m %H:%M") if i.fin else "",
                "duracion": f"{mins} min" if mins else "<1 min",
            })
    except Exception:
        pass

    # ── Métricas de DTE (volumen para costos + señal de vida + tasa éxito) ─────
    total_dtes = total_rango = 0
    por_tipo, por_dia, pico = {}, [], None
    exitosos = errores = pendientes = 0
    tasa_exito = None
    ultima_actividad = None
    ultimos_errores = []
    try:
        total_dtes = (await db.execute(
            select(func.count(DTE.id)).where(DTE.emisor_id == emisor_id))).scalar() or 0

        ult = (await db.execute(
            select(func.max(DTE.created_at)).where(DTE.emisor_id == emisor_id))).scalar()
        if ult:
            if getattr(ult, "tzinfo", None) is None:
                ult = ult.replace(tzinfo=timezone.utc)
            horas = (ahora - ult).total_seconds() / 3600
            ultima_actividad = {
                "fecha": ult.strftime("%d/%m/%Y %H:%M"),
                "hace_horas": round(horas, 1),
                "reciente": horas < 48,
            }

        res_est = await db.execute(
            select(DTE.estado, func.count(DTE.id))
            .where(DTE.emisor_id == emisor_id, DTE.created_at >= desde, DTE.created_at < hasta)
            .group_by(DTE.estado))
        for est, c in res_est.fetchall():
            total_rango += c
            if est in EXITO:   exitosos += c
            elif est in ERROR: errores += c
            else:              pendientes += c
        if (exitosos + errores) > 0:
            tasa_exito = round(exitosos / (exitosos + errores) * 100)

        res_tipo = await db.execute(
            select(DTE.tipo_dte, func.count(DTE.id))
            .where(DTE.emisor_id == emisor_id, DTE.created_at >= desde, DTE.created_at < hasta)
            .group_by(DTE.tipo_dte))
        por_tipo = {TIPO_LBL.get(t, str(t)): c for t, c in res_tipo.fetchall()}

        res_dia = await db.execute(
            select(func.date(DTE.created_at), func.count(DTE.id))
            .where(DTE.emisor_id == emisor_id, DTE.created_at >= desde, DTE.created_at < hasta)
            .group_by(func.date(DTE.created_at)))
        conteo = {}
        for d, c in res_dia.fetchall():
            conteo[d.isoformat() if hasattr(d, "isoformat") else str(d)] = c
        n_dias = max(1, (hasta.date() - desde.date()).days)
        base_day = desde.date()
        for i in range(n_dias):
            d = base_day + timedelta(days=i)
            por_dia.append({"fecha": d.strftime("%d/%m"), "iso": d.isoformat(),
                            "count": conteo.get(d.isoformat(), 0)})
        if por_dia:
            top = max(por_dia, key=lambda x: x["count"])
            if top["count"] > 0:
                pico = {"fecha": top["fecha"], "count": top["count"]}

        res_err = await db.execute(
            select(DTE.tipo_dte, DTE.folio, DTE.estado, DTE.track_id, DTE.created_at)
            .where(DTE.emisor_id == emisor_id, DTE.estado.in_(list(ERROR)))
            .order_by(DTE.created_at.desc()).limit(8))
        ultimos_errores = [{
            "tipo": TIPO_LBL.get(r[0], str(r[0])), "folio": r[1],
            "estado": r[2], "track_id": r[3] or "—",
            "fecha": r[4].strftime("%d/%m/%Y %H:%M") if r[4] else "",
        } for r in res_err.fetchall()]
    except Exception:
        pass

    return {
        "ok": True,
        "cuenta": {
            "id": emisor.id, "nombre_app": emisor.nombre_app, "url_app": emisor.url_app,
            "correo": emisor.correo, "api_key": emisor.api_key,
            "estado_pago": emisor.estado_pago, "activa": emisor.activo,
        },
        "suscripcion": {
            "estado": emisor.estado_pago,
            "inicio": emisor.suscripcion_inicio.strftime("%d/%m/%Y") if emisor.suscripcion_inicio else None,
            "fin": emisor.suscripcion_fin.strftime("%d/%m/%Y") if emisor.suscripcion_fin else None,
            "dias_restantes": dias_restantes, "porcentaje_tiempo": porcentaje_tiempo,
            "vigente": vigente,
        },
        "integracion": {
            "app_vinculada": emisor.nombre_app, "url_app": emisor.url_app,
            "activa": emisor.activo,
            "ultima_actividad": ultima_actividad,
            "tasa_exito": tasa_exito,
        },
        "sii": {
            "monitor_activo": monitor_activo,
            "servidores": sii_servers,
            "incidentes": sii_incidentes,
        },
        "uso": {
            "modo": modo, "rango_dias": (None if mes else dias), "mes": mes,
            "total_dtes": total_dtes, "total_rango": total_rango,
            "por_dia": por_dia, "pico": pico, "por_tipo": por_tipo,
            "por_estado": {"exitosos": exitosos, "errores": errores, "pendientes": pendientes},
            "ultimos_errores": ultimos_errores,
        },
    }


# ══════════════════════════════════════════════════════════════
# JOB DIARIO — Notificaciones de renovación
# Se llama desde un endpoint protegido que Railway ejecuta
# vía cron job (o puede llamarse desde un scheduler externo).
#
# Lógica:
#   - 30 días antes del vencimiento → email de aviso
#   - 7 días antes → email urgente
#   - El día del vencimiento → email final
# ══════════════════════════════════════════════════════════════

@router.post("/notificar-renovaciones")
async def notificar_renovaciones(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Job diario que envía emails de renovación a cuentas por vencer.
    Protegido por header X-Cron-Secret para que solo Railway lo llame.
    """
    # Validar secret para que no lo llame cualquiera
    _verificar_secreto(request, "X-Cron-Secret")

    ahora = datetime.now(timezone.utc)

    # ── Marcar como vencidas las cuentas cuya suscripción ya expiró ────────────
    # La fecha (suscripcion_fin) es la fuente de verdad; acá la reflejamos en
    # estado_pago para que el panel y el bloqueo de renovación queden coherentes.
    # Una cuenta con fin en el futuro (ej. 2300, cuentas permanentes) NUNCA se toca.
    res_venc = await db.execute(
        select(Emisor).where(
            Emisor.estado_pago == "pagado",
            Emisor.suscripcion_fin.isnot(None),
            Emisor.suscripcion_fin < ahora,
        )
    )
    vencidas = res_venc.scalars().all()
    for _em in vencidas:
        _em.estado_pago = "vencido"
    if vencidas:
        await db.commit()
        logger.info(f"[RENOVACION] {len(vencidas)} cuenta(s) marcadas como vencidas")

    notificados = []

    # Buscar emisores que vencen en 30, 7 o 1 día
    for dias_aviso in [30, 7, 1]:
        fecha_objetivo = ahora + timedelta(days=dias_aviso)
        fecha_desde    = fecha_objetivo.replace(hour=0, minute=0, second=0, microsecond=0)
        fecha_hasta    = fecha_objetivo.replace(hour=23, minute=59, second=59)

        res = await db.execute(
            select(Emisor).where(
                Emisor.estado_pago == "pagado",
                Emisor.suscripcion_fin >= fecha_desde,
                Emisor.suscripcion_fin <= fecha_hasta,
                Emisor.correo.isnot(None),
            )
        )
        emisores = res.scalars().all()

        for emisor in emisores:
            try:
                from app.services.email_service import enviar_email

                if dias_aviso == 30:
                    asunto = "Tu suscripción YeparDTEcore vence en 30 días"
                    urgencia = "En 30 días"
                    color = "#d97706"
                elif dias_aviso == 7:
                    asunto = "⚠️ Tu suscripción YeparDTEcore vence en 7 días"
                    urgencia = "En solo 7 días"
                    color = "#dc2626"
                else:
                    asunto = "🚨 Tu suscripción YeparDTEcore vence HOY"
                    urgencia = "HOY"
                    color = "#dc2626"

                fin_fmt = emisor.suscripcion_fin.strftime("%d/%m/%Y") if emisor.suscripcion_fin else "—"

                html = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"></head>
<body style="font-family:'DM Sans',Arial,sans-serif;background:#f8f9fa;margin:0;padding:40px 20px;">
  <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:16px;
              padding:36px;border:1px solid #e0e0e0;">
    <img src="https://app.yepardtecore.cl/static/logo-300x130.png"
         alt="YeparDTEcore" style="height:36px;margin-bottom:28px;">
    <h2 style="color:{color};font-size:1.3rem;margin-bottom:10px;">
      Tu suscripción vence {urgencia}
    </h2>
    <p style="color:#4a4a4a;line-height:1.7;margin-bottom:16px;">
      Hola <strong>{emisor.nombre_app}</strong>, tu suscripción a YeparDTEcore
      vence el <strong>{fin_fmt}</strong>. Para continuar usando la API sin
      interrupciones, renueva ahora.
    </p>
    <div style="background:#f0f4ff;border-radius:10px;padding:16px;margin-bottom:20px;">
      <strong>Plan Anual — $150.000 + IVA ($178.500 CLP)</strong><br>
      <span style="font-size:.85rem;color:#64748b;">
        DTEs ilimitados · Misma API key · Sin cambios en tu software
      </span>
    </div>
    <a href="https://app.yepardtecore.cl/dashboard"
       style="display:block;text-align:center;background:{color};color:#fff;
              padding:13px;border-radius:10px;text-decoration:none;
              font-weight:700;font-size:.95rem;margin-bottom:16px;">
      Renovar suscripción →
    </a>
    <p style="font-size:.78rem;color:#94a3b8;line-height:1.5;">
      Si ya renovaste, ignora este mensaje.<br>
      Soporte: soporte@yeparsolutions.com
    </p>
    <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
    <p style="font-size:.72rem;color:#999;">
      YeparDTEcore · Yepar Solutions SpA · Santiago, Chile
    </p>
  </div>
</body></html>"""

                enviado = await enviar_email(emisor.correo, asunto, html)
                if enviado:
                    notificados.append({
                        "emisor_id": emisor.id,
                        "nombre_app": emisor.nombre_app,
                        "dias": dias_aviso,
                        "vence": fin_fmt,
                    })
                    logger.info(
                        f"[RENOVACION] Email enviado a {emisor.correo} "
                        f"({dias_aviso} días para vencer)"
                    )
            except Exception as e:
                logger.error(f"[RENOVACION] Error notificando {emisor.correo}: {e}")

    return {
        "ok":          True,
        "notificados": len(notificados),
        "detalle":     notificados,
        "ejecutado":   ahora.isoformat(),
    }


# ══════════════════════════════════════════════════════════════
# CAMBIAR AMBIENTE DEL EMISOR
# El desarrollador puede cambiar entre certificacion/produccion
# desde su dashboard. El ambiente guardado se usa como default
# cuando no viene en el request.
# ══════════════════════════════════════════════════════════════

@router.put("/ambiente/{emisor_id}")
async def cambiar_ambiente(
    emisor_id: int,
    db: AsyncSession = Depends(get_db),
    ambiente: str = "certificacion",
):
    """
    Cambia el ambiente default del emisor.
    El desarrollador lo controla desde su dashboard.
    Puede ser sobreescrito por request incluyendo el campo ambiente.
    """
    if ambiente not in ("certificacion", "produccion"):
        raise HTTPException(422, "Ambiente debe ser 'certificacion' o 'produccion'")

    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(404, "Emisor no encontrado")

    ambiente_anterior = emisor.ambiente
    emisor.ambiente   = ambiente
    await db.commit()

    logger.info(
        f"[AMBIENTE] Emisor {emisor_id} ({emisor.nombre_app}): "
        f"{ambiente_anterior} → {ambiente}"
    )

    return {
        "ok":       True,
        "emisor_id": emisor_id,
        "ambiente":  ambiente,
        "mensaje":  f"Ambiente cambiado a {ambiente}. "
                    f"Las llamadas sin campo 'ambiente' usarán {ambiente} por defecto.",
    }


# ══════════════════════════════════════════════════════════════
# PANEL DE ADMIN — Datos para el panel de administración
# Protegido por X-Admin-Secret en Railway
# ══════════════════════════════════════════════════════════════

@router.get("/admin/resumen")
async def admin_resumen(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Resumen general para el panel de admin."""
    _verificar_secreto(request)

    from sqlalchemy import func
    from app.models.usuario import Usuario

    ahora = datetime.now(timezone.utc)

    # Totales
    res_total = await db.execute(select(func.count(Emisor.id)).where(Emisor.nombre_app.isnot(None)))
    total_devs = res_total.scalar() or 0

    res_pagados = await db.execute(select(func.count(Emisor.id)).where(
        Emisor.nombre_app.isnot(None), Emisor.estado_pago == "pagado"))
    total_pagados = res_pagados.scalar() or 0

    res_pendientes = await db.execute(select(func.count(Emisor.id)).where(
        Emisor.nombre_app.isnot(None), Emisor.estado_pago == "pendiente"))
    total_pendientes = res_pendientes.scalar() or 0

    # Ingresos estimados
    ingresos_estimados = total_pagados * PRECIO_NETO   # neto; el IVA se remite al SII

    # Vencen en 30 días
    en_30 = ahora + timedelta(days=30)
    res_vencen = await db.execute(select(func.count(Emisor.id)).where(
        Emisor.estado_pago == "pagado",
        Emisor.suscripcion_fin <= en_30,
        Emisor.suscripcion_fin >= ahora,
    ))
    vencen_30 = res_vencen.scalar() or 0

    # Ya vencidos
    res_vencidos = await db.execute(select(func.count(Emisor.id)).where(
        Emisor.estado_pago == "pagado",
        Emisor.suscripcion_fin < ahora,
    ))
    ya_vencidos = res_vencidos.scalar() or 0

    # Lista de desarrolladores
    res_devs = await db.execute(
        select(Emisor).where(Emisor.nombre_app.isnot(None)).order_by(Emisor.id.desc())
    )
    devs = res_devs.scalars().all()

    lista = []
    for d in devs:
        dias_restantes = None
        if d.suscripcion_fin:
            fin = d.suscripcion_fin
            if hasattr(fin, 'tzinfo') and fin.tzinfo is None:
                fin = fin.replace(tzinfo=timezone.utc)
            dias_restantes = (fin - ahora).days

        lista.append({
            "id":            d.id,
            "nombre_app":    d.nombre_app,
            "url_app":       d.url_app,
            "correo":        d.correo,
            "estado_pago":   d.estado_pago,
            "activo":        d.activo,
            "ambiente":      d.ambiente,
            "plan":          d.plan,
            "suscripcion_inicio": d.suscripcion_inicio.strftime("%d/%m/%Y") if d.suscripcion_inicio else None,
            "suscripcion_fin":    d.suscripcion_fin.strftime("%d/%m/%Y") if d.suscripcion_fin else None,
            "dias_restantes": dias_restantes,
            "api_key_prefix": d.api_key[:16] + "..." if d.api_key else None,
            "created_at":    d.created_at.strftime("%d/%m/%Y") if d.created_at else None,
        })

    return {
        "ok": True,
        "resumen": {
            "total_desarrolladores": total_devs,
            "pagados":               total_pagados,
            "pendientes":            total_pendientes,
            "ingresos_estimados_clp": ingresos_estimados,
            "vencen_en_30_dias":     vencen_30,
            "ya_vencidos":           ya_vencidos,
            "consultado_en":         ahora.strftime("%d/%m/%Y %H:%M"),
        },
        "desarrolladores": lista,
    }


@router.put("/admin/emisor/{emisor_id}/toggle")
async def admin_toggle_emisor(
    emisor_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Activa o desactiva un emisor desde el panel de admin."""
    _verificar_secreto(request)

    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(404, "Emisor no encontrado")

    emisor.activo = not emisor.activo
    await db.commit()

    logger.info(f"[ADMIN] Emisor {emisor_id} ({emisor.nombre_app}): activo={emisor.activo}")

    return {
        "ok":       True,
        "emisor_id": emisor_id,
        "nombre_app": emisor.nombre_app,
        "activo":   emisor.activo,
    }


@router.put("/admin/emisor/{emisor_id}/activar-pago")
async def admin_activar_pago(
    emisor_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Activa manualmente el pago de un emisor (ej: transferencia bancaria)."""
    _verificar_secreto(request)

    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(404, "Emisor no encontrado")

    ahora = datetime.now(timezone.utc)

    # Si ya tiene suscripción activa, extender desde el fin actual
    if emisor.suscripcion_fin and emisor.suscripcion_fin > ahora:
        fin = emisor.suscripcion_fin
        if hasattr(fin, 'tzinfo') and fin.tzinfo is None:
            fin = fin.replace(tzinfo=timezone.utc)
        nuevo_fin = fin + timedelta(days=365)
    else:
        nuevo_fin = ahora + timedelta(days=365)

    emisor.estado_pago        = "pagado"
    emisor.ambiente           = "produccion"   # al activar el pago, se habilita producción
    emisor.suscripcion_inicio = ahora
    emisor.suscripcion_fin    = nuevo_fin
    await db.commit()

    logger.info(f"[ADMIN] Pago activado manualmente para emisor {emisor_id} ({emisor.nombre_app})")

    return {
        "ok":              True,
        "emisor_id":       emisor_id,
        "nombre_app":      emisor.nombre_app,
        "estado_pago":     emisor.estado_pago,
        "suscripcion_fin": nuevo_fin.strftime("%d/%m/%Y"),
    }
