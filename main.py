from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from app.core.config import settings
from app.api.v1.router import api_router
from app.db.base import engine, Base
from contextlib import asynccontextmanager
import os

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Esto asegura que tus tablas (Emisor, Usuario, etc.) se creen en Railway
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs"
)

# Configuración de CORS para que el portal no se bloquee
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_PREFIX)

# ── EL PORTAL (DISEÑO RECUPERADO) ──────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>YeparDTEcore — Portal de Gestión</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600&family=Syne:wght@700;800&display=swap" rel="stylesheet">
        <style>
            :root { --primary: #22C55E; --bg: #0a0a0a; --card: #141414; }
            body { 
                margin: 0; padding: 0; 
                background-color: var(--bg); 
                color: white; 
                font-family: 'Inter', sans-serif;
                display: flex; align-items: center; justify-content: center;
                height: 100vh; overflow: hidden;
            }
            .container {
                text-align: center;
                padding: 40px;
                background: var(--card);
                border: 1px solid #222;
                border-radius: 24px;
                box-shadow: 0 20px 50px rgba(0,0,0,0.5);
                max-width: 450px;
                width: 90%;
            }
            h1 { 
                font-family: 'Syne', sans-serif; 
                font-size: 2.5rem; 
                margin-bottom: 10px;
                background: linear-gradient(90deg, #fff, #666);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }
            p { color: #888; line-height: 1.6; margin-bottom: 30px; }
            .grid { display: grid; gap: 15px; }
            .btn {
                padding: 14px 20px;
                border-radius: 12px;
                text-decoration: none;
                font-weight: 600;
                transition: all 0.3s ease;
                display: block;
            }
            .btn-primary { background: var(--primary); color: #000; }
            .btn-primary:hover { background: #16a34a; transform: translateY(-2px); }
            .btn-secondary { background: #1f1f1f; color: #fff; border: 1px solid #333; }
            .btn-secondary:hover { background: #2a2a2a; }
            .badge {
                font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
                background: #222; padding: 4px 12px; border-radius: 20px; color: var(--primary);
                margin-bottom: 20px; display: inline-block;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="badge">Sistema Activo v0.1.0</div>
            <h1>YeparDTEcore</h1>
            <p>Motor de facturación electrónica chilena. Registra tu empresa y gestiona tus folios CAF desde un solo lugar.</p>
            
            <div class="grid">
                <a href="/docs" class="btn btn-primary">Configurar Mi Empresa</a>
                <a href="/v1/health" class="btn btn-secondary">Estado del Servicio</a>
            </div>
        </div>
    </body>
    </html>
    """
