# app/api/v1/endpoints/dte.py
# ══════════════════════════════════════════════════════════════
# Endpoints de emisión DTE
#
#   POST /v1/dte/emitir          — Emite un DTE (boleta, factura, etc.)
#   GET  /v1/dte/{id}            — Obtiene un DTE por ID
#   GET  /v1/dte/{id}/xml        — Descarga el XML firmado
#   GET  /v1/dte/{id}/estado     — Consulta estado en el SII
#   POST /v1/dte/{id}/reenviar   — Reenvía al SII
#   GET  /v1/dte/emisor/{id}     — Lista DTEs de un emisor
# ══════════════════════════════════════════════════════════════

import logging
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field
from typing import Optional
from datetime import date

from app.db.base import get_db
from app.models.dte import DTE
from app.models.emisor import Emisor
from app.models.certificado import Certificado
from app.services.dte_service import DTEService
from app.services.sii_sender import SIISender
from app.services.firma_digital import FirmaDigital
from app.core.security import validar_api_key

logger = logging.getLogger("yepardtecore.endpoints.dte")

router = APIRouter(prefix="/dte", tags=["DTE — Documentos Tributarios"])


# ── Schemas ───────────────────────────────────────────────────

class ItemInput(BaseModel):
    nombre:          str
    cantidad:        float = 1.0
    precio_unitario: float
    descuento_pct:   float = 0.0
    codigo:          str   = ""
    unidad:          str   = "UN"
    exento:          bool  = False


class ReceptorInput(BaseModel):
    rut:          str = "66.666.666-6"
    razon_social: str = "Consumidor Final"
    giro:         str = ""
    direccion:    str = ""
    comuna:       str = ""
    ciudad:       str = ""
    correo:       str = ""


class ReferenciaInput(BaseModel):
    tipo_doc_ref: int
    folio_ref:    int
    fecha_ref:    str  # "YYYY-MM-DD"
    razon_ref:    str = ""
    cod_ref:      int = 0


class EmitirDTEInput(BaseModel):
    emisor_id:          int
    tipo_dte:           int   = Field(..., description="33=Factura, 34=FactExenta, 39=Boleta, 52=Guía, 56=ND, 61=NC")
    receptor:           ReceptorInput = ReceptorInput()
    items:              list[ItemInput]
    fecha_emision:      str   = Field(default_factory=lambda: date.today().isoformat())
    forma_pago:         int   = Field(1, description="1=Contado, 2=Crédito, 3=Sin Costo")
    referencias:        list[ReferenciaInput] = []
    referencia_interna: Optional[str] = None
    idempotency_key:      Optional[str] = Field(
        None,
        description="Clave única para evitar DTEs duplicados. "
                    "Si ya existe un DTE con esta clave, retorna el existente. "
                    "Ejemplo: 'orden_123', 'venta_2026-04-11_001'"
    )
    descuento_global_pct: float = Field(
        0.0,
        description="Descuento global sobre items afectos en porcentaje. "
                    "Se aplica después de descuentos por línea. "
                    "Ejemplo: 14.0 para 14% de descuento global."
    )
    observacion:          str  = ""
    auto_enviar:          bool = True

    class Config:
        json_schema_extra = {
            "example": {
                "emisor_id": 1,
                "tipo_dte": 39,
                "receptor": {
                    "rut": "66.666.666-6",
                    "razon_social": "Sin Nombre"
                },
                "items": [
                    {
                        "nombre": "Producto de prueba",
                        "cantidad": 2,
                        "precio_unitario": 5000
                    }
                ],
                "idempotency_key": "orden_123",
                "forma_pago": 1,
                "auto_enviar": True
            }
        }


class DTERespuesta(BaseModel):
    id:              int
    tipo_dte:        int
    folio:           int
    folio_fmt:       str
    monto_total:     float
    monto_neto:      float
    monto_iva:       float
    estado:          str
    track_id:        Optional[str]
    ambiente:        str
    rut_receptor:    Optional[str]
    nombre_receptor: Optional[str]

    class Config:
        from_attributes = True


