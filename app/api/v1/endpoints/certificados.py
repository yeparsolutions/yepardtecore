# app/api/v1/endpoints/certificados.py
# ══════════════════════════════════════════════════════════════
# Endpoints de gestión de certificados digitales - VERSIÓN CORREGIDA
# ══════════════════════════════════════════════════════════════

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.backends import default_backend
from datetime import datetime, timezone
from app.db.base import get_db
from app.models.emisor import Emisor
from app.models.certificado import Certificado  # <-- IMPORTANTE: Usar el modelo correcto
import re

router = APIRouter(prefix="/certificados", tags=["Certificados Digitales"])

def _extraer_rut_subject(subject_str: str) -> str:
    """Extrae el RUT del subject del certificado (campo OU)."""
    match = re.search(r"OU=(\d{7,8}-[\dkK])", subject_str, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    return ""

def _extraer_nombre_subject(subject_str: str) -> str:
    """Extrae el nombre común (CN) del certificado."""
    match = re.search(r"CN=([^,]+)", subject_str)
    if match:
        return match.group(1).strip()
    return "Titular Desconocido"

@router.post("/{emisor_id}/subir")
async def subir_certificado(
    emisor_id: int, 
    file: UploadFile = File(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Sube y valida el certificado digital (.p12 o .pfx).
    Guarda los datos en la tabla 'certificados'.
    """
    # 1. Verificar que el emisor existe
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(status_code=404, detail=f"Emisor con ID {emisor_id} no encontrado")

    try:
        # 2. Leer contenido del archivo
        p12_bytes = await file.read()
        
        # 3. Validar con cryptography
        try:
            private_key, certificate, additional_certificates = pkcs12.load_key_and_certificates(
                p12_bytes,
                password.encode(),
                default_backend()
            )
        except Exception:
            raise HTTPException(status_code=400, detail="Contraseña incorrecta o archivo PFX corrupto")

        # 4. Extraer metadatos
        subject_str = certificate.subject.rfc4514_string()
        rut_cert = _extraer_rut_subject(subject_str)
        nombre_cert = _extraer_nombre_subject(subject_str)
        
        # 5. Buscar si ya existe un registro en la tabla 'certificados' para este emisor
        stmt = select(Certificado).where(Certificado.emisor_id == emisor_id)
        result = await db.execute(stmt)
        cert_db = result.scalar_one_or_none()

        if not cert_db:
            # Crear nuevo registro si no existe
            cert_db = Certificado(emisor_id=emisor_id)
            db.add(cert_db)

        # 6. Actualizar campos (según la estructura de tu tabla Postgres)
        cert_db.certificado_p12 = p12_bytes
        cert_db.certificado_password = password
        cert_db.rut_firmante = rut_cert
        cert_db.nombre_firmante = nombre_cert
        cert_db.fecha_emision = certificate.not_valid_before_utc
        cert_db.fecha_vencimiento = certificate.not_valid_after_utc
        cert_db.activo = True

        await db.commit()
        
        return {
            "status": "success",
            "message": "Certificado cargado y vinculado exitosamente",
            "titular": nombre_cert,
            "rut": rut_cert,
            "vence": certificate.not_valid_after_utc.strftime("%Y-%m-%d")
        }

    except HTTPException as he:
        raise he
    except Exception as e:
        await db.rollback()
        print(f"DEBUG ERROR: {str(e)}") # Esto saldrá en tu terminal uvicorn
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")

@router.get("/{emisor_id}/info")
async def obtener_info_certificado(emisor_id: int, db: AsyncSession = Depends(get_db)):
    """Obtiene la información pública del certificado cargado."""
    stmt = select(Certificado).where(Certificado.emisor_id == emisor_id)
    result = await db.execute(stmt)
    cert = result.scalar_one_or_none()
    
    if not cert or not cert.certificado_p12:
        return {"tiene_certificado": False}

    ahora = datetime.now(timezone.utc)
    # Asegurarnos que la fecha de vencimiento tenga timezone
    vence = cert.fecha_vencimiento.replace(tzinfo=timezone.utc) if cert.fecha_vencimiento.tzinfo is None else cert.fecha_vencimiento
    dias_restantes = (vence - ahora).days

    return {
        "tiene_certificado": True,
        "titular": cert.nombre_firmante,
        "rut": cert.rut_firmante,
        "fecha_vencimiento": cert.fecha_vencimiento.strftime("%Y-%m-%d"),
        "dias_restantes": dias_restantes,
        "vigente": dias_restantes > 0
    }
