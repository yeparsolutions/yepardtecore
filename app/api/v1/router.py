# app/api/v1/router.py
from fastapi import APIRouter
from app.api.v1.endpoints import (
    health, emisores, auth, dte, caf,
    certificados, sii_auth, certificacion
)

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(emisores.router)
api_router.include_router(caf.router)
api_router.include_router(dte.router)
api_router.include_router(certificados.router)
api_router.include_router(sii_auth.router)
api_router.include_router(certificacion.router)
