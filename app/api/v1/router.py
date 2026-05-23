# app/api/v1/router.py
# Agregar certificacion_dinamica a los imports y al router

from fastapi import APIRouter
from app.api.v1.endpoints import (
    health,
    emisores,
    auth,
    dte,
    caf,
    certificados,
    sii_auth,
    certificacion,
    certificacion_facturas,
    certificacion_facturas2,
    certificacion_boletas,
    certificacion_exentas,
    certificacion_guia,
    certificacion_notas,
    certificacion_libro_ventas,
    certificacion_libro_compras,
    certificacion_libro_guias,
    certificacion_dinamica,   # ← NUEVO
)

api_router = APIRouter()

# ── Sistema ───────────────────────────────────────────────────
api_router.include_router(health.router)

# ── Autenticación ─────────────────────────────────────────────
api_router.include_router(auth.router)

# ── Negocio ───────────────────────────────────────────────────
api_router.include_router(emisores.router)
api_router.include_router(caf.router)
api_router.include_router(dte.router)
api_router.include_router(certificados.router)
api_router.include_router(sii_auth.router)

# ── Certificación Sets de Prueba ──────────────────────────────
api_router.include_router(certificacion.router)
api_router.include_router(certificacion_facturas.router)
api_router.include_router(certificacion_facturas2.router)
api_router.include_router(certificacion_boletas.router)
api_router.include_router(certificacion_exentas.router)
api_router.include_router(certificacion_guia.router)
api_router.include_router(certificacion_notas.router)
api_router.include_router(certificacion_libro_ventas.router)
api_router.include_router(certificacion_libro_compras.router)
api_router.include_router(certificacion_libro_guias.router)
api_router.include_router(certificacion_dinamica.router)   # ← NUEVO
