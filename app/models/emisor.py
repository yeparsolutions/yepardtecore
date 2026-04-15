# app/models/emisor.py
# ══════════════════════════════════════════════════════════════
# Modelo: Emisor
# Representa cada empresa/negocio que usa YeparDTEcore
# para emitir documentos tributarios.
#
# Analogia: si DTEcore es una notaría, el Emisor es cada
# cliente que tiene un expediente ahí con sus documentos,
# sello y firma registrados.
# ══════════════════════════════════════════════════════════════

from sqlalchemy import String, Boolean, Text, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from app.db.base import Base


class Emisor(Base):
    """
    Empresa que emite documentos tributarios (boletas/facturas).
    Cada YeparStock, YeparDTE o app externa tiene su propio Emisor.
    """
    __tablename__ = "emisores"

    # ── Identificación ────────────────────────────────────────
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # RUT con formato chileno: 12.345.678-9
    rut: Mapped[str] = mapped_column(String(12), unique=True, nullable=False, index=True)

    razon_social: Mapped[str] = mapped_column(String(200), nullable=False)
    giro: Mapped[str] = mapped_column(String(200), nullable=False)

    # ── Dirección ─────────────────────────────────────────────
    direccion: Mapped[str] = mapped_column(String(300), nullable=False)
    comuna: Mapped[str] = mapped_column(String(100), nullable=False)
    ciudad: Mapped[str] = mapped_column(String(100), nullable=False)
    telefono: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # ── Certificado digital (legacy — se mantiene por compatibilidad) ──────
    # NOTA: el certificado activo ahora vive en la tabla certificados.
    # Estos campos se conservan para no romper el código existente
    # mientras se migra completamente a la tabla certificados.
    # Analogia: la firma del notario está en la caja fuerte,
    # no pegada en la pared para que cualquiera la use
    certificado_p12: Mapped[bytes | None] = mapped_column(nullable=True)
    certificado_password: Mapped[str | None] = mapped_column(String(200), nullable=True)
    certificado_vigencia: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # ── Logo para PDF ─────────────────────────────────────────
    logo_base64: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Estado ────────────────────────────────────────────────
    activo: Mapped[bool] = mapped_column(Boolean, default=True)

    # ambiente: certificacion = pruebas con SII, produccion = documentos reales
    ambiente: Mapped[str] = mapped_column(String(20), default="certificacion")

    # ── API Key de acceso ─────────────────────────────────────
    # Cada emisor tiene su API key para autenticarse
    # Analogia: como la contraseña de la caja fuerte — única por emisor
    api_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, index=True)

    # ── Correo de contacto ────────────────────────────────────
    correo: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # ── Timestamps ────────────────────────────────────────────
    created_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # ── Relaciones ────────────────────────────────────────────
    # Un Emisor tiene muchos CAFs (códigos de autorización de folios)
    cafs: Mapped[list["CAF"]] = relationship("CAF", back_populates="emisor", lazy="selectin")

    # Un Emisor tiene muchos documentos DTE emitidos
    documentos: Mapped[list["DTE"]] = relationship("DTE", back_populates="emisor", lazy="selectin")

    # Un Emisor puede tener múltiples certificados digitales
    # El activo se obtiene con: next((c for c in e.certificados_list if c.activo), None)
    certificados_list: Mapped[list["Certificado"]] = relationship(
        "Certificado", back_populates="emisor", lazy="selectin"
    )

    @property
    def certificado_activo(self):
        """
        Retorna el certificado activo del emisor.
        Primero busca en la tabla certificados (nuevo modelo),
        si no hay usa el certificado legacy del emisor.
        Analogia: busca la llave en el llavero nuevo,
        si no está ahí usa la llave vieja de repuesto.
        """
        # Buscar en tabla certificados
        cert = next((c for c in (self.certificados_list or []) if c.activo), None)
        if cert:
            return cert
        # Fallback al certificado legacy en emisores
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
