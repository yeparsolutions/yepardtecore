# app/api/v1/endpoints/certificacion.py
# ══════════════════════════════════════════════════════════════
# Endpoints de Certificación SII
#
# Orquesta el set oficial de 5 boletas que el SII exige
# para certificar a una empresa como emisor DTE.
#
# Flujo interno (sin intervención manual):
#   1. Emitir cada boleta con auto_enviar=False
#      → cada DTE queda con su propia firma + TED incrustado
#   2. Acumular los 5 XMLs firmados
#   3. Construir UN SOLO sobre EnvioBOLETA
#   4. Firmar el sobre
#   5. Enviar a maullin.sii.cl (ambiente certificación)
#
# Analogía: es como preparar 5 cartas certificadas
# (cada una con su propio sello y firma),
# meterlas en un sobre común, firmar el sobre,
# y llevarlo al correo de una sola vez.
#
# Endpoints:
#   POST /v1/certificacion/set-boletas   — Envía el set completo
#   GET  /v1/certificacion/set-boletas/{emisor_id}/preview
#                                         — Muestra los 5 casos sin enviar
# ══════════════════════════════════════════════════════════════

import logging
from datetime import date
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from app.db.base import get_db
from app.models.emisor import Emisor
from app.services.dte_service import DTEService
from app.services.firma_digital import FirmaDigital
from app.services.sii_sender import SIISender

logger = logging.getLogger("yepardtecore.certificacion")

router = APIRouter(prefix="/certificacion", tags=["Certificación SII"])


# ── Schema de respuesta ────────────────────────────────────────

class DTECertificacionResultado(BaseModel):
    caso:        int
    descripcion: str
    dte_id:      Optional[int]
    folio:       Optional[int]
    monto_total: Optional[float]
    estado:      str
    error:       Optional[str] = None


class SetBoletasRespuesta(BaseModel):
    emisor_id:   int
    rut_emisor:  str
    ambiente:    str
    track_id:    Optional[str]
    estado_envio: str
    mensaje:     str
    documentos:  list[DTECertificacionResultado]
    sobre_xml:   Optional[str] = None  # Solo si debug=True


# ── Casos de certificación SII ────────────────────────────────
#
# El SII exige exactamente estos 5 casos para certificar boletas.
# Fuente: Manual de Certificación DTE, SII Chile.
#
# Cada caso es un dict compatible con EmitirDTEInput.

