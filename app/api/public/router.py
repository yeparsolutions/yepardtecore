# app/api/public/router.py
# ══════════════════════════════════════════════════════════════
# API Pública para Desarrolladores — Opción A
# Autenticación: API Key en header X-API-Key
# Prefix: /api
# ══════════════════════════════════════════════════════════════

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.base import get_db
from app.models.emisor import Emisor
from app.models.caf import CAF
from app.models.dte import DTE
from app.services.dte_service import DTEService
from app.services.sii_sender import SIISender
from app.models.certificado import Certificado
import logging
logger = logging.getLogger("yepardtecore.api")

router = APIRouter(prefix="/api", tags=["API Desarrolladores"])


async def get_emisor_by_api_key(
    x_api_key: str = Header(..., description="API Key del desarrollador"),
    db: AsyncSession = Depends(get_db),
) -> Emisor:
    emisor = (await db.execute(
        select(Emisor).where(
            Emisor.api_key == x_api_key,
            Emisor.activo  == True,
        )
    )).scalar_one_or_none()
    if not emisor:
        raise HTTPException(401, "API Key inválida o inactiva")
    return emisor


@router.get("/health")
async def health():
    return {"ok": True, "servicio": "YeparDTEcore", "version": "1.1",
            "docs": "https://yepardtecore.cl/api/docs"}


class ReceptorInput(BaseModel):
    rut:       Optional[str] = "66666666-6"
    nombre:    Optional[str] = "Consumidor Final"
    giro:      Optional[str] = ""
    direccion: Optional[str] = ""
    comuna:    Optional[str] = ""
    ciudad:    Optional[str] = ""
    email:     Optional[str] = None

class ItemInput(BaseModel):
    nombre:   str
    cantidad: float = 1
    precio:   float
    exento:   bool = False

class EmitirInput(BaseModel):
    tipo:        int
    receptor:    ReceptorInput
    items:       list[ItemInput]
    exento:      bool = False
    fecha:       Optional[str] = None
    auto_enviar: bool = True
    referencia:  Optional[dict] = None


@router.post("/emitir")
async def emitir_dte(
    datos:  EmitirInput,
    emisor: Emisor = Depends(get_emisor_by_api_key),
    db:     AsyncSession = Depends(get_db),
):
    tipos_validos = {33, 34, 39, 41, 52, 56, 61}
    if datos.tipo not in tipos_validos:
        raise HTTPException(422, f"Tipo DTE no válido: {datos.tipo}")

    fecha = datos.fecha or datetime.now().strftime("%Y-%m-%d")
    datos_dte = {
        "tipo_dte":      datos.tipo,
        "fecha_emision": fecha,
        "receptor": {
            "rut":          datos.receptor.rut or "66666666-6",
            "razon_social": datos.receptor.nombre or "Consumidor Final",
            "giro":         datos.receptor.giro or "",
            "direccion":    datos.receptor.direccion or "",
            "comuna":       datos.receptor.comuna or "",
            "ciudad":       datos.receptor.ciudad or "",
            "correo":       datos.receptor.email or "",
        },
        "items": [
            {"nombre": it.nombre, "cantidad": it.cantidad,
             "precio_unitario": it.precio, "exento": it.exento or datos.exento}
            for it in datos.items
        ],
        "referencias": [datos.referencia] if datos.referencia else [],
    }

    try:
        svc = DTEService(db=db)
        resultado = await svc.emitir(emisor_id=emisor.id, datos=datos_dte,
                                     auto_enviar=datos.auto_enviar)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Error emitiendo DTE: {e}")

    doc = resultado.get("dte", {})
    tipo_label = {33:"Factura Electrónica", 34:"Factura Exenta", 39:"Boleta Electrónica",
                  41:"Boleta Exenta", 52:"Guía de Despacho", 56:"Nota de Débito", 61:"Nota de Crédito"}
    return {
        "ok": True, "tipo": tipo_label.get(datos.tipo, str(datos.tipo)),
        "folio": doc.get("folio"), "folio_fmt": doc.get("folio_fmt"),
        "monto_total": doc.get("monto_total"), "estado": doc.get("estado"),
        "track_id": doc.get("track_id"), "fecha": fecha,
        "receptor": datos.receptor.nombre, "xml_firmado": doc.get("xml_firmado"),
        "ambiente": emisor.ambiente or "certificacion",
    }


