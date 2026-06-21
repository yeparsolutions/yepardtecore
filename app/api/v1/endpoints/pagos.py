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

logger = logging.getLogger("yepardtecore.pagos")
router = APIRouter(prefix="/pagos", tags=["Pagos"])

MONTO_SUSCRIPCION = 1000   # $100.000 CLP
DIAS_SUSCRIPCION  = 365


# ── Crear preferencia de pago ─────────────────────────────────

class CrearPreferenciaInput(BaseModel):
    emisor_id: int
    email:     str   # email del pagador (para MP)


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

    if emisor.estado_pago == "pagado":
        return {
            "ok":     True,
            "pagado": True,
            "mensaje": "Esta cuenta ya tiene una suscripción activa.",
        }

    # Construir la preferencia para Checkout Pro
    preferencia = {
        "items": [
            {
                "title":       f"YeparDTEcore API — Suscripción Anual ({emisor.nombre_app})",
                "quantity":    1,
                "unit_price":  MONTO_SUSCRIPCION,
                "currency_id": "CLP",
                "description": "Acceso ilimitado a la API de facturación electrónica por 1 año",
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
        "external_reference": str(datos.emisor_id),   # para identificar en webhook
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

    if settings.MP_WEBHOOK_SECRET and x_signature:
        ts    = ""
        v1    = ""
        for part in x_signature.split(","):
            k, _, v = part.partition("=")
            if k.strip() == "ts":   ts = v.strip()
            if k.strip() == "v1":   v1 = v.strip()

        manifest = f"id:{request.query_params.get('data.id', '')};request-id:{x_request_id};ts:{ts};"
        esperado = hmac.HMAC(
            settings.MP_WEBHOOK_SECRET.encode(),
            manifest.encode(),
            hashlib.sha256)

        if not hmac.compare_digest(esperado, v1):
            logger.warning("[MP-WEBHOOK] Firma inválida — request ignorado")
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

    try:
        emisor_id = int(referencia)
    except ValueError:
        logger.warning(f"[MP-WEBHOOK] external_reference no es int: {referencia}")
        return {"ok": True, "advertencia": "referencia inválida"}

    res = await db.execute(select(Emisor).where(Emisor.id == emisor_id))
    emisor = res.scalar_one_or_none()

    if not emisor:
        logger.error(f"[MP-WEBHOOK] Emisor {emisor_id} no encontrado")
        return {"ok": False, "error": "Emisor no encontrado"}

    ahora = datetime.now(timezone.utc)
    emisor.estado_pago        = "pagado"
    emisor.suscripcion_inicio = ahora
    emisor.suscripcion_fin    = ahora + timedelta(days=DIAS_SUSCRIPCION)
    await db.commit()

    logger.info(
        f"[MP-WEBHOOK] ✓ Emisor {emisor_id} ({emisor.nombre_app}) activado. "
        f"Suscripción hasta {emisor.suscripcion_fin.date()}"
    )

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

@router.get("/estado/{emisor_id}")
async def estado_pago(
    emisor_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    El frontend consulta este endpoint después de que MP redirige
    al usuario de vuelta al sitio. Si está pagado, devuelve la API key.
    """
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
