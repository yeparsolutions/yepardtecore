# verificar_libros.py
# ══════════════════════════════════════════════════════════════
# Verificador de la versión del generador de libros de certificación.
#
# ¿Qué hace? (analogía: el cerrajero que revisa la llave antes de
# que salgas de viaje)
#   1. Busca todos los archivos de libros en el proyecto y detecta
#      DUPLICADOS con el mismo router (la causa del drift).
#   2. Revisa que certificacion_libros_dinamico.py tenga el FIX T46
#      (MntIVA completo + IVARetTotal) y NO la lógica vieja (MntIVA=0).
#   3. Te da un veredicto claro: LISTO PARA DEPLOYAR o NO.
#
# Uso:
#   python3 verificar_libros.py /ruta/al/proyecto
#   (si no pasas ruta, usa el directorio actual)
# ══════════════════════════════════════════════════════════════

import sys
import re
from pathlib import Path

# ── Configuración ─────────────────────────────────────────────

# Nombre del archivo canónico (el ÚNICO que debe existir)
ARCHIVO_CANONICO = "certificacion_libros_dinamico.py"

# Archivos viejos que NO deben existir en el proyecto
ARCHIVOS_PROHIBIDOS = [
    "certificacion_libro_compras.py",
    "certificacion_libro_ventas.py",
    "certificacion_libro_guias.py",
    "certificacion_libro_compras_dinamico.py",
]

# Huellas de la versión CORRECTA (deben estar presentes).
# El patrón \}\}\} busca la emisión REAL del tag XML dentro del f-string
# etree.SubElement(det, f"{{{NS}}}Tag") — así los comentarios que solo
# MENCIONAN un tag no generan falsos positivos.
HUELLAS_VERSION_NUEVA = [
    # El fix del T46: el detalle emite el tag IVARetTotal
    r'\}\}\}IVARetTotal',
    # El resumen emite los totales de retención
    r'\}\}\}TotOpIVARetTotal',
    # Soporte de rectificación para períodos cerrados (LTC)
    r'cod_aut_rec',
    # Marca del fix en el encabezado
    r'FIX T46',
]

# Huellas de la versión VIEJA (NO deben estar presentes COMO CÓDIGO)
HUELLAS_VERSION_VIEJA = [
    # La lógica rechazada por el SII: emisión del tag MntSinCred
    r'\}\}\}MntSinCred',
    # El total de resumen que era parte de las combinaciones rechazadas
    r'\}\}\}TotImpSinCredito',
]


def buscar_archivos(raiz: Path) -> dict:
    """Recorre el proyecto y clasifica los archivos de libros encontrados."""
    encontrados = {"canonico": [], "prohibidos": []}
    for py in raiz.rglob("*.py"):
        # Ignorar entornos virtuales y caches para no dar falsos positivos
        if any(p in py.parts for p in ("venv", ".venv", "__pycache__", "node_modules")):
            continue
        if py.name == ARCHIVO_CANONICO:
            encontrados["canonico"].append(py)
        elif py.name in ARCHIVOS_PROHIBIDOS:
            encontrados["prohibidos"].append(py)
    return encontrados


def verificar_contenido(ruta: Path) -> tuple[list, list]:
    """Revisa las huellas de versión dentro del archivo canónico.

    Retorna (huellas_nuevas_faltantes, huellas_viejas_presentes).
    Ambas listas vacías = versión correcta.
    """
    texto = ruta.read_text(encoding="utf-8", errors="replace")
    faltantes = [h for h in HUELLAS_VERSION_NUEVA if not re.search(h, texto)]
    presentes = [h for h in HUELLAS_VERSION_VIEJA if re.search(h, texto)]
    return faltantes, presentes


def main():
    # Raíz del proyecto: primer argumento o directorio actual
    raiz = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    print(f"🔍 Revisando proyecto en: {raiz.resolve()}\n")

    resultado = buscar_archivos(raiz)
    ok = True

    # ── Chequeo 1: duplicados y archivos prohibidos ──────────
    if resultado["prohibidos"]:
        ok = False
        print("❌ ARCHIVOS VIEJOS QUE DEBES ELIMINAR (y sus include_router):")
        for p in resultado["prohibidos"]:
            print(f"   - {p}")
    else:
        print("✅ Sin archivos viejos de libros en el proyecto")

    # ── Chequeo 2: el canónico existe y es único ─────────────
    canonicos = resultado["canonico"]
    if len(canonicos) == 0:
        ok = False
        print(f"❌ No se encontró {ARCHIVO_CANONICO} — falta subir el archivo nuevo")
    elif len(canonicos) > 1:
        ok = False
        print(f"❌ Hay {len(canonicos)} copias de {ARCHIVO_CANONICO} — deja UNA sola:")
        for p in canonicos:
            print(f"   - {p}")
    else:
        print(f"✅ Un solo {ARCHIVO_CANONICO} encontrado: {canonicos[0]}")

        # ── Chequeo 3: contenido = versión con el FIX T46 ────
        faltantes, viejas = verificar_contenido(canonicos[0])
        if faltantes:
            ok = False
            print(f"❌ Es la VERSIÓN VIEJA — le faltan las huellas del fix: {faltantes}")
        if viejas:
            ok = False
            print(f"❌ Contiene lógica vieja rechazada por el SII: {viejas}")
        if not faltantes and not viejas:
            print("✅ Contenido correcto: FIX T46 presente, lógica vieja ausente")

    # ── Veredicto final ──────────────────────────────────────
    print()
    if ok:
        print("🟢 LISTO PARA DEPLOYAR")
        print("   Siguiente paso: redeploy → preview del libro → T46 folio 9")
        print("   debe salir con MntIVA=1926. Recién ahí, firmar y enviar.")
    else:
        print("🔴 NO DEPLOYAR TODAVÍA — corrige lo marcado con ❌ arriba")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