@router.get("/estado/{track_id}")
async def estado_envio(
    track_id: str,
    emisor:   Emisor = Depends(get_emisor_by_api_key),
    db:       AsyncSession = Depends(get_db),
):
    cert = (await db.execute(
        select(Certificado).where(Certificado.emisor_id == emisor.id,
                                   Certificado.activo == True).limit(1)
    )).scalar_one_or_none()
    if not cert:
        raise HTTPException(400, "No hay certificado configurado")

    sender = SIISender(
        ambiente  = emisor.ambiente or "certificacion",
        nro_resol = emisor.nro_resolucion or "0",
        fch_resol = emisor.fch_resolucion or "2000-01-01",
    )
    try:
        resultado = await sender.consultar_estado(track_id=track_id, rut_emisor=emisor.rut)
    except Exception as e:
        raise HTTPException(500, f"Error consultando SII: {e}")
    return {"track_id": track_id, "estado": resultado.get("estado"),
            "glosa": resultado.get("glosa"), "detalle": resultado.get("detalle")}


@router.get("/folios")
async def folios_disponibles(
    emisor: Emisor = Depends(get_emisor_by_api_key),
    db:     AsyncSession = Depends(get_db),
):
    cafs = (await db.execute(
        select(CAF).where(CAF.emisor_id == emisor.id, CAF.activo == True)
        .order_by(CAF.tipo_dte.asc())
    )).scalars().all()
    tipo_label = {33:"Factura", 34:"F.Exenta", 39:"Boleta",
                  41:"B.Exenta", 52:"Guía", 56:"N.Débito", 61:"N.Crédito"}
    return {
        "folios": [
            {"tipo": c.tipo_dte, "descripcion": tipo_label.get(c.tipo_dte, str(c.tipo_dte)),
             "desde": c.folio_desde, "hasta": c.folio_hasta,
             "actual": c.folio_actual, "disponibles": max(0, c.folio_hasta - c.folio_actual + 1)}
            for c in cafs
        ]
    }


# ══════════════════════════════════════════════════════════════
# STATELESS — FIRMAR Y ENVIAR
# El cliente trae su propio certificado (.p12) y CAF (XML).
# YeparDTEcore firma y envía al SII sin guardar nada en BD.
# ══════════════════════════════════════════════════════════════

import base64 as _b64
from datetime import date as _date

class EmisorStateless(BaseModel):
    rut:            str
    razon_social:   str
    giro:           str
    direccion:      str = ""
    comuna:         str = ""
    ciudad:         str = ""
    acteco:         str = ""
    telefono:       str = ""
    correo:         str = ""
    nro_resolucion: str = "0"
    fch_resolucion: str = "2000-01-01"

class ReceptorStateless(BaseModel):
    rut:       str = "66666666-6"
    nombre:    str = "Consumidor Final"
    giro:      Optional[str] = ""
    direccion: Optional[str] = ""
    comuna:    Optional[str] = ""
    ciudad:    Optional[str] = ""
    email:     Optional[str] = None

class ItemStateless(BaseModel):
    nombre:    str
    cantidad:  float = 1
    precio:    float
    exento:    bool = False
    descuento: float = 0
    unidad:    Optional[str] = None
    codigo:    Optional[str] = ""

class ReferenciaStateless(BaseModel):
    tipo_doc_ref: int = 801   # 801=SET, 33=Factura, 61=NC, etc
    folio_ref:    str = ""
    fecha_ref:    str = ""
    razon:        Optional[str] = None
    razon_ref:    Optional[str] = None
    cod_ref:      Optional[int] = None

class FirmarYEnviarInput(BaseModel):
    emisor:       EmisorStateless
    pfx_base64:   str
    pfx_password: str
    caf_base64:   str
    tipo:         int
    receptor:     ReceptorStateless
    items:        list[ItemStateless]
    exento:       bool = False
    fecha:        Optional[str] = None
    referencias:  list[ReferenciaStateless] = []
    ambiente:     str = "certificacion"
    auto_enviar:  bool = True


