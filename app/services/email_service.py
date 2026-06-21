# app/services/email_service.py
# ══════════════════════════════════════════════════════════════
# Servicio de envío de emails transaccionales
# Usa aiosmtplib (async SMTP) para no bloquear el event loop.
#
# Variables de entorno requeridas en Railway:
#   SMTP_HOST     — servidor SMTP  (ej: smtp.gmail.com)
#   SMTP_PORT     — puerto         (ej: 587)
#   SMTP_USER     — usuario/email  (ej: noreply@yeparsolutions.com)
#   SMTP_PASSWORD — contraseña / app password
#   EMAIL_FROM    — dirección "De" (puede ser igual a SMTP_USER)
#
# Analogía: es el cartero del sistema — recibe el sobre,
# sabe a qué buzón llevarlo, y confirma que llegó.
# ══════════════════════════════════════════════════════════════

import logging
import aiosmtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from app.core.config import settings

logger = logging.getLogger("yepardtecore.email")


async def enviar_email(destinatario: str, asunto: str, html: str) -> bool:
    """
    Envía un email HTML de forma asíncrona.
    Retorna True si se envió, False si falló (nunca lanza excepción).
    Así el flujo de negocio no se rompe si el email falla.
    """
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = asunto
        msg["From"]    = settings.EMAIL_FROM
        msg["To"]      = destinatario
        msg.attach(MIMEText(html, "html", "utf-8"))

        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            start_tls=True,
        )
        logger.info(f"[EMAIL] Enviado a {destinatario}: {asunto}")
        return True

    except Exception as e:
        logger.error(f"[EMAIL] Error enviando a {destinatario}: {e}")
        return False


# ── Templates de email ────────────────────────────────────────

def _base_template(titulo: str, cuerpo: str) -> str:
    """Template base con el estilo de Yepar."""
    return f"""
    <!DOCTYPE html>
    <html lang="es">
    <head><meta charset="UTF-8"></head>
    <body style="font-family:'DM Sans',Arial,sans-serif;background:#f8f9fa;
                 margin:0;padding:40px 20px;">
      <div style="max-width:480px;margin:0 auto;background:#fff;
                  border-radius:16px;padding:36px;
                  border:1px solid #e0e0e0;">
        <img src="https://yepardtecore.cl/static/logo-horizontal.svg"
             alt="YeparDTEcore" style="height:36px;margin-bottom:28px;">
        <h2 style="font-size:1.4rem;color:#1a1a1a;margin-bottom:10px;">{titulo}</h2>
        {cuerpo}
        <hr style="border:none;border-top:1px solid #eee;margin:28px 0;">
        <p style="font-size:0.75rem;color:#999;line-height:1.5;">
          YeparDTEcore · Yepar Solutions SpA<br>
          Este es un email automático, no respondas a este mensaje.
        </p>
      </div>
    </body>
    </html>
    """


def email_verificacion(nombre: str, codigo: str) -> tuple[str, str]:
    """
    Email de verificación con código OTP de 6 dígitos.
    Retorna (asunto, html).
    """
    asunto = "Verifica tu email — YeparDTEcore"
    cuerpo = f"""
        <p style="color:#4a4a4a;margin-bottom:20px;line-height:1.6;">
          Hola <strong>{nombre}</strong>, gracias por registrarte.<br>
          Usa este código para verificar tu email:
        </p>
        <div style="background:#f0f4ff;border-radius:12px;padding:20px;
                    text-align:center;margin:20px 0;">
          <span style="font-size:2.4rem;font-weight:700;letter-spacing:10px;
                       color:#0057ff;font-family:monospace;">{codigo}</span>
        </div>
        <p style="color:#6c757d;font-size:0.85rem;line-height:1.5;">
          Este código expira en <strong>15 minutos</strong>.<br>
          Si no creaste esta cuenta, ignora este mensaje.
        </p>
    """
    return asunto, _base_template("Verifica tu email", cuerpo)


def email_recuperacion(nombre: str, codigo: str) -> tuple[str, str]:
    """
    Email de recuperación de contraseña con código OTP.
    Retorna (asunto, html).
    """
    asunto = "Recupera tu contraseña — YeparDTEcore"
    cuerpo = f"""
        <p style="color:#4a4a4a;margin-bottom:20px;line-height:1.6;">
          Hola <strong>{nombre}</strong>, recibimos una solicitud para
          restablecer la contraseña de tu cuenta.
        </p>
        <p style="color:#4a4a4a;margin-bottom:16px;">
          Usa este código para crear una nueva contraseña:
        </p>
        <div style="background:#fff8e1;border:1px solid #ffe082;
                    border-radius:12px;padding:20px;
                    text-align:center;margin:20px 0;">
          <span style="font-size:2.4rem;font-weight:700;letter-spacing:10px;
                       color:#e67e00;font-family:monospace;">{codigo}</span>
        </div>
        <p style="color:#6c757d;font-size:0.85rem;line-height:1.5;">
          Este código expira en <strong>15 minutos</strong>.<br>
          Si no solicitaste esto, ignora este mensaje y tu contraseña
          no cambiará.
        </p>
    """
    return asunto, _base_template("Recupera tu contraseña", cuerpo)
