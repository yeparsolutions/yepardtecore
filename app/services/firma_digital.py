# app/services/firma_digital.py
# ══════════════════════════════════════════════════════════════
# Firma digital para SII Chile — v11.0 Java XMLDSig
#
# Flujo:
#   1. Python genera el DTE y lo timbra (TED con CAF)
#   2. Java firma el DTE (XMLDSig nativo — compatible con SII)
#   3. Python construye el sobre EnvioDTE
#   4. Java firma el sobre (SetDTE)
#   5. Python envía al SII
#
# Java usa javax.xml.crypto.dsig — el mismo motor que el SII.
# ══════════════════════════════════════════════════════════════

import asyncio
import base64
import logging
import os
import re
import subprocess
import tempfile

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.serialization import pkcs12

logger = logging.getLogger("yepardtecore.firma")

# Ruta al .class compilado de FirmaDTE
# En Railway: /app/FirmaDTE.class (copiado por Dockerfile)
# En local:   directorio actual
_JAVA_CLASS_DIR = os.environ.get("FIRMA_JAVA_DIR", "/app")
_JAVA_CLASS_NAME = "FirmaDTE"


def _java_disponible() -> bool:
    try:
        r = subprocess.run(["java", "-version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _firmar_con_java(modo: str, xml_bytes: bytes, pfx_bytes: bytes,
                     password: str, doc_id: str = "") -> bytes:
    """
    Llama a FirmaDTE.java via subprocess.

    Modos:
      firmar-dte   — firma el nodo Documento (URI="#DTE-33-61")
      firmar-sobre — firma el nodo SetDTE   (URI="#SetDoc")

    Returns:
        bytes ISO-8859-1 del XML firmado
    """
    xml_b64 = base64.b64encode(xml_bytes).decode()
    pfx_b64 = base64.b64encode(pfx_bytes).decode()

    cmd = ["java", "-cp", _JAVA_CLASS_DIR, _JAVA_CLASS_NAME,
           modo, xml_b64, pfx_b64, password]
    if modo == "firmar-dte" and doc_id:
        cmd.append(doc_id)

    logger.debug(f"[FirmaJava] {modo} doc_id={doc_id}")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    if result.returncode != 0:
        raise RuntimeError(
            f"FirmaDTE.java [{modo}] error: {result.stderr[:300]}"
        )

    if not result.stdout:
        raise RuntimeError(f"FirmaDTE.java [{modo}]: sin output")

    return base64.b64decode(result.stdout)


class FirmaDigital:
    """
    Fachada de firma digital para DTEs del SII Chile.

    Usa Java (FirmaDTE.class) para XMLDSig y Python (firma_dte.py)
    para el timbre TED con la llave CAF.
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

        # FirmaDTE Python (solo para timbre TED)
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
        Paso 2-3 del flujo:
          a. Python inserta el TED (timbre con CAF)
          b. Java firma el Documento (XMLDSig)

        Returns: bytes ISO-8859-1 del DTE timbrado y firmado
        """
        # a. Timbre TED con Python (firma_dte.py existente)
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

        # b. Firma XMLDSig con Java
        doc_id = f"DTE-{tipo_dte}-{folio}"
        logger.info(f"[FirmaDigital] Firmando con Java {doc_id}")

        loop = asyncio.get_event_loop()
        xml_firmado = await loop.run_in_executor(
            None,
            lambda: _firmar_con_java(
                "firmar-dte", xml_timbrado, self._p12_bytes,
                self._password, doc_id
            )
        )

        return xml_firmado

    async def firmar_sobre(self, sobre_xml: str) -> str:
        """
        Firma todos los DTEs y el SetDTE dentro del árbol completo.
        Usa el modo firmar-sobre-completo de Java para que los DTEs
        se firmen en el contexto DOM correcto (sin xmlns standalone).

        Returns: EnvioDTE firmado (string ISO-8859-1)
        """
        logger.info("[FirmaDigital] Firmando sobre completo con Java (in-tree)")

        xml_bytes = sobre_xml.encode("ISO-8859-1")

        loop = asyncio.get_event_loop()
        xml_firmado = await loop.run_in_executor(
            None,
            lambda: _firmar_con_java(
                "firmar-sobre-completo", xml_bytes, self._p12_bytes, self._password
            )
        )

        result = xml_firmado.decode("ISO-8859-1")
        if not result.lstrip().startswith("<?xml"):
            result = '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + result

        return result

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
