# app/api/v1/router.py
# ══════════════════════════════════════════════════════════════
# Router principal de la API v1
# ══════════════════════════════════════════════════════════════

# app/api/v1/router.py
# ══════════════════════════════════════════════════════════════
# Router principal de la API v1
#
# Arquitectura separada por tipo de DTE para mayor claridad:
#
#   certificacion           → Boleta Electrónica (tipos 39, 41)  [LEGACY]
#   certificacion_facturas  → Factura Electrónica (tipos 33, 34)
#   certificacion_boletas   → Boleta Electrónica (tipos 39, 41)  [NUEVO]
#   certificacion_notas     → Notas de Crédito/Débito (tipos 56, 61)
#
# Analogía: cada módulo de certificación es como un formulario
# específico del SII — cada tipo de documento tiene su propio
# formulario con reglas distintas.
# ══════════════════════════════════════════════════════════════

from fastapi import APIRouter
from app.api.v1.endpoints import (
    health,
    emisores,
    auth,
    dte,
    caf,
    certificados,
    sii_auth,
    certificacion,           # Boleta — módulo legacy (mantener por compatibilidad)
    certificacion_facturas,  # Factura Electrónica (tipos 33, 34)
    certificacion_boletas,   # Boleta Electrónica separada (tipos 39, 41)
    certificacion_notas,     # Notas de Crédito y Débito (tipos 56, 61)
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

# ── Certificación por tipo de DTE ─────────────────────────────
# Separados para mantener el XSD correcto por tipo de documento:
# - Facturas → EnvioDTE_v10.xsd
# - Boletas  → EnvioBOLETA_v11.xsd  (diferente! causa RFR si se mezcla)
# - Notas    → EnvioDTE_v10.xsd
api_router.include_router(certificacion.router)          # Legacy (boleta)
api_router.include_router(certificacion_facturas.router) # Tipo 33, 34
api_router.include_router(certificacion_boletas.router)  # Tipo 39, 41
api_router.include_router(certificacion_notas.router)    # Tipo 56, 61
