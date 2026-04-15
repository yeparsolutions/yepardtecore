# app/models/caf.py
# ══════════════════════════════════════════════════════════════
# Modelo: CAF (Código de Autorización de Folios)
# El CAF es el archivo XML que el SII entrega a cada empresa
# autorizándola a usar un rango de números de folio.
#
# Analogia: el SII es el Registro Civil y el CAF es el
# talonario de RUTs que te autoriza a asignar — solo puedes
# usar los números que el SII te dio, en orden, sin saltarte.
# ══════════════════════════════════════════════════════════════

from sqlalchemy import String, Boolean, Integer, ForeignKey, Text, DateTime, Date
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from app.db.base import Base


class CAF(Base):
    """
    Código de Autorización de Folios entregado por el SII.
    Cada CAF autoriza un rango de folios para un tipo DTE específico.
    Por ejemplo: folios 1-100 para boletas (tipo 39).
    """
    __tablename__ = "cafs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # ── Relación con el Emisor ────────────────────────────────
    emisor_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("emisores.id"), nullable=False, index=True
    )
    emisor: Mapped["Emisor"] = relationship("Emisor", back_populates="cafs")

    # ── Tipo de DTE ───────────────────────────────────────────
    # 33 = Factura Electrónica
    # 39 = Boleta Electrónica
    # 56 = Nota de Débito
    # 61 = Nota de Crédito
    tipo_dte: Mapped[int] = mapped_column(Integer, nullable=False)

    # ── Rango de folios autorizados ───────────────────────────
    folio_desde: Mapped[int] = mapped_column(Integer, nullable=False)
    folio_hasta: Mapped[int] = mapped_column(Integer, nullable=False)

    # Lleva la cuenta de cuál folio toca usar a continuación
    # Empieza en folio_desde y sube hasta folio_hasta
    folio_actual: Mapped[int] = mapped_column(Integer, nullable=False)

    # ── Datos del CAF ─────────────────────────────────────────
    # El XML completo del CAF se guarda en BD (contiene la firma del SII)
    xml_caf: Mapped[str] = mapped_column(Text, nullable=False)

    # Fecha de vencimiento del CAF (el SII pone fecha límite)
    fecha_vencimiento: Mapped[str | None] = mapped_column(Date, nullable=True)

    # ── Estado ────────────────────────────────────────────────
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    ambiente: Mapped[str] = mapped_column(String(20), default="certificacion")

    # ── Timestamps ────────────────────────────────────────────
    created_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @property
    def folios_disponibles(self) -> int:
        """Cuántos folios quedan disponibles en este CAF."""
        return self.folio_hasta - self.folio_actual + 1

    @property
    def esta_agotado(self) -> bool:
        """True cuando ya no quedan folios en este CAF."""
        return self.folio_actual > self.folio_hasta

    @property
    def porcentaje_uso(self) -> float:
        """Qué porcentaje del CAF ya fue usado (0-100)."""
        total = self.folio_hasta - self.folio_desde + 1
        usados = self.folio_actual - self.folio_desde
        return round((usados / total) * 100, 1)

    def __repr__(self) -> str:
        return (
            f"<CAF tipo={self.tipo_dte} "
            f"folios={self.folio_desde}-{self.folio_hasta} "
            f"actual={self.folio_actual}>"
        )
