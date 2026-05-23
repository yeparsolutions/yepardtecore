# app/api/v1/router.py
# ══════════════════════════════════════════════════════════════
# ARQUITECTURA FINAL
#
# El admin usa EXCLUSIVAMENTE:
#   POST /v1/certificacion-dinamica/generar-xml   → sets (cualquier usuario)
#   POST /v1/certificacion-libros/generar-xml     → libros (cualquier usuario)
#
# Los endpoints legacy se mantienen por compatibilidad pero
# NO deben usarse para nuevos usuarios.
# ══════════════════════════════════════════════════════════════

from fastapi import APIRouter
from app.api.v1.endpoints import (
    health, emisores, auth, dte, caf, certificados, sii_auth,

    # ── DINÁMICOS — para todos los usuarios ──────────────────
    certificacion_dinamica,
    certificacion_libros_dinamico,

    # ── LEGACY — solo RUT 78377021-0 ─────────────────────────
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
)

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(emisores.router)
api_router.include_router(caf.router)
api_router.include_router(dte.router)
api_router.include_router(certificados.router)
api_router.include_router(sii_auth.router)

# ── Dinámicos (cualquier usuario) ─────────────────────────────
api_router.include_router(certificacion_dinamica.router)
api_router.include_router(certificacion_libros_dinamico.router)

# ── Legacy (hardcodeados para 78377021-0) ─────────────────────
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
