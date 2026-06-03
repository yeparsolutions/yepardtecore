# app/db/base.py
# ══════════════════════════════════════════════════════════════
# Configuración de la base de datos con SQLAlchemy async
# Analogia: si la base de datos es una bodega, SQLAlchemy es
# el bodeguero que sabe dónde está cada cosa y cómo pedirla.
# ══════════════════════════════════════════════════════════════

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings

# ── Motor de base de datos ────────────────────────────────────
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_pre_ping=True,
)

# ── Fábrica de sesiones ───────────────────────────────────────
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# ── Clase base para todos los modelos ────────────────────────
class Base(DeclarativeBase):
    pass

# ── Dependency de FastAPI ─────────────────────────────────────
async def get_db() -> AsyncSession:
    """
    Genera una sesión de BD para cada request.
    El 'finally' garantiza que la sesión siempre se cierra,
    aunque ocurra un error — evita fugas de conexiones.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

# ── Importar modelos para que SQLAlchemy cree las tablas ──────
from app.models.emisor      import Emisor       # noqa
from app.models.caf         import CAF          # noqa
from app.models.dte         import DTE, ItemDTE # noqa
from app.models.certificado import Certificado  # noqa
from app.models.libro_compras_set import SetLibroCompras, ItemSetLibroCompras  # noqa