@router.post("/firmar-y-enviar")
async def firmar_y_enviar(datos: FirmarYEnviarInput):
    """
    Endpoint stateless — firma y envía un DTE al SII.
    El cliente provee su certificado (.p12) y CAF (XML) en base64.
    YeparDTEcore NO guarda nada en BD.
    """
    from app.services.xml_builder import (
        XMLBuilder, InputDTE, EmisorDTE, ReceptorDTE, ItemDTE, ReferenciaDTE
    )
    from app.services.xml_builder_boleta import (
        XMLBuilderBoleta, InputBoleta, EmisorBoleta, ReceptorBoleta,
        ItemBoleta, ReferenciaBoleta
    )
    from app.services.firma_digital import FirmaDigital

    TIPOS_BOLETA = {39, 41}
    tipos_validos = {33, 34, 39, 41, 52, 56, 61}

    if datos.tipo not in tipos_validos:
        raise HTTPException(422, f"Tipo DTE no válido: {datos.tipo}")

    try:
        pfx_bytes = _b64.b64decode(datos.pfx_base64)
    except Exception:
        raise HTTPException(400, "pfx_base64 inválido")
    try:
        caf_xml_bytes = _b64.b64decode(datos.caf_base64)
    except Exception:
        raise HTTPException(400, "caf_base64 inválido")

    fecha_str = datos.fecha or datetime.now().strftime("%Y-%m-%d")
    fecha_dt  = _date.fromisoformat(fecha_str)

    # ── Construir dataclasses de emisor/receptor/items ────────────────────────
    e = datos.emisor
    r = datos.receptor
    fecha_hoy = _date.today().isoformat()

    def parse_ref(ref):
        try:
            folio_ref_int = int(ref.folio_ref) if ref.folio_ref else 0
        except (ValueError, TypeError):
            folio_ref_int = 0
        try:
            fecha_ref_dt = _date.fromisoformat(ref.fecha_ref) if ref.fecha_ref else _date.today()
        except ValueError:
            fecha_ref_dt = _date.today()
        razon = ref.razon or ref.razon_ref or ""
        # tipo_doc_ref es siempre int (801=SET)
        try:
            tipo = int(ref.tipo_doc_ref)
        except (ValueError, TypeError):
            tipo = 801
        # Para XMLBuilder: 801 → "SET", resto → int
        tipo_builder = "SET" if tipo == 801 else tipo
        return folio_ref_int, fecha_ref_dt, razon, tipo_builder

    try:
        if datos.tipo in TIPOS_BOLETA:
            items_input = [
                ItemBoleta(
                    nombre          = it.nombre,
                    cantidad        = it.cantidad,
                    precio_unitario = it.precio,
                    descuento_pct   = it.descuento,
                    codigo          = it.codigo or "",
                    unidad          = it.unidad or "",
                    exento          = it.exento or datos.exento,
                )
                for it in datos.items
            ]
            refs_input = []
            for ref in datos.referencias:
                folio_r, fecha_r, razon_r, tipo_r = parse_ref(ref)
                refs_input.append(ReferenciaBoleta(
                    tipo_doc_ref = tipo_r,
                    folio_ref    = folio_r,
                    fecha_ref    = fecha_r,
                    razon_ref    = razon_r,
                ))
            input_dte = InputBoleta(
                tipo_dte      = datos.tipo,
                folio         = 0,  # se asigna al parsear el CAF
                fecha_emision = fecha_dt,
                emisor        = EmisorBoleta(
                    rut=e.rut, razon_social=e.razon_social, giro=e.giro,
                    direccion=e.direccion, comuna=e.comuna, ciudad=e.ciudad,
                    acteco=e.acteco, telefono=e.telefono, correo=e.correo,
                ),
                receptor      = ReceptorBoleta(
                    rut=r.rut, razon_social=r.nombre, correo=r.email or "",
                ),
                items         = items_input,
                referencias   = refs_input,
            )
            builder = XMLBuilderBoleta(input_dte)
        else:
            items_input = [
                ItemDTE(
                    nombre          = it.nombre,
                    cantidad        = it.cantidad,
                    precio_unitario = it.precio,
                    descuento_pct   = it.descuento,
                    codigo          = it.codigo or "",
                    unidad          = it.unidad or "",
                    exento          = it.exento or datos.exento,
                )
                for it in datos.items
            ]
            refs_input = []
            for ref in datos.referencias:
                folio_r, fecha_r, razon_r, tipo_r = parse_ref(ref)
                refs_input.append(ReferenciaDTE(
                    tipo_doc_ref = tipo_r,
                    folio_ref    = folio_r,
                    fecha_ref    = fecha_r,
                    razon_ref    = razon_r,
                    cod_ref      = ref.cod_ref or 0,
                ))
            input_dte = InputDTE(
                tipo_dte      = datos.tipo,
                folio         = 0,
                fecha_emision = fecha_dt,
                emisor        = EmisorDTE(
                    rut=e.rut, razon_social=e.razon_social, giro=e.giro,
                    direccion=e.direccion, comuna=e.comuna, ciudad=e.ciudad,
                    acteco=e.acteco, telefono=e.telefono, correo=e.correo,
                ),
                receptor      = ReceptorDTE(
                    rut=r.rut, razon_social=r.nombre, giro=r.giro or "",
                    direccion=r.direccion or "", comuna=r.comuna or "",
                    ciudad=r.ciudad or "", correo=r.email or "",
                ),
                items         = items_input,
                referencias   = refs_input,
                ambiente      = datos.ambiente,
            )
            builder = XMLBuilder(input_dte)

    except Exception as ex:
        logger.error(f"[STATELESS] Error construyendo input: {ex}", exc_info=True)
        raise HTTPException(500, f"Error construyendo DTE: {ex}")

    # ── Extraer folio del CAF ─────────────────────────────────────────────────
    try:
        from lxml import etree as _etree
        caf_el    = _etree.fromstring(caf_xml_bytes)
        folio     = int(caf_el.findtext(".//D") or caf_el.findtext(".//DESDE") or 1)
        # Actualizar folio en input_dte
        input_dte.folio = folio
        if datos.tipo in TIPOS_BOLETA:
            builder = XMLBuilderBoleta(input_dte)
        else:
            builder = XMLBuilder(input_dte)
    except Exception as ex:
        raise HTTPException(400, f"Error parseando CAF: {ex}")

    # ── Construir y firmar XML ────────────────────────────────────────────────
    try:
        xml_sin_firma = builder.construir()
        monto_total   = builder.monto_total

        it1 = input_dte.items[0].nombre if input_dte.items else "PRODUCTO"

        firma = FirmaDigital(pfx_bytes, datos.pfx_password, ambiente=datos.ambiente)
        # firmar_dte espera xml_caf como str, no bytes
        caf_xml_str = caf_xml_bytes.decode("utf-8") if isinstance(caf_xml_bytes, bytes) else caf_xml_bytes

        xml_firmado_bytes = await firma.firmar_dte(
            xml_bytes     = xml_sin_firma,
            folio         = folio,
            tipo_dte      = datos.tipo,
            xml_caf       = caf_xml_str,
            fecha_emision = fecha_str,
            rut_emisor    = e.rut,
            monto_total   = monto_total,
            it1_nombre    = it1,
        )
        xml_firmado = xml_firmado_bytes.decode("ISO-8859-1")

    except Exception as ex:
        logger.error(f"[STATELESS] Error firmando: {ex}", exc_info=True)
        raise HTTPException(500, f"Error firmando DTE: {ex}")

    # ── Enviar al SII ─────────────────────────────────────────────────────────
    track_id   = None
    estado_sii = "FIRMADO"

    if datos.auto_enviar:
        try:
            sender = SIISender(
                ambiente  = datos.ambiente,
                nro_resol = e.nro_resolucion,
                fch_resol = e.fch_resolucion,
            )
            rut_firmante = getattr(firma, "rut_certificado", None) or e.rut
            sobre_xml = await sender.construir_sobre(
                dtes_xml     = [xml_firmado],
                rut_emisor   = e.rut,
                rut_enviador = rut_firmante,
                firma_service= firma,
            )
            resultado = await sender.enviar_sobre(
                sobre_xml      = sobre_xml,
                rut_emisor     = e.rut,
                rut_enviador   = rut_firmante,
                p12_bytes      = pfx_bytes,
                password       = datos.pfx_password,
                auth_p12_bytes = pfx_bytes,
                auth_password  = datos.pfx_password,
            )
            track_id   = resultado.get("track_id")
            estado_sii = resultado.get("estado", "ENVIADO")
            logger.info(f"[STATELESS] track_id={track_id} estado={estado_sii}")
        except Exception as ex:
            logger.error(f"[STATELESS] Error enviando: {ex}", exc_info=True)
            estado_sii = "ERROR_ENVIO"

    tipo_label = {
        33:"Factura Electrónica", 34:"Factura Exenta",
        39:"Boleta Electrónica",  41:"Boleta Exenta",
        52:"Guía de Despacho",    56:"Nota de Débito", 61:"Nota de Crédito",
    }
    return {
        "ok":          True,
        "tipo":        tipo_label.get(datos.tipo, str(datos.tipo)),
        "folio":       folio,
        "xml_firmado": xml_firmado,
        "track_id":    track_id,
        "estado":      estado_sii,
        "ambiente":    datos.ambiente,
        "fecha":       fecha_str,
    }



