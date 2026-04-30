# Dockerfile
# ══════════════════════════════════════════════════════════════
# Imagen de producción para Railway
# Analogía: es la receta que le dice a Railway exactamente
# qué ingredientes necesita y cómo preparar el plato.
#
# IMPORTANTE: python-xmlsec necesita librerías del sistema C
# (libxmlsec1, libxml2, libxslt) antes de que pip pueda instalarla.
# Sin esto, Railway crashea con "No module named 'xmlsec'".
# ══════════════════════════════════════════════════════════════

# Imagen base oficial de Python — slim = sin extras innecesarios
FROM python:3.11-slim

# Cache bust — incrementar para forzar rebuild limpio
ARG CACHEBUST=5

# Directorio de trabajo dentro del contenedor
WORKDIR /app

# ── Dependencias del sistema ──────────────────────────────────
# lxml y xmlsec DEBEN usar la MISMA libxml2 del sistema.
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Motor XML de libxmlsec1 (firma XMLDSig — el núcleo del asunto)
    libxmlsec1-dev \
    libxmlsec1-openssl \
    # Parser XML base (requerido por lxml y xmlsec)
    libxml2-dev \
    # Transformaciones XSL (requerido por xmlsec)
    libxslt1-dev \
    # pkg-config: le dice a pip dónde están las libs del sistema
    pkg-config \
    # Compilador C completo (gcc + g++ + make) para compilar lxml desde source
    build-essential \
    # Limpieza: el cache de apt no se necesita en producción
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements primero (optimización de cache de Docker)
# Si el código cambia pero requirements no, Docker no reinstala todo
COPY requirements.txt .

# ── Instalar dependencias Python ─────────────────────────────
# PASO 1: lxml compilado desde source usando la libxml2 del sistema.
# Analogía: lxml y xmlsec son dos piezas de Lego que deben encajar.
# Si lxml viene del sobre (wheel precompilado manylinux), trae su
# propia pieza de libxml2 adentro. xmlsec usa la pieza del sistema.
# Dos piezas distintas → "libxml2 version mismatch" al arrancar.
# --no-binary lxml fuerza compilar lxml con la pieza del sistema.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --no-binary lxml lxml==5.2.2

# PASO 2: resto de dependencias (lxml ya está instalada, se saltea)
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto del código
COPY . .

# Puerto en que corre la app (Railway lo lee automáticamente)
EXPOSE 8000

# Comando de arranque
# --host 0.0.0.0 = acepta conexiones externas (necesario en Railway)
# --port 8000    = puerto estándar
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
