# app/api/v1/endpoints/emisores.py
# ══════════════════════════════════════════════════════════════
# Endpoints: Gestión de Emisores
# Un Emisor es cada empresa/negocio que emite DTE.
# ══════════════════════════════════════════════════════════════

from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timedelta, timezone
from app.db.base import get_db
from app.models.emisor import Emisor
from app.models.usuario import Usuario
from app.core.security import hash_password, crear_access_token
from app.services.email_service import enviar_email, email_verificacion
import random
from pydantic import BaseModel, EmailStr
import secrets
import logging

logger = logging.getLogger("yepardtecore.emisores")

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


# ══════════════════════════════════════════════════════════════
# REGISTRO DE DESARROLLADORES
# Flujo para que un desarrollador externo contrate la API:
#   1. Se registra con sus datos + nombre de la app
#   2. Recibe su API key + JWT para acceder a su panel
#   3. La API key se "vincula" al dominio de su app en la
#      primera llamada (Opción C — sin carga para el dev)
#
# Analogía: como abrir una cuenta de Stripe — solo necesitas
# nombre, email y el nombre de tu negocio. La tarjeta (API key)
# te la dan al terminar el registro.
# ══════════════════════════════════════════════════════════════

class RegistroDesarrolladorInput(BaseModel):
    # Datos del desarrollador
    nombre:    str
    apellido:  str
    email:     EmailStr
    password:  str
    # App que va a integrar
    nombre_app: str
    url_app:    str


class RegistroDesarrolladorRespuesta(BaseModel):
    ok:           bool
    api_key:      str
    emisor_id:    int
    access_token: str   # JWT para acceder al panel/docs
    nombre_app:   str
    mensaje:      str


@router.post(
    "/registro-desarrollador",
    response_model=RegistroDesarrolladorRespuesta,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar desarrollador externo",
    description=(
        "Crea la cuenta de un desarrollador que quiere integrar YeparDTEcore en su software. "
        "No requiere datos de empresa ni certificado — solo sus datos y el nombre de su app. "
        "Genera automáticamente su API key y un token de acceso al panel."
    ),
)
async def registro_desarrollador(
    datos: RegistroDesarrolladorInput,
    db: AsyncSession = Depends(get_db),
):
    """
    Registra un desarrollador nuevo y le entrega su API key.

    Flujo interno:
      1. Valida que el email no esté en uso (en emisores ni en usuarios)
      2. Crea el Emisor como cuenta de API (sin RUT real, con datos de la app)
      3. Crea el Usuario vinculado al emisor (para acceso al panel)
      4. Devuelve la API key + JWT

    El campo `rut` se genera con el ID del emisor para no necesitar
    un RUT real (los desarrolladores no son emisores fiscales).
    """
    # ── Validaciones previas ───────────────────────────────────
    if len(datos.password) < 8:
        raise HTTPException(
            status_code=422,
            detail="La contraseña debe tener al menos 8 caracteres"
        )

    # Verificar email libre en usuarios
    res_usr = await db.execute(
        select(Usuario).where(Usuario.email == datos.email.lower().strip())
    )
    if res_usr.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="Ya existe una cuenta con ese email"
        )

    # ── Crear el Emisor (cuenta de API del desarrollador) ──────
    # Usamos un RUT placeholder: "DEV-{timestamp}" — no es un emisor
    # fiscal, así que no necesita RUT SII real.
    api_key   = "yek_" + secrets.token_hex(30)
    rut_dev   = f"DEV-{int(datetime.now(timezone.utc).timestamp()) % 1000000}"

    emisor = Emisor(
        rut=rut_dev,
        razon_social=datos.nombre_app,          # nombre de la app como razón social
        giro="Desarrollo de Software",
        direccion="No aplica",
        comuna="No aplica",
        ciudad="No aplica",
        nombre_app=datos.nombre_app,
        url_app=datos.url_app.strip("/"),        # URL sin slash final
        api_key=api_key,
        activo=True,
        ambiente="produccion",
        plan="anual",
        estado_pago="pendiente",                 # pendiente hasta confirmar pago
        correo=datos.email.lower().strip(),
    )
    db.add(emisor)
    await db.flush()   # obtiene el ID sin commit todavía

    # ── Crear el Usuario vinculado ─────────────────────────────
    usuario = Usuario(
        nombre=datos.nombre.strip(),
        apellido=datos.apellido.strip(),
        email=datos.email.lower().strip(),
        hashed_password=hash_password(datos.password),
        activo=True,
        verificado=False,
        es_admin=False,
        emisor_id=emisor.id,
    )
    db.add(usuario)
    await db.flush()

    # ── Generar OTP de verificación y enviar email ────────────
    otp = str(random.randint(100000, 999999))
    from datetime import timedelta
    usuario.otp_verificacion        = otp
    usuario.otp_verificacion_expira = datetime.now(timezone.utc) + timedelta(minutes=15)

    await db.commit()

    asunto, html = email_verificacion(usuario.nombre, otp)
    await enviar_email(usuario.email, asunto, html)

    # ── Generar JWT de acceso ──────────────────────────────────
    token = crear_access_token({
        "sub":      str(usuario.id),
        "email":    usuario.email,
        "emisor_id": emisor.id,
    })

    logger.info(
        f"[REGISTRO_DEV] Nuevo desarrollador: {datos.email} — "
        f"App: {datos.nombre_app} — Emisor ID: {emisor.id}"
    )

    return {
        "ok":           True,
        "api_key":      api_key,
        "emisor_id":    emisor.id,
        "access_token": token,
        "nombre_app":   datos.nombre_app,
        "mensaje": (
            f"¡Bienvenido a YeparDTEcore! Tu API key está lista. "
            f"Úsala en el header X-API-Key en cada llamada. "
            f"Estado de suscripción: pendiente de pago."
        ),
    }


# ── Liberar vinculación de app ─────────────────────────────────
# Cuando el desarrollador quiere mover su API key a otra app,
# libera el dominio vinculado desde su panel.
# Analogía: "desinstalar la licencia de esta máquina para
# instalarla en otra".

@router.post("/{emisor_id}/liberar-app")
async def liberar_app(
    emisor_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Libera la vinculación de dominio de la API key.
    Después de esto, la key se puede usar desde otro dominio
    (se vinculará al nuevo en la primera llamada).
    """
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(404, "Emisor no encontrado")

    dominio_anterior = emisor.origen_vinculado

    emisor.origen_vinculado = None
    emisor.vinculada_en     = None

    await db.commit()

    logger.info(
        f"[LIBERAR_APP] Emisor {emisor_id} liberó vinculación "
        f"de dominio: {dominio_anterior}"
    )

    return {
        "ok":              True,
        "emisor_id":       emisor_id,
        "dominio_liberado": dominio_anterior,
        "mensaje": (
            "Vinculación liberada. La próxima llamada a la API "
            "vinculará la key al nuevo dominio automáticamente."
        ),
    }
