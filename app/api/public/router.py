# app/api/public/router.py
# ══════════════════════════════════════════════════════════════
# API Pública — contrato para apps cliente (YeparDTE, YeparStock, terceros)
# Prefix: /api
#
# Convención de respuesta YeparDTE:
#   POST /api/auth/login      → { token, usuario }
#   POST /api/dte/emitir      → { ok, documento }
#   GET  /api/dte/historial   → { documentos, total }
# ══════════════════════════════════════════════════════════════

import random, string
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.db.base import get_db
from app.models.emisor  import Emisor
from app.models.usuario import Usuario
from app.models.dte     import DTE
from app.core.security  import (
    hash_password, verify_password,
    crear_access_token, verificar_token,
)


router = APIRouter(prefix="/api", tags=["API Pública"])
bearer = HTTPBearer()


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def _gen_otp(n=6) -> str:
    return "".join(random.choices(string.digits, k=n))

def _usuario_payload(u: Usuario, emisor: Emisor | None) -> dict:
    plan  = getattr(emisor, "plan",              "gratuito") if emisor else "gratuito"
    usado = getattr(emisor, "docs_usados",       0)          if emisor else 0
    limit = getattr(emisor, "docs_limit",        20)         if emisor else 20
    vlim  = getattr(emisor, "vendedores_limit",  0)          if emisor else 0
    return {
        "id":                  u.id,
        "nombre":              u.nombre,
        "email":               u.email,
        "rut":                 emisor.rut          if emisor else None,
        "rol":                 "admin" if u.es_admin else "vendedor",
        "plan":                plan,
        "docsUsados":          usado,
        "docsLimit":           limit,
        "vendedoresLimit":     vlim,
        "tributarioCompleto":  bool(emisor and emisor.rut and emisor.giro),
    }

async def _get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> tuple[Usuario, Emisor | None]:
    payload = verificar_token(credentials.credentials)
    if not payload:
        raise HTTPException(401, "Token inválido o expirado")
    uid = payload.get("sub")
    u = (await db.execute(
        select(Usuario).where(Usuario.id == int(uid), Usuario.activo == True)
    )).scalar_one_or_none()
    if not u:
        raise HTTPException(401, "Usuario no encontrado")
    emisor = None
    if u.emisor_id:
        emisor = (await db.execute(
            select(Emisor).where(Emisor.id == u.emisor_id)
        )).scalar_one_or_none()
    return u, emisor


# ══════════════════════════════════════════════════════════════
# HEALTH
# ══════════════════════════════════════════════════════════════

@router.get("/health")
async def health():
    return {"ok": True, "servicio": "YeparDTEcore", "version": "1.0"}


# ══════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════

class RegistroInput(BaseModel):
    nombre:    str
    email:     EmailStr
    password:  str
    # Datos empresa (opcionales en paso 1)
    rut:              Optional[str] = None
    razon_social:     Optional[str] = None
    giro:             Optional[str] = None
    direccion:        Optional[str] = None
    comuna:           Optional[str] = None
    ciudad:           Optional[str] = None
    acteco:           Optional[str] = None
    fch_resol:        Optional[str] = None
    nro_resol:        Optional[str] = None

class LoginInput(BaseModel):
    email:    EmailStr
    password: str

class VendedorLoginInput(BaseModel):
    adminRut: str
    pin:      str

class VerificarOTPInput(BaseModel):
    email:  str
    codigo: str


