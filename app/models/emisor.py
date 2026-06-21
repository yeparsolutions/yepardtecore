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

    # Código de Actividad Económica (ej: 620100 = Servicios de Informática)
    # Se obtiene del SII en: sii.cl → Mis datos → Actividades económicas
    acteco: Mapped[str | None] = mapped_column(String(10), nullable=True)

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

    # ── Plan y límites (para apps cliente como YeparDTE) ─────
    plan:             Mapped[str | None]  = mapped_column(String(20),  nullable=True, default="gratuito")
    docs_usados:      Mapped[int | None]  = mapped_column(Integer,     nullable=True, default=0)
    docs_limit:       Mapped[int | None]  = mapped_column(Integer,     nullable=True, default=20)
    vendedores_limit: Mapped[int | None]  = mapped_column(Integer,     nullable=True, default=0)
    otp_code:         Mapped[str | None]  = mapped_column(String(10),  nullable=True)
    otp_expira:       Mapped[str | None]  = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Resolución SII ───────────────────────────────────────
    # Cada ambiente tiene su propio número y fecha de resolución
    # Analogía: es como tener dos talonarios distintos —
    # uno para ensayos (certificación) y otro para el negocio real (producción)
    nro_resol_cert: Mapped[str | None] = mapped_column(String(10), nullable=True, default="0")
    fch_resol_cert: Mapped[str | None] = mapped_column(String(10), nullable=True, default="2000-01-01")
    nro_resol_prod: Mapped[str | None] = mapped_column(String(10), nullable=True, default="0")
    fch_resol_prod: Mapped[str | None] = mapped_column(String(10), nullable=True, default="2000-01-01")

    # ── Datos de la APP del desarrollador (modelo API para terceros) ──────────
    # Cuando un desarrollador contrata la API, registra el nombre y la URL de
    # SU software. La API key se "vincula" a ese dominio: solo funciona desde
    # ahí. Analogía: la licencia se instala en una sola máquina; para moverla,
    # primero hay que liberarla.
    nombre_app: Mapped[str | None] = mapped_column(String(120), nullable=True)
    url_app:    Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Dominio capturado en la PRIMERA llamada — la key queda atada a este.
    # NULL = aún no se ha vinculado a ninguna app (se vincula en el primer uso).
    origen_vinculado: Mapped[str | None] = mapped_column(String(300), nullable=True)
    vinculada_en:     Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Cobro / suscripción anual ($100.000/año por API, DTE ilimitados) ──────
    estado_pago:        Mapped[str | None] = mapped_column(String(20), nullable=True, default="pendiente")
    suscripcion_inicio: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suscripcion_fin:    Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)

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

    def get_resolucion(self, ambiente: str) -> tuple[str, str]:
        """
        Retorna (nro_resol, fch_resol) según el ambiente.
        Analogía: según si estás en el local de prueba o en el real,
        te damos el letrero correcto.
        """
        if ambiente == "produccion":
            return (
                self.nro_resol_prod or "0",
                self.fch_resol_prod or "2000-01-01",
            )
        else:
            return (
                self.nro_resol_cert or "0",
                self.fch_resol_cert or "2000-01-01",
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
