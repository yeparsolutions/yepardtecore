# app/models/sii_health.py
# ══════════════════════════════════════════════════════════════
# Monitor de salud de los servidores del SII.
#   SIIHealth     — estado ACTUAL de cada servidor (1 fila por servidor)
#   SIIIncidente  — caídas ya cerradas (historial de "hubo caída de X a Y")
# Las tablas se crean solas vía Base.metadata.create_all al desplegar.
# ══════════════════════════════════════════════════════════════
from datetime import datetime, timezone
from sqlalchemy import String, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


def _now():
    return datetime.now(timezone.utc)


class SIIHealth(Base):
    __tablename__ = "sii_health"

    # clave del servidor: maullin | palena | boleta_cert | boleta_prod
    servidor:     Mapped[str] = mapped_column(String(30), primary_key=True)
    nombre:       Mapped[str] = mapped_column(String(60))
    url:          Mapped[str] = mapped_column(String(200))
    estado:       Mapped[str] = mapped_column(String(12), default="desconocido")  # ok | lento | caido
    latencia_ms:  Mapped[int | None]      = mapped_column(Integer, nullable=True)
    ultimo_ok:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ultimo_check: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # si está caído, desde cuándo (null = está arriba)
    caido_desde:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SIIIncidente(Base):
    __tablename__ = "sii_incidentes"

    id:           Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    servidor:     Mapped[str] = mapped_column(String(30), index=True)
    nombre:       Mapped[str] = mapped_column(String(60))
    inicio:       Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fin:          Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duracion_seg: Mapped[int]      = mapped_column(Integer, default=0)
    creado_en:    Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
