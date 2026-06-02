# app/api/v1/endpoints/certificacion_boletas.py
# ══════════════════════════════════════════════════════════════
# Endpoint para certificación de Boletas Electrónicas (T39/T41)
# POST /v1/certificacion-boletas/generar-xml
#
# FIX v2.0: insertar TED real con CAF antes de armar el sobre.
#   El flujo correcto es idéntico al de facturas:
#     1. XMLBuilderBoleta → DTE con placeholder <TED><DD/></TED>
#     2. firma.firmar_dte() → reemplaza placeholder con TED real (CAF)
#     3. construir_sobre() → arma EnvioBOLETA con DTEs ya timbrados
#     4. firmar_sobre() → firma DTEs in-tree + Java firma SetDTE
#
#   Analogía: el TED es el timbre notarial de la boleta.
#   Sin él el SII ve un sobre con documentos sin sello → SCH-00001.
# ══════════════════════════════════════════════════════════════

import logging
import re
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


# ── Schemas de entrada ────────────────────────────────────────

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


# ── Helper: extraer MntTotal del XML generado ─────────────────

def _extraer_monto_total(xml_str: str) -> int:
    """
    Lee MntTotal del XML de la boleta.
    Analogía: antes de poner el timbre en el sobre,
    verificamos cuánto dice el cheque adentro.
    """
    m = re.search(r'<MntTotal>(\d+)</MntTotal>', xml_str)
    return int(m.group(1)) if m else 0


# ── Endpoint ──────────────────────────────────────────────────

