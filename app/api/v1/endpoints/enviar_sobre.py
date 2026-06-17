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
    sobre_id: str | None = None        # Ticket del guardarropa — PREFERIDO:
                                       # usa los bytes originales firmados,
                                       # sin viaje de ida y vuelta por JSON


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
    nro_resol, fch_resol = emisor.get_resolucion(emisor.ambiente or "certificacion")
    sender = SIISender(
        ambiente  = emisor.ambiente or "certificacion",
        fch_resol = fch_resol,
        nro_resol = nro_resol,
    )

    # Decodificar XML — orden de preferencia:
    #   1. sobre_id (ticket del guardarropa: bytes originales, cero riesgo)
    #   2. base64 (preserva ISO-8859-1 pero hizo viaje por JSON)
    #   3. string plano (legacy)
    import base64
    sobre_id_usado = None
    if body.sobre_id:
        from app.services import sobre_store
        sobre_bytes = sobre_store.obtener(body.sobre_id)
        if sobre_bytes is None:
            raise HTTPException(
                410,
                "El sobre temporal ya no está disponible (expiró o el "
                "servicio se reinició). Genera el set nuevamente."
            )
        sobre_xml_final = sobre_bytes.decode("ISO-8859-1")
        sobre_id_usado  = body.sobre_id
    elif body.xml_sobre_b64:
        sobre_xml_final = base64.b64decode(body.xml_sobre_b64).decode("ISO-8859-1")
    else:
        sobre_xml_final = body.xml_sobre or ""

    try:
        # Usar certificado de autenticación (auth_p12) si existe,
        # sino usar el certificado de firma — el SII necesita auth para el token
        auth_p12 = bytes(cert.certificado_auth_p12) if cert.certificado_auth_p12 else None
        auth_pwd = cert.certificado_auth_password if cert.certificado_auth_p12 else None

        resultado = await sender.enviar_sobre(
            sobre_xml      = sobre_xml_final,
            rut_emisor     = emisor.rut,
            rut_enviador   = rut_enviador,
            p12_bytes      = bytes(cert.certificado_p12),
            password       = cert.certificado_password,
            auth_p12_bytes = auth_p12,
            auth_password  = auth_pwd,
            # Para boletas: permite obtener el token desde api.sii.cl y
            # persistirlo en BD (cert.token_boleta) para reutilizarlo.
            db             = db,
            emisor_id      = body.emisor_id,
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

    # Si es boleta y el envío fue exitoso, persistir el token en BD
    # para que el endpoint stateless pueda reutilizarlo (maullin2 no es
    # accesible desde servidores fuera de Chile como Railway US West)
    es_boleta = "EnvioBOLETA" in sobre_xml_final[:500]
    if es_boleta and auth_p12:
        try:
            from app.services.sii_auth import _token_cache_boleta
            ambiente_actual = emisor.ambiente or "certificacion"
            cache_key = f"boleta_{ambiente_actual}_{hash(auth_p12)}"
            cached = _token_cache_boleta.get(cache_key)
            if cached:
                from datetime import timezone
                cert.token_boleta        = cached["token"]
                cert.token_boleta_expira = cached["expira"].isoformat()
                await db.commit()
                import logging
                logging.getLogger("yepardtecore.enviar_sobre").info(
                    f"[ENVIAR-SOBRE] Token boleta persistido en BD — expira: {cert.token_boleta_expira}"
                )
        except Exception as ex:
            import logging
            logging.getLogger("yepardtecore.enviar_sobre").warning(
                f"[ENVIAR-SOBRE] No se pudo persistir token boleta: {ex}"
            )

    # Envío exitoso → retirar el sobre del guardarropa.
    # Si falló, se CONSERVA: el mismo ticket sirve para reintentar
    # sin regenerar (y sin quemar folios).
    if sobre_id_usado and resultado.get("track_id"):
        from app.services import sobre_store
        sobre_store.descartar(sobre_id_usado)

    return {
        "ok":       resultado.get("track_id") is not None or resultado.get("estado") == "RECIBIDO",
        "track_id": resultado.get("track_id"),
        "estado":   resultado.get("estado"),
        "mensaje":  resultado.get("mensaje"),
        "ambiente": emisor.ambiente or "certificacion",
    }
