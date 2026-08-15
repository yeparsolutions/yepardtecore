import os
import asyncio
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
        from app.models.sii_health   import SIIHealth, SIIIncidente
        await conn.run_sync(Base.metadata.create_all)
    # Monitor interno del SII: corre cada 5 min DENTRO del proceso, sin cron externo
    tarea_monitor = asyncio.create_task(_monitor_sii_loop())
    yield
    tarea_monitor.cancel()
    await engine.dispose()


async def _monitor_sii_loop():
    """Chequea los servidores del SII cada 5 minutos, dentro del propio proceso."""
    from app.db.base import AsyncSessionLocal
    from app.api.v1.endpoints.pagos import ejecutar_check_sii
    _log = logging.getLogger("yepardtecore.monitor")
    await asyncio.sleep(10)  # dejar que la app termine de levantar
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await ejecutar_check_sii(db)
            _log.info("[MONITOR-SII] chequeo completado")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log.error(f"[MONITOR-SII] error: {e}")
        await asyncio.sleep(60)  # 1 minuto

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

@app.get("/api/docs", include_in_schema=False)
async def api_docs_redirect():
    """Redirige /api/docs al dashboard de documentación."""
    return RedirectResponse(url="/dashboard")

@app.get("/api", include_in_schema=False)
async def api_root():
    """Info básica de la API."""
    return {
        "servicio": "YeparDTEcore API",
        "version":  "2.1",
        "docs":     "https://app.yepardtecore.cl/dashboard",
        "health":   "https://app.yepardtecore.cl/api/health",
    }

@app.get("/onboarding", response_class=HTMLResponse, tags=["Frontend"])
async def onboarding():
    return _leer_html("static/onboarding.html", "onboarding.html no encontrado")

@app.get("/dashboard", response_class=HTMLResponse, tags=["Frontend"])
async def dashboard():
    """Panel de desarrollador — métricas, API key y suscripción."""
    return _leer_html("static/dashboard.html", "dashboard.html no encontrado")

@app.get("/admin", response_class=HTMLResponse, tags=["Frontend"])
async def admin():
    """Panel de administración de YeparDTEcore."""
    contenido = _leer_html("static/admin_yepar.html")
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
