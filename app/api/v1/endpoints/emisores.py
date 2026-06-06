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
    # Verificar que el RUT no esté ya registrado
    resultado = await db.execute(select(Emisor).where(Emisor.rut == datos.rut))
    existente = resultado.scalar_one_or_none()
    if existente:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya existe un emisor con RUT {datos.rut}"
        )

    # Generar API key única (64 caracteres hex)
    # Analogia: es la llave de la caja fuerte — única, secreta, intransferible
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
    await db.flush()  # Obtiene el ID sin hacer commit todavía

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


# ── Actualizar datos generales del emisor ────────────────────────────────────
class EmisorUpdate(BaseModel):
    # Analogía: actualizar el expediente de un cliente en la notaría
    razon_social: str | None = None
    giro:         str | None = None
    direccion:    str | None = None
    comuna:       str | None = None
    ciudad:       str | None = None
    telefono:     str | None = None
    acteco:       str | None = None
    ambiente:     str | None = None  # "certificacion" | "produccion"


@router.put("/{emisor_id}")
async def actualizar_emisor(
    emisor_id: int,
    datos: EmisorUpdate,
    db: AsyncSession = Depends(get_db),
):
    """
    Actualiza los datos generales del emisor.
    Solo actualiza los campos que vienen en el body (PATCH semántico).
    """
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(404, "Emisor no encontrado")

    # Solo actualizar campos que vienen explícitamente en el body
    if datos.razon_social is not None: emisor.razon_social = datos.razon_social
    if datos.giro         is not None: emisor.giro         = datos.giro
    if datos.direccion    is not None: emisor.direccion    = datos.direccion
    if datos.comuna       is not None: emisor.comuna       = datos.comuna
    if datos.ciudad       is not None: emisor.ciudad       = datos.ciudad
    if datos.telefono     is not None: emisor.telefono     = datos.telefono
    if datos.acteco       is not None: emisor.acteco       = datos.acteco
    if datos.ambiente     is not None: emisor.ambiente     = datos.ambiente

    await db.commit()
    await db.refresh(emisor)
    return {"ok": True, "emisor_id": emisor.id, "razon_social": emisor.razon_social}


# ── Actualizar resoluciones SII ───────────────────────────────────────────────
class ResolucionUpdate(BaseModel):
    # Cada ambiente tiene su propio número y fecha de resolución
    # Analogía: cada local (prueba/real) tiene su propio número de patente
    nro_resol_cert: str | None = None  # Certificación: "0"
    fch_resol_cert: str | None = None  # Certificación: "2026-04-19"
    nro_resol_prod: str | None = None  # Producción:    "99"
    fch_resol_prod: str | None = None  # Producción:    "2014-10-21"


@router.put("/{emisor_id}/resolucion")
async def actualizar_resolucion(
    emisor_id: int,
    datos: ResolucionUpdate,
    db: AsyncSession = Depends(get_db),
):
    """
    Actualiza los datos de resolución SII del emisor para cada ambiente.
    """
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(404, "Emisor no encontrado")

    if datos.nro_resol_cert is not None: emisor.nro_resol_cert = datos.nro_resol_cert
    if datos.fch_resol_cert is not None: emisor.fch_resol_cert = datos.fch_resol_cert
    if datos.nro_resol_prod is not None: emisor.nro_resol_prod = datos.nro_resol_prod
    if datos.fch_resol_prod is not None: emisor.fch_resol_prod = datos.fch_resol_prod

    await db.commit()
    return {
        "ok": True,
        "emisor_id": emisor_id,
        "certificacion": {"nro": emisor.nro_resol_cert, "fch": emisor.fch_resol_cert},
        "produccion":    {"nro": emisor.nro_resol_prod, "fch": emisor.fch_resol_prod},
    }