@router.post("/auth/registro")
async def registro(datos: RegistroInput, db: AsyncSession = Depends(get_db)):
    # Verificar email único
    if (await db.execute(select(Usuario).where(Usuario.email == datos.email.lower()))).scalar_one_or_none():
        raise HTTPException(409, "Ya existe una cuenta con ese email")
    if len(datos.password) < 8:
        raise HTTPException(422, "La contraseña debe tener al menos 8 caracteres")

    # Crear emisor si viene RUT
    emisor = None
    if datos.rut:
        emisor = Emisor(
            rut=datos.rut,
            razon_social=datos.razon_social or datos.nombre,
            giro=datos.giro or "",
            direccion=datos.direccion or "",
            comuna=datos.comuna or "",
            ciudad=datos.ciudad or "",
            acteco=datos.acteco or "",
            correo=datos.email.lower(),
            activo=True,
            ambiente="certificacion",
            plan="gratuito",
            docs_usados=0,
            docs_limit=20,
            vendedores_limit=0,
        )
        db.add(emisor)
        await db.flush()

    # Crear usuario admin
    otp = _gen_otp()
    u = Usuario(
        nombre=datos.nombre.strip(),
        apellido="",
        email=datos.email.lower().strip(),
        hashed_password=hash_password(datos.password),
        emisor_id=emisor.id if emisor else None,
        activo=True,
        verificado=False,
        es_admin=True,
    )
    db.add(u)
    await db.flush()

    # Guardar OTP en emisor (o en usuario si no hay emisor)
    if emisor:
        emisor.otp_code   = otp
        emisor.otp_expira = datetime.now(timezone.utc) + timedelta(minutes=15)

    token = crear_access_token({"sub": str(u.id), "email": u.email})

    # TODO: enviar OTP por email (integrar con servicio de correo)
    print(f"[OTP] {datos.email}: {otp}")

    return {
        "token":   token,
        "usuario": _usuario_payload(u, emisor),
    }


@router.post("/auth/login")
async def login(datos: LoginInput, db: AsyncSession = Depends(get_db)):
    u = (await db.execute(
        select(Usuario).where(Usuario.email == datos.email.lower())
    )).scalar_one_or_none()
    if not u or not verify_password(datos.password, u.hashed_password):
        raise HTTPException(401, "Email o contraseña incorrectos")
    if not u.activo:
        raise HTTPException(403, "Cuenta desactivada")

    emisor = None
    if u.emisor_id:
        emisor = (await db.execute(
            select(Emisor).where(Emisor.id == u.emisor_id)
        )).scalar_one_or_none()

    u.ultimo_login = datetime.now(timezone.utc)
    token = crear_access_token({"sub": str(u.id), "email": u.email})

    return {
        "token":   token,
        "usuario": _usuario_payload(u, emisor),
    }


@router.post("/auth/vendedor")
async def login_vendedor(datos: VendedorLoginInput, db: AsyncSession = Depends(get_db)):
    # Buscar emisor por RUT
    rut_limpio = datos.adminRut.replace(".", "").strip()
    emisor = (await db.execute(
        select(Emisor).where(Emisor.rut == rut_limpio)
    )).scalar_one_or_none()
    if not emisor:
        raise HTTPException(401, "RUT de empresa no encontrado")

    # Buscar vendedor por pin
    v = (await db.execute(
        select(Usuario).where(
            Usuario.emisor_id == emisor.id,
            Usuario.es_admin  == False,
            Usuario.activo    == True,
        )
    )).scalars().all()

    vendedor = None
    for u in v:
        # pin guardado en campo extra — usamos apellido como pin temporal
        if getattr(u, "pin", None) == datos.pin or u.apellido == datos.pin:
            vendedor = u
            break

    if not vendedor:
        raise HTTPException(401, "PIN incorrecto")

    token = crear_access_token({"sub": str(vendedor.id), "email": vendedor.email})
    return {
        "token":   token,
        "usuario": _usuario_payload(vendedor, emisor),
    }


@router.post("/auth/verificar")
async def verificar_otp(datos: VerificarOTPInput, db: AsyncSession = Depends(get_db)):
    u = (await db.execute(
        select(Usuario).where(Usuario.email == datos.email.lower())
    )).scalar_one_or_none()
    if not u:
        raise HTTPException(404, "Usuario no encontrado")

    emisor = None
    if u.emisor_id:
        emisor = (await db.execute(
            select(Emisor).where(Emisor.id == u.emisor_id)
        )).scalar_one_or_none()

    if not emisor or emisor.otp_code != datos.codigo:
        raise HTTPException(400, "Código incorrecto")
    if emisor.otp_expira and datetime.now(timezone.utc) > emisor.otp_expira:
        raise HTTPException(400, "Código expirado")

    u.verificado    = True
    emisor.otp_code = None
    token = crear_access_token({"sub": str(u.id), "email": u.email})

    return {
        "token":   token,
        "usuario": _usuario_payload(u, emisor),
    }


