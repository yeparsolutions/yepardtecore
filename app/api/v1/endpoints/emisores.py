# app/api/v1/endpoints/emisores.py
# ══════════════════════════════════════════════════════════════
# Endpoints: Gestión de Emisores
# Un Emisor es cada empresa/negocio que emite DTE.
# ══════════════════════════════════════════════════════════════

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.base import get_db
from app.models.emisor import Emisor
from pydantic import BaseModel
import secrets

router = APIRouter(prefix="/emisores", tags=["Emisores"])


# ── Schemas de entrada/salida ─────────────────────────────────
class EmisorCrear(BaseModel):
    rut: str
    razon_social: str
    giro: str
    direccion: str
    comuna: str
    ciudad: str
    telefono: str | None = None
    ambiente: str = "certificacion"
    acteco: str | None = None


class EmisorRespuesta(BaseModel):
    id: int
    rut: str
    razon_social: str
    giro: str
    direccion: str
    comuna: str
    ciudad: str
    activo: bool
    ambiente: str
    acteco: str | None = None
    api_key: str | None

    class Config:
        from_attributes = True


# ── Endpoints ─────────────────────────────────────────────────

@router.post("/", response_model=EmisorRespuesta, status_code=status.HTTP_201_CREATED)
async def crear_emisor(datos: EmisorCrear, db: AsyncSession = Depends(get_db)):
    """
    Registra una nueva empresa emisora en YeparDTEcore.
    Genera automáticamente una API key única para que el emisor
    pueda autenticarse al llamar a la API.
    """
    # Si ya existe el emisor, actualizar sus datos
    resultado = await db.execute(select(Emisor).where(Emisor.rut == datos.rut))
    existente = resultado.scalar_one_or_none()
    if existente:
        existente.razon_social = datos.razon_social
        existente.giro         = datos.giro
        existente.direccion    = datos.direccion
        existente.comuna       = datos.comuna
        existente.ciudad       = datos.ciudad
        existente.telefono     = datos.telefono
        existente.ambiente     = datos.ambiente
        existente.acteco       = datos.acteco
        await db.flush()
        return existente

    # Generar API key única (64 caracteres hex)
    api_key = "yek_" + secrets.token_hex(30)  # yek = Yepar Key

    # Crear el emisor
    emisor = Emisor(
        rut=datos.rut,
        razon_social=datos.razon_social,
        giro=datos.giro,
        direccion=datos.direccion,
        comuna=datos.comuna,
        ciudad=datos.ciudad,
        telefono=datos.telefono,
        ambiente=datos.ambiente,
        acteco=datos.acteco,
        api_key=api_key,
    )

    db.add(emisor)
    await db.flush()

    return emisor


@router.get("/", response_model=list[EmisorRespuesta])
async def listar_emisores(db: AsyncSession = Depends(get_db)):
    """Lista todos los emisores registrados."""
    resultado = await db.execute(select(Emisor).where(Emisor.activo == True))
    return resultado.scalars().all()


@router.get("/{emisor_id}", response_model=EmisorRespuesta)
async def obtener_emisor(emisor_id: int, db: AsyncSession = Depends(get_db)):
    """Obtiene los datos de un emisor por ID."""
    resultado = await db.execute(select(Emisor).where(Emisor.id == emisor_id))
    emisor = resultado.scalar_one_or_none()
    if not emisor:
        raise HTTPException(status_code=404, detail="Emisor no encontrado")
    return emisor


@router.get("/{emisor_id}/folios")
async def folios_disponibles(emisor_id: int, db: AsyncSession = Depends(get_db)):
    """
    Muestra cuántos folios quedan disponibles por tipo de DTE.
    Útil para saber cuándo hay que pedir un nuevo CAF al SII.
    """
    from app.models.caf import CAF
    resultado = await db.execute(
        select(CAF).where(
            CAF.emisor_id == emisor_id,
            CAF.activo == True
        )
    )
    cafs = resultado.scalars().all()

    if not cafs:
        return {"emisor_id": emisor_id, "cafs": [], "mensaje": "Sin CAFs cargados"}

    return {
        "emisor_id": emisor_id,
        "cafs": [
            {
                "tipo_dte": c.tipo_dte,
                "tipo_nombre": {33: "Factura", 39: "Boleta", 61: "Nota Crédito", 56: "Nota Débito"}.get(c.tipo_dte, "Otro"),
                "folio_actual": c.folio_actual,
                "folio_hasta": c.folio_hasta,
                "disponibles": c.folios_disponibles,
                "porcentaje_uso": c.porcentaje_uso,
                "agotado": c.esta_agotado,
                "ambiente": c.ambiente,
            }
            for c in cafs
        ]
    }
