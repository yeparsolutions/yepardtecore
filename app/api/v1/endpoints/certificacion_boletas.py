# app/api/v1/endpoints/certificacion_boletas.py
# ══════════════════════════════════════════════════════════════
# Endpoint para certificación de Boletas Electrónicas (T39/T41)
# POST /v1/certificacion-boletas/generar-xml
# ══════════════════════════════════════════════════════════════

import logging
from datetime import date
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.db.base import get_db
from app.models.emisor import Emisor
from app.models.certificado import Certificado
from app.models.caf import CAF
from app.models.dte import DTE
from app.services.firma_digital import FirmaDigital
from app.services.sii_sender import SIISender
from app.services.xml_builder_boleta import (
    XMLBuilderBoleta, InputBoleta, EmisorBoleta,
    ReceptorBoleta, ItemBoleta, ReferenciaBoleta,
)

logger = logging.getLogger("yepardtecore.cert_boletas")
router = APIRouter(prefix="/certificacion-boletas", tags=["Certificacion Boletas"])


# ── Schemas ───────────────────────────────────────────────────

class ItemBoletaInput(BaseModel):
    nombre:         str
    cantidad:       float = 1.0
    precio_con_iva: float = 0.0   # precio bruto CON IVA (como viene del set)
    exento:         bool  = False
    unidad:         str   = ""
    codigo:         str   = ""

class CasoBoletaInput(BaseModel):
    numero_caso:     int
    tipo_dte:        int = 39        # 39=afecta, 41=exenta
    items:           List[ItemBoletaInput]
    rut_receptor:    str = "66666666-6"
    nombre_receptor: str = "Consumidor Final"
    observacion:     str = ""

class GenerarBoletasRequest(BaseModel):
    emisor_id:  int
    natencion:  str
    casos:      List[CasoBoletaInput]
    fecha:      Optional[str] = None   # YYYY-MM-DD, default hoy


# ── Helpers ───────────────────────────────────────────────────

def _precio_con_iva_a_neto(precio_bruto: float, tasa: float = 19.0) -> float:
    return round(precio_bruto / (1 + tasa / 100), 6)


# ── Endpoint ──────────────────────────────────────────────────

@router.post("/generar-xml")
async def generar_xml_boletas(
    body: GenerarBoletasRequest,
    db:   AsyncSession = Depends(get_db),
):
    # 1. Cargar emisor y certificado
    emisor = (await db.execute(
        select(Emisor).where(Emisor.id == body.emisor_id)
    )).scalar_one_or_none()
    if not emisor:
        raise HTTPException(404, "Emisor no encontrado")

    cert = (await db.execute(
        select(Certificado).where(
            Certificado.emisor_id == body.emisor_id,
            Certificado.activo == True
        ).limit(1)
    )).scalar_one_or_none()
    if not cert:
        raise HTTPException(404, "Certificado no encontrado")

    fecha_emision = (
        date.fromisoformat(body.fecha) if body.fecha
        else date.today()
    )

    firma = FirmaDigital(
        p12_bytes=bytes(cert.certificado_p12),
        password=cert.certificado_password,
    )

    emisor_b = EmisorBoleta(
        rut=emisor.rut,
        razon_social=emisor.razon_social,
        giro=emisor.giro or "",
        direccion=emisor.direccion or "",
        comuna=emisor.comuna or "",
        ciudad=emisor.ciudad or "",
        acteco=emisor.acteco or "620200",
    )

    # 2. Obtener CAFs necesarios
    tipos_necesarios = list({c.tipo_dte for c in body.casos})
    cafs: dict[int, CAF] = {}
    for tipo in tipos_necesarios:
        caf = (await db.execute(
            select(CAF).where(
                CAF.emisor_id == body.emisor_id,
                CAF.tipo_dte  == tipo,
                CAF.activo    == True,
            ).order_by(CAF.folio_desde)
        )).scalar_one_or_none()
        if not caf:
            raise HTTPException(404, f"No hay CAF activo para tipo {tipo}")
        cafs[tipo] = caf

    # 3. Generar XMLs individuales
    xmls_sin_firmar: list[str] = []
    folios_asignados: dict[int, int] = {}

    for caso in body.casos:
        caf   = cafs[caso.tipo_dte]
        folio = caf.folio_actual
        folios_asignados[caso.numero_caso] = folio

        items_b = []
        for it in caso.items:
            precio_neto = (
                it.precio_con_iva if it.exento
                else _precio_con_iva_a_neto(it.precio_con_iva)
            )
            items_b.append(ItemBoleta(
                nombre=it.nombre,
                cantidad=it.cantidad,
                precio_unitario=precio_neto,
                exento=it.exento,
                unidad=it.unidad,
                codigo=it.codigo,
            ))

        refs = [ReferenciaBoleta(
            tipo_doc_ref="SET",
            folio_ref=folio,
            fecha_ref=fecha_emision,
            cod_ref=None,
            razon_ref=f"CASO-{caso.numero_caso}",
        )]

        input_b = InputBoleta(
            tipo_dte=caso.tipo_dte,
            folio=folio,
            fecha_emision=fecha_emision,
            emisor=emisor_b,
            receptor=ReceptorBoleta(
                rut=caso.rut_receptor,
                razon_social=caso.nombre_receptor,
            ),
            items=items_b,
            referencias=refs,
            observacion=caso.observacion,
        )

        xml_bytes = XMLBuilderBoleta(input_b).construir()
        xml_str   = xml_bytes.decode("ISO-8859-1")
        xmls_sin_firmar.append(xml_str)

        # Actualizar folio_actual
        await db.execute(
            update(CAF).where(CAF.id == caf.id).values(folio_actual=folio + 1)
        )
        caf.folio_actual = folio + 1

        # Guardar DTE en BD
        db.add(DTE(
            emisor_id=body.emisor_id,
            tipo_dte=caso.tipo_dte,
            folio=folio,
            folio_fmt=f"B-{folio:08d}",
            rut_receptor=caso.rut_receptor,
            nombre_receptor=caso.nombre_receptor,
            monto_neto=0,
            monto_iva=0,
            monto_total=0,
            tasa_iva=19,
            estado="BORRADOR",
            xml_firmado=xml_str,
            ambiente=emisor.ambiente or "certificacion",
        ))

    await db.commit()

    # 4. Construir y firmar sobre EnvioBOLETA via SIISender
    sender = SIISender()
    rut_enviador = cert.rut_firmante or firma.rut_certificado or emisor.rut

    sobre_sin_firmas = await sender.construir_sobre(
        dtes_xml=xmls_sin_firmar,
        rut_emisor=emisor.rut,
        rut_enviador=rut_enviador,
        firma_service=firma,
    )

    # 5. Retornar XML
    rut_limpio = emisor.rut.replace("-", "").replace(".", "")
    filename   = (
        f"EnvioBOLETA_{body.natencion}_boletas_{rut_limpio}_"
        f"{fecha_emision.strftime('%Y%m%d')}.xml"
    )

    return Response(
        content=sobre_sin_firmas.encode("ISO-8859-1"),
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Casos-Generados":   str(len(body.casos)),
            "X-Folios":            str(folios_asignados),
        },
    )
