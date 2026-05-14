FROM python:3.11-slim-bookworm
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxmlsec1-dev \
    libxmlsec1-openssl \
    libxml2-dev \
    libxslt1-dev \
    zlib1g-dev \
    pkg-config \
    build-essential \
    xmlsec1 \
    default-jre-headless \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
ARG BUST=10
RUN echo "bust=$BUST" && \
    pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --no-binary lxml,xmlsec lxml==4.9.4 xmlsec==1.3.17 && \
    pip install --no-cache-dir -r requirements.txt
COPY . .
# Compilar FirmaDTE.java en el contenedor para garantizar compatibilidad.
RUN javac FirmaDTE.java
EXPOSE 8000
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
