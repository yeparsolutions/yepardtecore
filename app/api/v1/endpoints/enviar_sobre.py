# app/api/v1/endpoints/enviar_sobre.py
# Endpoint para envío directo de XML pre-firmado al SII

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional

from app.db.base import get_db
from app.models.emisor import Emisor
from app.models.certificado import Certificado
from app.services.sii_sender import SIISender
from app.services.firma_digital import FirmaDigital

router = APIRouter(prefix="/enviar-sobre", tags=["Enviar Sobre"])


class EnviarSobreRequest(BaseModel):
    emisor_id: int
    xml_sobre: str | None = None
    xml_sobre_b64: str | None = None
    sobre_id: str | None = None
    # Modo stateless — el cliente manda el certificado directamente
    pfx_base64: Optional[str] = None
    pfx_password: Optional[str] = None
    rut_emisor: Optional[str] = None
    rut_enviador: Optional[str] = None
    ambiente: Optional[str] = None
    nro_resolucion: Optional[str] = None
    fch_resolucion: Optional[str] = None


@router.post("/directo")
async def enviar_sobre_directo(
    body: EnviarSobreRequest,
    db:   AsyncSession = Depends(get_db),
):
    """Recibe un sobre XML firmado y lo envía directamente al SII.
    
    Modo stateful: usa emisor_id para buscar datos en BD.
    Modo stateless: recibe pfx_base64 + pfx_password + rut_emisor directamente.
    """
    import base64

    # ── Modo stateless: certificado viene en el request ───────────────────────
    if body.pfx_base64 and body.pfx_password:
        p12_bytes = base64.b64decode(body.pfx_base64)
        password  = body.pfx_password
        firma     = FirmaDigital(p12_bytes=p12_bytes, password=password)
        rut_env   = body.rut_enviador or firma.rut_certificado or body.rut_emisor or ""
        ambiente  = body.ambiente or "certificacion"
        nro_resol = body.nro_resolucion or "0"
        fch_resol = body.fch_resolucion or "2026-04-19"
        rut_em    = body.rut_emisor or ""

        sender = SIISender(
            ambiente  = ambiente,
            fch_resol = fch_resol,
            nro_resol = nro_resol,
        )

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
            resultado = await sender.enviar_sobre(
                sobre_xml      = sobre_xml_final,
                rut_emisor     = rut_em,
                rut_enviador   = rut_env,
                p12_bytes      = p12_bytes,
                password       = password,
                auth_p12_bytes = None,
                auth_password  = None,
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
                "ambiente": ambiente,
            }

        if sobre_id_usado and resultado.get("track_id"):
            from app.services import sobre_store
            sobre_store.descartar(sobre_id_usado)

        return {
            "ok":       resultado.get("track_id") is not None or resultado.get("estado") == "RECIBIDO",
            "track_id": resultado.get("track_id"),
            "estado":   resultado.get("estado"),
            "mensaje":  resultado.get("mensaje"),
            "ambiente": ambiente,
        }

    # ── Modo stateful: busca datos en BD por emisor_id ────────────────────────
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
        raise HTTPException(404, "El emisor no tiene certificado activo registrado")

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

    es_boleta = "EnvioBOLETA" in sobre_xml_final[:500]
    if es_boleta and auth_p12:
        try:
            from app.services.sii_auth import _token_cache_boleta
            ambiente_actual = emisor.ambiente or "certificacion"
            cache_key = f"boleta_{ambiente_actual}_{hash(auth_p12)}"
            cached = _token_cache_boleta.get(cache_key)
            if cached:
                cert.token_boleta        = cached["token"]
                cert.token_boleta_expira = cached["expira"].isoformat()
                await db.commit()
        except Exception:
            pass

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
