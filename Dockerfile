# Dockerfile
# ══════════════════════════════════════════════════════════════
# Imagen de producción para Railway
# ══════════════════════════════════════════════════════════════

# IMPORTANTE: usar python:3.11-slim-bookworm (Debian 12), NO python:3.11-slim.
# python:3.11-slim sin tag apunta a Trixie (Debian 13) con libxml2 2.12.x.
# lxml wheel manylinux bundlea libxml2 2.10.x → mismatch → crash:
#   "lxml & xmlsec libxml2 library version mismatch"
#
# Bookworm (Debian 12): libxml2 = 2.9.14
# lxml 5.2.2 wheel manylinux_2_17: libxml2 = 2.9.x
# xmlsec compilado contra sistema: libxml2 = 2.9.14
# Las tres piezas usan la misma versión → sin mismatch.
FROM python:3.11-slim-bookworm

# Cache bust — incrementar para forzar rebuild limpio
ARG CACHEBUST=6

WORKDIR /app

# ── Dependencias del sistema ──────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxmlsec1-dev \
    libxmlsec1-openssl \
    libxml2-dev \
    libxslt1-dev \
    pkg-config \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
