
# app/core/config.py
# ══════════════════════════════════════════════════════════════
# Configuración central de YeparDTEcore
# Analogia: es el tablero de control del avión — desde aquí
# se leen todas las variables de entorno y se validan antes
# de que la app arranque. Si falta algo crítico, falla aquí
# y no en producción cuando ya hay clientes.
# ══════════════════════════════════════════════════════════════

from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import Literal


class Settings(BaseSettings):
    """
    Lee automáticamente las variables desde el archivo .env
    o desde las variables de entorno del sistema (Railway).
    """

    # ── Identificación de la app ──────────────────────────────
    APP_NAME: str = "YeparDTEcore"
    APP_VERSION: str = "0.1.0"
    API_PREFIX: str = "/v1"

    # ── Entorno ───────────────────────────────────────────────
    ENVIRONMENT: Literal["development", "production"] = "development"
    DEBUG: bool = True
    APP_BASE_URL: str = "https://yepardtecore.yeparsolutions.com"
    FRONTEND_URL: str = "https://yeparstock.yeparsolutions.com"

    # ── Base de datos ─────────────────────────────────────────
    # En desarrollo: SQLite local (sin instalar nada)
    # En producción: PostgreSQL en Railway
    DATABASE_URL: str = "sqlite+aiosqlite:///./yepardte_dev.db"

    # ── Seguridad JWT ─────────────────────────────────────────
    SECRET_KEY: str = "dev_key_insegura_cambiar_en_produccion"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # ── SII Chile ─────────────────────────────────────────────
    SII_AMBIENTE: Literal["certificacion", "produccion"] = "certificacion"
    SII_URL_CERTIFICACION: str = "https://maullin.sii.cl/cgi_dte/UPL/DTEUpload"
    SII_URL_PRODUCCION: str = "https://palena.sii.cl/cgi_dte/UPL/DTEUpload"

    @property
    def SII_URL_ACTIVA(self) -> str:
        """Devuelve la URL correcta según el ambiente configurado."""
        if self.SII_AMBIENTE == "produccion":
            return self.SII_URL_PRODUCCION
        return self.SII_URL_CERTIFICACION

    @property
    def ES_PRODUCCION(self) -> bool:
        return self.ENVIRONMENT == "production"

    @field_validator("SECRET_KEY")
    @classmethod
    def validar_secret_key(cls, v: str) -> str:
        # En producción la clave debe ser segura (mínimo 32 caracteres)
        if v == "dev_key_insegura_cambiar_en_produccion":
            return v  # OK en desarrollo
        if len(v) < 32:
            raise ValueError("SECRET_KEY debe tener al menos 32 caracteres en producción")
        return v

    class Config:
        # Lee el archivo .env automáticamente si existe
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True


# ── Instancia global ──────────────────────────────────────────
# Se importa desde cualquier módulo con: from app.core.config import settings
settings = Settings()