# ══════════════════════════════════════════════════════════════
# GENERAR SET COMPLETO — stateless
# Recibe todos los casos, pfx y CAF del cliente.
# Genera cada DTE timbrado y devuelve el EnvioBOLETA/EnvioDTE
# firmado listo para subir al SII.
# ══════════════════════════════════════════════════════════════

class ItemSetInput(BaseModel):
    nombre:         str
    cantidad:       float = 1.0
    precio_con_iva: float = 0.0
    precio_neto:    float = 0.0   # si viene de factura, ya viene neto
    exento:         bool  = False
    unidad:         str   = ""
    codigo:         str   = ""
    descuento:      float = 0.0

class CasoSetInput(BaseModel):
    numero_caso:     int
    tipo_dte:        int = 39
    items:           list[ItemSetInput]
    rut_receptor:    str = "66666666-6"
    nombre_receptor: str = "Consumidor Final"
    observacion:     str = ""

class GenerarSetInput(BaseModel):
    emisor:         EmisorStateless
    pfx_base64:     str
    pfx_password:   str
    caf_base64:     str            # CAF del tipo principal del set
    casos:          list[CasoSetInput]
    natencion:      str = "SET"
    fecha:          Optional[str] = None
    ambiente:       str = "certificacion"
    auto_enviar:    bool = False   # True = enviar al SII, False = solo descargar


