# app/models/dte.py
# ══════════════════════════════════════════════════════════════
# Modelo: DTE (Documento Tributario Electrónico)
# Es el registro central — cada boleta o factura emitida.
#
# Analogia: si el Emisor es el restaurante y el CAF es el
# talonario de boletas, el DTE es cada boleta individual
# con el detalle de lo que comió cada cliente.
# ══════════════════════════════════════════════════════════════

from sqlalchemy import String, Integer, Float, ForeignKey, Text, DateTime, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from app.db.base import Base


class DTE(Base):
    """
    Documento Tributario Electrónico emitido.
    Puede ser una Boleta (tipo 39) o Factura (tipo 33).
    """
    __tablename__ = "dtes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # ── Relación con el Emisor ────────────────────────────────
    emisor_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("emisores.id"), nullable=False, index=True
    )
    emisor: Mapped["Emisor"] = relationship("Emisor", back_populates="documentos")

    # ── Tipo e identificación ─────────────────────────────────
    tipo_dte: Mapped[int] = mapped_column(Integer, nullable=False)
    # 39 = Boleta, 33 = Factura, 61 = Nota de Crédito, 56 = Nota de Débito

    folio: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    # folio_fmt es el folio con formato visual: "B-00000001", "F-00000001"
    folio_fmt: Mapped[str] = mapped_column(String(20), nullable=False)

    # ── Datos del receptor (quien recibe el documento) ────────
    rut_receptor: Mapped[str | None] = mapped_column(String(12), nullable=True)
    nombre_receptor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    giro_receptor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    direccion_receptor: Mapped[str | None] = mapped_column(String(300), nullable=True)
    ciudad_receptor: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # ── Montos ────────────────────────────────────────────────
    monto_neto: Mapped[float] = mapped_column(Float, default=0.0)
    monto_iva: Mapped[float] = mapped_column(Float, default=0.0)
    monto_total: Mapped[float] = mapped_column(Float, nullable=False)
    tasa_iva: Mapped[int] = mapped_column(Integer, default=19)

    # ── Estado ante el SII ────────────────────────────────────
    # Ciclo de vida:
    # BORRADOR → ENVIADO → ACEPTADO / RECHAZADO
    # ACEPTADO puede luego ser ANULADO (con nota de crédito)
    estado: Mapped[str] = mapped_column(String(30), default="BORRADOR", index=True)
    # Estados posibles:
    # BORRADOR              — generado localmente, no enviado
    # ENVIADO               — sobre enviado al SII, esperando respuesta
    # ACEPTADO              — SII procesó y aceptó
    # ACEPTADO_CON_REPAROS  — SII acepta con observaciones (igual es válido)
    # RECHAZADO             — SII rechazó, hay que corregir y reenviar
    # ANULADO               — nota de crédito emitida contra este DTE

    # TrackID que entrega el SII al recibir el sobre
    # Se usa para hacer polling y saber si fue aceptado/rechazado
    track_id: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # ── Documentos generados ──────────────────────────────────
    # XML firmado del DTE (el documento oficial)
    xml_firmado: Mapped[str | None] = mapped_column(Text, nullable=True)

    # PDF en base64 (para descargar o mostrar en pantalla)
    pdf_base64: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Referencia interna del cliente ────────────────────────
    # YeparStock puede enviar "VENTA-20250323-001" para rastrear
    referencia_interna: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # ── Ambiente ──────────────────────────────────────────────
    ambiente: Mapped[str] = mapped_column(String(20), default="certificacion")

    # ── Timestamps ────────────────────────────────────────────
    created_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # ── Relaciones ────────────────────────────────────────────
    # Un DTE tiene varios items (líneas de detalle)
    items: Mapped[list["ItemDTE"]] = relationship(
        "ItemDTE", back_populates="dte", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<DTE tipo={self.tipo_dte} folio={self.folio} estado={self.estado}>"


class ItemDTE(Base):
    """
    Línea de detalle de un DTE.
    Cada producto o servicio del documento.
    """
    __tablename__ = "items_dte"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ── Relación con el DTE ───────────────────────────────────
    dte_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("dtes.id"), nullable=False, index=True
    )
    dte: Mapped["DTE"] = relationship("DTE", back_populates="items")

    # ── Datos del item ────────────────────────────────────────
    numero_linea: Mapped[int] = mapped_column(Integer, nullable=False)
    codigo: Mapped[str | None] = mapped_column(String(50), nullable=True)
    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    descripcion: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cantidad: Mapped[float] = mapped_column(Float, default=1.0)
    unidad: Mapped[str | None] = mapped_column(String(20), nullable=True)
    precio_unitario: Mapped[float] = mapped_column(Float, nullable=False)
    descuento_pct: Mapped[float] = mapped_column(Float, default=0.0)
    monto_item: Mapped[float] = mapped_column(Float, nullable=False)

    def __repr__(self) -> str:
        return f"<ItemDTE {self.nombre} x{self.cantidad} = ${self.monto_item}>"
