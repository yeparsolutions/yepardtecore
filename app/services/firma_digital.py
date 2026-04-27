# app/services/firma_digital.py
# ══════════════════════════════════════════════════════════════
# Compatibilidad: re-exporta FirmaDTE y FirmaSobre bajo el nombre
# FirmaDigital para no romper los imports existentes en el proyecto.
#
# La lógica real está en:
#   - firma_dte.py   → firma DTEs individuales (SignedInfo SIN xmlns:xsi)
#   - firma_sobre.py → firma el EnvioDTE     (SignedInfo CON xmlns:xsi)
# ══════════════════════════════════════════════════════════════

from app.services.firma_dte   import FirmaDTE
from app.services.firma_sobre import FirmaSobre
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.backends import default_backend
from base64 import b64decode


class FirmaDigital:
    """
    Fachada de compatibilidad.
    Delega la firma de DTEs a FirmaDTE y la firma del sobre a FirmaSobre.
    """

    def __init__(self, p12_bytes: bytes, password: str):
        self._dte   = FirmaDTE(p12_bytes, password)
        self._sobre = FirmaSobre(p12_bytes, password)

        # Propiedades que usa el código existente
        self.rut_certificado = self._dte.rut_certificado
        self.esta_vigente    = self._dte._cert.not_valid_before_utc is not None  # siempre True si cargó

    @property
    def vigente_hasta(self):
        return self._dte._cert.not_valid_after_utc

    # ── API pública usada por dte_service.py ──────────────────

    def firmar_dte(self, xml_bytes: bytes, folio: int, tipo_dte: int,
                   xml_caf: str, fecha_emision: str, rut_emisor: str,
                   monto_total: int, it1_nombre: str = 'PRODUCTO') -> bytes:
        """Delega a FirmaDTE.firmar()"""
        return self._dte.firmar(
            xml_bytes, folio, tipo_dte, xml_caf,
            fecha_emision, rut_emisor, monto_total, it1_nombre
        )

    def firmar_sobre(self, sobre_xml: str) -> str:
        """Delega a FirmaSobre.firmar()"""
        return self._sobre.firmar(sobre_xml)

    def info_certificado(self) -> dict:
        cert = self._dte._cert
        return {
            "subject":      cert.subject.rfc4514_string(),
            "emisor":       cert.issuer.rfc4514_string(),
            "valido_hasta": cert.not_valid_after_utc.isoformat(),
            "vigente":      self.vigente_hasta is not None,
            "rut":          self.rut_certificado,
        }

    @staticmethod
    def cargar_desde_base64(cert_b64: str, password: str) -> "FirmaDigital":
        return FirmaDigital(b64decode(cert_b64), password)