@router.post("/generar-set")
async def generar_set(datos: GenerarSetInput):
    """
    Stateless: genera el EnvioBOLETA/EnvioDTE completo con todos los casos.
    El cliente provee su pfx y CAF. YeparDTEcore firma y arma el sobre.
    """
    from app.services.xml_builder_boleta import (
        XMLBuilderBoleta, InputBoleta, EmisorBoleta,
        ReceptorBoleta, ItemBoleta, ReferenciaBoleta,
    )
    from app.services.xml_builder import (
        XMLBuilder, InputDTE, EmisorDTE, ReceptorDTE, ItemDTE, ReferenciaDTE,
    )
    from app.services.firma_digital import FirmaDigital
    from lxml import etree as _etree
    from datetime import date as _date
    import re as _re

    TIPOS_BOLETA = {39, 41}

    if not datos.casos:
        raise HTTPException(400, "No hay casos para generar")

    try:
        pfx_bytes = _b64.b64decode(datos.pfx_base64)
    except Exception:
        raise HTTPException(400, "pfx_base64 inválido")
    try:
        caf_xml_bytes = _b64.b64decode(datos.caf_base64)
        caf_xml_str   = caf_xml_bytes.decode("utf-8")
    except Exception:
        raise HTTPException(400, "caf_base64 inválido")

    # Extraer folio inicial del CAF
    try:
        caf_el      = _etree.fromstring(caf_xml_bytes)
        folio_desde = int(caf_el.findtext(".//D") or caf_el.findtext(".//DESDE") or 1)
        folio_hasta = int(caf_el.findtext(".//H") or caf_el.findtext(".//HASTA") or folio_desde)
    except Exception as e:
        raise HTTPException(400, f"Error parseando CAF: {e}")

    if len(datos.casos) > (folio_hasta - folio_desde + 1):
        raise HTTPException(400, f"CAF insuficiente: tiene {folio_hasta - folio_desde + 1} folios pero hay {len(datos.casos)} casos")

    fecha_str = datos.fecha or datetime.now().strftime("%Y-%m-%d")
    fecha_dt  = _date.fromisoformat(fecha_str)
    e         = datos.emisor

    firma = FirmaDigital(pfx_bytes, datos.pfx_password, ambiente=datos.ambiente)

    emisor_b = EmisorBoleta(
        rut=e.rut, razon_social=e.razon_social, giro=e.giro,
        direccion=e.direccion, comuna=e.comuna, ciudad=e.ciudad,
        acteco=getattr(e, "acteco", "") or "",
        telefono=getattr(e, "telefono", "") or "",
        correo=getattr(e, "correo", "") or "",
    )

    xmls_timbrados = []

    for i, caso in enumerate(datos.casos):
        folio = folio_desde + i

        tipo_dte = caso.tipo_dte
        es_boleta = tipo_dte in TIPOS_BOLETA

        rut_recep = caso.rut_receptor or "66666666-6"
        nom_recep = caso.nombre_receptor or "Consumidor Final"

        if es_boleta:
            items_b = []
            for it in caso.items:
                # Para boletas el precio viene CON IVA, convertir a neto
                # XMLBuilderBoleta espera precio_unitario = precio CON IVA (bruto)
                # El builder internamente divide por 1.19 para obtener MntNeto
                precio_bruto = it.precio_con_iva if it.precio_con_iva else round((it.precio_neto or 0) * 1.19)
                items_b.append(ItemBoleta(
                    nombre=it.nombre, cantidad=it.cantidad,
                    precio_unitario=precio_bruto,
                    exento=it.exento, unidad=it.unidad, codigo=it.codigo,
                    descuento_pct=it.descuento,
                ))
            refs = [ReferenciaBoleta(
                tipo_doc_ref="SET", folio_ref=folio,
                fecha_ref=fecha_dt, razon_ref=f"CASO-{caso.numero_caso}",
            )]
            input_obj = InputBoleta(
                tipo_dte=tipo_dte, folio=folio, fecha_emision=fecha_dt,
                emisor=emisor_b,
                receptor=ReceptorBoleta(rut=rut_recep, razon_social=nom_recep),
                items=items_b, referencias=refs,
                observacion=caso.observacion,
            )
            xml_bytes = XMLBuilderBoleta(input_obj).construir()
        else:
            emisor_dte = EmisorDTE(
                rut=e.rut, razon_social=e.razon_social, giro=e.giro,
                direccion=e.direccion, comuna=e.comuna, ciudad=e.ciudad,
                acteco=getattr(e, "acteco", "") or "",
                telefono=getattr(e, "telefono", "") or "",
                correo=getattr(e, "correo", "") or "",
            )
            items_d = []
            for it in caso.items:
                items_d.append(ItemDTE(
                    nombre=it.nombre, cantidad=it.cantidad,
                    precio_unitario=it.precio_neto or round(it.precio_con_iva / 1.19),
                    exento=it.exento, unidad=it.unidad, codigo=it.codigo,
                    descuento_pct=it.descuento,
                ))
            refs = [ReferenciaDTE(
                tipo_doc_ref="SET", folio_ref=folio,
                fecha_ref=fecha_dt, razon_ref=f"CASO-{caso.numero_caso}",
                cod_ref=0,
            )]
            input_obj = InputDTE(
                tipo_dte=tipo_dte, folio=folio, fecha_emision=fecha_dt,
                emisor=emisor_dte,
                receptor=ReceptorDTE(
                    rut=rut_recep, razon_social=nom_recep,
                    giro="", direccion="", comuna="", ciudad="", correo="",
                ),
                items=items_d, referencias=refs, ambiente=datos.ambiente,
            )
            xml_bytes = XMLBuilder(input_obj).construir()

        # Extraer monto total del XML generado
        xml_str = xml_bytes.decode("ISO-8859-1")
        m = _re.search(r"<MntTotal>(\d+)</MntTotal>", xml_str)
        monto_total = int(m.group(1)) if m else 0
        it1 = caso.items[0].nombre if caso.items else "PRODUCTO"

        try:
            xml_timbrado_bytes = await firma.firmar_dte(
                xml_bytes=xml_bytes, folio=folio, tipo_dte=tipo_dte,
                xml_caf=caf_xml_str, fecha_emision=fecha_str,
                rut_emisor=e.rut, monto_total=monto_total, it1_nombre=it1,
            )
        except Exception as ex:
            logger.error(f"[SET] Error timbrando caso {caso.numero_caso} folio {folio}: {ex}", exc_info=True)
            raise HTTPException(500, f"Error timbrando caso {caso.numero_caso}: {ex}")

        xmls_timbrados.append(xml_timbrado_bytes.decode("ISO-8859-1"))
        logger.info(f"[SET] Caso {caso.numero_caso} folio {folio} timbrado OK")

    # Armar EnvioBOLETA/EnvioDTE con construir_sobre
    sender       = SIISender(
        ambiente  = datos.ambiente,
        nro_resol = e.nro_resolucion,
        fch_resol = e.fch_resolucion,
    )
    rut_firmante = getattr(firma, "rut_certificado", None) or e.rut

    try:
        sobre_firmado = await sender.construir_sobre(
            dtes_xml     = xmls_timbrados,
            rut_emisor   = e.rut,
            rut_enviador = rut_firmante,
            firma_service= firma,
        )
    except Exception as ex:
        logger.error(f"[SET] Error construyendo sobre: {ex}", exc_info=True)
        raise HTTPException(500, f"Error armando sobre: {ex}")

    if not datos.auto_enviar:
        import base64 as _b64e
        # Devolver también en base64 para preservar encoding ISO-8859-1
        sobre_b64 = _b64e.b64encode(sobre_firmado.encode('ISO-8859-1')).decode('ascii')
        return {
            "ok":           True,
            "sobre_xml":    sobre_firmado,
            "sobre_xml_b64": sobre_b64,
            "n_casos":      len(datos.casos),
            "folio_desde":  folio_desde,
            "folio_hasta":  folio_desde + len(datos.casos) - 1,
        }

    # Enviar al SII
    try:
        resultado = await sender.enviar_sobre(
            sobre_xml      = sobre_firmado,
            rut_emisor     = e.rut,
            rut_enviador   = rut_firmante,
            p12_bytes      = pfx_bytes,
            password       = datos.pfx_password,
            auth_p12_bytes = pfx_bytes,
            auth_password  = datos.pfx_password,
        )
        return {
            "ok":          True,
            "track_id":    resultado.get("track_id"),
            "estado":      resultado.get("estado", "ENVIADO"),
            "sobre_xml":   sobre_firmado,
            "n_casos":     len(datos.casos),
            "folio_desde": folio_desde,
            "folio_hasta": folio_desde + len(datos.casos) - 1,
        }
    except Exception as ex:
        logger.error(f"[SET] Error enviando: {ex}", exc_info=True)
        raise HTTPException(500, f"Error enviando al SII: {ex}")