def _casos_certificacion(fecha: str) -> list[dict]:
    """
    Retorna los 5 casos oficiales del set de certificación SII.
    Analogía: son como los 5 ejercicios del examen de manejo —
    el SII definió exactamente qué debe demostrar cada empresa.
    """
    return [
        # ── Caso 1: Boleta afecta simple ─────────────────────
        # El caso más básico: un item, sin descuentos.
        # Valida que el motor puede emitir, firmar y calcular IVA.
        {
            "caso":        1,
            "descripcion": "Boleta afecta simple (1 item sin descuento)",
            "tipo_dte":    39,
            "receptor": {
                "rut":          "66.666.666-6",
                "razon_social": "Consumidor Final",
            },
            "items": [
                {
                    "nombre":          "Producto de certificación caso 1",
                    "cantidad":        1,
                    "precio_unitario": 10000,
                    "exento":          False,
                }
            ],
            "fecha_emision": fecha,
            "forma_pago":    1,  # 1 = Contado
        },

        # ── Caso 2: Boleta afecta con descuento por línea ────
        # Valida que el motor aplica descuentos por línea correctamente.
        {
            "caso":        2,
            "descripcion": "Boleta afecta con descuento por línea (10%)",
            "tipo_dte":    39,
            "receptor": {
                "rut":          "66.666.666-6",
                "razon_social": "Consumidor Final",
            },
            "items": [
                {
                    "nombre":          "Producto con descuento",
                    "cantidad":        2,
                    "precio_unitario": 15000,
                    "descuento_pct":   10.0,  # 10% de descuento por línea
                    "exento":          False,
                }
            ],
            "fecha_emision": fecha,
            "forma_pago":    1,
        },

        # ── Caso 3: Boleta afecta con múltiples ítems ────────
        # Valida subtotales y suma de líneas.
        {
            "caso":        3,
            "descripcion": "Boleta afecta con 3 ítems distintos",
            "tipo_dte":    39,
            "receptor": {
                "rut":          "66.666.666-6",
                "razon_social": "Consumidor Final",
            },
            "items": [
                {
                    "nombre":          "Item certificación A",
                    "cantidad":        1,
                    "precio_unitario": 5000,
                    "exento":          False,
                },
                {
                    "nombre":          "Item certificación B",
                    "cantidad":        3,
                    "precio_unitario": 3000,
                    "exento":          False,
                },
                {
                    "nombre":          "Item certificación C",
                    "cantidad":        2,
                    "precio_unitario": 8500,
                    "exento":          False,
                },
            ],
            "fecha_emision": fecha,
            "forma_pago":    1,
        },

        # ── Caso 4: Boleta exenta de IVA (tipo 41) ───────────
        # Valida boletas de servicios no gravados (honorarios, etc.)
        # El motor detecta tipo 41 → monto_iva = 0, monto_neto = 0.
        {
            "caso":        4,
            "descripcion": "Boleta exenta (tipo 41, sin IVA)",
            "tipo_dte":    41,  # Boleta No Afecta o Exenta
            "receptor": {
                "rut":          "66.666.666-6",
                "razon_social": "Consumidor Final",
            },
            "items": [
                {
                    "nombre":          "Servicio exento de IVA",
                    "cantidad":        1,
                    "precio_unitario": 20000,
                    "exento":          True,  # Explícitamente exento
                }
            ],
            "fecha_emision": fecha,
            "forma_pago":    1,
        },

        # ── Caso 5: Boleta afecta con descuento global ───────
        # Valida que el descuento global se aplica sobre el subtotal
        # de todos los ítems afectos, DESPUÉS de los descuentos por línea.
        {
            "caso":        5,
            "descripcion": "Boleta afecta con descuento global (15%)",
            "tipo_dte":    39,
            "receptor": {
                "rut":          "66.666.666-6",
                "razon_social": "Consumidor Final",
            },
            "items": [
                {
                    "nombre":          "Producto con descuento global",
                    "cantidad":        4,
                    "precio_unitario": 12000,
                    "exento":          False,
                }
            ],
            "descuento_global_pct": 15.0,  # 15% sobre el total de ítems afectos
            "fecha_emision": fecha,
            "forma_pago":    1,
        },
    ]


# ── Endpoints ─────────────────────────────────────────────────

