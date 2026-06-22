# app/models/usuario.py
# ══════════════════════════════════════════════════════════════
# Modelo: Usuario
# Representa a cada persona que accede a YeparDTEcore.
#
# Analogia: el usuario es el carnet de identidad —
# sin él no puedes entrar al edificio (la app).
# ══════════════════════════════════════════════════════════════

from sqlalchemy import String, Boolean, Integer, ForeignKey, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from app.db.base import Base


class Usuario(Base):
    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # ── Identidad ─────────────────────────────────────────────
    nombre:   Mapped[str] = mapped_column(String(100), nullable=False)
    apellido: Mapped[str] = mapped_column(String(100), nullable=False)
    email:    Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)

    # Contraseña hasheada — NUNCA se guarda en texto plano
    hashed_password: Mapped[str] = mapped_column(String(200), nullable=False)

    # ── Relación con Emisor ───────────────────────────────────
    emisor_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("emisores.id"), nullable=True, index=True
    )

    # ── Estado ────────────────────────────────────────────────
    activo:     Mapped[bool] = mapped_column(Boolean, default=True)
    verificado: Mapped[bool] = mapped_column(Boolean, default=False)
    es_admin:   Mapped[bool] = mapped_column(Boolean, default=False)

    # ── OTP de verificación de email ──────────────────────────
    # Se genera al registrarse; el usuario lo ingresa para activar su cuenta.
    # Analogía: el código que te mandan al cel cuando abres una cuenta bancaria.
    otp_verificacion:        Mapped[str | None] = mapped_column(String(6),  nullable=True)
    otp_verificacion_expira: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── OTP de recuperación de contraseña ─────────────────────
    # Se genera al solicitar "olvidé mi contraseña".
    otp_recuperacion:        Mapped[str | None] = mapped_column(String(6),  nullable=True)
    otp_recuperacion_expira: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Timestamps ────────────────────────────────────────────
    created_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # ── Aceptación de Términos y Condiciones ──────────────────
    # Guardamos fecha y versión para trazabilidad legal.
    tyc_aceptado:    Mapped[bool]          = mapped_column(Boolean, default=False)
    tyc_fecha:       Mapped[str | None]    = mapped_column(DateTime(timezone=True), nullable=True)
    tyc_version:     Mapped[str | None]    = mapped_column(String(10), nullable=True)  # ej: "1.0"
    tyc_ip:          Mapped[str | None]    = mapped_column(String(45), nullable=True)  # IP del cliente

    ultimo_login: Mapped[str | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return f"<Usuario {self.email}>"
