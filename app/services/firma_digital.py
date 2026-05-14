# app/services/firma_digital.py
# ══════════════════════════════════════════════════════════════
# Fachada de firma digital para SII Chile — v10.0 AppDTE
#
# Delega timbre y firma XMLDSig a la API de AppDTE, que implementa
# correctamente el algoritmo de canonicalización esperado por el SII.
#
# Analogía: en vez de hacer el notariado en casa (con riesgo de
# errores en el protocolo C14N), usamos el notario certificado
# AppDTE que ya fue validado contra el SII.
#
# Flujo de firma de un DTE:
#   1. timbre_dte → AppDTE inserta el TED con la llave del CAF
#   2. firma_xml  → AppDTE firma el nodo Documento (XMLDSig)
#
# Flujo de firma del sobre:
#   3. firma_xml  → AppDTE firma el nodo SetDTE (XMLDSig)
# ══════════════════════════════════════════════════════════════

import logging
import re
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.backends import default_backend

from app.services.appdte_client import AppDTEClient

logger = logging.getLogger("yepardtecore.firma")


class FirmaDigital:
    """
    Fachada principal de firma digital para DTEs del SII Chile.

    Usa AppDTE para timbre y firma XMLDSig, garantizando
    compatibilidad con el validador del SII.
    """

    def __init__(self, p12_bytes: bytes, password: str,
                 ambiente: str = "certificacion"):
        # Guardar credenciales para pasar a AppDTE en cada llamada
        self._p12_bytes = p12_bytes
        self._password  = password
        self._ambiente  = ambiente

        # Inicializar cliente AppDTE con la URL correcta según ambiente
        self._appdte = AppDTEClient(ambiente=ambiente)

        # Leer el certificado localmente solo para extraer metadatos
        # (RUT del firmante, fecha de vencimiento) — NO firma localmente
        pwd = password.encode("utf-8") if isinstance(password, str) else password
        _, cert, _ = pkcs12.load_key_and_certificates(
            p12_bytes, pwd, backend=default_backend()
        )
        self._cert = cert

        # RUT del certificado (usado como rut_enviador)
        self.rut_certificado = self._extraer_rut(cert)
        self.esta_vigente    = True

    # ── Propiedades ───────────────────────────────────────────────────

    @property
    def vigente_hasta(self):
        """Fecha de vencimiento del certificado."""
        return self._cert.not_valid_after_utc

    # ── API pública ───────────────────────────────────────────────────

    async def firmar_dte(
        self,
        xml_bytes:    bytes,
        folio:        int,
        tipo_dte:     int,
        xml_caf:      str,
        fecha_emision: str,
        rut_emisor:   str,
        monto_total:  int,
        it1_nombre:   str = "PRODUCTO",
    ) -> bytes:
        """
        Timbra y firma un DTE individual usando AppDTE.

        Pasos:
          1. /api/timbredte → AppDTE inserta el TED con firma CAF
          2. /api/firmaxml  → AppDTE firma el nodo Documento (XMLDSig)

        El id_referencia sigue el formato de nuestro xml_builder:
        "DTE-{tipo}-{folio}" (ej: "DTE-33-65")

        Args:
            xml_bytes:    XML del DTE sin timbre (bytes ISO-8859-1)
            folio:        Número de folio
            tipo_dte:     Tipo de DTE (33, 56, 61, etc.)
            xml_caf:      XML del CAF del SII
            fecha_emision: Fecha emisión (formato ISO: "2026-05-14")
            rut_emisor:   RUT del emisor
            monto_total:  Monto total del documento
            it1_nombre:   Nombre del primer ítem (va en el TED)

        Returns:
            bytes ISO-8859-1 del DTE timbrado y firmado
        """
        # Decodificar bytes a string ISO-8859-1
        xml_str = xml_bytes.decode("iso-8859-1")

        logger.info(f"[FirmaDigital] Timbrando folio={folio} tipo={tipo_dte}")

        # Paso 1: Timbre — AppDTE inserta el TED con la llave del CAF
        xml_timbrado = await self._appdte.timbre_dte(xml_str, xml_caf)

        logger.info(f"[FirmaDigital] Firmando DTE folio={folio} tipo={tipo_dte}")

        # Paso 2: Firma XMLDSig del Documento
        # El id_referencia debe coincidir con el atributo ID del Documento
        # en nuestro xml_builder: Documento ID="DTE-{tipo}-{folio}"
        id_referencia = f"DTE-{tipo_dte}-{folio}"

        xml_firmado = await self._appdte.firma_xml(
            xml_iso       = xml_timbrado,
            pfx_bytes     = self._p12_bytes,
            password      = self._password,
            nodo_xml      = "Documento",
            id_referencia = id_referencia,
        )

        return xml_firmado.encode("iso-8859-1")

    async def firmar_sobre(self, sobre_xml: str) -> str:
        """
        Firma el SetDTE del EnvioDTE usando AppDTE.

        El sobre se firma con el nodo SetDTE (ID="SetDoc"),
        que es la referencia que usa el SII para verificar
        la autenticidad del envío completo.

        Args:
            sobre_xml: EnvioDTE sin firma (string UTF-8 o ISO-8859-1)

        Returns:
            EnvioDTE firmado (string ISO-8859-1) con declaración XML
        """
        logger.info("[FirmaDigital] Firmando sobre EnvioDTE (SetDTE)")

        xml_firmado = await self._appdte.firma_xml(
            xml_iso       = sobre_xml,
            pfx_bytes     = self._p12_bytes,
            password      = self._password,
            nodo_xml      = "SetDTE",
            id_referencia = "SetDoc",
        )

        # Asegurar declaración XML al inicio
        if not xml_firmado.lstrip().startswith("<?xml"):
            xml_firmado = '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + xml_firmado

        return xml_firmado

    def info_certificado(self) -> dict:
        """Retorna metadata del certificado digital."""
        cert = self._cert
        return {
            "subject":      cert.subject.rfc4514_string(),
            "emisor":       cert.issuer.rfc4514_string(),
            "valido_hasta": cert.not_valid_after_utc.isoformat(),
            "vigente":      True,
            "rut":          self.rut_certificado,
        }

    # ── Métodos estáticos ────────────────────────────────────────────

    @staticmethod
    def cargar_desde_base64(cert_b64: str, password: str,
                            ambiente: str = "certificacion") -> "FirmaDigital":
        """Crea una instancia desde un certificado en Base64."""
        from base64 import b64decode
        return FirmaDigital(b64decode(cert_b64), password, ambiente)

    # ── Internos ─────────────────────────────────────────────────────

    @staticmethod
    def _extraer_rut(cert) -> str:
        """Extrae el RUT del sujeto del certificado X.509."""
        subject = cert.subject.rfc4514_string()
        m = re.search(r"(\d{1,2}\.?\d{3}\.?\d{3}-[\dkK])", subject, re.I)
        if m:
            return m.group(1)
        # Fallback: buscar en SAN
        try:
            from cryptography.x509 import ExtensionOID
            san = cert.extensions.get_extension_for_oid(
                ExtensionOID.SUBJECT_ALTERNATIVE_NAME
            )
            for name in san.value:
                if hasattr(name, "value") and isinstance(name.value, bytes):
                    raw = name.value.decode("utf-8", errors="replace")
                    m2  = re.search(r"(\d{7,8}-[\dkK])", raw)
                    if m2:
                        return m2.group(1)
        except Exception:
            pass
        return ""
