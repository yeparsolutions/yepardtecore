# app/api/v1/endpoints/certificados.py
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.backends import default_backend
from datetime import datetime, timezone
from app.db.base import get_db
from app.models.emisor import Emisor
from app.models.certificado import Certificado # <-- Asegúrate que este modelo exista
import re

router = APIRouter(prefix="/certificados", tags=["Certificados Digitales"])

def _extraer_rut_subject(subject_str: str) -> str:
    match = re.search(r"OU=(\d{7,8}-[\dkK])", subject_str, re.IGNORECASE)
    return match.group(1).upper() if match else ""

def _extraer_nombre_subject(subject_str: str) -> str:
    match = re.search(r"CN=([^,]+)", subject_str)
    return match.group(1).strip() if match else ""

@router.post("/{emisor_id}/subir")
async def subir_certificado(
    emisor_id: int,
    password: str = Form(...),
    archivo: UploadFile = File(...),
    db: AsyncSession = Depends(get_db)
):
    # 1. Validar emisor
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(status_code=404, detail="Emisor no encontrado")

    # 2. Leer y validar PFX
    p12_bytes = await archivo.read()
    try:
        private_key, certificate, _ = pkcs12.load_key_and_certificates(
            p12_bytes, password.encode(), backend=default_backend()
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Contraseña incorrecta o archivo inválido")

    # 3. Extraer info
    subject_str = certificate.subject.rfc4514_string()
    rut_cert = _extraer_rut_subject(subject_str)
    nombre_cert = _extraer_nombre_subject(subject_str)

    # 4. GUARDAR EN TABLA 'certificados' (No en 'emisores')
    stmt = select(Certificado).where(Certificado.emisor_id == emisor_id)
    result = await db.execute(stmt)
    cert_db = result.scalar_one_or_none()

    if not cert_db:
        cert_db = Certificado(emisor_id=emisor_id)
        db.add(cert_db)

    # Ajuste de nombres según tu 'psql \d certificados'
    cert_db.certificado_p12 = p12_bytes
    cert_db.certificado_password = password
    cert_db.rut_firmante = rut_cert
    cert_db.nombre_firmante = nombre_cert
    cert_db.fecha_emision = certificate.not_valid_before_utc
    cert_db.fecha_vencimiento = certificate.not_valid_after_utc
    cert_db.activo = True

    await db.commit()

    return {
        "ok": True,
        "mensaje": f"✅ Certificado de {nombre_cert} cargado correctamente",
        "vence": cert_db.fecha_vencimiento.strftime("%Y-%m-%d")
    }
