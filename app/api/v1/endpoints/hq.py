# app/api/v1/endpoints/hq.py
# ══════════════════════════════════════════════════════════════
# Endpoints para "Yepar HQ" (panel único de todos tus productos).
# Protegido con X-Yepar-HQ-Key — separado del X-Admin-Secret que ya
# usa /emisores para operaciones internas.
#
# YeparDTEcore es 100% Chile (SII) — no hace falta filtro de país.
# Los "clientes" acá son los Emisores: cada empresa que otro
# producto (YeparStock, YeparDTE...) registró para emitir DTE.
# ══════════════════════════════════════════════════════════════

from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.db.base import get_db
from app.models.emisor import Emisor
from app.core.config import settings

router = APIRouter(prefix="/hq", tags=["Yepar HQ"])


def _verificar_hq(request: Request) -> None:
    secret = request.headers.get("X-Yepar-HQ-Key", "")
    if not settings.YEPAR_HQ_API_KEY or secret != settings.YEPAR_HQ_API_KEY:
        raise HTTPException(status_code=403, detail="Llave del Hub inválida o no configurada")


@router.get("/stats")
async def hq_stats(request: Request, db: AsyncSession = Depends(get_db)):
    _verificar_hq(request)
    total = (await db.execute(select(func.count(Emisor.id)))).scalar() or 0
    return {"totalEmisores": total}


@router.get("/empresas")
async def hq_empresas(request: Request, db: AsyncSession = Depends(get_db)):
    """Lista de Emisores registrados — el equivalente de "empresas"
    acá (cada uno fue dado de alta por otro producto, no directo)."""
    _verificar_hq(request)
    rows = (await db.execute(select(Emisor).order_by(Emisor.id.desc()))).scalars().all()
    return [
        {
            "id": e.id,
            "nombre": e.razon_social,
            "rut": e.rut,
            "pais": "CL",
        }
        for e in rows
    ]
