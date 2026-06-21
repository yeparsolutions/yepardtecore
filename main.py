import os
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from contextlib import asynccontextmanager

# Configurar logging a nivel INFO para que los logger.info de la app
# (ej. 'yepardtecore.api') se escriban al stdout que Railway captura.
# Sin esto, los logger con nombre propio quedan en WARNING por defecto y
# sus mensajes INFO son invisibles — por eso los logs [SET] no aparecían.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# Asegurar que el logger de la app emita INFO
logging.getLogger("yepardtecore").setLevel(logging.INFO)
logging.getLogger("yepardtecore.api").setLevel(logging.INFO)

from app.core.config import settings
from app.api.v1.router import api_router
from app.api.public.router import router as public_router
from app.db.base import engine, Base

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        from app.models.emisor      import Emisor
        from app.models.caf         import CAF
        from app.models.dte         import DTE
        from app.models.certificado import Certificado
        from app.models.usuario     import Usuario
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API ───────────────────────────────────────────────────────
app.include_router(api_router, prefix=settings.API_PREFIX)
app.include_router(public_router)

# ── Estáticos ─────────────────────────────────────────────────
if not os.path.exists("static"):
    os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Helpers ───────────────────────────────────────────────────
def _leer_html(path: str, fallback: str = "") -> str:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return fallback

# ── Rutas frontend ────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, tags=["Frontend"])
async def root():
    return _leer_html("static/onboarding.html", "<a href='/api/docs'>API Docs</a>")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return RedirectResponse(url="/static/favicon-32.png")

@app.get("/onboarding", response_class=HTMLResponse, tags=["Frontend"])
async def onboarding():
    return _leer_html("static/onboarding.html", "onboarding.html no encontrado")

@app.get("/admin", response_class=HTMLResponse, tags=["Frontend"])
async def admin():
    """Panel de administración: emisor, certificado, CAFs y certificación SII."""
    contenido = _leer_html("static/admin.html")
    if contenido:
        return HTMLResponse(content=contenido, media_type="text/html; charset=utf-8")
    # Si el archivo no existe, devolver página de error clara
    return HTMLResponse(
        status_code=404,
        content="""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
        <title>YeparDTEcore</title></head>
        <body style="background:#07090f;color:#e2e8f0;font-family:monospace;
        display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
        <div style="text-align:center">
          <h2 style="color:#ef4444">static/admin.html no encontrado</h2>
          <p style="color:#64748b;margin-top:8px">
            Sube el archivo admin.html a la carpeta static/ en GitHub
          </p>
          <a href="/api/docs" style="color:#10b981">Ir a API Docs</a>
        </div></body></html>"""
    )
