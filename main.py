import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager

from app.core.config import settings
from app.api.v1.router import api_router
from app.db.base import engine, Base

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Asegura que las tablas existan en Railway
    async with engine.begin() as conn:
        # Importamos modelos aquí para asegurar que SQLAlchemy los registre
        from app.models.emisor import Emisor
        from app.models.caf import CAF
        from app.models.dte import DTE
        from app.models.certificado import Certificado
        from app.models.usuario import Usuario
        await conn.run_sync(Base.metadata.create_all)
    yield
    # Shutdown
    await engine.dispose()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    # Movemos la documentación a /api/docs para que no choque con el inicio
    docs_url="/api/docs",
    redoc_url="/api/redoc"
)

# ── Configuración de CORS ─────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Rutas del Backend (API) ──────────────────────────────────
app.include_router(api_router, prefix=settings.API_PREFIX)

# ── Archivos Estáticos ────────────────────────────────────────
# Verifica y monta la carpeta static para que onboarding.html sea accesible
if not os.path.exists("static"):
    os.makedirs("static")

app.mount("/static", StaticFiles(directory="static"), name="static")

# ── FRONTEND: Onboarding / Registro ───────────────────────────

@app.get("/", response_class=HTMLResponse, tags=["Frontend"])
async def root():
    """
    Ruta principal: Intenta cargar el portal onboarding.html.
    Si no existe, muestra una página de bienvenida por defecto.
    """
    path = "static/onboarding.html"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    
    # Página de respaldo en caso de que el archivo no esté en la raíz/static
    return """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <title>YeparDTEcore — Configuración Requerida</title>
        <style>
            body { font-family: sans-serif; background: #000; color: #fff; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
            .msg { text-align: center; border: 1px solid #333; padding: 2rem; border-radius: 20px; }
            .btn { background: #fff; color: #000; padding: 10px 20px; text-decoration: none; border-radius: 10px; font-weight: bold; }
        </style>
    </head>
    <body>
        <div class="msg">
            <h1>YeparDTEcore está activo</h1>
            <p>Sube el archivo <b>onboarding.html</b> a la carpeta <b>/static</b> para ver tu portal.</p>
            <br>
            <a href="/api/docs" class="btn">Ir a Documentación API</a>
        </div>
    </body>
    </html>
    """

@app.get("/onboarding", response_class=HTMLResponse, tags=["Frontend"])
async def onboarding_direct():
    """Ruta secundaria para acceder directamente al onboarding."""
    path = "static/onboarding.html"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return HTMLResponse(content="Archivo static/onboarding.html no encontrado.", status_code=404)
