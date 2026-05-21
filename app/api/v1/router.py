# app/api/v1/router.py

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
api_router.include_router(certificacion.router)           # Legacy
api_router.include_router(certificacion_facturas.router)  # Set 4839621 ✅ APROBADO
api_router.include_router(certificacion_facturas2.router) # Set 4841543
api_router.include_router(certificacion_boletas.router)   # Set Boletas
api_router.include_router(certificacion_exentas.router)   # Set 4841548
api_router.include_router(certificacion_guia.router)      # Set 4841546
api_router.include_router(certificacion_notas.router)     # Notas legacy
