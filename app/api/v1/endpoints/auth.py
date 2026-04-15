# app/api/v1/endpoints/auth.py
# ══════════════════════════════════════════════════════════════
# Endpoints de autenticación
# POST /v1/auth/registro  — crea cuenta nueva
# POST /v1/auth/login     — inicia sesión, devuelve JWT
# GET  /v1/auth/me        — datos del usuario autenticado
# POST /v1/auth/logout    — cierra sesión (invalida token en cliente)
# ══════════════════════════════════════════════════════════════

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timezone
from pydantic import BaseModel, EmailStr
from app.db.base import get_db
from app.models.usuario import Usuario
from app.core.security import hash_password, verify_password, crear_access_token, verificar_token

router  = APIRouter(prefix="/auth", tags=["Autenticación"])
bearer  = HTTPBearer()


# ── Schemas ───────────────────────────────────────────────────

class RegistroInput(BaseModel):
    nombre:    str
    apellido:  str
    email:     EmailStr
    password:  str

class LoginInput(BaseModel):
    email:    EmailStr
    password: str

class TokenRespuesta(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    usuario: dict

class UsuarioRespuesta(BaseModel):
    id:        int
    nombre:    str
    apellido:  str
    email:     str
    es_admin:  bool
    emisor_id: int | None

    class Config:
        from_attributes = True


# ── Dependency: usuario autenticado ──────────────────────────

async def get_usuario_actual(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
    db: AsyncSession = Depends(get_db)
) -> Usuario:
    """
    Extrae y valida el JWT del header Authorization.
    Se usa como dependencia en endpoints protegidos:
        @router.get("/ruta")
        async def ruta(usuario = Depends(get_usuario_actual)):
    """
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


# ── Endpoints ─────────────────────────────────────────────────

@router.post("/registro", response_model=TokenRespuesta, status_code=201)
async def registro(datos: RegistroInput, db: AsyncSession = Depends(get_db)):
    """
    Crea una cuenta nueva en YeparDTE.
    Valida que el email no esté en uso y hashea la contraseña.
    Devuelve JWT para que el usuario quede logueado inmediatamente.
    """
    # Verificar que el email no existe
    resultado = await db.execute(select(Usuario).where(Usuario.email == datos.email))
    if resultado.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe una cuenta con ese email"
        )

    # Validar contraseña mínimo 8 caracteres
    if len(datos.password) < 8:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="La contraseña debe tener al menos 8 caracteres"
        )

    # Crear usuario
    usuario = Usuario(
        nombre=datos.nombre.strip(),
        apellido=datos.apellido.strip(),
        email=datos.email.lower().strip(),
        hashed_password=hash_password(datos.password),
        activo=True,
        verificado=False,
        es_admin=False,
    )
    db.add(usuario)
    await db.flush()

    # Generar token JWT
    token = crear_access_token({"sub": str(usuario.id), "email": usuario.email})

    return {
        "access_token": token,
        "token_type": "bearer",
        "usuario": {
            "id":        usuario.id,
            "nombre":    usuario.nombre,
            "apellido":  usuario.apellido,
            "email":     usuario.email,
            "es_admin":  usuario.es_admin,
            "emisor_id": usuario.emisor_id,
        }
    }


@router.post("/login", response_model=TokenRespuesta)
async def login(datos: LoginInput, db: AsyncSession = Depends(get_db)):
    """
    Inicia sesión con email y contraseña.
    Devuelve JWT token válido por ACCESS_TOKEN_EXPIRE_MINUTES minutos.
    """
    # Buscar usuario por email
    resultado = await db.execute(
        select(Usuario).where(Usuario.email == datos.email.lower().strip())
    )
    usuario = resultado.scalar_one_or_none()

    # Mismo mensaje para email o password incorrectos — no revelar cuál falló
    if not usuario or not verify_password(datos.password, usuario.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o contraseña incorrectos"
        )

    if not usuario.activo:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cuenta desactivada — contacta a soporte"
        )

    # Actualizar último login
    usuario.ultimo_login = datetime.now(timezone.utc)

    # Generar token
    token = crear_access_token({"sub": str(usuario.id), "email": usuario.email})

    return {
        "access_token": token,
        "token_type": "bearer",
        "usuario": {
            "id":        usuario.id,
            "nombre":    usuario.nombre,
            "apellido":  usuario.apellido,
            "email":     usuario.email,
            "es_admin":  usuario.es_admin,
            "emisor_id": usuario.emisor_id,
        }
    }


@router.get("/me", response_model=UsuarioRespuesta)
async def me(usuario: Usuario = Depends(get_usuario_actual)):
    """Devuelve los datos del usuario autenticado."""
    return usuario


@router.post("/logout")
async def logout():
    """
    Cierra sesión.
    El cliente debe eliminar el token de su almacenamiento.
    Los JWT son stateless — no se pueden invalidar en el servidor
    sin una lista negra (lo implementamos en Fase 2 si es necesario).
    """
    return {"mensaje": "Sesión cerrada — elimina el token del cliente"}
