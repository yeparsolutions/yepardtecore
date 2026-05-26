"""
Migración: crea tablas sets_libro_compras e items_set_libro_compras
Ejecutar UNA SOLA VEZ: python migrate_set_libro_compras.py
"""
import asyncio, os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./yepardtecore.db")

SQL = [
    """
    CREATE TABLE IF NOT EXISTS sets_libro_compras (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        emisor_id   INTEGER NOT NULL REFERENCES emisores(id),
        natencion   VARCHAR(20) NOT NULL,
        periodo     VARCHAR(7)  NOT NULL,
        fch_resol   VARCHAR(10) NOT NULL DEFAULT '2026-04-19',
        nro_resol   VARCHAR(10) NOT NULL DEFAULT '0',
        fct_prop    VARCHAR(6)  NOT NULL DEFAULT '0.60',
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS items_set_libro_compras (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        set_id         INTEGER NOT NULL REFERENCES sets_libro_compras(id),
        tipo_dte       INTEGER NOT NULL,
        folio          INTEGER NOT NULL,
        fecha_doc      VARCHAR(10) NOT NULL,
        rut_doc        VARCHAR(12) NOT NULL,
        razon_doc      VARCHAR(100) NOT NULL,
        monto_neto     REAL NOT NULL DEFAULT 0.0,
        monto_exe      REAL NOT NULL DEFAULT 0.0,
        monto_iva      REAL NOT NULL DEFAULT 0.0,
        monto_total    REAL NOT NULL,
        tipo_especial  VARCHAR(20) NOT NULL DEFAULT '',
        iva_uso_comun  REAL NOT NULL DEFAULT 0.0,
        iva_no_rec     REAL NOT NULL DEFAULT 0.0,
        cod_iva_no_rec INTEGER NOT NULL DEFAULT 9,
        iva_ret_total  REAL NOT NULL DEFAULT 0.0
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_sets_lc_emisor ON sets_libro_compras(emisor_id)",
    "CREATE INDEX IF NOT EXISTS ix_items_lc_set   ON items_set_libro_compras(set_id)",
]

async def main():
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        for sql in SQL:
            await conn.execute(text(sql))
            print(f"  ✓ {sql.strip()[:60]}...")
    await engine.dispose()
    print("\nMigración completada.")

if __name__ == "__main__":
    asyncio.run(main())