# ── Endpoints ─────────────────────────────────────────────────

@router.post("/emitir", status_code=201)
async def emitir_dte(
    datos: EmitirDTEInput,
    db: AsyncSession = Depends(get_db),
    emisor_auth: object = Depends(validar_api_key),
):
    """
    Emite un Documento Tributario Electrónico.

    **Idempotencia:** usa `idempotency_key` para evitar duplicados.
    Si mandas la misma clave dos veces, retorna el DTE original.

    **Tipos DTE soportados:**
    - `33` Factura Electrónica (requiere RUT receptor)
    - `34` Factura No Afecta/Exenta
    - `39` Boleta Electrónica (consumidor final)
    - `52` Guía de Despacho
    - `56` Nota de Débito (requiere referencia)
    - `61` Nota de Crédito (requiere referencia)

    **Estados del DTE:**
    - `BORRADOR` → generado pero no enviado (auto_enviar=false)
    - `PENDIENTE_ENVIO` → en cola
    - `EN_PROCESO` → enviado, esperando SII
    - `ENVIADO` → SII recibió el sobre (tiene TrackID)
    - `ACEPTADO` → SII aceptó sin problemas
    - `ACEPTADO_CON_REPAROS` → válido pero con observaciones
    - `RECHAZADO` → SII rechazó
    - `ERROR_ENVIO` → error de red al enviar
    """
    # Validar tipo DTE
    tipos_validos = {33, 34, 39, 41, 52, 56, 61}
    if datos.tipo_dte not in tipos_validos:
        raise HTTPException(
            status_code=400,
            detail=f"tipo_dte {datos.tipo_dte} inválido. Válidos: {sorted(tipos_validos)}",
        )

    # Factura exige RUT receptor
    if datos.tipo_dte == 33 and datos.receptor.rut == "66.666.666-6":
        raise HTTPException(
            status_code=400,
            detail="Factura Electrónica (tipo 33) requiere RUT del receptor",
        )

    # NC y ND exigen referencia
    if datos.tipo_dte in (56, 61) and not datos.referencias:
        raise HTTPException(
            status_code=400,
            detail="Nota de Crédito/Débito requiere referencia al documento original",
        )

    logger.info(
        f"[ENDPOINT] Emitir DTE — emisor_id={datos.emisor_id} "
        f"tipo={datos.tipo_dte} idem_key={datos.idempotency_key}"
    )

    try:
        service   = DTEService(db)
        resultado = await service.emitir(
            emisor_id   = datos.emisor_id,
            datos       = datos.model_dump(),
            auto_enviar = datos.auto_enviar,
        )
        return resultado

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[ENDPOINT] Error emitiendo DTE: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")


@router.get("/{dte_id}", response_model=DTERespuesta)
async def obtener_dte(dte_id: int, db: AsyncSession = Depends(get_db)):
    """Obtiene los datos de un DTE por ID."""
    resultado = await db.execute(select(DTE).where(DTE.id == dte_id))
    dte = resultado.scalar_one_or_none()
    if not dte:
        raise HTTPException(status_code=404, detail="DTE no encontrado")
    return dte


@router.get("/{dte_id}/xml")
async def descargar_xml(dte_id: int, db: AsyncSession = Depends(get_db)):
    """Descarga el XML firmado del DTE."""
    resultado = await db.execute(select(DTE).where(DTE.id == dte_id))
    dte = resultado.scalar_one_or_none()
    if not dte:
        raise HTTPException(status_code=404, detail="DTE no encontrado")
    if not dte.xml_firmado:
        raise HTTPException(status_code=404, detail="DTE sin XML firmado")

    nombre_archivo = f"DTE_{dte.tipo_dte}_{dte.folio}.xml"
    return Response(
        content=dte.xml_firmado.encode("ISO-8859-1"),
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{nombre_archivo}"'},
    )


