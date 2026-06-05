# app/models/emisor.py
from sqlalchemy import String, Boolean, Text, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from app.db.base import Base


class Emisor(Base):
    __tablename__ = "emisores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    rut: Mapped[str] = mapped_column(String(12), unique=True, nullable=False, index=True)
    razon_social: Mapped[str] = mapped_column(String(200), nullable=False)
    giro: Mapped[str] = mapped_column(String(200), nullable=False)
    acteco: Mapped[str | None] = mapped_column(String(10), nullable=True)

    direccion: Mapped[str] = mapped_column(String(300), nullable=False)
    comuna: Mapped[str] = mapped_column(String(100), nullable=False)
    ciudad: Mapped[str] = mapped_column(String(100), nullable=False)
    telefono: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # ── Resolución SII ────────────────────────────────────────
    # Número y fecha de la resolución que autoriza al emisor como
    # contribuyente electrónico. Se obtiene en sii.cl → Mi SII →
    # Factura Electrónica → Consulta Contribuyente Autorizado.
    nro_resolucion: Mapped[str] = mapped_column(String(10), default="0")
    fch_resolucion: Mapped[str] = mapped_column(String(10), default="2000-01-01")

    # ── Certificado legacy ────────────────────────────────────
    certificado_p12: Mapped[bytes | None] = mapped_column(nullable=True)
    certificado_password: Mapped[str | None] = mapped_column(String(200), nullable=True)
    certificado_vigencia: Mapped[str | None] = mapped_column(String(30), nullable=True)

    logo_base64: Mapped[str | None] = mapped_column(Text, nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    ambiente: Mapped[str] = mapped_column(String(20), default="certificacion")
    api_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, index=True)
    correo: Mapped[str | None] = mapped_column(String(200), nullable=True)

    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    cafs: Mapped[list["CAF"]] = relationship("CAF", back_populates="emisor", lazy="selectin")
    documentos: Mapped[list["DTE"]] = relationship("DTE", back_populates="emisor", lazy="selectin")
    certificados_list: Mapped[list["Certificado"]] = relationship("Certificado", back_populates="emisor", lazy="selectin")

    @property
    def certificado_activo(self):
        cert = next((c for c in (self.certificados_list or []) if c.activo), None)
        if cert:
            return cert
        if self.certificado_p12:
            from app.models.certificado import Certificado
            fallback = Certificado()
            fallback.certificado_p12      = self.certificado_p12
            fallback.certificado_password = self.certificado_password
            fallback.rut_firmante         = None
            fallback.nombre_firmante      = None
            return fallback
        return None

    def __repr__(self) -> str:
        return f"<Emisor {self.rut} - {self.razon_social}>"
