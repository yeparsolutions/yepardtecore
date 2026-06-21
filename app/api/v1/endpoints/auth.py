# app/api/v1/endpoints/auth.py
# ══════════════════════════════════════════════════════════════
# Endpoints de autenticación
#
#   POST /v1/auth/login               — login con email + password
#   GET  /v1/auth/me                  — datos del usuario autenticado
#   POST /v1/auth/logout              — cierra sesión
#   POST /v1/auth/verificar-email     — valida OTP de verificación
#   POST /v1/auth/reenviar-verificacion — reenvía el OTP de verificación
#   POST /v1/auth/recuperar-password  — solicita OTP de recuperación
#   POST /v1/auth/resetear-password   — valida OTP y cambia contraseña
# ══════════════════════════════════════════════════════════════

import random
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, EmailStr

from app.db.base import get_db
from app.models.usuario import Usuario
from app.core.security import (
    hash_password, verify_password,
    crear_access_token, verificar_token,
)
from app.services.email_service import (
    enviar_email, email_verificacion, email_recuperacion,
)

logger = logging.getLogger("yepardtecore.auth")
router = APIRouter(prefix="/auth", tags=["Autenticación"])
bearer = HTTPBearer()

OTP_EXPIRA_MINUTOS = 15


# ── Helpers ───────────────────────────────────────────────────

def _generar_otp() -> str:
    """Genera un código numérico de 6 dígitos."""
    return str(random.randint(100000, 999999))


def _otp_expira() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRA_MINUTOS)


# ── Schemas ───────────────────────────────────────────────────

class LoginInput(BaseModel):
    email:    EmailStr
    password: str

class TokenRespuesta(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    usuario:      dict
    verificado:   bool

class UsuarioRespuesta(BaseModel):
    id:        int
    nombre:    str
    apellido:  str
    email:     str
    es_admin:  bool
    emisor_id: int | None
    verificado: bool

    class Config:
        from_attributes = True

class VerificarEmailInput(BaseModel):
    email: EmailStr
    codigo: str

class ReenviarVerificacionInput(BaseModel):
    email: EmailStr

class RecuperarPasswordInput(BaseModel):
    email: EmailStr

class ResetearPasswordInput(BaseModel):
    email:    EmailStr
    codigo:   str
    password: str   # nueva contraseña


# ── Dependency: usuario autenticado ──────────────────────────

async def get_usuario_actual(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> Usuario:
    token = credentials.credentials
    payload = verificar_token(token)

    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o expirado",
            headers={"WWW-Authenticate": "Bearer"},
        )

    usuario_id = payload.get("sub")
    if not usuario_id:
        raise HTTPException(status_code=401, detail="Token sin usuario")

    resultado = await db.execute(
        select(Usuario).where(Usuario.id == int(usuario_id), Usuario.activo == True)
    )
    usuario = resultado.scalar_one_or_none()

    if not usuario:
        raise HTTPException(status_code=401, detail="Usuario no encontrado o inactivo")

    return usuario


# ── LOGIN ──────────────────────────────────────────────────────

@router.post("/login", response_model=TokenRespuesta)
async def login(datos: LoginInput, db: AsyncSession = Depends(get_db)):
    """
    Inicia sesión. Devuelve JWT + flag verificado para que
    el cliente sepa si debe pedir verificación de email.
    """
    resultado = await db.execute(
        select(Usuario).where(Usuario.email == datos.email.lower().strip())
    )
    usuario = resultado.scalar_one_or_none()

    if not usuario or not verify_password(datos.password, usuario.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o contraseña incorrectos",
        )

    if not usuario.activo:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cuenta desactivada — contacta a soporte",
        )

    usuario.ultimo_login = datetime.now(timezone.utc)
    await db.commit()

    token = crear_access_token({"sub": str(usuario.id), "email": usuario.email})

    return {
        "access_token": token,
        "token_type":   "bearer",
        "verificado":   usuario.verificado,
        "usuario": {
            "id":        usuario.id,
            "nombre":    usuario.nombre,
            "apellido":  usuario.apellido,
            "email":     usuario.email,
            "es_admin":  usuario.es_admin,
            "emisor_id": usuario.emisor_id,
            "verificado": usuario.verificado,
        },
    }


# ── ME ────────────────────────────────────────────────────────

@router.get("/me", response_model=UsuarioRespuesta)
async def me(usuario: Usuario = Depends(get_usuario_actual)):
    """Datos del usuario autenticado."""
    return usuario


# ── LOGOUT ────────────────────────────────────────────────────

@router.post("/logout")
async def logout():
    """El cliente debe eliminar el token. Los JWT son stateless."""
    return {"mensaje": "Sesión cerrada — elimina el token del cliente"}


# ── VERIFICACIÓN DE EMAIL ─────────────────────────────────────

