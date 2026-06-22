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

MONTO_SUSCRIPCION = 100000   # $100.000 CLP
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
                <img src="https://yepardtecore.cl/static/logo-horizontal.svg"
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
                  Documentación: <a href="https://yepardtecore.cl/api/docs">yepardtecore.cl/api/docs</a>
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


# ── Dashboard del desarrollador ───────────────────────────────

@router.get("/dashboard/{emisor_id}")
async def dashboard(
    emisor_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Datos completos para el dashboard del desarrollador.
    Incluye: info de la cuenta, suscripción, métricas de uso y API key.
    """
    from app.models.dte import DTE
    from sqlalchemy import func, case
    from datetime import datetime, timezone

    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(404, "Cuenta no encontrada")

    ahora = datetime.now(timezone.utc)

    # Días restantes de suscripción
    dias_restantes = None
    porcentaje_tiempo = None
    if emisor.suscripcion_fin:
        fin = emisor.suscripcion_fin
        if hasattr(fin, 'tzinfo') and fin.tzinfo is None:
            fin = fin.replace(tzinfo=timezone.utc)
        delta = fin - ahora
        dias_restantes = max(0, delta.days)
        if emisor.suscripcion_inicio:
            inicio = emisor.suscripcion_inicio
            if hasattr(inicio, 'tzinfo') and inicio.tzinfo is None:
                inicio = inicio.replace(tzinfo=timezone.utc)
            total_dias = (fin - inicio).days or 365
            transcurridos = (ahora - inicio).days
            porcentaje_tiempo = min(100, round(transcurridos / total_dias * 100))

    # Métricas de DTE (solo si tiene documentos en BD)
    try:
        res_total = await db.execute(
            select(func.count(DTE.id)).where(DTE.emisor_id == emisor_id)
        )
        total_dtes = res_total.scalar() or 0

        res_por_tipo = await db.execute(
            select(DTE.tipo_dte, func.count(DTE.id))
            .where(DTE.emisor_id == emisor_id)
            .group_by(DTE.tipo_dte)
        )
        por_tipo = {
            {33:"Facturas",34:"F.Exentas",39:"Boletas",41:"B.Exentas",
             52:"Guías",56:"N.Débito",61:"N.Crédito"}.get(t, str(t)): c
            for t, c in res_por_tipo.fetchall()
        }

        res_mes = await db.execute(
            select(func.count(DTE.id)).where(
                DTE.emisor_id == emisor_id,
                DTE.created_at >= ahora.replace(day=1, hour=0, minute=0, second=0)
            )
        )
        dtes_este_mes = res_mes.scalar() or 0

        res_recientes = await db.execute(
            select(DTE.tipo_dte, DTE.folio, DTE.monto_total, DTE.estado, DTE.created_at)
            .where(DTE.emisor_id == emisor_id)
            .order_by(DTE.created_at.desc())
            .limit(5)
        )
        recientes = [
            {
                "tipo": {33:"Factura",34:"F.Exenta",39:"Boleta",41:"B.Exenta",
                         52:"Guía",56:"N.Débito",61:"N.Crédito"}.get(r[0], str(r[0])),
                "folio": r[1],
                "monto": r[2],
                "estado": r[3],
                "fecha": r[4].strftime("%d/%m/%Y %H:%M") if r[4] else "",
            }
            for r in res_recientes.fetchall()
        ]
    except Exception:
        total_dtes = dtes_este_mes = 0
        por_tipo = {}
        recientes = []

    return {
        "ok": True,
        "cuenta": {
            "id":         emisor.id,
            "nombre_app": emisor.nombre_app,
            "url_app":    emisor.url_app,
            "correo":     emisor.correo,
            "api_key":    emisor.api_key,
            "estado_pago": emisor.estado_pago,
            "ambiente":   emisor.ambiente,
        },
        "suscripcion": {
            "estado":           emisor.estado_pago,
            "inicio":           emisor.suscripcion_inicio.strftime("%d/%m/%Y") if emisor.suscripcion_inicio else None,
            "fin":              emisor.suscripcion_fin.strftime("%d/%m/%Y") if emisor.suscripcion_fin else None,
            "dias_restantes":   dias_restantes,
            "porcentaje_tiempo": porcentaje_tiempo,
        },
        "metricas": {
            "total_dtes":     total_dtes,
            "dtes_este_mes":  dtes_este_mes,
            "por_tipo":       por_tipo,
            "recientes":      recientes,
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
    secret = request.headers.get("X-Cron-Secret", "")
    if secret != settings.MP_WEBHOOK_SECRET:
        raise HTTPException(403, "No autorizado")

    ahora = datetime.now(timezone.utc)
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
    <img src="https://yepardtecore.cl/static/logo-horizontal.svg"
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
      <strong>Plan Anual — $100.000 CLP</strong><br>
      <span style="font-size:.85rem;color:#64748b;">
        DTEs ilimitados · Misma API key · Sin cambios en tu software
      </span>
    </div>
    <a href="https://yepardtecore.cl/dashboard"
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