# ══════════════════════════════════════════════════════════════
# EMPRESA
# ══════════════════════════════════════════════════════════════

class EmpresaUpdate(BaseModel):
    razon_social:  Optional[str] = None
    giro:          Optional[str] = None
    direccion:     Optional[str] = None
    comuna:        Optional[str] = None
    ciudad:        Optional[str] = None
    telefono:      Optional[str] = None
    correo:        Optional[str] = None
    acteco:        Optional[str] = None


@router.get("/empresa")
async def get_empresa(current=Depends(_get_current_user)):
    u, emisor = current
    if not emisor:
        raise HTTPException(404, "Empresa no configurada")
    return {
        "rut":          emisor.rut,
        "nombre":       emisor.razon_social,
        "giro":         emisor.giro,
        "direccion":    emisor.direccion,
        "comuna":       emisor.comuna,
        "ciudad":       emisor.ciudad,
        "telefono":     emisor.telefono,
        "correo":       emisor.correo,
        "acteco":       emisor.acteco,
        "plan":         getattr(emisor, "plan", "gratuito"),
        "tributarioCompleto": bool(emisor.rut and emisor.giro),
    }


@router.put("/empresa")
async def update_empresa(
    datos: EmpresaUpdate,
    current=Depends(_get_current_user),
    db: AsyncSession = Depends(get_db),
):
    u, emisor = current
    if not emisor:
        raise HTTPException(404, "Empresa no configurada")
    for campo, valor in datos.model_dump(exclude_none=True).items():
        setattr(emisor, campo, valor)
    return {"ok": True}


# ══════════════════════════════════════════════════════════════
# VENDEDORES
# ══════════════════════════════════════════════════════════════

class VendedorInput(BaseModel):
    nombre: str
    pin:    str


@router.get("/usuarios/vendedores")
async def listar_vendedores(
    current=Depends(_get_current_user),
    db: AsyncSession = Depends(get_db),
):
    u, emisor = current
    if not emisor:
        return {"vendedores": []}
    vs = (await db.execute(
        select(Usuario).where(
            Usuario.emisor_id == emisor.id,
            Usuario.es_admin  == False,
            Usuario.activo    == True,
        )
    )).scalars().all()
    return {"vendedores": [{"id": v.id, "nombre": v.nombre, "pin": v.apellido} for v in vs]}


@router.post("/usuarios/vendedores", status_code=201)
async def crear_vendedor(
    datos: VendedorInput,
    current=Depends(_get_current_user),
    db: AsyncSession = Depends(get_db),
):
    u, emisor = current
    if not emisor:
        raise HTTPException(400, "Configura la empresa primero")
    v = Usuario(
        nombre=datos.nombre,
        apellido=datos.pin,   # pin guardado en apellido temporalmente
        email=f"vendedor_{datos.pin}_{emisor.id}@yepar.internal",
        hashed_password=hash_password(datos.pin),
        emisor_id=emisor.id,
        activo=True,
        verificado=True,
        es_admin=False,
    )
    db.add(v)
    await db.flush()
    return {"vendedor": {"id": v.id, "nombre": v.nombre, "pin": datos.pin}}


@router.delete("/usuarios/vendedores/{vendedor_id}")
async def eliminar_vendedor(
    vendedor_id: int,
    current=Depends(_get_current_user),
    db: AsyncSession = Depends(get_db),
):
    u, emisor = current
    v = (await db.execute(
        select(Usuario).where(
            Usuario.id == vendedor_id,
            Usuario.emisor_id == emisor.id,
        )
    )).scalar_one_or_none()
    if not v:
        raise HTTPException(404, "Vendedor no encontrado")
    v.activo = False
    return {"ok": True}


# ══════════════════════════════════════════════════════════════
# DTE — EMITIR
# ══════════════════════════════════════════════════════════════

class ItemInput(BaseModel):
    nombre:  str
    qty:     float
    precio:  float

class ReceptorInput(BaseModel):
    nombre:    str
    rut:       Optional[str] = None
    email:     Optional[str] = None
    direccion: Optional[str] = None
    giro:      Optional[str] = None

