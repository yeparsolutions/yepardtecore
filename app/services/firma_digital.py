# app/services/firma_digital.py
# ══════════════════════════════════════════════════════════════
# Firma digital para SII Chile — v12.0 Híbrido
#
# Flujo:
#   1. Python genera el DTE y lo timbra (TED con CAF)
#   2. Python firma cada DTE con digest in-tree (correcto para SII)
#   3. Python construye el sobre EnvioDTE
#   4. Java firma el sobre (SetDTE) — probado y funciona con EPR
# ══════════════════════════════════════════════════════════════

import asyncio
import base64
import logging
import os
import re
import subprocess

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.serialization import pkcs12

logger = logging.getLogger("yepardtecore.firma")

_JAVA_CLASS_DIR = os.environ.get("FIRMA_JAVA_DIR", "/app")


def _java_disponible() -> bool:
    try:
        r = subprocess.run(["java", "-version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _firmar_sobre_con_java(sobre_xml_bytes: bytes, pfx_bytes: bytes, password: str,
                           modo: str = "firmar-sobre") -> bytes:
    """Usa Java para firmar el SetDTE del sobre o el EnvioLibro del libro."""
    import tempfile
    xml_b64 = base64.b64encode(sobre_xml_bytes).decode()
    pfx_b64 = base64.b64encode(pfx_bytes).decode()

    cmd = ["java", "-cp", _JAVA_CLASS_DIR, "FirmaDTE",
           modo, xml_b64, pfx_b64, password]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    if result.returncode != 0:
        raise RuntimeError(f"FirmaDTE.java [firmar-sobre] error: {result.stderr[:300]}")
    if not result.stdout:
        raise RuntimeError("FirmaDTE.java [firmar-sobre]: sin output")

    return base64.b64decode(result.stdout)


class FirmaDigital:
    """
    Fachada de firma digital para DTEs del SII Chile.
    
    - Timbre TED: Python (firma_dte.py)
    - Firma DTEs: Python in-tree (firma_xml_sii.py) — digest correcto para SII
    - Firma Sobre: Java (FirmaDTE.java) — probado y funciona con EPR
    """

    def __init__(self, p12_bytes: bytes, password: str,
                 ambiente: str = "certificacion"):
        self._p12_bytes = p12_bytes
        self._password  = password
        self._ambiente  = ambiente

        pwd = password.encode("utf-8") if isinstance(password, str) else password
        _, cert, _ = pkcs12.load_key_and_certificates(
            p12_bytes, pwd, backend=default_backend()
        )
        self._cert = cert
        self.rut_certificado = self._extraer_rut(cert)
        self.esta_vigente    = True

        from app.services.firma_dte import FirmaDTE as FirmaDTEPy
        self._dte = FirmaDTEPy(p12_bytes, password)

    @property
    def vigente_hasta(self):
        return self._cert.not_valid_after_utc

    async def firmar_dte(
        self,
        xml_bytes:     bytes,
        folio:         int,
        tipo_dte:      int,
        xml_caf:       str,
        fecha_emision: str,
        rut_emisor:    str,
        monto_total:   int,
        it1_nombre:    str = "PRODUCTO",
    ) -> bytes:
        """
        Paso 1-2: Timbra el DTE con CAF (Python).
        La firma XMLDSig se aplica en firmar_sobre junto con el sobre.
        """
        logger.info(f"[FirmaDigital] Timbrando DTE {tipo_dte}-{folio}")
        xml_timbrado = self._dte.generar_xml_con_ted(
            xml_bytes     = xml_bytes,
            folio         = folio,
            tipo_dte      = tipo_dte,
            xml_caf       = xml_caf,
            fecha_emision = fecha_emision,
            rut_emisor    = rut_emisor,
            monto_total   = monto_total,
            it1_nombre    = it1_nombre,
        )
        return xml_timbrado

    async def firmar_libro(self, libro_xml: str) -> str:
        """
        Firma el LibroCompraVenta con XMLDSig sobre EnvioLibro ID="LibroVentas".
        Usa FirmaDTE.java en modo firmar-libro.
        """
        import asyncio
        p12  = self._p12_bytes
        pwd  = self._password

        def _firmar():
            return _firmar_sobre_con_java(
                libro_xml.encode("ISO-8859-1"), p12, pwd,
                modo="firmar-libro"
            )
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _firmar)
        return result.decode("ISO-8859-1")

    async def firmar_sobre(self, sobre_xml: str) -> str:
        """
        Firma todos los DTEs con Python in-tree y el SetDTE con Java.
        
        Returns: EnvioDTE firmado (string ISO-8859-1)
        """
        logger.info("[FirmaDigital] Firmando DTEs con Python in-tree + sobre con Java")

        from app.services.firma_xml_sii import firmar_dtes_y_sobre_con_java

        loop = asyncio.get_event_loop()
        resultado = await loop.run_in_executor(
            None,
            lambda: firmar_dtes_y_sobre_con_java(
                sobre_xml, self._p12_bytes, self._password
            )
        )
        return resultado

    def info_certificado(self) -> dict:
        cert = self._cert
        return {
            "subject":      cert.subject.rfc4514_string(),
            "emisor":       cert.issuer.rfc4514_string(),
            "valido_hasta": cert.not_valid_after_utc.isoformat(),
            "vigente":      True,
            "rut":          self.rut_certificado,
        }

    @staticmethod
    def _extraer_rut(cert) -> str:
        subject = cert.subject.rfc4514_string()
        m = re.search(r"(\d{1,2}\.?\d{3}\.?\d{3}-[\dkK])", subject, re.I)
        if m:
            return m.group(1)
        try:
            from cryptography.x509 import ExtensionOID
            san = cert.extensions.get_extension_for_oid(
                ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
            for name in san.value:
                if hasattr(name, "value") and isinstance(name.value, bytes):
                    raw = name.value.decode("utf-8", errors="replace")
                    m2  = re.search(r"(\d{7,8}-[\dkK])", raw)
                    if m2:
                        return m2.group(1)
        except Exception:
            pass
        return ""