@router.post("/verificar-email")
async def verificar_email(datos: VerificarEmailInput, db: AsyncSession = Depends(get_db)):
    """
    El usuario ingresa el código OTP de 6 dígitos que recibió por email.
    Si es válido y no expiró, marca su cuenta como verificada.
    """
    res = await db.execute(
        select(Usuario).where(Usuario.email == datos.email.lower().strip())
    )
    usuario = res.scalar_one_or_none()

    if not usuario:
        raise HTTPException(404, "Usuario no encontrado")

    if usuario.verificado:
        return {"ok": True, "mensaje": "Email ya verificado"}

    # Validar OTP
    if not usuario.otp_verificacion or usuario.otp_verificacion != datos.codigo.strip():
        raise HTTPException(400, "Código incorrecto")

    # Validar expiración
    if usuario.otp_verificacion_expira:
        expira = usuario.otp_verificacion_expira
        if hasattr(expira, 'tzinfo') and expira.tzinfo is None:
            expira = expira.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expira:
            raise HTTPException(400, "El código expiró. Solicita uno nuevo.")

    # Marcar verificado y limpiar OTP
    usuario.verificado             = True
    usuario.otp_verificacion       = None
    usuario.otp_verificacion_expira = None
    await db.commit()

    logger.info(f"[AUTH] Email verificado: {usuario.email}")
    return {"ok": True, "mensaje": "Email verificado correctamente"}


@router.post("/reenviar-verificacion")
async def reenviar_verificacion(
    datos: ReenviarVerificacionInput,
    db: AsyncSession = Depends(get_db),
):
    """
    Genera y envía un nuevo OTP de verificación.
    Útil si el anterior expiró o el usuario no lo recibió.
    """
    res = await db.execute(
        select(Usuario).where(Usuario.email == datos.email.lower().strip())
    )
    usuario = res.scalar_one_or_none()

    if not usuario:
        # No revelamos si el email existe o no
        return {"ok": True, "mensaje": "Si el email existe, recibirás un código."}

    if usuario.verificado:
        return {"ok": True, "mensaje": "Tu email ya está verificado."}

    # Generar nuevo OTP
    otp = _generar_otp()
    usuario.otp_verificacion        = otp
    usuario.otp_verificacion_expira = _otp_expira()
    await db.commit()

    asunto, html = email_verificacion(usuario.nombre, otp)
    await enviar_email(usuario.email, asunto, html)

    logger.info(f"[AUTH] OTP verificación reenviado a {usuario.email}")
    return {"ok": True, "mensaje": "Código enviado. Revisa tu bandeja de entrada."}


# ── RECUPERACIÓN DE CONTRASEÑA ────────────────────────────────

@router.post("/recuperar-password")
async def recuperar_password(
    datos: RecuperarPasswordInput,
    db: AsyncSession = Depends(get_db),
):
    """
    Genera un OTP de recuperación y lo envía al email.
    Siempre responde igual para no revelar si el email existe.
    """
    res = await db.execute(
        select(Usuario).where(Usuario.email == datos.email.lower().strip())
    )
    usuario = res.scalar_one_or_none()

    # Respuesta genérica independiente de si existe o no
    respuesta = {"ok": True, "mensaje": "Si el email existe, recibirás un código."}

    if not usuario or not usuario.activo:
        return respuesta

    otp = _generar_otp()
    usuario.otp_recuperacion        = otp
    usuario.otp_recuperacion_expira = _otp_expira()
    await db.commit()

    asunto, html = email_recuperacion(usuario.nombre, otp)
    await enviar_email(usuario.email, asunto, html)

    logger.info(f"[AUTH] OTP recuperación enviado a {usuario.email}")
    return respuesta


@router.post("/resetear-password")
async def resetear_password(
    datos: ResetearPasswordInput,
    db: AsyncSession = Depends(get_db),
):
    """
    Valida el OTP de recuperación y actualiza la contraseña.
    Invalida el OTP tras el primer uso exitoso.
    """
    if len(datos.password) < 8:
        raise HTTPException(422, "La nueva contraseña debe tener al menos 8 caracteres")

    res = await db.execute(
        select(Usuario).where(Usuario.email == datos.email.lower().strip())
    )
    usuario = res.scalar_one_or_none()

    if not usuario:
        raise HTTPException(400, "Código incorrecto o expirado")

    if not usuario.otp_recuperacion or usuario.otp_recuperacion != datos.codigo.strip():
        raise HTTPException(400, "Código incorrecto")

    if usuario.otp_recuperacion_expira:
        expira = usuario.otp_recuperacion_expira
        if hasattr(expira, 'tzinfo') and expira.tzinfo is None:
            expira = expira.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expira:
            raise HTTPException(400, "El código expiró. Solicita uno nuevo.")

    # Actualizar contraseña y limpiar OTP
    usuario.hashed_password         = hash_password(datos.password)
    usuario.otp_recuperacion        = None
    usuario.otp_recuperacion_expira = None
    await db.commit()

    logger.info(f"[AUTH] Contraseña reseteada para {usuario.email}")
    return {"ok": True, "mensaje": "Contraseña actualizada. Ya puedes iniciar sesión."}
