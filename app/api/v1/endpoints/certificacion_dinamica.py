# app/api/v1/endpoints/certificacion_dinamica.py
# ══════════════════════════════════════════════════════════════
# Endpoint DINÁMICO — recibe casos completos desde el admin
# No tiene datos hardcodeados. Todo viene del body (JSON).
#
# POST /v1/certificacion-dinamica/generar-xml
# POST /v1/certificacion-dinamica/enviar
# ══════════════════════════════════════════════════════════════

import logging
from datetime import date
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.base import get_db
from app.models.emisor import Emisor
from app.models.certificado import Certificado
from app.services.dte_service import DTEService
from app.services.firma_digital import FirmaDigital
from app.services.sii_sender import SIISender

logger = logging.getLogger("yepardtecore.cert_dinamica")
router = APIRouter(prefix="/certificacion-dinamica", tags=["Certificacion Dinamica"])


# ── Schemas de entrada ────────────────────────────────────────

class ItemInput(BaseModel):
    nombre:          str
    descripcion:     str = ""
    cantidad:        float = 1.0
    precio_unitario: float = 0.0
    descuento_pct:   float = 0.0
    exento:          bool  = False
    codigo:          str   = ""      # VlrCodigo (codigo interno)
    unidad:          str   = "UN"    # UnmdItem

class ReferenciaInput(BaseModel):
    tipo_doc_ref: str | int   # "SET" o numero tipo DTE
    folio_ref:    int
    fecha_ref:    str         # YYYY-MM-DD
    cod_ref:      int | None  = None   # 1=anula, 2=corrige giro, 3=corrige montos
    razon_ref:    str         = ""
    # num_caso_ref: caso del MISMO set al que apunta esta referencia
    # Si está presente, el backend lo resuelve al folio real DESPUÉS de generarlos todos
    num_caso_ref: int | None  = None

class ReceptorInput(BaseModel):
    rut:          str = "66666666-6"
    razon_social: str = "Consumidor Final"
    giro:         str = ""
    direccion:    str = ""
    comuna:       str = ""
    ciudad:       str = ""
    correo:       str = ""

class CasoInput(BaseModel):
    numero_caso:         int
    tipo_dte:            int
    receptor:            ReceptorInput = Field(default_factory=ReceptorInput)
    items:               List[ItemInput] = []
    referencias:         List[ReferenciaInput] = []
    descuento_global_pct: float = 0.0
    forma_pago:          int   = 1
    indicador_traslado:  int   = 0
    indicador_despacho:  int   = 0
    forzar_monto_cero:   bool  = False
    fecha_emision:       str   = ""    # vacío → fecha de hoy

class SetInput(BaseModel):
    natencion:     str
    label:         str = ""
    casos:         List[CasoInput]
    fecha_emision: str = ""            # vacío → fecha de hoy

class GenerarXMLRequest(BaseModel):
    emisor_id: int
    sets:      List[SetInput]          # uno o más sets del mismo archivo .txt
    set_key:   str = ""                # "basico2", "exentas", "guias", etc. (informativo)


# ── Helpers ───────────────────────────────────────────────────

async def _get_emisor_y_cert(emisor_id: int, db: AsyncSession):
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(404, f"Emisor {emisor_id} no encontrado")
    cert_result = await db.execute(
        select(Certificado).where(
            Certificado.emisor_id == emisor_id,
            Certificado.activo == True,
        ).limit(1)
    )
    cert = cert_result.scalar_one_or_none()
    if not cert or not cert.certificado_p12:
        raise HTTPException(400, "Sin certificado .p12 cargado")
    return emisor, cert


def _caso_a_datos(caso: CasoInput, fecha: str) -> dict:
    """Convierte un CasoInput del admin al formato que espera DTEService."""
    fecha_caso = caso.fecha_emision or fecha

    return {
        "tipo_dte":            caso.tipo_dte,
        "fecha_emision":       fecha_caso,
        "forzar_monto_cero":   caso.forzar_monto_cero,
        "forma_pago":          caso.forma_pago,
        "indicador_traslado":  caso.indicador_traslado,
        "indicador_despacho":  caso.indicador_despacho,
        "descuento_global_pct": caso.descuento_global_pct,
        "receptor": {
            "rut":          caso.receptor.rut,
            "razon_social": caso.receptor.razon_social,
            "giro":         caso.receptor.giro,
            "direccion":    caso.receptor.direccion,
            "comuna":       caso.receptor.comuna,
            "ciudad":       caso.receptor.ciudad,
            "correo":       caso.receptor.correo,
        },
        "items": [
            {
                "nombre":          it.nombre,
                "cantidad":        it.cantidad,
                "precio_unitario": it.precio_unitario,
                "descuento_pct":   it.descuento_pct,
                "exento":          it.exento,
                "codigo":          it.codigo,
                "unidad":          it.unidad,
            }
            for it in caso.items
        ],
        "referencias": [
            {
                "tipo_doc_ref": ref.tipo_doc_ref,
                "folio_ref":    ref.folio_ref,
                "fecha_ref":    ref.fecha_ref or fecha_caso,
                "cod_ref":      ref.cod_ref or 0,
                "razon_ref":    ref.razon_ref,
            }
            for ref in caso.referencias
        ],
    }


