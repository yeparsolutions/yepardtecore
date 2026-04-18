# app/api/v1/router.py
# ══════════════════════════════════════════════════════════════
# Router principal de la API v1
# ══════════════════════════════════════════════════════════════

from fastapi import APIRouter
from app.api.v1.endpoints import health, emisores, auth, dte, caf, certificados, sii_auth, certificacion

api_router = APIRouter()

# Sistema
api_router.include_router(health.router)

# Autenticación
api_router.include_router(auth.router)

# Negocio
api_router.include_router(emisores.router)
api_router.include_router(caf.router)
api_router.include_router(dte.router)
api_router.include_router(certificados.router)
api_router.include_router(sii_auth.router)

# Certificación SII
api_router.include_router(certificacion.router)
