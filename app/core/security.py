# app/core/security.py
# ══════════════════════════════════════════════════════════════
# Seguridad: API Key, Rate Limiting, JWT y passwords
#
# Analogía: es el portero del edificio con lista de invitados,
# contador de entradas por hora y verificación de vigencia
# de los pases de acceso.
# ══════════════════════════════════════════════════════════════

import time
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Security, HTTPException, Depends, Request
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.db.base import get_db
from app.models.emisor import Emisor
from app.core.config import settings

logger = logging.getLogger("yepardtecore.security")

# ── API Key Header ────────────────────────────────────────────
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# ── Rate Limiter en memoria ───────────────────────────────────
# Analogía: es un torniquete que solo deja pasar N personas
# por minuto — si intentas pasar más rápido, se bloquea.
# Estructura: {api_key: [(timestamp1), (timestamp2), ...]}
_rate_limit_store: dict[str, list[float]] = defaultdict(list)

# Límites por plan (requests por minuto)
RATE_LIMITS = {
    "default":    60,   # 60 req/min para plan básico
    "pro":       200,   # 200 req/min para plan pro
    "enterprise": 600,  # 600 req/min para enterprise
}
RATE_WINDOW = 60  # ventana de 60 segundos


def _check_rate_limit(api_key: str, limite: int = RATE_LIMITS["default"]) -> None:
    """
    Verifica que el cliente no supere el límite de requests por minuto.
    Usa ventana deslizante — más justo que ventana fija.

    Raises:
        HTTPException 429: Si se supera el límite
    """
    ahora = time.time()
    ventana_inicio = ahora - RATE_WINDOW

    # Limpiar requests fuera de la ventana
    _rate_limit_store[api_key] = [
        ts for ts in _rate_limit_store[api_key]
        if ts > ventana_inicio
    ]

    requests_en_ventana = len(_rate_limit_store[api_key])

    if requests_en_ventana >= limite:
        logger.warning(
            f"[RATE_LIMIT] API Key {api_key[:12]}... superó límite "
            f"{requests_en_ventana}/{limite} req/min"
        )
        raise HTTPException(
            status_code=429,
            detail=f"Límite de {limite} requests por minuto superado. "
                   "Espera un momento antes de reintentar.",
            headers={"Retry-After": str(RATE_WINDOW)},
        )

    # Registrar esta request
    _rate_limit_store[api_key].append(ahora)


# ── Validar API Key ───────────────────────────────────────────

async def validar_api_key(
    request: Request,
    api_key: str = Security(api_key_header),
    db: AsyncSession = Depends(get_db),
) -> Emisor:
    """
    Valida la API Key, verifica expiración y aplica rate limiting.

    Flujo:
    1. Verifica que venga el header X-API-Key
    2. Busca el emisor en BD
    3. Verifica que el emisor esté activo
    4. Verifica que la API Key no haya expirado
    5. Aplica rate limiting
    6. Retorna el emisor

    Uso en endpoint:
        @router.post("/emitir")
        async def emitir(emisor: Emisor = Depends(validar_api_key)):
            ...
    """
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="API Key requerida. Incluye el header: X-API-Key: yek_...",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    # Buscar emisor por API Key en BD
    resultado = await db.execute(
        select(Emisor).where(
            Emisor.api_key == api_key,
            Emisor.activo  == True,
        )
    )
    emisor = resultado.scalar_one_or_none()

    if not emisor:
        logger.warning(
            f"[AUTH] API Key inválida: {api_key[:12]}... "
            f"desde {request.client.host if request.client else 'unknown'}"
        )
        raise HTTPException(
            status_code=403,
            detail="API Key inválida o emisor inactivo",
        )

    # Verificar expiración de API Key (si el emisor tiene fecha de expiración)
    if hasattr(emisor, 'api_key_expires_at') and emisor.api_key_expires_at:
        if datetime.now(timezone.utc) > emisor.api_key_expires_at:
            logger.warning(
                f"[AUTH] API Key expirada para emisor {emisor.rut}"
            )
            raise HTTPException(
                status_code=403,
                detail="API Key expirada. Genera una nueva desde el portal.",
            )

    # Aplicar rate limiting
    # TODO: leer el plan del emisor para aplicar límite correcto
    _check_rate_limit(api_key, RATE_LIMITS["default"])

    logger.debug(
        f"[AUTH] Emisor {emisor.rut} autenticado "
        f"desde {request.client.host if request.client else 'unknown'}"
    )

    return emisor


# ── Password hashing ──────────────────────────────────────────
# Analogía: bcrypt es la caja fuerte — convierte la contraseña
# en un hash irreversible. Nunca guardamos la contraseña real.

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Convierte una contraseña en texto plano a hash seguro."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica que una contraseña coincide con su hash."""
    return pwd_context.verify(plain_password, hashed_password)


# ── JWT Tokens ────────────────────────────────────────────────
# Analogía: el JWT es el carnet temporal del edificio —
# tiene tu nombre, fecha de vencimiento y el sello del guardia.

def crear_access_token(
    data: dict[str, Any],
    expires_delta: timedelta | None = None,
) -> str:
    """
    Genera un JWT token firmado.
    El token contiene: datos del usuario + fecha de expiración + firma.
    """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    })
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def verificar_token(token: str) -> dict | None:
    """
    Verifica y decodifica un JWT token.
    Retorna el payload si es válido, None si expiró o fue manipulado.
    """
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
        return payload
    except JWTError:
        return None


# ── Generar API Key ───────────────────────────────────────────

def generar_api_key() -> str:
    """
    Genera una API Key segura con prefijo yek_.
    Analogía: como generar un número de tarjeta de crédito
    único y difícil de adivinar.
    """
    import secrets
    token = secrets.token_hex(32)
    return f"yek_{token}"
