# app/api/v1/endpoints/enviar_sobre.py
# Endpoint para envío directo de XML pre-firmado al SII

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.base import get_db
from app.models.emisor import Emisor
from app.models.certificado import Certificado
from app.services.sii_sender import SIISender
from app.services.firma_digital import FirmaDigital

router = APIRouter(prefix="/enviar-sobre", tags=["Enviar Sobre"])


class EnviarSobreRequest(BaseModel):
    emisor_id: int
    xml_sobre: str | None = None       # XML como string (legacy)
    xml_sobre_b64: str | None = None   # XML en base64 (preserva ISO-8859-1)


@router.post("/directo")
async def enviar_sobre_directo(
    body: EnviarSobreRequest,
    db:   AsyncSession = Depends(get_db),
):
    """Recibe un sobre XML firmado y lo envía directamente al SII."""
    emisor = (await db.execute(
        select(Emisor).where(Emisor.id == body.emisor_id)
    )).scalar_one_or_none()
    if not emisor:
        raise HTTPException(404, "Emisor no encontrado")

    cert = (await db.execute(
        select(Certificado).where(
            Certificado.emisor_id == body.emisor_id,
            Certificado.activo == True,
        ).limit(1)
    )).scalar_one_or_none()
    if not cert:
        raise HTTPException(404, "Certificado no encontrado")

    firma = FirmaDigital(
        p12_bytes=bytes(cert.certificado_p12),
        password=cert.certificado_password,
    )
    rut_enviador = cert.rut_firmante or firma.rut_certificado or emisor.rut
    sender = SIISender(ambiente=emisor.ambiente or "certificacion")

    # Decodificar XML: preferir base64 (preserva ISO-8859-1), fallback a string
    import base64
    if body.xml_sobre_b64:
        sobre_xml_final = base64.b64decode(body.xml_sobre_b64).decode("ISO-8859-1")
    else:
        sobre_xml_final = body.xml_sobre or ""

    try:
        resultado = await sender.enviar_sobre(
            sobre_xml=sobre_xml_final,
            rut_emisor=emisor.rut,
            rut_enviador=rut_enviador,
            p12_bytes=bytes(cert.certificado_p12),
            password=cert.certificado_password,
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "ok":       False,
            "track_id": None,
            "estado":   "ERROR",
            "mensaje":  str(e),
            "ambiente": emisor.ambiente or "certificacion",
        }

    return {
        "ok":       resultado.get("track_id") is not None or resultado.get("estado") == "RECIBIDO",
        "track_id": resultado.get("track_id"),
        "estado":   resultado.get("estado"),
        "mensaje":  resultado.get("mensaje"),
        "ambiente": emisor.ambiente or "certificacion",
    }
