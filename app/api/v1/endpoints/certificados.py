# app/api/v1/endpoints/certificados.py
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.backends import default_backend
from datetime import datetime, timezone
from app.db.base import get_db
from app.models.emisor import Emisor
from app.models.certificado import Certificado  # <-- Importar el modelo correcto
import re

router = APIRouter(prefix="/certificados", tags=["Certificados Digitales"])

def _extraer_rut_subject(subject_str: str) -> str:
    """Extrae el RUT del subject del certificado."""
    match = re.search(r"OU=(\d{7,8}-[\dkK])", subject_str, re.IGNORECASE)
    return match.group(1).upper() if match else ""

def _extraer_nombre_subject(subject_str: str) -> str:
    """Extrae el nombre del titular."""
    match = re.search(r"CN=([^,]+)", subject_str)
    return match.group(1).strip() if match else "Titular Desconocido"

@router.post("/{emisor_id}/subir")
async def subir_certificado(
    emisor_id: int,
    password: str = Form(...),
    archivo: UploadFile = File(...),
    db: AsyncSession = Depends(get_db)
):
    # 1. Verificar que el emisor existe
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(status_code=404, detail="Emisor no encontrado")

    # 2. Leer archivo y validar con cryptography
    p12_bytes = await archivo.read()
    try:
        private_key, certificate, _ = pkcs12.load_key_and_certificates(
            p12_bytes, password.encode(), backend=default_backend()
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Contraseña incorrecta o archivo PFX inválido")

    # 3. Extraer metadatos
    subject_str = certificate.subject.rfc4514_string()
    rut_cert = _extraer_rut_subject(subject_str)
    nombre_cert = _extraer_nombre_subject(subject_str)

    # 4. Buscar o crear registro en la tabla 'certificados'
    stmt = select(Certificado).where(Certificado.emisor_id == emisor_id)
    result = await db.execute(stmt)
    cert_db = result.scalar_one_or_none()

    if not cert_db:
        cert_db = Certificado(emisor_id=emisor_id)
        db.add(cert_db)

    # 5. Mapeo de columnas según tu base de datos Postgres
    cert_db.certificado_p12 = p12_bytes
    cert_db.certificado_password = password
    cert_db.rut_firmante = rut_cert
    cert_db.nombre_firmante = nombre_cert
    cert_db.fecha_emision = certificate.not_valid_before_utc
    cert_db.fecha_vencimiento = certificate.not_valid_after_utc
    cert_db.activo = True

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        print(f"DEBUG ERROR DB: {str(e)}")
        raise HTTPException(status_code=500, detail="Error al guardar en base de datos")

    return {
        "ok": True,
        "mensaje": f"✅ Certificado de {nombre_cert} vinculado exitosamente",
        "rut": rut_cert,
        "vence": cert_db.fecha_vencimiento.strftime("%Y-%m-%d")
    }

@router.get("/{emisor_id}/info")
async def info_certificado(emisor_id: int, db: AsyncSession = Depends(get_db)):
    stmt = select(Certificado).where(Certificado.emisor_id == emisor_id)
    result = await db.execute(stmt)
    cert = result.scalar_one_or_none()
    
    if not cert:
        return {"tiene_certificado": False}

    return {
        "tiene_certificado": True,
        "titular": cert.nombre_firmante,
        "rut": cert.rut_firmante,
        "vence": cert.fecha_vencimiento.strftime("%Y-%m-%d")
    }