@router.post("/generar-xml")
async def generar_xml_boletas(
    body: GenerarBoletasRequest,
    db:   AsyncSession = Depends(get_db),
):
    # ── 1. Cargar emisor y certificado ────────────────────────
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

    # Instanciar firma digital (timbra TED + firma XMLDSig)
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

    # ── 2. Obtener CAFs necesarios ────────────────────────────
    tipos_necesarios = list({c.tipo_dte for c in body.casos})
    cafs: dict[int, CAF] = {}
    for tipo in tipos_necesarios:
        caf = (await db.execute(
            select(CAF).where(
                CAF.emisor_id == body.emisor_id,
                CAF.tipo_dte  == tipo,
                CAF.activo    == True,
            ).order_by(CAF.folio_desde).limit(1)
        )).scalar_one_or_none()
        if not caf:
            raise HTTPException(404, f"No hay CAF activo para tipo {tipo}")
        cafs[tipo] = caf

    # ── 3. Generar y TIMBRAR cada DTE ─────────────────────────
    # El timbre TED es el sello notarial que el SII exige en cada boleta.
    # Sin él el portal rechaza el sobre con SCH-00001 aunque el XML
    # esté estructuralmente correcto.
    xmls_timbrados: list[str] = []
    folios_asignados: dict[int, int] = {}

    for caso in body.casos:
        caf   = cafs[caso.tipo_dte]
        folio = caf.folio_actual
        folios_asignados[caso.numero_caso] = folio

        # Construir items para el builder
        items_b = []
        for it in caso.items:
            # precio_con_iva viene del set SII (precio bruto)
            # El builder divide por 1.19 internamente para calcular MntNeto
            items_b.append(ItemBoleta(
                nombre=it.nombre,
                cantidad=it.cantidad,
                precio_unitario=it.precio_con_iva,
                exento=it.exento,
                unidad=it.unidad,
                codigo=it.codigo,
            ))

        refs = [ReferenciaBoleta(
            tipo_doc_ref="SET",
            folio_ref=folio,
            fecha_ref=fecha_emision,
            cod_ref="SET",
            razon_ref=f"CASO-{caso.numero_caso}",
        )]

        # Receptor: genérico para boletas de ventas
        rut_recep = caso.rut_receptor or "66666666-6"
        nom_recep = caso.nombre_receptor or "Consumidor Final"
        if not rut_recep or rut_recep in ("77777777-7", "0"):
            rut_recep = "66666666-6"
            nom_recep = "Consumidor Final"

        input_b = InputBoleta(
            tipo_dte=caso.tipo_dte,
            folio=folio,
            fecha_emision=fecha_emision,
            emisor=emisor_b,
            receptor=ReceptorBoleta(
                rut=rut_recep,
                razon_social=nom_recep,
            ),
            items=items_b,
            referencias=refs,
            observacion=caso.observacion,
        )

        # Paso 3a: construir XML con placeholder TED vacío
        xml_bytes = XMLBuilderBoleta(input_b).construir()
        xml_str   = xml_bytes.decode("ISO-8859-1")

        # Paso 3b: TIMBRAR — insertar TED real con datos del CAF
        # Esto es lo que faltaba: generar_xml_con_ted reemplaza <TED><DD/></TED>
        # con el timbre electrónico firmado con la llave privada del CAF.
        # Sin este paso el SII ve el sello en blanco → SCH-00001.
        monto_total  = _extraer_monto_total(xml_str)
        it1_nombre   = caso.items[0].nombre if caso.items else "PRODUCTO"

        try:
            xml_timbrado_bytes = await firma.firmar_dte(
                xml_bytes     = xml_bytes,
                folio         = folio,
                tipo_dte      = caso.tipo_dte,
                xml_caf       = caf.xml_caf,
                fecha_emision = fecha_emision.isoformat(),
                rut_emisor    = emisor.rut,
                monto_total   = monto_total,
                it1_nombre    = it1_nombre,
            )
        except Exception as e:
            logger.error(f"[BOLETA] Error timbrando folio {folio}: {e}", exc_info=True)
            raise HTTPException(500, f"Error timbrando folio {folio}: {str(e)}")
        xml_timbrado_str = xml_timbrado_bytes.decode("ISO-8859-1")
        xmls_timbrados.append(xml_timbrado_str)

        logger.info(
            f"[BOLETA] Folio {folio} timbrado OK "
            f"(tipo={caso.tipo_dte}, total={monto_total})"
        )

        # Actualizar folio_actual en el CAF
        await db.execute(
            update(CAF).where(CAF.id == caf.id).values(folio_actual=folio + 1)
        )
        caf.folio_actual = folio + 1

        # Guardar DTE en BD con el XML ya timbrado
        db.add(DTE(
            emisor_id=body.emisor_id,
            tipo_dte=caso.tipo_dte,
            folio=folio,
            folio_fmt=f"B-{folio:08d}",
            rut_receptor=rut_recep,
            nombre_receptor=nom_recep,
            monto_neto=0,
            monto_iva=0,
            monto_total=monto_total,
            tasa_iva=19,
            estado="BORRADOR",
            xml_firmado=xml_timbrado_str,
            ambiente=emisor.ambiente or "certificacion",
        ))

    await db.commit()

    # ── 4. Construir sobre EnvioBOLETA y firmarlo ─────────────
    # construir_sobre llama a firma_service.firmar_sobre que:
    #   - Firma cada DTE in-tree con Python
    #   - Firma el SetDTE con Java
    sender       = SIISender()
    rut_enviador = cert.rut_firmante or firma.rut_certificado or emisor.rut

    sobre_firmado = await sender.construir_sobre(
        dtes_xml     = xmls_timbrados,   # DTEs YA timbrados con TED real
        rut_emisor   = emisor.rut,
        rut_enviador = rut_enviador,
        firma_service= firma,
    )

    # ── 5. Retornar XML ───────────────────────────────────────
    rut_limpio = emisor.rut.replace("-", "").replace(".", "")
    filename   = (
        f"EnvioBOLETA_{body.natencion}_boletas_{rut_limpio}_"
        f"{fecha_emision.strftime('%Y%m%d')}.xml"
    )

    return Response(
        content=sobre_firmado.encode("ISO-8859-1"),
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Casos-Generados":   str(len(body.casos)),
            "X-Folios":            str(folios_asignados),
        },
    )
