# app/services/firma_digital.py
# ══════════════════════════════════════════════════════════════
# Fachada de compatibilidad — re-exporta FirmaDigital
#
# Delega firma de DTEs a FirmaDTE y firma del sobre a FirmaSobre.
# El resto del proyecto solo necesita importar FirmaDigital.
# ══════════════════════════════════════════════════════════════

from app.services.firma_dte   import FirmaDTE
from app.services.firma_sobre import FirmaSobre
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.backends import default_backend
from base64 import b64decode


class FirmaDigital:
    """
    Fachada principal de firma para SII Chile.

    Uso:
        fd = FirmaDigital(p12_bytes, password)
        xml_dte_firmado = fd.firmar_dte(xml_bytes, folio, tipo, caf,
                                         fecha, rut_emisor, monto, item)
        sobre_firmado   = fd.firmar_sobre(sobre_xml_str)
    """

    def __init__(self, p12_bytes: bytes, password: str):
        self._dte   = FirmaDTE(p12_bytes, password)
        self._sobre = FirmaSobre(p12_bytes, password)
        self.rut_certificado = self._dte.rut_certificado
        self.esta_vigente    = True  # True si el p12 cargó correctamente

    @property
    def vigente_hasta(self):
        return self._dte._cert.not_valid_after_utc

    # ── API pública ───────────────────────────────────────────

    def firmar_dte(self, xml_bytes: bytes, folio: int, tipo_dte: int,
                   xml_caf: str, fecha_emision: str, rut_emisor: str,
                   monto_total: int, it1_nombre: str = 'PRODUCTO') -> bytes:
        """Firma un DTE individual. Retorna bytes ISO-8859-1."""
        return self._dte.firmar(
            xml_bytes, folio, tipo_dte, xml_caf,
            fecha_emision, rut_emisor, monto_total, it1_nombre
        )

    def firmar_sobre(self, sobre_xml: str) -> str:
        """Firma el EnvioDTE completo. Retorna string con declaración XML."""
        return self._sobre.firmar(sobre_xml)

    def info_certificado(self) -> dict:
        cert = self._dte._cert
        return {
            "subject":      cert.subject.rfc4514_string(),
            "emisor":       cert.issuer.rfc4514_string(),
            "valido_hasta": cert.not_valid_after_utc.isoformat(),
            "vigente":      True,
            "rut":          self.rut_certificado,
        }

    @staticmethod
    def cargar_desde_base64(cert_b64: str, password: str) -> "FirmaDigital":
        return FirmaDigital(b64decode(cert_b64), password)
