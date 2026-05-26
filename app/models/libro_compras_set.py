# app/models/libro_compras_set.py
# ══════════════════════════════════════════════════════════════
# Modelo: SetLibroCompras + ItemSetLibroCompras
#
# Guarda los documentos del set de compras que carga el usuario
# desde el .txt del SII. Permite generar el LibroCompras dinámico
# con exactamente los documentos correctos, para cualquier emisor.
# ══════════════════════════════════════════════════════════════

from sqlalchemy import String, Integer, Float, ForeignKey, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from sqlalchemy import DateTime
from app.db.base import Base


class SetLibroCompras(Base):
    """
    Cabecera del set de compras. Un emisor puede tener un set por período.
    """
    __tablename__ = "sets_libro_compras"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    emisor_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("emisores.id"), nullable=False, index=True
    )

    # N° de atención del SII (ej: "4841545")
    natencion: Mapped[str] = mapped_column(String(20), nullable=False)

    # Período tributario (ej: "2026-05")
    periodo: Mapped[str] = mapped_column(String(7), nullable=False)

    # Fecha resolución y número
    fch_resol: Mapped[str] = mapped_column(String(10), default="2026-04-19")
    nro_resol: Mapped[str] = mapped_column(String(10), default="0")

    # Factor de proporcionalidad IVA uso común
    fct_prop: Mapped[str] = mapped_column(String(6), default="0.60")

    created_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relación con los documentos del set
    items: Mapped[list["ItemSetLibroCompras"]] = relationship(
        "ItemSetLibroCompras",
        back_populates="set_compras",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self):
        return f"<SetLibroCompras emisor={self.emisor_id} natencion={self.natencion} periodo={self.periodo}>"


class ItemSetLibroCompras(Base):
    """
    Documento individual del set de compras.
    Cada fila = una línea del .txt del SII.
    """
    __tablename__ = "items_set_libro_compras"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    set_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sets_libro_compras.id"), nullable=False, index=True
    )
    set_compras: Mapped["SetLibroCompras"] = relationship(
        "SetLibroCompras", back_populates="items"
    )

    # ── Identificación del documento ──────────────────────────
    tipo_dte:  Mapped[int] = mapped_column(Integer, nullable=False)
    # Tipos válidos: 30, 33, 34, 46, 55, 60, 61, 56
    folio:     Mapped[int] = mapped_column(Integer, nullable=False)
    fecha_doc: Mapped[str] = mapped_column(String(10), nullable=False)  # AAAA-MM-DD

    # ── Proveedor ─────────────────────────────────────────────
    rut_doc:   Mapped[str] = mapped_column(String(12), nullable=False)
    razon_doc: Mapped[str] = mapped_column(String(100), nullable=False)

    # ── Montos base ───────────────────────────────────────────
    monto_neto:  Mapped[float] = mapped_column(Float, default=0.0)
    monto_exe:   Mapped[float] = mapped_column(Float, default=0.0)
    monto_iva:   Mapped[float] = mapped_column(Float, default=0.0)
    monto_total: Mapped[float] = mapped_column(Float, nullable=False)

    # ── Tipo especial IVA ─────────────────────────────────────
    # "" = normal
    # "iva_uso_comun"  → IVA proporcional (art. 23 N°1)
    # "iva_no_rec"     → IVA no recuperable (art. 23 N°5), ej: entrega gratuita
    # "iva_ret_total"  → IVA retenido total (Doc 46, Factura de Compra)
    tipo_especial:  Mapped[str] = mapped_column(String(20), default="")
    iva_uso_comun:  Mapped[float] = mapped_column(Float, default=0.0)
    iva_no_rec:     Mapped[float] = mapped_column(Float, default=0.0)
    cod_iva_no_rec: Mapped[int]   = mapped_column(Integer, default=9)
    iva_ret_total:  Mapped[float] = mapped_column(Float, default=0.0)

    def __repr__(self):
        return f"<ItemSetLibroCompras T{self.tipo_dte}-F{self.folio} ${self.monto_total}>"
