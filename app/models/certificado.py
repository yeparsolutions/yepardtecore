# app/models/certificado.py
# ══════════════════════════════════════════════════════════════
# Modelo: Certificado digital
#
# Separado del emisor porque:
# - Una empresa puede tener múltiples firmantes
# - El certificado puede cambiar (vence cada 1-2 años)
# - Necesitamos saber QUIÉN firmó cada DTE
#
# Analogía: el emisor es la empresa, el certificado es
# el carnet de identidad del firmante — pueden haber
# varios firmantes autorizados por la misma empresa.
# ══════════════════════════════════════════════════════════════

from sqlalchemy import String, Boolean, Integer, ForeignKey, DateTime, Date, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from app.db.base import Base


class Certificado(Base):
    """
    Certificado digital (.p12) de un firmante autorizado por un emisor.
    Un emisor puede tener múltiples certificados (ej: distintos representantes).
    Solo uno puede estar activo a la vez.
    """
    __tablename__ = "certificados"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # ── Relación con el emisor ────────────────────────────────
    emisor_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("emisores.id"), nullable=False, index=True
    )
    emisor: Mapped["Emisor"] = relationship("Emisor", back_populates="certificados_list")

    # ── Datos del firmante (extraídos del .p12) ───────────────
    # El RUT del firmante puede ser distinto al RUT del emisor
    # Ejemplo: Alberto Yépez (25.648.612-1) firma por Yepar Solutions (78.377.021-0)
    rut_firmante: Mapped[str | None] = mapped_column(String(12), nullable=True)
    nombre_firmante: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # ── Certificado de FIRMA (firma los DTEs) ─────────────────
    # Guardado como bytes en BD — nunca en disco
    certificado_p12: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    certificado_password: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # ── Certificado de AUTENTICACION SII (obtiene el token) ───
    # Separado del de firma: el SII puede requerir un certificado
    # homologado (ej: E-Sign) para autenticarse, mientras otro
    # (ej: Firmadox con nonRepudiation) firma los DTEs.
    # Si es None, se usa certificado_p12 para ambas funciones.
    certificado_auth_p12: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    certificado_auth_password: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # ── Vigencia ──────────────────────────────────────────────
    fecha_emision: Mapped[str | None] = mapped_column(Date, nullable=True)
    fecha_vencimiento: Mapped[str | None] = mapped_column(Date, nullable=True)

    # ── Estado ────────────────────────────────────────────────
    # Solo un certificado activo por emisor a la vez
    activo: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Timestamps ────────────────────────────────────────────
    created_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Certificado {self.rut_firmante} → emisor_id={self.emisor_id} activo={self.activo}>"
