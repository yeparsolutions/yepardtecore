# app/api/v1/endpoints/sii_auth.py
# ══════════════════════════════════════════════════════════════
# Endpoints de autenticación con el SII
#
#   POST /v1/sii-auth/{emisor_id}/token   — Obtiene token del SII
#   GET  /v1/sii-auth/{emisor_id}/semilla — Prueba la semilla (diagnóstico)
# ══════════════════════════════════════════════════════════════

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.base    import get_db
from app.models.emisor import Emisor
from app.services.sii_auth import SIIAuth, obtener_token_cached

router = APIRouter(prefix="/sii-auth", tags=["Autenticación SII"])


@router.post("/{emisor_id}/token")
async def obtener_token_sii(
    emisor_id: int,
    db: AsyncSession = Depends(get_db)
):
    """
    Obtiene un TOKEN de autenticación del SII.

    Flujo interno:
    1. Carga el certificado del emisor desde BD
    2. Pide una semilla al SII
    3. Firma la semilla con el certificado
    4. El SII devuelve un TOKEN válido ~1 hora

    Este TOKEN se usa automáticamente al enviar documentos.
    No necesitas llamarlo manualmente — el motor lo hace solo.
    Úsalo para verificar que tu certificado funciona con el SII.
    """
    # Cargar emisor
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(status_code=404, detail="Emisor no encontrado")

    if not emisor.certificado_p12:
        raise HTTPException(
            status_code=400,
            detail="El emisor no tiene certificado digital cargado. "
                   "Usa POST /v1/certificados/{id}/subir primero."
        )

    try:
        token = await obtener_token_cached(
            p12_bytes = emisor.certificado_p12,
            password  = emisor.certificado_password or "",
            ambiente  = emisor.ambiente,
        )
        return {
            "ok":       True,
            "token":    token,
            "emisor":   emisor.razon_social,
            "ambiente": emisor.ambiente,
            "mensaje":  "✅ Token obtenido correctamente — válido por ~1 hora",
            "uso":      "Se usa automáticamente al enviar DTE. No necesitas guardarlo."
        }

    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Error al autenticar con el SII: {str(e)}"
        )


@router.get("/{emisor_id}/semilla")
async def probar_semilla(
    emisor_id: int,
    db: AsyncSession = Depends(get_db)
):
    """
    Prueba de diagnóstico: pide una semilla al SII sin firmarla.
    Útil para verificar que hay conectividad con el SII.
    """
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(status_code=404, detail="Emisor no encontrado")

    if not emisor.certificado_p12:
        raise HTTPException(status_code=400, detail="Sin certificado cargado")

    try:
        auth    = SIIAuth(emisor.certificado_p12, emisor.certificado_password or "",
                          emisor.ambiente)
        semilla = await auth._pedir_semilla()
        return {
            "ok":       True,
            "semilla":  semilla,
            "ambiente": emisor.ambiente,
            "mensaje":  "✅ Conectividad con SII OK — semilla obtenida correctamente"
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
