# YeparDTEcore 🧾

Motor de facturación electrónica para Chile — by **YeparSolutions SpA** (78.377.021-0)

## ¿Qué hace?

Genera, firma y envía documentos tributarios electrónicos (DTE) al SII de Chile.
Es el motor central que alimenta YeparStock, YeparDTE y apps externas vía API.

## Stack

- **Python 3.11** + **FastAPI** — API REST async
- **SQLAlchemy 2.0** async — ORM
- **PostgreSQL** (producción) / **SQLite** (desarrollo local)
- **lxml + signxml + cryptography** — firma digital XML
- **Railway** — hosting

## Arrancar en desarrollo local

```bash
# 1. Clonar el repo
git clone https://github.com/yeparsolutions/yepardtecore.git
cd yepardtecore

# 2. Crear entorno virtual
python -m venv venv
source venv/bin/activate        # Mac/Linux
# venv\Scripts\activate         # Windows

# 3. Instalar dependencias
pip install -r requirements.txt

# 4. Configurar variables de entorno
cp .env.example .env
# Edita .env si necesitas cambiar algo (en desarrollo funciona sin cambios)

# 5. Arrancar
uvicorn main:app --reload

# La app queda en: http://localhost:8000
# Swagger docs en: http://localhost:8000/docs
```

## Estructura del proyecto

```
yepardtecore/
├── main.py                     # Punto de entrada
├── requirements.txt
├── .env.example
├── Dockerfile
├── railway.toml
└── app/
    ├── api/
    │   └── v1/
    │       ├── router.py       # Agrupa todos los endpoints
    │       └── endpoints/
    │           ├── health.py   # GET /v1/health
    │           ├── emisores.py # CRUD emisores
    │           ├── caf.py      # Carga de CAFs (Fase 1)
    │           └── dte.py      # Emisión de DTE (Fase 1)
    ├── core/
    │   └── config.py           # Variables de entorno
    ├── db/
    │   └── base.py             # Conexión BD + sesiones
    ├── models/
    │   ├── emisor.py           # Empresa que factura
    │   ├── caf.py              # Código Autorización Folios
    │   └── dte.py              # Documento Tributario + Items
    ├── schemas/                # Validación entrada/salida (Pydantic)
    └── services/               # Lógica de negocio (firma, PDF, SII)
```

## Roadmap

- [x] **Fase 0** — Scaffold, modelos BD, endpoint /health
- [ ] **Fase 1** — Carga de CAF, generador XML, firma digital, PDF tributario
- [ ] **Fase 2** — Certificación SII (sets de prueba, envío de sobres)
- [ ] **Fase 3** — Integración con YeparStock
- [ ] **Fase 4** — Portal YeparDTE
- [ ] **Fase 5** — API pública SaaS multi-tenant

## Empresa

**YeparSolutions SpA**
RUT: 78.377.021-0
Quinta Normal, Santiago, Chile
