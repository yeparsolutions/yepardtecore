# app/api/v1/endpoints/certificacion_libro_compras_dinamico.py
# ══════════════════════════════════════════════════════════════
# ALIAS del Libro de Compras dinámico — SIN lógica propia.
#
# ⚠ ESTE ARCHIVO NO CONTIENE CÓDIGO DE GENERACIÓN DE LIBROS.
#   Toda la lógica vive en UN solo lugar:
#       certificacion_libros_dinamico.py   ← archivo canónico
#
# ¿Por qué existe este alias entonces?
#   Para que cualquier import viejo del proyecto que apunte a
#   "certificacion_libro_compras_dinamico" siga funcionando sin
#   romperse, pero SIN duplicar la implementación. Analogía: es un
#   cambio de domicilio en el correo — la dirección antigua sigue
#   recibiendo cartas, pero todas se entregan en la única casa real.
#   Así es IMPOSIBLE que las dos "versiones" diverjan, porque solo
#   hay una.
#
# REGLAS DE USO:
#   1. En main.py registra el router UNA SOLA VEZ. Da lo mismo desde
#      cuál de los dos archivos lo importes (es el mismo objeto), pero
#      NO lo incluyas dos veces:
#
#         # ✅ CORRECTO (una sola inclusión)
#         from app.api.v1.endpoints.certificacion_libros_dinamico import router as libros_router
#         app.include_router(libros_router)
#
#         # ❌ INCORRECTO (inclusión doble → rutas duplicadas)
#         app.include_router(libros_dinamico.router)
#         app.include_router(libro_compras_dinamico.router)
#
#   2. Si algún día quieres modificar la generación de libros,
#      edita SOLO certificacion_libros_dinamico.py. Este archivo
#      no se toca jamás.
#
#   3. Los endpoints de compras son los mismos del canónico, usando
#      tipo_libro="compras":
#         POST /certificacion-libros/manual          (datos manuales del .txt del SII)
#         POST /certificacion-libros/preview-manual  (inspección sin firma)
#         POST /certificacion-libros/generar-xml     (desde la BD)
#         POST /certificacion-libros/desde-xml       (desde EnvioDTE aceptados)
# ══════════════════════════════════════════════════════════════

# Re-exportamos TODO desde el archivo canónico. Cero duplicación.
from app.api.v1.endpoints.certificacion_libros_dinamico import (  # noqa: F401
    router,                    # el router FastAPI (mismo objeto, no una copia)
    _construir_libro_xml,      # generador del XML (con el FIX T46 incluido)
    _parsear_dtes_desde_xml,   # parser de EnvioDTE para el modo desde-xml
    _DTEFake,                  # DTE liviano para modos manual/desde-xml
    DTEManualInput,            # modelo Pydantic de entrada manual
    LibroManualRequest,        # modelo Pydantic del request manual
    TIPOS_VENTAS,              # tipos DTE del libro de ventas
    TIPOS_COMPRAS,             # tipos DTE del libro de compras
    TIPOS_GUIAS,               # tipos DTE del libro de guías
)
