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
    track_id:   str,
    rut_emisor: Optional[str] = None,   # RUT de la EMPRESA emisora del DTE (cliente de YeparDTE)
    tipo:       Optional[int] = None,   # tipo de DTE: 39/41 → ventanilla boletas; resto → QueryEstUp
    ambiente:   Optional[str] = None,   # "certificacion" | "produccion" — si no viene, usa el del emisor API
    emisor:     Emisor = Depends(get_emisor_by_api_key),
    db:         AsyncSession = Depends(get_db),
):
    """
    Consulta al SII el estado de un envío por su track_id.

    Analogía: el track_id es el número de seguimiento de una carta
    certificada — el SII nunca avisa solo, hay que ir a la ventanilla
    a preguntar. Este endpoint ES esa visita.

    Autenticación al SII: usa el certificado del emisor dueño de la
    API Key (Yepar Solutions, que tiene el e-Sign registrado en el SII),
    NO el certificado de la empresa cliente — exactamente el mismo
    principio que /v1/enviar-sobre/directo.
    """
    from app.services.sii_status import SIIStatusChecker, TIPOS_BOLETA
    from app.services.sii_auth import (
        obtener_token_cached, obtener_token_boleta_cached,
    )

    # Sanitizar entradas: espacios colados al copiar/pegar y RUT con puntos
    track_id = str(track_id).strip()
    if rut_emisor:
        rut_emisor = _norm_rut(rut_emisor)

    # ── Certificado de autenticación (el de Yepar, registrado en SII) ────────
    cert = (await db.execute(
        select(Certificado).where(Certificado.emisor_id == emisor.id,
                                   Certificado.activo == True).limit(1)
    )).scalar_one_or_none()
    if not cert:
        raise HTTPException(400, "No hay certificado configurado")

    # Preferir el certificado e-Sign de autenticación; si no existe, el de firma
    auth_p12 = bytes(cert.certificado_auth_p12) if cert.certificado_auth_p12 \
               else bytes(cert.certificado_p12)
    auth_pwd = cert.certificado_auth_password if cert.certificado_auth_p12 \
               else cert.certificado_password

    ambiente_q = ambiente or emisor.ambiente or "certificacion"
    rut_query  = rut_emisor or emisor.rut

    # ── ¿Ventanilla boletas o ventanilla DTE? ─────────────────────────────────
    # Detector principal: el tipo. De respaldo: los track_id de boleta
    # tienen 15 dígitos; los de DTE clásico, 10.
    es_boleta = (tipo in TIPOS_BOLETA) if tipo is not None \
                else len(str(track_id).strip()) >= 15

    checker = SIIStatusChecker(ambiente=ambiente_q)

    try:
        if es_boleta:
            # Token de boletas — reutiliza el persistido en BD si maullin2
            # no es alcanzable directamente desde el servidor
            token = await obtener_token_boleta_cached(
                auth_p12, auth_pwd, ambiente_q,
                db=db, emisor_id=emisor.id,
            )
            resultado = await checker.consultar_envio_boleta(
                rut_emisor=rut_query, track_id=track_id, token_boleta=token,
            )
        else:
            # Token DTE estándar (maullin/palena)
            token = await obtener_token_cached(auth_p12, auth_pwd, ambiente_q)
            resultado = await checker.consultar_envio_dte(
                rut_emisor=rut_query, track_id=track_id, token=token,
            )
    except Exception as e:
        logger.error(f"[ESTADO] Error consultando SII track={track_id}: {e}",
                     exc_info=True)
        raise HTTPException(500, f"Error consultando SII: {e}")

    return {
        "track_id":   track_id,
        "rut_emisor": rut_query,
        "ambiente":   ambiente_q,
        "es_boleta":  es_boleta,
        "estado":     resultado.get("estado"),
        "codigo_sii": resultado.get("codigo_sii"),
        "glosa":      resultado.get("glosa"),
        "informados": resultado.get("informados"),
        "aceptados":  resultado.get("aceptados"),
        "rechazados": resultado.get("rechazados"),
        "reparos":    resultado.get("reparos"),
        "detalle":    resultado.get("detalle"),
    }


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

def _norm_rut(rut: str) -> str:
    """
    Normaliza un RUT al formato que exige el esquema del SII: sin puntos,
    con guion, dígito verificador en mayúscula. '78.377.021-0' → '78377021-0'.

    Analogía: el SII es un portero con lista estricta — si tu nombre está
    escrito con adornos, no te encuentra. Aquí le quitamos los adornos
    a TODOS los RUT antes de que entren al XML.
    """
    if not rut:
        return rut
    limpio = rut.replace(".", "").replace(" ", "").strip().upper()
    # Asegurar el guion si vino sin él (raro, pero defensivo)
    if "-" not in limpio and len(limpio) > 1:
        limpio = f"{limpio[:-1]}-{limpio[-1]}"
    return limpio


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
    folio_actual: Optional[int] = None  # Folio a usar (el contador lo lleva
                                        # el cliente). Si no viene, se usa el
                                        # inicio del rango del CAF. El CAF
                                        # jamás se modifica: es la chequera
                                        # firmada por el banco, esto solo
                                        # indica por cuál cheque vamos.


