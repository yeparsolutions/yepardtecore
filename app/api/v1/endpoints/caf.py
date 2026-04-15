# app/api/v1/endpoints/caf.py
# ══════════════════════════════════════════════════════════════
# Endpoints de gestión de CAF
#
#   POST /v1/caf/cargar          — Carga un CAF XML del SII
#   GET  /v1/caf/emisor/{id}     — Lista CAFs de un emisor
#   GET  /v1/caf/emisor/{id}/folios — Estado de folios disponibles
#   POST /v1/caf/validar         — Valida un CAF sin guardarlo
# ══════════════════════════════════════════════════════════════

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.db.base import get_db
from app.models.caf import CAF
from app.services.caf_service import CAFService

router = APIRouter(prefix="/caf", tags=["CAF — Códigos de Autorización de Folios"])


# ── Schemas ───────────────────────────────────────────────────

class CAFCargarInput(BaseModel):
    emisor_id: int
    xml_caf:   str
    ambiente:  str = "certificacion"

    class Config:
        json_schema_extra = {
            "example": {
                "emisor_id": 1,
                "xml_caf":   "<AUTORIZACION>...</AUTORIZACION>",
                "ambiente":  "certificacion"
            }
        }


# ── Endpoints ─────────────────────────────────────────────────

@router.post("/cargar", status_code=201)
async def cargar_caf(datos: CAFCargarInput, db: AsyncSession = Depends(get_db)):
    """
    Carga un CAF (Código de Autorización de Folios) del SII.

    El CAF es el XML que el SII entrega cuando solicitas
    autorización para emitir documentos. Debes tenerlo
    antes de poder emitir cualquier DTE.

    **Cómo obtener el CAF:**
    1. Ingresar al portal SII (www.sii.cl)
    2. Ir a: Factura Electrónica → Autorización de Folios
    3. Seleccionar tipo de documento y cantidad
    4. Descargar el XML del CAF
    5. Pegarlo en este endpoint
    """
    if datos.ambiente not in ("certificacion", "produccion"):
        raise HTTPException(status_code=400, detail="ambiente debe ser 'certificacion' o 'produccion'")

    service = CAFService(db)

    # Validar antes de guardar
    validacion = CAFService.validar_xml_caf(datos.xml_caf)
    if not validacion["valido"]:
        raise HTTPException(status_code=400, detail=f"CAF inválido: {validacion['error']}")

    try:
        caf = await service.cargar_caf(
            emisor_id = datos.emisor_id,
            xml_caf   = datos.xml_caf,
            ambiente  = datos.ambiente,
        )
        return {
            "id":           caf.id,
            "emisor_id":    caf.emisor_id,
            "tipo_dte":     caf.tipo_dte,
            "folio_desde":  caf.folio_desde,
            "folio_hasta":  caf.folio_hasta,
            "disponibles":  caf.folios_disponibles,
            "ambiente":     caf.ambiente,
            "mensaje":      f"CAF cargado correctamente — {caf.folios_disponibles} folios disponibles",
        }
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/cargar-archivo", status_code=201)
async def cargar_caf_archivo(
    emisor_id: int  = Form(...),
    ambiente:  str  = Form("certificacion"),
    archivo:   UploadFile = File(..., description="Archivo XML del CAF"),
    db: AsyncSession = Depends(get_db)
):
    """
    Carga un CAF desde un archivo XML subido directamente.
    Alternativa a /cargar para cuando el frontend sube el archivo.
    """
    if not archivo.filename.endswith(".xml"):
        raise HTTPException(status_code=400, detail="El archivo debe ser .xml")

    contenido = await archivo.read()
    try:
        xml_caf = contenido.decode("utf-8")
    except UnicodeDecodeError:
        try:
            xml_caf = contenido.decode("ISO-8859-1")
        except Exception:
            raise HTTPException(status_code=400, detail="No se pudo decodificar el XML")

    service    = CAFService(db)
    validacion = CAFService.validar_xml_caf(xml_caf)
    if not validacion["valido"]:
        raise HTTPException(status_code=400, detail=f"CAF inválido: {validacion['error']}")

    try:
        caf = await service.cargar_caf(emisor_id, xml_caf, ambiente)
        return {
            "id":          caf.id,
            "tipo_dte":    caf.tipo_dte,
            "folio_desde": caf.folio_desde,
            "folio_hasta": caf.folio_hasta,
            "disponibles": caf.folios_disponibles,
            "mensaje":     f"CAF cargado — {caf.folios_disponibles} folios para tipo {caf.tipo_dte}",
        }
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/validar")
async def validar_caf(datos: dict):
    """
    Valida un XML de CAF sin guardarlo en BD.
    Útil para mostrar preview antes de confirmar la carga.
    """
    xml_caf = datos.get("xml_caf", "")
    if not xml_caf:
        raise HTTPException(status_code=400, detail="Falta xml_caf")

    return CAFService.validar_xml_caf(xml_caf)


@router.get("/emisor/{emisor_id}")
async def listar_cafs(emisor_id: int, db: AsyncSession = Depends(get_db)):
    """Lista todos los CAFs de un emisor."""
    resultado = await db.execute(
        select(CAF).where(CAF.emisor_id == emisor_id).order_by(CAF.tipo_dte, CAF.folio_desde)
    )
    cafs = resultado.scalars().all()

    return {
        "emisor_id": emisor_id,
        "cafs": [
            {
                "id":            c.id,
                "tipo_dte":      c.tipo_dte,
                "tipo_nombre":   {33:"Factura",34:"Fact.Exenta",39:"Boleta",52:"Guía",56:"ND",61:"NC"}.get(c.tipo_dte),
                "folio_desde":   c.folio_desde,
                "folio_hasta":   c.folio_hasta,
                "folio_actual":  c.folio_actual,
                "disponibles":   c.folios_disponibles,
                "porcentaje":    c.porcentaje_uso,
                "activo":        c.activo,
                "agotado":       c.esta_agotado,
                "ambiente":      c.ambiente,
                "vencimiento":   str(c.fecha_vencimiento) if c.fecha_vencimiento else None,
            }
            for c in cafs
        ]
    }


@router.get("/emisor/{emisor_id}/folios")
async def folios_disponibles(
    emisor_id: int,
    ambiente: str = "certificacion",
    db: AsyncSession = Depends(get_db)
):
    """
    Resumen rápido de folios disponibles por tipo de DTE.
    Úsalo para mostrar en el dashboard cuántos folios quedan.
    """
    service = CAFService(db)
    estado  = await service.estado_folios(emisor_id, ambiente)

    # Agregar alertas
    alertas = [
        f"⚠️ Tipo {e['tipo_dte']}: solo {e['disponibles']} folios disponibles"
        for e in estado
        if 0 < e["disponibles"] <= 20
    ]
    sin_folios = [
        f"🚨 Tipo {e['tipo_dte']}: SIN FOLIOS — solicita CAF urgente"
        for e in estado
        if e["disponibles"] == 0
    ]

    return {
        "emisor_id": emisor_id,
        "ambiente":  ambiente,
        "resumen":   estado,
        "alertas":   sin_folios + alertas,
    }