async def _emitir_casos(
    casos: List[CasoInput],
    fecha: str,
    service: DTEService,
    emisor_id: int,
    natencion: str,
) -> tuple[list, dict, list]:
    """
    Emite todos los casos de un set dinámicamente.

    TWO-PASS para referencias cruzadas correctas:
    - Pass 1: pre-asignar folios a cada caso (peek del CAF sin consumir)
    - Pass 2: generar DTEs con folio_ref actualizado al folio real de cada caso

    Esto resuelve REF-3-750 causado por folio_ref desactualizado del frontend.
    """
    xmls_firmados: list[str] = []
    folios: dict[int, int] = {}
    errores: list[str] = []

    # ── Pass 1: pre-asignar folios ───────────────────────────────────────────
    # Obtenemos el folio_actual de cada tipo de DTE y simulamos la secuencia
    # para saber qué folio recibirá cada caso, sin consumirlos aún.
    # Esto permite actualizar las referencias cruzadas antes de generar.
    from app.models.caf import CAF

    tipo_next: dict[int, int] = {}     # {tipo_dte: proximo_folio_disponible}
    folio_por_caso: dict[int, int] = {}  # {numero_caso: folio_predicho}

    for caso in casos:
        tipo = caso.tipo_dte
        if tipo not in tipo_next:
            try:
                resultado = await service.db.execute(
                    select(CAF).where(
                        CAF.emisor_id == emisor_id,
                        CAF.tipo_dte  == tipo,
                        CAF.activo    == True,
                        CAF.ambiente  == "certificacion",
                    ).order_by(CAF.folio_desde.asc())
                )
                cafs = resultado.scalars().all()
                caf_disp = next((c for c in cafs if not c.esta_agotado), None)
                tipo_next[tipo] = caf_disp.folio_actual if caf_disp else 0
            except Exception:
                tipo_next[tipo] = 0
        if tipo_next[tipo]:
            folio_por_caso[caso.numero_caso] = tipo_next[tipo]
            tipo_next[tipo] += 1

    logger.info(
        f"[CERT DIN] Pass1 N°{natencion}: folios previstos por caso → {folio_por_caso}"
    )

    # ── Pass 2: generar DTEs con referencias resueltas ───────────────────────
    for caso in casos:
        for ref in caso.referencias:
            tpo = str(ref.tipo_doc_ref).upper()
            if tpo == "SET":
                # Referencia al set (auto-referencia): folio = el propio DTE
                # El frontend envía el valor predicho en asignarFolios() que puede
                # estar desfasado si el CAF avanzó entre la carga y la generación.
                # Lo corregimos aquí con el folio_actual real leído en Pass 1.
                if caso.numero_caso in folio_por_caso:
                    ref.folio_ref = folio_por_caso[caso.numero_caso]
                    logger.info(
                        f"[CERT DIN] Caso {caso.numero_caso} SET FolioRef corregido → {ref.folio_ref}"
                    )
            elif ref.num_caso_ref and ref.num_caso_ref in folio_por_caso:
                # Referencia cruzada a otro caso del set
                logger.info(
                    f"[CERT DIN] Caso {caso.numero_caso} ref→caso {ref.num_caso_ref} "
                    f"folio_ref resuelto a {folio_por_caso[ref.num_caso_ref]}"
                )
                ref.folio_ref = folio_por_caso[ref.num_caso_ref]

        datos = _caso_a_datos(caso, fecha)
        try:
            r = await service.emitir(
                emisor_id=emisor_id,
                datos={**datos, "emisor_id": emisor_id},
                auto_enviar=False,
            )
            xmls_firmados.append(r["xml_firmado"])
            folios[caso.numero_caso] = r["folio"]
            logger.info(
                f"[CERT DIN] N°{natencion} caso {caso.numero_caso} OK "
                f"T{caso.tipo_dte} folio={r['folio']} total=${r['monto_total']:,.0f}"
            )
        except Exception as e:
            errores.append(f"Caso {caso.numero_caso}: {e}")
            logger.error(f"[CERT DIN] Error caso {caso.numero_caso}: {e}", exc_info=True)

    return xmls_firmados, folios, errores


# ── Endpoint principal ────────────────────────────────────────

