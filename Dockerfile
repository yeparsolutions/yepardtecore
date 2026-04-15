# Dockerfile
# ══════════════════════════════════════════════════════════════
# Imagen de producción para Railway
# Analogia: es la receta que le dice a Railway exactamente
# qué ingredientes necesita y cómo preparar el plato.
# ══════════════════════════════════════════════════════════════

# Imagen base oficial de Python — slim = sin extras innecesarios
FROM python:3.11-slim

# Directorio de trabajo dentro del contenedor
WORKDIR /app

# Copiar requirements primero (optimización de cache de Docker)
# Si el código cambia pero requirements no, Docker no reinstala todo
COPY requirements.txt .

# Instalar dependencias
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copiar el resto del código
COPY . .

# Puerto en que corre la app (Railway lo lee automáticamente)
EXPOSE 8000

# Comando de arranque
# --host 0.0.0.0 = acepta conexiones externas (necesario en Railway)
# --port 8000    = puerto estándar
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
