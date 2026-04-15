# app/models/usuario.py
# ══════════════════════════════════════════════════════════════
# Modelo: Usuario
# Representa a cada persona que accede a YeparDTE u otros
# productos del ecosistema Yepar.
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
    # Analogia: guardamos la huella, no la llave
    hashed_password: Mapped[str] = mapped_column(String(200), nullable=False)

    # ── Relación con Emisor ───────────────────────────────────
    # Un usuario pertenece a un emisor (empresa)
    # Un emisor puede tener varios usuarios
    emisor_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("emisores.id"), nullable=True, index=True
    )

    # ── Estado ────────────────────────────────────────────────
    activo:       Mapped[bool] = mapped_column(Boolean, default=True)
    verificado:   Mapped[bool] = mapped_column(Boolean, default=False)
    es_admin:     Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Timestamps ────────────────────────────────────────────
    created_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ultimo_login: Mapped[str | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return f"<Usuario {self.email}>"
