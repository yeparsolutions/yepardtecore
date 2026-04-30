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
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

ARG BUST=8
RUN echo "bust=$BUST" && \
    pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --no-binary lxml lxml==4.9.4 && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