@router.get("/{dte_id}/estado")
async def estado_sii(dte_id: int, db: AsyncSession = Depends(get_db)):
    """
    Consulta el estado del DTE en el SII por TrackID.
    Llama periódicamente hasta obtener ACEPTADO o RECHAZADO.
    """
    try:
        service = DTEService(db)
        return await service.consultar_estado_sii(dte_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{dte_id}/reenviar")
async def reenviar_dte(dte_id: int, db: AsyncSession = Depends(get_db)):
    """
    Reenvía un DTE al SII.
    Útil cuando el primer envío falló por timeout o error de red.
    No genera un nuevo DTE — reutiliza el XML ya firmado.
    """
    try:
        service = DTEService(db)
        return await service.reenviar(dte_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/emisor/{emisor_id}")
async def listar_dtes_emisor(
    emisor_id: int,
    tipo_dte:  Optional[int] = None,
    estado:    Optional[str] = None,
    limite:    int = 50,
    db: AsyncSession = Depends(get_db),
):
    """
    Lista los DTEs emitidos por un emisor.
    Filtros opcionales: tipo_dte, estado, limite.
    """
    query = select(DTE).where(DTE.emisor_id == emisor_id)

    if tipo_dte:
        query = query.where(DTE.tipo_dte == tipo_dte)
    if estado:
        query = query.where(DTE.estado == estado.upper())

    query = query.order_by(DTE.created_at.desc()).limit(min(limite, 200))

    resultado = await db.execute(query)
    dtes = resultado.scalars().all()

    return {
        "emisor_id": emisor_id,
        "total":     len(dtes),
        "documentos": [
            {
                "id":           d.id,
                "tipo_dte":     d.tipo_dte,
                "folio":        d.folio,
                "folio_fmt":    d.folio_fmt,
                "receptor":     d.nombre_receptor,
                "monto_total":  d.monto_total,
                "estado":       d.estado,
                "track_id":     d.track_id,
                "fecha":        str(d.created_at)[:10] if d.created_at else None,
            }
            for d in dtes
        ],
    }


@router.get("/{dte_id}/pdf")
async def descargar_pdf(
    dte_id: int,
    formato: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Genera y descarga el PDF del DTE.
    Formatos: a4, carta, ticket80, ticket58
    """
    from app.services.pdf_service import generar_pdf_dte
    from app.models.emisor import Emisor

    resultado = await db.execute(select(DTE).where(DTE.id == dte_id))
    dte = resultado.scalar_one_or_none()
    if not dte:
        raise HTTPException(status_code=404, detail="DTE no encontrado")

    emisor = await db.get(Emisor, dte.emisor_id)
    if not emisor:
        raise HTTPException(status_code=404, detail="Emisor no encontrado")

    dte_data = {
        "tipo_dte":        dte.tipo_dte,
        "folio":           dte.folio,
        "fecha_emision":   str(dte.created_at)[:10] if dte.created_at else "",
        "rut_receptor":    dte.rut_receptor,
        "nombre_receptor": dte.nombre_receptor,
        "monto_neto":      float(dte.monto_neto or 0),
        "monto_iva":       float(dte.monto_iva or 0),
        "monto_total":     float(dte.monto_total or 0),
        "items_json": [
            {
                "nombre":          i.nombre,
                "cantidad":        float(i.cantidad or 1),
                "precio_unitario": float(i.precio_unitario or 0),
                "monto_item":      float(i.monto_item or 0),
            }
            for i in (dte.items or [])
        ],
    }

    emisor_data = {
        "rut":          emisor.rut,
        "razon_social": emisor.razon_social,
        "giro":         emisor.giro,
        "direccion":    emisor.direccion,
        "comuna":       emisor.comuna,
        "ciudad":       emisor.ciudad,
    }

    pdf_bytes = generar_pdf_dte(dte_data, emisor_data, formato=formato)

    nombre_archivo = f"DTE_{dte.tipo_dte}_{dte.folio}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nombre_archivo}"'},
    )

# POST /v1/dte/enviar-set
# Envía al SII un conjunto de DTEs ya guardados en BD
# Body: { emisor_id, folios: [{tipo, folio}] }


class FolioItem(BaseModel):
    tipo: int
    folio: int

class EnviarSetRequest(BaseModel):
    emisor_id: int
    folios: list[FolioItem]

@router.post("/enviar-set", summary="Envía al SII DTEs ya generados")
async def enviar_set_sii(
    body: EnviarSetRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Toma DTEs ya generados en BD (por tipo+folio) y los envía al SII.
    Retorna track_id y estado del envío.
    """
    if not body.folios:
        raise HTTPException(400, "Debe indicar al menos un folio")

    # Obtener emisor
    emisor = await db.get(Emisor, body.emisor_id)
    if not emisor:
        raise HTTPException(404, f"Emisor {body.emisor_id} no encontrado")

    # Obtener certificado
    res = await db.execute(
        select(Certificado).where(
            Certificado.emisor_id == body.emisor_id,
            Certificado.activo == True,
        ).limit(1)
    )
    cert = res.scalar_one_or_none()
    if not cert:
        raise HTTPException(400, "Sin certificado activo")

    # Buscar DTEs en BD
    xmls_firmados = []
    no_encontrados = []
    for item in body.folios:
        res = await db.execute(
            select(DTE).where(
                DTE.emisor_id == body.emisor_id,
                DTE.tipo_dte  == item.tipo,
                DTE.folio     == item.folio,
            ).limit(1)
        )
        dte = res.scalar_one_or_none()
        if not dte or not dte.xml_firmado:
            no_encontrados.append(f"T{item.tipo}-F{item.folio}")
            continue
        xmls_firmados.append(dte.xml_firmado)

    if not xmls_firmados:
        raise HTTPException(404, f"No se encontraron DTEs: {no_encontrados}")

    # Construir sobre y enviar
    firma  = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    sender = SIISender(ambiente=emisor.ambiente)

    # RutEnvia: probar con RUT del emisor para debug STATUS=10
    rut_enviador = cert.rut_firmante or firma.rut_certificado or emisor.rut
    logger.warning(f"[ENVIAR-SET] RutEmisor={emisor.rut} RutEnvia={rut_enviador} cert.rut_firmante={cert.rut_firmante}")

    try:
        sobre_xml = await sender.construir_sobre(
            dtes_xml     = xmls_firmados,
            rut_emisor   = emisor.rut,
            rut_enviador = rut_enviador,
            firma_service= firma,
        )
    except Exception as e:
        import traceback
        logger.error(f"[ENVIAR-SET] Error armando sobre: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Error armando sobre: {type(e).__name__}: {e}")

    try:
        resultado = await sender.enviar_sobre(
            sobre_xml      = sobre_xml,
            rut_emisor     = emisor.rut,
            rut_enviador   = rut_enviador,
            p12_bytes      = cert.certificado_p12,
            password       = cert.certificado_password or "",
            auth_p12_bytes = getattr(cert, "certificado_auth_p12", None),
            auth_password  = getattr(cert, "certificado_auth_password", None),
        )
    except Exception as e:
        import traceback
        logger.error(f"[ENVIAR-SET] Error enviando al SII: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Error enviando al SII: {type(e).__name__}: {e}")

    # Actualizar estado en BD
    track_id = resultado.get("track_id")
    if track_id:
        for item in body.folios:
            res = await db.execute(
                select(DTE).where(
                    DTE.emisor_id == body.emisor_id,
                    DTE.tipo_dte  == item.tipo,
                    DTE.folio     == item.folio,
                ).limit(1)
            )
            dte = res.scalar_one_or_none()
            if dte:
                dte.track_id = str(track_id)
                dte.estado   = "ENVIADO"
        await db.commit()

    return {
        "ok":             True,
        "track_id":       track_id,
        "estado":         resultado.get("estado"),
        "mensaje":        resultado.get("mensaje"),
        "docs_enviados":  len(xmls_firmados),
        "no_encontrados": no_encontrados,
        "ambiente":       emisor.ambiente,
    }
