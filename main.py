from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from app.core.config import settings
from app.api.v1.router import api_router
from app.db.base import engine, Base
import os
import base64

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Verificación de tablas
    async with engine.begin() as conn:
        from app.models import Emisor, CAF, DTE, ItemDTE
        from app.models.usuario import Usuario
        await conn.run_sync(Base.metadata.create_all)
    yield
    # Shutdown
    await engine.dispose()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── CORS Actualizado ──────────────────────────────────────────
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

if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(api_router, prefix=settings.API_PREFIX)

# ── RUTA RAÍZ: La "Página Blanca" Dinámica ────────────────────
@app.get("/", response_class=HTMLResponse, tags=["Frontend"])
async def root():
    # Aquí puedes poner todo el HTML que genera la página blanca
    # He incluido el código que me mostraste adaptado para responder directo
    
    html_content = """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>YeparDTEcore — API Facturación Electrónica Chile</title>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;600;700;800&family=Inter:wght@300;400;500&display=swap" rel="stylesheet">
        <style>
            /* Aquí va tu CSS dinámico */
            body { 
                font-family: 'Inter', sans-serif; 
                margin: 0; 
                display: flex; 
                flex-direction: column; 
                align-items: center; 
                justify-content: center; 
                height: 100vh;
                background-color: #ffffff; /* El fondo blanco que buscas */
            }
            .container { text-align: center; }
            h1 { font-family: 'Syne', sans-serif; font-weight: 800; color: #1a1a1a; }
            .btn-docs { 
                margin-top: 20px;
                display: inline-block;
                padding: 10px 20px;
                background: #22C55E;
                color: white;
                text-decoration: none;
                border-radius: 8px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Facturación electrónica sin dolor</h1>
            <p>YeparDTEcore Motor v""" + settings.APP_VERSION + """</p>
            <a href="/docs" class="btn-docs">Documentación API</a>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)