# ══════════════════════════════════════════════════════════════
# ENVIAR SOBRE — recibe XML ya generado y lo envía al SII
# El cliente generó el sobre previamente con /generar-set.
# Este endpoint solo autentica con el certificado del cliente
# y envía — no genera ni consume CAFs.
# ══════════════════════════════════════════════════════════════

class EnviarSobreInput(BaseModel):
    xml_sobre_b64: str      # EnvioBOLETA o EnvioDTE en base64
    rut_emisor:   str
    pfx_base64:   str
    pfx_password: str
    ambiente:     str = "certificacion"


@router.post("/enviar-sobre")
async def enviar_sobre_directo(datos: EnviarSobreInput):
    """
    Recibe un sobre XML ya firmado y lo envía al SII.
    No genera ni consume CAFs — solo autentica y envía.
    Detecta automáticamente si es boleta (usa token maullin2)
    o DTE (usa token maullin).
    """
    from app.services.firma_digital import FirmaDigital

    try:
        pfx_bytes  = _b64.b64decode(datos.pfx_base64)
        sobre_xml  = _b64.b64decode(datos.xml_sobre_b64).decode("ISO-8859-1")
    except Exception as ex:
        raise HTTPException(400, f"Error decodificando datos: {ex}")

    # Extraer rut del firmante desde el certificado
    try:
        firma        = FirmaDigital(pfx_bytes, datos.pfx_password, ambiente=datos.ambiente)
        rut_firmante = getattr(firma, "rut_certificado", None) or datos.rut_emisor
    except Exception as ex:
        raise HTTPException(400, f"Error leyendo certificado: {ex}")

    sender = SIISender(ambiente=datos.ambiente)

    try:
        resultado = await sender.enviar_sobre(
            sobre_xml      = sobre_xml,
            rut_emisor     = datos.rut_emisor,
            rut_enviador   = rut_firmante,
            p12_bytes      = pfx_bytes,
            password       = datos.pfx_password,
            auth_p12_bytes = pfx_bytes,
            auth_password  = datos.pfx_password,
        )
    except Exception as ex:
        logger.error(f"[ENVIAR-SOBRE] Error: {ex}", exc_info=True)
        raise HTTPException(500, f"Error enviando al SII: {ex}")

    logger.info(f"[ENVIAR-SOBRE] track_id={resultado.get('track_id')} estado={resultado.get('estado')}")

    return {
        "ok":       resultado.get("track_id") is not None,
        "track_id": resultado.get("track_id"),
        "estado":   resultado.get("estado", "ENVIADO"),
        "mensaje":  resultado.get("mensaje", ""),
    }