@router.post("/firmar-y-enviar")
async def firmar_y_enviar(
    datos:      FirmarYEnviarInput,
    emisor_api: Emisor = Depends(get_emisor_by_api_key),
    db:         AsyncSession = Depends(get_db),
):
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
    # Normalizar RUTs en la puerta — el esquema del SII no perdona puntos
    e.rut = _norm_rut(e.rut)
    r.rut = _norm_rut(r.rut)
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

    # ── Determinar folio: el contador del cliente, validado contra el CAF ────
    try:
        from lxml import etree as _etree
        caf_el      = _etree.fromstring(caf_xml_bytes)
        folio_desde = int(caf_el.findtext(".//D") or caf_el.findtext(".//DESDE") or 1)
        folio_hasta = int(caf_el.findtext(".//H") or caf_el.findtext(".//HASTA") or folio_desde)

        # El cliente lleva el contador (folio_actual); el CAF firmado por el
        # SII define el rango permitido. Validamos que el contador esté
        # dentro de la chequera — nunca reimprimimos la chequera.
        folio = datos.folio_actual or folio_desde
        if folio < folio_desde or folio > folio_hasta:
            raise HTTPException(400,
                f"Folio {folio} fuera del rango del CAF ({folio_desde}-{folio_hasta})")

        input_dte.folio = folio
        if datos.tipo in TIPOS_BOLETA:
            builder = XMLBuilderBoleta(input_dte)
        else:
            builder = XMLBuilder(input_dte)
    except HTTPException:
        raise
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
            # ── FIX AUTENTICACIÓN SII ─────────────────────────────────────────
            # El .p12 del CLIENTE firma los DTEs, pero NO sirve para
            # autenticarse ante el SII (no está registrado como enviador
            # → el SII devuelve xsi:nil en la semilla).
            #
            # Analogía: el cliente firma sus propias cartas, pero quien
            # las lleva a Correos y se identifica en el mostrador es el
            # cartero acreditado (el e-Sign de Yepar, registrado en SII).
            # Mismo principio que /v1/enviar-sobre/directo.
            cert_yepar = (await db.execute(
                select(Certificado).where(
                    Certificado.emisor_id == emisor_api.id,
                    Certificado.activo == True,
                ).limit(1)
            )).scalar_one_or_none()

            if cert_yepar and cert_yepar.certificado_auth_p12:
                auth_p12 = bytes(cert_yepar.certificado_auth_p12)
                auth_pwd = cert_yepar.certificado_auth_password
                rut_enviador = cert_yepar.rut_firmante or \
                               getattr(firma, "rut_certificado", None) or e.rut
                # El SOBRE lo firma el enviador acreditado (Yepar) para que
                # la firma del EnvioDTE/EnvioBOLETA coincida con RutEnvia.
                # El DTE interno conserva la firma del cliente (su .p12).
                from app.services.firma_digital import FirmaDigital as _FD
                firma_sobre = _FD(
                    bytes(cert_yepar.certificado_p12),
                    cert_yepar.certificado_password,
                    ambiente=datos.ambiente,
                ) if cert_yepar.certificado_p12 else firma
            else:
                # Sin cert auth configurado: fallback al p12 del cliente
                # (comportamiento anterior — solo funciona si ese RUT
                # está autorizado como enviador en el SII)
                auth_p12 = pfx_bytes
                auth_pwd = datos.pfx_password
                rut_enviador = getattr(firma, "rut_certificado", None) or e.rut
                firma_sobre  = firma

            # El RutEnvia de la carátula debe coincidir con quien se
            # autentica ante el SII (el enviador acreditado)
            sobre_xml = await sender.construir_sobre(
                dtes_xml     = [xml_firmado],
                rut_emisor   = e.rut,
                rut_enviador = rut_enviador,
                firma_service= firma_sobre,
            )
            resultado = await sender.enviar_sobre(
                sobre_xml      = sobre_xml,
                rut_emisor     = e.rut,
                rut_enviador   = rut_enviador,
                p12_bytes      = pfx_bytes,
                password       = datos.pfx_password,
                auth_p12_bytes = auth_p12,
                auth_password  = auth_pwd,
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
    caf_base64:     str            # CAF del tipo principal del set — VERBATIM,
                                   # tal como lo firmó el SII (jamás modificarlo)
    casos:          list[CasoSetInput]
    natencion:      str = "SET"
    fecha:          Optional[str] = None
    ambiente:       str = "certificacion"
    auto_enviar:    bool = False   # True = enviar al SII, False = solo descargar
    folio_inicio:   Optional[int] = None  # Primer folio a usar. Si no viene,
                                          # se usa el inicio del rango del CAF.
                                          # Así el cliente controla el contador
                                          # SIN tocar el CAF firmado.


@router.post("/generar-set")
async def generar_set(datos: GenerarSetInput, db: AsyncSession = Depends(get_db)):
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

    # ── Folio inicial: parámetro explícito, validado contra el CAF real ──────
    # El CAF dice "estás autorizado del folio D al H" (firmado por el SII).
    # El cliente nos dice DESDE dónde de ese rango quiere partir (su contador).
    # Analogía: el CAF es la chequera autorizada por el banco; folio_inicio
    # es por cuál cheque vas — se hojea la chequera, NUNCA se reimprime.
    folio_base = datos.folio_inicio or folio_desde
    if folio_base < folio_desde:
        raise HTTPException(400,
            f"folio_inicio {folio_base} es menor al inicio del CAF ({folio_desde})")
    if folio_base + len(datos.casos) - 1 > folio_hasta:
        disponibles = folio_hasta - folio_base + 1
        raise HTTPException(400,
            f"CAF insuficiente: desde el folio {folio_base} quedan {disponibles} "
            f"folios pero hay {len(datos.casos)} casos")

    fecha_str = datos.fecha or datetime.now().strftime("%Y-%m-%d")
    fecha_dt  = _date.fromisoformat(fecha_str)
    e         = datos.emisor
    e.rut     = _norm_rut(e.rut)  # sin puntos: el esquema del SII es estricto

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
        folio = folio_base + i   # hojear la chequera desde donde va el contador

        tipo_dte = caso.tipo_dte
        es_boleta = tipo_dte in TIPOS_BOLETA

        rut_recep = _norm_rut(caso.rut_receptor or "66666666-6")
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
        # ── Guardarropa: colgar el sobre ORIGINAL y entregar el ticket ────────
        # Los bytes ISO-8859-1 que se guardan aquí son EXACTAMENTE los que
        # subirán al SII cuando llegue el ticket de vuelta. El sobre_xml del
        # response es solo una copia de cortesía para visualizar/descargar.
        from app.services import sobre_store
        sobre_id = sobre_store.guardar(
            sobre_firmado.encode("ISO-8859-1"),
            emisor_rut=e.rut,
        )
        return {
            "ok":          True,
            "sobre_id":    sobre_id,
            "sobre_xml":   sobre_firmado,
            "n_casos":     len(datos.casos),
            "folio_desde": folio_base,
            "folio_hasta": folio_base + len(datos.casos) - 1,
        }

    # Enviar al SII — usar auth_p12 de BD para autenticarse (certificado registrado)
    # El pfx del cliente firma el XML pero el auth_p12 de BD obtiene el token SII
    auth_p12 = pfx_bytes
    auth_pwd  = datos.pfx_password
    try:
        cert_auth = (await db.execute(
            select(Certificado).where(
                Certificado.activo == True,
                Certificado.certificado_auth_p12 != None,
            ).limit(1)
        )).scalar_one_or_none()
        if cert_auth and cert_auth.certificado_auth_p12:
            auth_p12 = bytes(cert_auth.certificado_auth_p12)
            auth_pwd  = cert_auth.certificado_auth_password or datos.pfx_password
    except Exception as _ex:
        logger.warning(f"[SET] No se pudo cargar auth_p12 de BD: {_ex}")

    try:
        resultado = await sender.enviar_sobre(
            sobre_xml      = sobre_firmado,
            rut_emisor     = e.rut,
            rut_enviador   = rut_firmante,
            p12_bytes      = pfx_bytes,
            password       = datos.pfx_password,
            auth_p12_bytes = auth_p12,
            auth_password  = auth_pwd,
        )
        estado   = resultado.get("estado", "ENVIADO")
        track_id = resultado.get("track_id")
        mensaje  = resultado.get("mensaje", "")
        logger.info(f"[SET] Resultado SII: estado={estado} track_id={track_id} mensaje={mensaje}")
        return {
            "ok":          track_id is not None,
            "track_id":    track_id,
            "estado":      estado,
            "mensaje":     mensaje,
            "sobre_xml":   sobre_firmado,
            "n_casos":     len(datos.casos),
            "folio_desde": folio_base,
            "folio_hasta": folio_base + len(datos.casos) - 1,
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