class EmitirInput(BaseModel):
    tipoCode:      str          # "39","41","33","33x","52","56","61"
    exento:        bool = False
    ivaIncluido:   bool = False
    receptor:      ReceptorInput
    items:         list[ItemInput]
    condicionPago: Optional[str] = None
    montoNeto:     Optional[float] = None
    montoExento:   Optional[float] = None
    montoIva:      Optional[float] = None
    montoTotal:    Optional[float] = None
    vendedorNombre: Optional[str] = None
    guia:          Optional[dict] = None


@router.post("/dte/emitir")
async def emitir_dte(
    datos: EmitirInput,
    current=Depends(_get_current_user),
    db: AsyncSession = Depends(get_db),
):
    u, emisor = current
    if not emisor:
        raise HTTPException(400, "Configura la empresa antes de emitir")

    # Verificar límite del plan
    docs_usados = getattr(emisor, "docs_usados", 0) or 0
    docs_limit  = getattr(emisor, "docs_limit",  20) or 20
    plan        = getattr(emisor, "plan", "gratuito") or "gratuito"
    if plan == "gratuito" and docs_usados >= docs_limit:
        raise HTTPException(402, "Límite de documentos alcanzado — actualiza tu plan")

    # Mapear tipoCode → tipo_dte
    tipo_map = {"39": 39, "41": 41, "33": 33, "33x": 34, "52": 52, "56": 56, "61": 61}
    tipo_dte = tipo_map.get(datos.tipoCode)
    if not tipo_dte:
        raise HTTPException(422, f"Tipo no soportado: {datos.tipoCode}")

    from app.services.dte_service import DTEService
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")

    datos_dte = {
        "tipo_dte":      tipo_dte,
        "fecha_emision": fecha_hoy,
        "receptor": {
            "rut":          datos.receptor.rut or "66666666-6",
            "razon_social": datos.receptor.nombre or "Consumidor Final",
            "giro":         datos.receptor.giro or "",
            "direccion":    datos.receptor.direccion or "",
            "comuna":       "",
            "ciudad":       "",
        },
        "items": [
            {
                "nombre":          it.nombre,
                "cantidad":        it.qty,
                "precio_unitario": it.precio,
                "exento":          datos.exento,
            }
            for it in datos.items
        ],
        "referencias": [],
    }

    try:
        svc = DTEService(db=db)
        resultado = await svc.emitir(
            emisor_id=emisor.id,
            datos=datos_dte,
            auto_enviar=True,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Error emitiendo DTE: {e}")

    emisor.docs_usados = docs_usados + 1

    doc = resultado.get("dte", {})
    tipo_label = {39:"Boleta",41:"Boleta Exenta",33:"Factura",34:"F. Exenta",52:"Guía",56:"N. Débito",61:"N. Crédito"}

    return {
        "ok": True,
        "documento": {
            "id":       doc.get("id"),
            "tipo":     tipo_label.get(tipo_dte, str(tipo_dte)),
            "tipoCode": datos.tipoCode,
            "numero":   doc.get("folio_fmt"),
            "folio":    doc.get("folio"),
            "monto":    doc.get("monto_total"),
            "estado":   doc.get("estado"),
            "track_id": doc.get("track_id"),
            "fecha":    datetime.now().isoformat(),
            "receptor": datos.receptor.nombre,
            "rut":      datos.receptor.rut,
        }
    }


@router.get("/dte/historial")
async def historial(
    page:  int = 1,
    limit: int = 20,
    tipo:  Optional[str] = None,
    current=Depends(_get_current_user),
    db: AsyncSession = Depends(get_db),
):
    u, emisor = current
    if not emisor:
        return {"documentos": [], "total": 0}

    q = select(DTE).where(DTE.emisor_id == emisor.id)
    if tipo:
        tipo_map = {"39":39,"41":41,"33":33,"33x":34,"52":52,"56":56,"61":61}
        if tipo in tipo_map:
            q = q.where(DTE.tipo_dte == tipo_map[tipo])

    total = (await db.execute(
        select(func.count()).select_from(q.subquery())
    )).scalar()

    dtes = (await db.execute(
        q.order_by(DTE.created_at.desc())
         .offset((page - 1) * limit)
         .limit(limit)
    )).scalars().all()

    tipo_label = {39:"Boleta",41:"Boleta Exenta",33:"Factura",34:"F. Exenta",52:"Guía",56:"N. Débito",61:"N. Crédito"}

    return {
        "documentos": [
            {
                "id":       d.id,
                "tipo":     tipo_label.get(d.tipo_dte, str(d.tipo_dte)),
                "tipoCode": str(d.tipo_dte),
                "numero":   d.folio_fmt,
                "folio":    d.folio,
                "receptor": d.nombre_receptor,
                "rut":      d.rut_receptor,
                "monto":    d.monto_total,
                "estado":   d.estado,
                "fecha":    d.created_at.isoformat() if d.created_at else None,
            }
            for d in dtes
        ],
        "total": total,
    }


@router.get("/dte/estadisticas")
async def estadisticas(
    current=Depends(_get_current_user),
    db: AsyncSession = Depends(get_db),
):
    u, emisor = current
    if not emisor:
        return {"totalDocs":0,"boletasMes":0,"facturasMes":0,"montoBoletasMes":0,"montoFacturasMes":0}

    ahora  = datetime.now(timezone.utc)
    inicio = ahora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    dtes_mes = (await db.execute(
        select(DTE).where(
            DTE.emisor_id  == emisor.id,
            DTE.created_at >= inicio,
        )
    )).scalars().all()

    boletas  = [d for d in dtes_mes if d.tipo_dte in (39, 41)]
    facturas = [d for d in dtes_mes if d.tipo_dte in (33, 34)]

    docs_usados = getattr(emisor, "docs_usados", 0) or 0
    docs_limit  = getattr(emisor, "docs_limit",  20) or 20
    excedente   = max(0, docs_usados - docs_limit)

    return {
        "totalDocs":         docs_usados,
        "boletasMes":        len(boletas),
        "facturasMes":       len(facturas),
        "montoBoletasMes":   sum(d.monto_total or 0 for d in boletas),
        "montoFacturasMes":  sum(d.monto_total or 0 for d in facturas),
        "excedentes": {
            "cantidad":  excedente,
            "montoNeto": excedente * 20,
        },
    }


@router.get("/dte/{dte_id}/pdf")
async def get_pdf(
    dte_id: int,
    current=Depends(_get_current_user),
    db: AsyncSession = Depends(get_db),
):
    u, emisor = current
    d = (await db.execute(
        select(DTE).where(DTE.id == dte_id, DTE.emisor_id == emisor.id)
    )).scalar_one_or_none()
    if not d:
        raise HTTPException(404, "Documento no encontrado")
    if not d.pdf_base64:
        raise HTTPException(404, "PDF no disponible aún")
    import base64
    from fastapi.responses import Response
    return Response(
        content=base64.b64decode(d.pdf_base64),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=DTE-{d.folio}.pdf"},
    )


# ══════════════════════════════════════════════════════════════
# CLIENTES (receptores frecuentes)
# ══════════════════════════════════════════════════════════════

@router.get("/clientes/buscar")
async def buscar_cliente(
    rut: str,
    current=Depends(_get_current_user),
    db: AsyncSession = Depends(get_db),
):
    u, emisor = current
    if not emisor:
        return {"cliente": None}
    # Buscar en historial de DTEs como receptor frecuente
    d = (await db.execute(
        select(DTE).where(
            DTE.emisor_id    == emisor.id,
            DTE.rut_receptor == rut.replace(".", "").strip(),
        ).order_by(DTE.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if not d:
        return {"cliente": None}
    return {"cliente": {"rut": d.rut_receptor, "nombre": d.nombre_receptor}}


@router.post("/clientes")
async def guardar_cliente(
    datos: dict,
    current=Depends(_get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Los clientes se guardan implícitamente en el historial de DTEs
    # Este endpoint acepta la llamada pero no requiere tabla separada
    return {"ok": True}


# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════

@router.post("/config/conexion")
async def toggle_conexion(datos: dict, current=Depends(_get_current_user)):
    return {"ok": True, "activa": datos.get("activa", True)}
