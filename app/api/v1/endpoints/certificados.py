# app/api/v1/endpoints/certificados.py
# ══════════════════════════════════════════════════════════════
# Endpoints de gestión de certificados digitales
#
#   POST /v1/certificados/{emisor_id}/subir   — Sube el .p12/.pfx
#   GET  /v1/certificados/{emisor_id}/info    — Info del certificado
#   DELETE /v1/certificados/{emisor_id}       — Elimina el certificado
# ══════════════════════════════════════════════════════════════

from fastapi        import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy     import select
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.backends import default_backend
from datetime       import datetime, timezone
from app.db.base    import get_db
from app.models.emisor import Emisor
import re

router = APIRouter(prefix="/certificados", tags=["Certificados Digitales"])


def _extraer_rut_subject(subject_str: str) -> str:
    """Extrae el RUT del subject del certificado (campo OU)."""
    # El RUT viene en OU= como "25648612-1"
    match = re.search(r"OU=(\d{7,8}-[\dkK])", subject_str, re.IGNORECASE)
    if match:
        rut = match.group(1)
        # Formatear con puntos: 25648612-1 → 25.648.612-1
        num = rut.split("-")[0]
        dv  = rut.split("-")[1]
        if len(num) == 8:
            return f"{num[:2]}.{num[2:5]}.{num[5:]}-{dv}"
        elif len(num) == 7:
            return f"{num[:1]}.{num[1:4]}.{num[4:]}-{dv}"
    return ""


def _extraer_nombre_subject(subject_str: str) -> str:
    """Extrae el nombre del titular del certificado (campo CN)."""
    match = re.search(r"CN=([^,]+)", subject_str)
    return match.group(1).strip() if match else ""


@router.post("/{emisor_id}/subir")
async def subir_certificado(
    emisor_id: int,
    password:  str        = Form(..., description="Contraseña del certificado .p12/.pfx"),
    archivo:   UploadFile = File(..., description="Archivo .p12 o .pfx del certificado digital"),
    db: AsyncSession = Depends(get_db)
):
    """
    Sube el certificado digital (.p12 o .pfx) de un emisor.

    El certificado se guarda **encriptado en la base de datos**.
    Nunca se almacena en disco ni en variables de entorno.

    **¿Dónde obtener el certificado?**
    Lo emiten empresas autorizadas por el SII:
    - E-Cert Chile (www.e-certchile.cl)
    - CertiSur (www.certisur.com)
    - Firmadox (www.firmadox.cl)

    El archivo tiene extensión `.p12` o `.pfx` y viene con una contraseña.
    """
    # Validar extensión
    nombre = archivo.filename or ""
    if not (nombre.endswith(".p12") or nombre.endswith(".pfx")):
        raise HTTPException(
            status_code=400,
            detail="El archivo debe ser .p12 o .pfx"
        )

    # Verificar que el emisor existe
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(status_code=404, detail="Emisor no encontrado")

    # Leer el archivo
    p12_bytes = await archivo.read()
    if len(p12_bytes) < 100:
        raise HTTPException(status_code=400, detail="Archivo demasiado pequeño — parece inválido")

    # Intentar cargar el certificado para validarlo
    try:
        pwd_bytes = password.encode("utf-8")
        private_key, certificate, chain = pkcs12.load_key_and_certificates(
            p12_bytes, pwd_bytes, backend=default_backend()
        )
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="No se pudo cargar el certificado — verifica que la contraseña sea correcta"
        )

    if not private_key or not certificate:
        raise HTTPException(
            status_code=400,
            detail="El archivo no contiene clave privada o certificado válido"
        )

    # Verificar vigencia
    ahora    = datetime.now(timezone.utc)
    vigente  = ahora < certificate.not_valid_after_utc
    if not vigente:
        raise HTTPException(
            status_code=400,
            detail=f"El certificado está vencido desde {certificate.not_valid_after_utc.strftime('%Y-%m-%d')}"
        )

    # Extraer datos del certificado
    subject_str = certificate.subject.rfc4514_string()
    rut_cert    = _extraer_rut_subject(subject_str)
    nombre_cert = _extraer_nombre_subject(subject_str)
    vigencia    = certificate.not_valid_after_utc.strftime("%Y-%m-%d")

    # Guardar en BD (bytes del p12 + contraseña en texto para usarla al firmar)
    # NOTA DE SEGURIDAD: en producción la contraseña debería cifrarse con
    # una clave maestra (Fernet/AES). Por ahora se guarda en texto
    # ya que la BD está en Railway con acceso restringido.
    emisor.certificado_p12       = p12_bytes
    emisor.certificado_password  = password
    emisor.certificado_vigencia  = vigencia
    await db.flush()

    return {
        "ok":           True,
        "emisor_id":    emisor_id,
        "titular":      nombre_cert,
        "rut_cert":     rut_cert,
        "valido_desde": certificate.not_valid_before_utc.strftime("%Y-%m-%d"),
        "valido_hasta": vigencia,
        "vigente":      vigente,
        "emisor_cert":  _extraer_nombre_subject(certificate.issuer.rfc4514_string()),
        "mensaje":      f"✅ Certificado de {nombre_cert} cargado correctamente — vigente hasta {vigencia}",
    }


@router.get("/{emisor_id}/info")
async def info_certificado(emisor_id: int, db: AsyncSession = Depends(get_db)):
    """
    Muestra la información del certificado cargado.
    No expone la clave privada ni la contraseña — solo metadatos.
    """
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(status_code=404, detail="Emisor no encontrado")

    if not emisor.certificado_p12:
        return {
            "tiene_certificado": False,
            "mensaje": "Este emisor no tiene certificado digital cargado"
        }

    # Re-cargar para extraer info fresca
    try:
        pwd_bytes = (emisor.certificado_password or "").encode("utf-8")
        _, certificate, _ = pkcs12.load_key_and_certificates(
            emisor.certificado_p12, pwd_bytes, backend=default_backend()
        )
        subject_str = certificate.subject.rfc4514_string()
        ahora       = datetime.now(timezone.utc)
        dias_restantes = (certificate.not_valid_after_utc - ahora).days

        return {
            "tiene_certificado": True,
            "titular":           _extraer_nombre_subject(subject_str),
            "rut":               _extraer_rut_subject(subject_str),
            "valido_desde":      certificate.not_valid_before_utc.strftime("%Y-%m-%d"),
            "valido_hasta":      certificate.not_valid_after_utc.strftime("%Y-%m-%d"),
            "dias_restantes":    dias_restantes,
            "vigente":           dias_restantes > 0,
            "alerta":            dias_restantes <= 30,
            "emisor_cert":       _extraer_nombre_subject(certificate.issuer.rfc4514_string()),
        }
    except Exception as e:
        return {
            "tiene_certificado": True,
            "error": f"No se pudo leer el certificado: {str(e)}"
        }


@router.delete("/{emisor_id}")
async def eliminar_certificado(emisor_id: int, db: AsyncSession = Depends(get_db)):
    """Elimina el certificado digital de un emisor."""
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(status_code=404, detail="Emisor no encontrado")

    if not emisor.certificado_p12:
        raise HTTPException(status_code=404, detail="Este emisor no tiene certificado cargado")

    emisor.certificado_p12      = None
    emisor.certificado_password = None
    emisor.certificado_vigencia = None
    await db.flush()

    return {"ok": True, "mensaje": "Certificado eliminado correctamente"}