@router.post(
    "/set-boletas",
    summary="Enviar set de certificación de boletas al SII",
    response_model=SetBoletasRespuesta,
    status_code=200,
)
async def enviar_set_boletas(
    emisor_id:   int,
    debug:       bool = False,
    fecha_override: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Envía el set oficial de 5 boletas al SII para certificación.

    **¿Qué hace exactamente?**

    1. Emite los 5 casos SII con `auto_enviar=False`
       (cada DTE queda firmado individualmente con su TED)
    2. Acumula los 5 XMLs firmados
    3. Construye un solo sobre `EnvioBOLETA`
    4. Firma el sobre con el certificado del emisor
    5. Envía a `maullin.sii.cl` (ambiente certificación)

    **Parámetros:**
    - `emisor_id`: ID del emisor en BD (debe tener certificado y CAFs cargados)
    - `debug`: Si True, incluye el XML del sobre en la respuesta
    - `fecha_override`: Fuerza una fecha de emisión (formato YYYY-MM-DD)
      Por defecto usa la fecha de hoy.

    **Requisitos previos:**
    - El emisor debe tener un certificado `.p12` cargado en BD
      → `POST /v1/certificados/{emisor_id}/subir`
    - El emisor debe tener CAFs para tipo 39 Y tipo 41 en ambiente `certificacion`
      → `POST /v1/caf/cargar-archivo`
    """

    # ── 1. Cargar y validar emisor ────────────────────────────
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(status_code=404, detail=f"Emisor {emisor_id} no encontrado")
    if not emisor.activo:
        raise HTTPException(status_code=400, detail="El emisor está desactivado")

    # Verificar que el emisor tiene certificado
    cert = emisor.certificado_activo
    if not cert or not cert.certificado_p12:
        raise HTTPException(
            status_code=400,
            detail=(
                f"El emisor {emisor.rut} no tiene certificado .p12 cargado. "
                "Súbelo con POST /v1/certificados/{emisor_id}/subir"
            ),
        )

    # Forzar ambiente certificación para este endpoint
    if emisor.ambiente != "certificacion":
        raise HTTPException(
            status_code=400,
            detail=(
                "Este endpoint es solo para ambiente 'certificacion'. "
                f"El emisor está en '{emisor.ambiente}'. "
                "Cambia el ambiente del emisor antes de certificar."
            ),
        )

    fecha = fecha_override or date.today().isoformat()
    logger.info(
        f"[CERT] Iniciando set de certificación — "
        f"emisor={emisor.rut} fecha={fecha}"
    )

    # ── 2. Emitir los 5 casos individualmente ────────────────
    # Cada uno se emite con auto_enviar=False para obtener el XML
    # firmado sin enviarlo todavía al SII.
    # Analogía: horneamos 5 panes (cada uno con su corteza propia)
    # antes de meterlos en la bolsa final.

    service     = DTEService(db)
    casos       = _casos_certificacion(fecha)
    resultados  = []
    xmls_firmados = []  # Acumulamos los XMLs para el sobre

    for caso in casos:
        caso_num  = caso.pop("caso")         # Extraer para el resultado
        desc      = caso.pop("descripcion")  # Extraer para el resultado

        logger.info(
            f"[CERT] Emitiendo caso {caso_num}/5 — {desc}"
        )

        try:
            # Emitir SIN enviar al SII (auto_enviar=False)
            resultado = await service.emitir(
                emisor_id   = emisor_id,
                datos       = {**caso, "emisor_id": emisor_id},
                auto_enviar = False,  # ← CRÍTICO: no enviar todavía
            )

            xmls_firmados.append(resultado["xml_firmado"])

            resultados.append(DTECertificacionResultado(
                caso        = caso_num,
                descripcion = desc,
                dte_id      = resultado["dte_id"],
                folio       = resultado["folio"],
                monto_total = resultado["monto_total"],
                estado      = "FIRMADO_OK",
            ))

            logger.info(
                f"[CERT] Caso {caso_num} OK — "
                f"folio={resultado['folio']} monto=${resultado['monto_total']:,.0f}"
            )

        except Exception as e:
            # Si falla un caso, registrar el error y continuar con los demás
            logger.error(f"[CERT] Error en caso {caso_num}: {e}", exc_info=True)
            resultados.append(DTECertificacionResultado(
                caso        = caso_num,
                descripcion = desc,
                dte_id      = None,
                folio       = None,
                monto_total = None,
                estado      = "ERROR",
                error       = str(e),
            ))

    # Verificar que al menos algunos casos fueron exitosos
    casos_ok = [r for r in resultados if r.estado == "FIRMADO_OK"]
    if not casos_ok:
        raise HTTPException(
            status_code=500,
            detail=(
                "No se pudo emitir ningún caso. "
                "Verifica que el emisor tiene CAFs disponibles para tipos 39 y 41."
            ),
        )

    if len(casos_ok) < len(casos):
        casos_fallidos = [r.caso for r in resultados if r.estado == "ERROR"]
        logger.warning(
            f"[CERT] Solo {len(casos_ok)}/5 casos exitosos. "
            f"Casos fallidos: {casos_fallidos}"
        )

    # ── 3. Construir el sobre EnvioBOLETA ────────────────────
    # Un solo sobre con todos los DTEs firmados adentro.
    # Analogía: la bolsa que contiene todos los panes,
    # también tiene su propio sello de la panadería.

    logger.info(
        f"[CERT] Construyendo sobre EnvioBOLETA con "
        f"{len(xmls_firmados)} DTEs firmados"
    )

    firma = FirmaDigital(
        cert.certificado_p12,
        cert.certificado_password or "",
    )

    sender = SIISender(ambiente="certificacion")

    try:
        sobre_xml = sender.construir_sobre(
            dtes_xml      = xmls_firmados,
            rut_emisor    = emisor.rut,
            rut_enviador  = firma.rut_certificado or emisor.rut,
            firma_service = firma,
        )
        logger.info("[CERT] Sobre EnvioBOLETA construido y firmado OK")

    except Exception as e:
        logger.error(f"[CERT] Error construyendo sobre: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error al construir el sobre: {str(e)}",
        )

    # ── 4. Enviar el sobre a maullin.sii.cl ──────────────────
    # Este es el único momento en que nos comunicamos con el SII.
    # Analogía: la carta con todos los panes va al correo una sola vez.

    logger.info(
        f"[CERT] Enviando sobre a maullin.sii.cl — "
        f"rut={emisor.rut} enviador={firma.rut_certificado or emisor.rut}"
    )

    try:
        resultado_envio = await sender.enviar_sobre(
            sobre_xml    = sobre_xml,
            rut_emisor   = emisor.rut,
            rut_enviador = firma.rut_certificado or emisor.rut,
            p12_bytes    = cert.certificado_p12,
            password     = cert.certificado_password or "",
        )
    except Exception as e:
        logger.error(f"[CERT] Error enviando sobre: {e}", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail=f"Error de comunicación con SII: {str(e)}",
        )

    track_id     = resultado_envio.get("track_id")
    estado_envio = resultado_envio.get("estado", "DESCONOCIDO")
    mensaje      = resultado_envio.get("mensaje", "Sin mensaje del SII")

    logger.info(
        f"[CERT] Respuesta SII — estado={estado_envio} "
        f"track_id={track_id} mensaje={mensaje}"
    )

    # ── 5. Retornar resultado completo ────────────────────────
    return SetBoletasRespuesta(
        emisor_id    = emisor_id,
        rut_emisor   = emisor.rut,
        ambiente     = "certificacion",
        track_id     = track_id,
        estado_envio = estado_envio,
        mensaje      = mensaje,
        documentos   = resultados,
        sobre_xml    = sobre_xml if debug else None,
    )


@router.get(
    "/set-boletas/{emisor_id}/preview",
    summary="Vista previa de los 5 casos de certificación (sin enviar)",
)
async def preview_set_boletas(
    emisor_id: int,
    fecha_override: Optional[str] = None,
):
    """
    Muestra los 5 casos del set de certificación sin emitir ni enviar nada.
    Útil para verificar los datos antes de ejecutar la certificación real.
    """
    fecha = fecha_override or date.today().isoformat()
    casos = _casos_certificacion(fecha)

    return {
        "emisor_id": emisor_id,
        "fecha":     fecha,
        "total_casos": len(casos) + 2,  # +2 por los keys que extrajimos
        "advertencia": (
            "Los casos 4 (tipo 41) requiere CAF tipo 41. "
            "Si no tienes CAF tipo 41, el caso 4 fallará al ejecutar."
        ),
        "casos": [
            {
                "caso":         i + 1,
                "descripcion":  c.get("descripcion", f"Caso {i+1}"),
                "tipo_dte":     c["tipo_dte"],
                "items":        len(c["items"]),
                "descuento_global": c.get("descuento_global_pct", 0),
            }
            for i, c in enumerate(casos)
        ],
    }
