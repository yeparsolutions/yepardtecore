from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from app.core.config import settings
from app.api.v1.router import api_router
from app.db.base import engine, Base
import os

# ... (mantén tu lifespan igual) ...

app = FastAPI(title=settings.APP_NAME, docs_url="/docs")

# ✅ IMPORTANTE: Esto permite que el portal se comunique con la BD de Railway
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_PREFIX)

# ── LA RUTA QUE MANDA EL PORTAL ──────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    # Esta es la página blanca que mencionas. 
    # Al ser dinámica, la definimos aquí para que cargue de inmediato.
    return """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <title>YeparDTE — Portal de Registro</title>
        <style>
            body { font-family: 'Inter', sans-serif; background: #F9FAFB; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
            .card { background: white; padding: 40px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); text-align: center; max-width: 500px; }
            h1 { color: #111827; font-size: 24px; }
            p { color: #6B7280; }
            .btn { background: #2563EB; color: white; padding: 12px 24px; border-radius: 6px; text-decoration: none; display: inline-block; margin-top: 20px; }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>Facturación electrónica sin dolor</h1>
            <p>Bienvenido al portal de YeparDTEcore. Aquí puedes registrar tu empresa y configurar tu certificado digital.</p>
            <a href="/docs" class="btn">Panel de Control API</a>
        </div>
    </body>
    </html>
    """
