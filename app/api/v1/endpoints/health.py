# app/api/v1/endpoints/health.py
# ══════════════════════════════════════════════════════════════
# Endpoint: GET /v1/health
# Verifica que el servicio está vivo y conectado a la BD.
#
# Analogia: es el "¿aló?" antes de la llamada importante.
# Railway lo usa para saber si debe reiniciar el servicio.
# YeparStock lo llama antes de emitir para confirmar que
# DTEcore está disponible.
# ══════════════════════════════════════════════════════════════

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from app.db.base import get_db
from app.core.config import settings
import datetime

router = APIRouter()


@router.get("/health", tags=["Sistema"])
async def health_check(db: AsyncSession = Depends(get_db)):
    """
    Verifica el estado del servicio.
    Comprueba: app corriendo + conexión a BD + configuración.
    """

    # Verificar que la BD responde
    db_ok = False
    db_error = None
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        db_error = str(e)

    # Construir respuesta con toda la info relevante
    estado = "ok" if db_ok else "degradado"

    return {
        "status": estado,
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "ambiente": settings.ENVIRONMENT,
        "sii_ambiente": settings.SII_AMBIENTE,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "servicios": {
            "base_de_datos": "ok" if db_ok else f"error: {db_error}",
            "sii_url": settings.SII_URL_ACTIVA,
        }
    }
