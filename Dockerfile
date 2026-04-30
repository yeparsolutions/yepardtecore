# Dockerfile
# ══════════════════════════════════════════════════════════════
# Imagen de producción para Railway
# ══════════════════════════════════════════════════════════════

# Bookworm (Debian 12) tiene libxml2 2.9.14.
# lxml 5.x requiere libxml2 >= 2.10 → no es compatible con Bookworm.
# lxml 4.9.4 + libxml2 2.9.14 → compatible.
# xmlsec compilado contra libxml2 2.9.14 del sistema → sin mismatch.
FROM python:3.11-slim-bookworm

WORKDIR /app

# ── Dependencias del sistema ──────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxmlsec1-dev \
    libxmlsec1-openssl \
    libxml2-dev \
    libxslt1-dev \
    pkg-config \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# CACHEBUST inline para invalidar el cache de pip en cada rebuild
# Cambiar el valor de BUST para forzar reinstalación
ARG BUST=7
RUN echo "bust=$BUST" && \
    pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --no-binary lxml lxml==4.9.4 && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
