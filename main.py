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
    async with engine.begin() as conn:
        from app.models.emisor import Emisor
        from app.models.caf import CAF
        from app.models.dte import DTE
        from app.models.certificado import Certificado
        from app.models.usuario import Usuario
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc"
)

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
if not os.path.exists("static"):
    os.makedirs("static")

app.mount("/static", StaticFiles(directory="static"), name="static")

# ── FRONTEND: Onboarding ──────────────────────────────────────

@app.get("/", response_class=HTMLResponse, tags=["Frontend"])
async def root():
    path = "static/onboarding.html"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return HTMLResponse(content="<a href='/api/docs'>API Docs</a>")

@app.get("/onboarding", response_class=HTMLResponse, tags=["Frontend"])
async def onboarding_direct():
    path = "static/onboarding.html"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return HTMLResponse(content="static/onboarding.html no encontrado.", status_code=404)

# ── FRONTEND: Panel de Administración ────────────────────────

@app.get("/admin", response_class=HTMLResponse, tags=["Frontend"])
async def admin_panel():
    """
    Panel de configuración: crear emisor, subir .p12 y CAFs,
    ejecutar certificación SII.
    """
    path = "static/admin.html"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return HTMLResponse(content="static/admin.html no encontrado.", status_code=404)
