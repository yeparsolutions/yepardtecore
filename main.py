from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from app.core.config import settings
from app.api.v1.router import api_router
from app.db.base import engine, Base
import os

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Esto asegura que tus modelos (Emisor, Usuario, etc.) se creen en la BD
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    # Cambiamos la ruta de docs para que no interfiera con el home
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

# ── 1. CARGAR RUTAS DE API PRIMERO ───────────────────────────
app.include_router(api_router, prefix=settings.API_PREFIX)

# ── 2. CONFIGURAR ARCHIVOS ESTÁTICOS ─────────────────────────
# Si usas imágenes o CSS externos, se buscan en la carpeta /static
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# ── 3. EL FRONTEND (Tu Página Blanca) ────────────────────────
@app.get("/", response_class=HTMLResponse, tags=["Frontend"])
async def read_root():
    """
    Esta función es la que 'pisa' al backend y muestra el portal.
    He puesto el diseño oscuro profesional que pediste.
    """
    return """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>YeparDTEcore — Portal Oficial</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&family=Syne:wght@700;800&display=swap" rel="stylesheet">
        <style>
            :root { --primary: #2563EB; --bg: #050505; --card: #0f0f0f; }
            body { 
                margin: 0; background: var(--bg); color: white; 
                font-family: 'Inter', sans-serif; display: flex; 
                align-items: center; justify-content: center; height: 100vh; 
            }
            .portal-card {
                background: var(--card); border: 1px solid #1f1f1f;
                padding: 3rem; border-radius: 32px; text-align: center;
                max-width: 500px; width: 90%; box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5);
            }
            h1 { font-family: 'Syne', sans-serif; font-size: 2.8rem; margin: 0 0 1rem 0; letter-spacing: -1px; }
            p { color: #888; font-size: 1.1rem; line-height: 1.6; margin-bottom: 2rem; }
            .btn-group { display: flex; flex-direction: column; gap: 12px; }
            .btn {
                padding: 16px; border-radius: 14px; text-decoration: none;
                font-weight: 600; transition: all 0.2s; font-size: 1rem;
            }
            .btn-main { background: #fff; color: #000; }
            .btn-main:hover { background: #e0e0e0; transform: scale(1.02); }
            .btn-sub { background: #1a1a1a; color: #fff; border: 1px solid #333; }
            .btn-sub:hover { background: #222; }
            .status { font-size: 0.8rem; color: #22C55E; margin-bottom: 1rem; display: block; opacity: 0.8; }
        </style>
    </head>
    <body>
        <div class="portal-card">
            <span class="status">● Sistema YeparDTE Online</span>
            <h1>Facturación sin dolor.</h1>
            <p>Accede al motor de emisión DTE para Chile. Registra tu empresa, carga tus folios y emite en segundos.</p>
            <div class="btn-group">
                <a href="/api/docs" class="btn btn-main">Ingresar al Portal de Gestión</a>
                <a href="/v1/health" class="btn btn-sub">Verificar Estado del SII</a>
            </div>
        </div>
    </body>
    </html>
    """
