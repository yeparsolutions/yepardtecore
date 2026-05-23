# app/api/v1/endpoints/__init__.py
# ══════════════════════════════════════════════════════════════
# ARQUITECTURA DE ENDPOINTS
#
# ✅ DINÁMICOS (usar estos — funcionan para cualquier usuario)
#   certificacion_dinamica        → sets básico, exentas, guías, boletas
#   certificacion_libros_dinamico → libros ventas/compras/guías
#
# ⚠️  LEGACY (solo para RUT 78377021-0 — NO usar en producción multi-usuario)
#   certificacion_facturas   → set 4839621 hardcodeado
#   certificacion_facturas2  → set 4841543 hardcodeado
#   certificacion_exentas    → set 4841548 hardcodeado
#   certificacion_guia       → set 4841546 hardcodeado
#   certificacion_libro_ventas/compras/guias → montos hardcodeados
#
# El admin.html usa EXCLUSIVAMENTE los endpoints dinámicos.
# Los legacy se mantienen para no romper scripts existentes.
# ══════════════════════════════════════════════════════════════
