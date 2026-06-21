# app/api/v1/router.py

from fastapi import APIRouter
from app.api.v1.endpoints import (
    pagos,
    health,
    emisores,
    auth,
    dte,
    caf,
    certificados,
    sii_auth,
    certificacion_dinamica,
    certificacion_libros_dinamico,
    certificacion_libro_compras,
    certificacion_libro_compras_dinamico,
    certificacion_boletas,
    certificacion_cof,
    enviar_sobre,
    libro_guias,
    libro_compras,
)

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(emisores.router)
api_router.include_router(caf.router)
api_router.include_router(dte.router)
api_router.include_router(certificados.router)
api_router.include_router(sii_auth.router)
api_router.include_router(certificacion_dinamica.router)
api_router.include_router(certificacion_libros_dinamico.router)
api_router.include_router(certificacion_libro_compras.router)
api_router.include_router(certificacion_libro_compras_dinamico.router)
api_router.include_router(certificacion_boletas.router)
api_router.include_router(certificacion_cof.router)
api_router.include_router(enviar_sobre.router)
api_router.include_router(libro_guias.router)
api_router.include_router(libro_compras.router)
api_router.include_router(pagos.router)
