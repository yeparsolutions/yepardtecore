# main.py
# ══════════════════════════════════════════════════════════════
# Punto de entrada de YeparDTEcore
# Railway ejecuta este archivo para arrancar el servidor.
# ══════════════════════════════════════════════════════════════

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse  # ✅ Añadido FileResponse
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

    # ✅ Tablas automáticas
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
    description="Motor de facturación electrónica YeparDTEcore.",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────────
# ✅ Se añade el dominio .cl para que el portal pueda hacer peticiones
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.DEBUG else [
        "https://yeparstock.yeparsolutions.com",
        "https://yeparte.yeparsolutions.com",
        "https://yepardte.yeparsolutions.com",
        "https://yepardtecore.cl", 
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Archivos estáticos ────────────────────────────────────────
# IMPORTANTE: Asegúrate de que tu index.html y assets estén en la carpeta /static
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Registrar rutas API ───────────────────────────────────────
app.include_router(api_router, prefix=settings.API_PREFIX)


# ── Ruta raíz — Portal de Facturación ────────────────────────
@app.get("/", response_class=FileResponse, tags=["Frontend"])
async def root():
    """
    Sirve la página principal del portal de facturación (Página Blanca).
    Busca el archivo index.html dentro de la carpeta /static.
    """
    frontend_index = os.path.join("static", "index.html")
    
    if os.path.exists(frontend_index):
        return FileResponse(frontend_index)
    
    # Fallback por si el archivo no existe aún en el servidor
    return HTMLResponse(content="""
        <html>
            <body style='font-family: sans-serif; text-align: center; padding-top: 50px;'>
                <h1>YeparDTEcore</h1>
                <p>El portal web aún no ha sido cargado en la carpeta /static.</p>
                <a href='/docs'>Ir a Documentación de la API</a>
            </body>
        </html>
    """, status_code=404)