@router.post(
    "/generar-xml",
    summary="Genera EnvioDTE dinámico desde datos del admin",
    description="""
Recibe los casos editados en el admin y genera el EnvioDTE firmado.
No tiene datos hardcodeados — todo viene del body.

Un request puede contener múltiples sets (basico, exentas, guias).
Cada set genera su propio EnvioDTE independiente (un archivo por set).
El response contiene el primer set generado.
Para múltiples sets, llamar este endpoint una vez por set.
    """,
)
async def generar_xml_dinamico(
    body: GenerarXMLRequest,
    db: AsyncSession = Depends(get_db),
):
    emisor, cert = await _get_emisor_y_cert(body.emisor_id, db)
    fecha_global = date.today().isoformat()
    service      = DTEService(db)

    # Procesar solo el primer set (llamar una vez por set desde el admin)
    if not body.sets:
        raise HTTPException(400, "No hay sets en el request")

    set_data = body.sets[0]
    fecha    = set_data.fecha_emision or fecha_global
    natencion = set_data.natencion

    xmls_firmados, folios, errores = await _emitir_casos(
        casos     = set_data.casos,
        fecha     = fecha,
        service   = service,
        emisor_id = body.emisor_id,
        natencion = natencion,
    )

    if not xmls_firmados:
        raise HTTPException(500,
            f"No se generó ningún documento. Errores: {'; '.join(errores)}")

    firma  = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    # Obtener resolución del emisor según su ambiente — dinámico por cliente
    nro_resol, fch_resol = emisor.get_resolucion(emisor.ambiente)
    sender = SIISender(
        ambiente  = emisor.ambiente,
        fch_resol = fch_resol,
        nro_resol = nro_resol,
    )

    try:
        sobre_xml = await sender.construir_sobre(
            dtes_xml     = xmls_firmados,
            rut_emisor   = emisor.rut,
            rut_enviador = firma.rut_certificado or emisor.rut,
            firma_service= firma,
        )
    except Exception as e:
        raise HTTPException(500, f"Error armando sobre: {e}")

    rut_limpio = emisor.rut.replace(".", "").replace("-", "")
    set_key    = body.set_key or "dinamico"
    nombre     = f"EnvioDTE_{natencion}_{set_key}_{rut_limpio}_{fecha.replace('-','')}.xml"

    logger.info(
        f"[CERT DIN] Sobre N°{natencion} listo "
        f"{len(xmls_firmados)}/{len(set_data.casos)} docs"
        + (f" errores: {errores}" if errores else " ✓")
    )

    return Response(
        content    = sobre_xml.encode("ISO-8859-1"),
        media_type = "application/octet-stream",
        headers    = {
            "Content-Disposition": f'attachment; filename="{nombre}"',
            "X-Casos-Generados":   str(len(xmls_firmados)),
            "X-Casos-Error":       str(len(errores)),
            "X-Errores-Detalle":   " | ".join(errores) if errores else "",
            "X-NroAtencion":       natencion,
            "X-SetKey":            set_key,
            "X-Folios":            str(folios),
        },
    )


@router.post("/enviar", summary="Genera Y envía al SII dinámicamente")
async def enviar_dinamico(
    body: GenerarXMLRequest,
    db: AsyncSession = Depends(get_db),
):
    emisor, cert = await _get_emisor_y_cert(body.emisor_id, db)
    fecha_global = date.today().isoformat()
    service      = DTEService(db)

    if not body.sets:
        raise HTTPException(400, "No hay sets en el request")

    set_data  = body.sets[0]
    fecha     = set_data.fecha_emision or fecha_global
    natencion = set_data.natencion

    xmls_firmados, folios, errores = await _emitir_casos(
        casos     = set_data.casos,
        fecha     = fecha,
        service   = service,
        emisor_id = body.emisor_id,
        natencion = natencion,
    )

    if not xmls_firmados:
        raise HTTPException(500, f"No se generó ningún documento. Errores: {'; '.join(errores)}")

    firma  = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    # Obtener resolución del emisor según su ambiente — dinámico por cliente
    nro_resol, fch_resol = emisor.get_resolucion(emisor.ambiente)
    sender = SIISender(
        ambiente  = emisor.ambiente,
        fch_resol = fch_resol,
        nro_resol = nro_resol,
    )

    try:
        sobre_xml = await sender.construir_sobre(
            dtes_xml     = xmls_firmados,
            rut_emisor   = emisor.rut,
            rut_enviador = firma.rut_certificado or emisor.rut,
            firma_service= firma,
        )
    except Exception as e:
        raise HTTPException(500, f"Error armando sobre: {e}")

    try:
        resultado = await sender.enviar_sobre(
            sobre_xml    = sobre_xml,
            rut_emisor   = emisor.rut,
            rut_enviador = firma.rut_certificado or emisor.rut,
            p12_bytes    = cert.certificado_p12,
            password     = cert.certificado_password or "",
            auth_p12_bytes = getattr(cert, "certificado_auth_p12", None),
            auth_password  = getattr(cert, "certificado_auth_password", None),
        )
        return {
            "estado":         resultado.get("estado"),
            "track_id":       resultado.get("track_id"),
            "mensaje":        resultado.get("mensaje"),
            "natencion":      natencion,
            "docs_generados": len(xmls_firmados),
            "folios":         folios,
            "errores":        errores,
        }
    except Exception as e:
        raise HTTPException(500, f"Error enviando al SII: {e}")
