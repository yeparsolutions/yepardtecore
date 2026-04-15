# main.py
# ══════════════════════════════════════════════════════════════
# Punto de entrada de YeparDTEcore
# Railway ejecuta este archivo para arrancar el servidor.
# ══════════════════════════════════════════════════════════════

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from app.core.config import settings
from app.api.v1.router import api_router
from app.db.base import engine, Base
import os


# ── Startup y Shutdown ────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"🚀 Arrancando {settings.APP_NAME} v{settings.APP_VERSION}")
    print(f"   Ambiente: {settings.ENVIRONMENT}")
    print(f"   SII: {settings.SII_AMBIENTE} → {settings.SII_URL_ACTIVA}")

    # ✅ FIX: crear tablas en TODOS los ambientes (development y production)
    # create_all es seguro — solo crea las tablas que no existen, no borra datos
    async with engine.begin() as conn:
        from app.models import Emisor, CAF, DTE, ItemDTE
        from app.models.usuario import Usuario
        await conn.run_sync(Base.metadata.create_all)
        print("   ✅ Tablas de BD verificadas")

    yield

    print(f"👋 Apagando {settings.APP_NAME}...")
    await engine.dispose()


# ── Crear la app FastAPI ──────────────────────────────────────
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="""
## YeparDTEcore 🧾

Motor de facturación electrónica para Chile.
Genera, firma y envía documentos tributarios al SII.

**Productos que consumen esta API:**
- YeparStock (ERP de inventario)
- YeparDTE (portal de facturación)
- Apps externas (via API key)

**Tipos de DTE soportados:**
- `39` Boleta Electrónica
- `33` Factura Electrónica
- `61` Nota de Crédito
- `56` Nota de Débito
    """,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.DEBUG else [
        "https://yeparstock.yeparsolutions.com",
        "https://yeparte.yeparsolutions.com",
        "https://yepardte.yeparsolutions.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Archivos estáticos (logos, favicons) ──────────────────────
# Sirve todo lo que esté en /static bajo la ruta /static
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Registrar rutas API ───────────────────────────────────────
app.include_router(api_router, prefix=settings.API_PREFIX)


# ── Ruta raíz — Página de bienvenida HTML ────────────────────
@app.get("/", response_class=HTMLResponse, tags=["Sistema"])
async def root():
    """Página de bienvenida con logo YeparDTEcore."""
    return HTMLResponse(content="""
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>YeparDTEcore — Motor de Facturación</title>
  <link rel="icon" type="image/svg+xml" href="/static/isotipo.svg">
  <link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png">
  <link rel="icon" type="image/png" sizes="16x16" href="/static/favicon-16.png">
  <link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
  <meta name="theme-color" content="#22C55E">
  <meta property="og:image" content="/static/og-image.png">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Segoe UI', system-ui, sans-serif;
      background: #0D0F14;
      color: #E8EAF0;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 40px 20px;
    }
    .logo { width: 280px; max-width: 90%; margin-bottom: 40px; }
    .badge {
      background: rgba(34,197,94,0.12);
      border: 1px solid rgba(34,197,94,0.3);
      color: #22C55E;
      padding: 6px 16px;
      border-radius: 99px;
      font-size: 13px;
      font-weight: 600;
      margin-bottom: 32px;
    }
    .cards {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 16px;
      max-width: 680px;
      width: 100%;
      margin-bottom: 40px;
    }
    .card {
      background: #1E2235;
      border: 1px solid rgba(255,255,255,0.07);
      border-radius: 14px;
      padding: 20px;
      text-align: center;
      text-decoration: none;
      color: inherit;
      transition: border-color 0.15s;
    }
    .card:hover { border-color: #22C55E; }
    .card-icon { font-size: 28px; margin-bottom: 10px; }
    .card-title { font-size: 14px; font-weight: 700; margin-bottom: 4px; }
    .card-sub { font-size: 12px; color: #6B7280; }
    .footer {
      font-size: 12px;
      color: #6B7280;
      text-align: center;
    }
    .footer span { color: #22C55E; }
  </style>
</head>
<body>

  <img class="logo" src="/static/logo-horizontal.svg" alt="YeparDTEcore">

  <div class="badge">✅ Servicio activo · v""" + settings.APP_VERSION + """</div>

  <div class="cards">
    <a class="card" href="/docs">
      <div class="card-icon">📚</div>
      <div class="card-title">Documentación</div>
      <div class="card-sub">Swagger UI · Explorar API</div>
    </a>
    <a class="card" href="/redoc">
      <div class="card-icon">📖</div>
      <div class="card-title">ReDoc</div>
      <div class="card-sub">Documentación completa</div>
    </a>
    <a class="card" href="/v1/health">
      <div class="card-icon">💚</div>
      <div class="card-title">Health Check</div>
      <div class="card-sub">Estado del servicio</div>
    </a>
  </div>

  <div class="footer">
    Motor de Facturación Electrónica para Chile · SII<br>
    <span>YeparSolutions SpA</span> · RUT 78.377.021-0
  </div>

</body>
</html>
""")
