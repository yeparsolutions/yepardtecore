# app/api/public/router.py
# ══════════════════════════════════════════════════════════════
# API Pública para Desarrolladores — Opción A
# Autenticación: API Key en header X-API-Key
# Prefix: /api
# ══════════════════════════════════════════════════════════════

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, field_validator
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


def _fix_mojibake(texto):
    """
    Repara texto doble-codificado (mojibake). Cuando bytes UTF-8 se leen como
    Latin-1 en algún punto del transporte, "ó" (UTF-8: C3 B3) aparece como
    "Ã³" (dos caracteres). Esto re-codifica a Latin-1 y decodifica como UTF-8,
    recuperando el carácter original. Se aplica en la ENTRADA, antes de
    construir cualquier XML o TED, para que el texto correcto fluya a todo.
    Analogía: deshace una mala traducción de ida y vuelta y recupera la
    palabra original.
    """
    if not isinstance(texto, str):
        return texto
    if 'Ã' not in texto and 'Â' not in texto:
        return texto  # sin señales de doble-codificación
    try:
        return texto.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return texto  # si no se puede reparar limpiamente, dejar como está


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
    # Verificar en vivo qué fixes están realmente cargados en este deploy.
    # Esto permite confirmar desde el navegador si el código nuevo está activo
    # sin tener que generar un set completo. Si un check da False, ese archivo
    # no se desplegó.
    checks = {}
    try:
        # Fix de codificación: ¿xml_builder tiene la reparación de mojibake?
        from app.services import xml_builder as _xb
        checks["fix_mojibake_builder"] = hasattr(_xb, "_reparar_mojibake")
        # Prueba real: reparar "CajÃ³n" debe dar "Cajón"
        if checks["fix_mojibake_builder"]:
            prueba = _xb._reparar_mojibake("Caj\u00c3\u00b3n")
            checks["mojibake_funciona"] = (prueba == "Caj\u00f3n")
    except Exception as ex:
        checks["fix_mojibake_builder"] = f"error: {ex}"
    try:
        # Fix de folios por tipo: ¿el modelo acepta cafs_por_tipo?
        checks["fix_cafs_por_tipo"] = "cafs_por_tipo" in GenerarSetInput.model_fields
    except Exception as ex:
        checks["fix_cafs_por_tipo"] = f"error: {ex}"
    return {"ok": True, "servicio": "YeparDTEcore", "version": "1.2",
            "fixes": checks,
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

    fecha = datos.fecha or _hoy_chile()
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
        # Cuerpo crudo del SII solo cuando algo falla — diagnóstico a la vista
        "raw":        resultado.get("raw") if resultado.get("estado") in ("ERROR", "DESCONOCIDO") else None,
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

def _hoy_chile() -> str:
    """
    Fecha de HOY en Chile (America/Santiago), formato YYYY-MM-DD.

    El servidor corre en UTC: después de las ~20:00 hora chilena, su
    calendario ya marca "mañana". Una boleta emitida a las 20:09 en
    Santiago nacía fechada al día siguiente — el SII no acepta
    documentos del futuro y el consumo de folios diario se desordena.
    El reloj que manda es el de Chile, donde ocurre la venta.
    """
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dtn
    return _dtn.now(ZoneInfo("America/Santiago")).strftime("%Y-%m-%d")


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

    fecha_str = datos.fecha or _hoy_chile()
    fecha_dt  = _date.fromisoformat(fecha_str)

    # ── Construir dataclasses de emisor/receptor/items ────────────────────────
    e = datos.emisor
    r = datos.receptor
    # Normalizar RUTs en la puerta — el esquema del SII no perdona puntos
    e.rut = _norm_rut(e.rut)
    r.rut = _norm_rut(r.rut)
    fecha_hoy = _hoy_chile()

    def parse_ref(ref):
        try:
            folio_ref_int = int(ref.folio_ref) if ref.folio_ref else 0
        except (ValueError, TypeError):
            folio_ref_int = 0
        try:
            fecha_ref_dt = _date.fromisoformat(ref.fecha_ref) if ref.fecha_ref else _date.fromisoformat(_hoy_chile())
        except ValueError:
            fecha_ref_dt = _date.fromisoformat(_hoy_chile())
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
        # ── Corregir FolioRef de las referencias al SET ───────────────────────
        # El cliente no conoce el folio antes de que se asigne aquí, así que
        # manda la referencia al SET con folio_ref=1 como marcador. El SII
        # espera que la referencia al SET use el FOLIO REAL del documento.
        # Analogía: la referencia al set es como anotar "este es mi documento
        # n° 141 del set"; no tendría sentido que dijera "n° 1" si el folio
        # asignado fue 141.
        for _ref in input_dte.referencias:
            if str(_ref.tipo_doc_ref).upper() == "SET" and (not _ref.folio_ref or _ref.folio_ref in (0, 1)):
                _ref.folio_ref = folio
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
            # ── Rescatar el DTE FIRMADO desde el sobre ───────────────────────
            # firmar_dte solo TIMBRA (TED); la firma XMLDSig la aplica el
            # motor Java al firmar el sobre completo. El DTE verdaderamente
            # firmado (con <Signature>) vive DENTRO del sobre firmado — lo
            # extraemos de ahí para que el cliente guarde la versión
            # notariada, no la fotocopia sin firma.
            import re as _re_dte
            m_dte = _re_dte.search(r"<DTE[\s>].*?</DTE>", sobre_xml, _re_dte.DOTALL)
            if m_dte:
                dte_extraido = m_dte.group(0)
                # Dentro del sobre, el DTE hereda el namespace del padre;
                # al extraerlo standalone hay que declarárselo explícito
                if "xmlns=" not in dte_extraido[:80]:
                    dte_extraido = dte_extraido.replace(
                        "<DTE ", '<DTE xmlns="http://www.sii.cl/SiiDte" ', 1)
                xml_firmado = ('<?xml version="1.0" encoding="ISO-8859-1"?>\n'
                               + dte_extraido)

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

    # Reparar doble-codificación del nombre apenas llega, antes de armar
    # el XML o el TED, para que los acentos lleguen correctos al SII.
    @field_validator("nombre")
    @classmethod
    def _reparar_nombre(cls, v):
        return _fix_mojibake(v)

class CasoSetInput(BaseModel):
    numero_caso:     int
    tipo_dte:        int = 39
    items:           list[ItemSetInput]
    rut_receptor:    str = "66666666-6"
    nombre_receptor: str = "Consumidor Final"
    # Datos completos del receptor (para facturas/guías/notas — el SII repara
    # si falta el giro). Para boletas se ignoran.
    receptor:        Optional[dict] = None
    observacion:     str = ""
    # Referencia al documento que esta NC/ND corrige. Sin ella, el SII
    # rechaza por esquema las notas de crédito/débito.
    #   caso_ref: número de caso referido (ej. "4841543-1")
    #   razon:    motivo (ej. "CORRIGE GIRO DEL RECEPTOR")
    #   tipo_doc_ref: tipo del documento referido (33, 61, etc.)
    referencia:      Optional[dict] = None

class GenerarSetInput(BaseModel):
    emisor:         EmisorStateless
    pfx_base64:     str
    pfx_password:   str
    caf_base64:     str            # CAF del tipo principal del set — VERBATIM,
                                   # tal como lo firmó el SII (jamás modificarlo)
    # CAFs adicionales por tipo de DTE, para sets que mezclan tipos (ej. el set
    # básico tiene facturas=33, notas de crédito=61 y notas de débito=56, cada
    # una con SU PROPIO CAF y su propia secuencia de folios). La clave es el
    # tipo de DTE como string ("33", "56", "61"...), el valor el CAF en base64.
    # Si un tipo no está aquí, se usa caf_base64 (compatibilidad con boletas).
    cafs_por_tipo:  Optional[dict] = None
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

    # ── Cargar UN CAF por cada tipo de DTE presente en el set ────────────────
    # El set básico mezcla facturas (33), notas de crédito (61) y notas de
    # débito (56). Cada tipo tiene su PROPIO CAF y su propia secuencia de
    # folios. Antes se usaba un solo CAF para todo, quemando folios del tipo
    # 33 para las notas — por eso el contador del 33 bajaba de a 8 y los de
    # 56/61 no se movían.
    # Analogía: cada tipo de documento es una chequera distinta del banco; no
    # se pueden pagar cheques de la cuenta corriente con la chequera de ahorro.
    def _parsear_caf_b64(b64_str):
        """Devuelve (caf_xml_str, folio_desde, folio_hasta) de un CAF base64."""
        caf_bytes = _b64.b64decode(b64_str)
        caf_str   = caf_bytes.decode("utf-8")
        caf_el    = _etree.fromstring(caf_bytes)
        f_desde   = int(caf_el.findtext(".//D") or caf_el.findtext(".//DESDE") or 1)
        f_hasta   = int(caf_el.findtext(".//H") or caf_el.findtext(".//HASTA") or f_desde)
        return caf_str, f_desde, f_hasta

    # Tipos de DTE presentes en el set (ej. {33, 61, 56})
    tipos_set = {c.tipo_dte for c in datos.casos}

    # Mapa tipo → datos de su CAF. Si cafs_por_tipo trae el tipo, se usa ese;
    # si no, se cae al caf_base64 (compatibilidad con sets de un solo tipo).
    caf_por_tipo = {}        # tipo → caf_xml_str
    folio_actual_por_tipo = {}  # tipo → próximo folio a usar (contador vivo)
    folio_max_por_tipo = {}   # tipo → último folio autorizado del CAF
    try:
        for tipo in tipos_set:
            cafs_in = datos.cafs_por_tipo or {}
            b64_tipo = cafs_in.get(str(tipo)) or cafs_in.get(tipo)
            if b64_tipo:
                caf_str, f_desde, f_hasta = _parsear_caf_b64(b64_tipo)
            else:
                # Sin CAF específico para este tipo → usar el principal
                caf_str, f_desde, f_hasta = _parsear_caf_b64(datos.caf_base64)
            caf_por_tipo[tipo] = caf_str
            folio_actual_por_tipo[tipo] = f_desde
            folio_max_por_tipo[tipo] = f_hasta
    except Exception as e:
        raise HTTPException(400, f"Error parseando CAF: {e}")

    # Validar que cada tipo tenga folios suficientes para sus casos
    casos_por_tipo = {}
    for c in datos.casos:
        casos_por_tipo[c.tipo_dte] = casos_por_tipo.get(c.tipo_dte, 0) + 1
    for tipo, n in casos_por_tipo.items():
        disponibles = folio_max_por_tipo[tipo] - folio_actual_por_tipo[tipo] + 1
        if n > disponibles:
            raise HTTPException(400,
                f"CAF del tipo {tipo} insuficiente: {n} casos pero solo "
                f"{disponibles} folios disponibles")

    fecha_str = datos.fecha or _hoy_chile()
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

    # ── Asignar el folio definitivo a cada caso, del CAF de SU tipo ──────────
    # Recorremos los casos en orden y a cada uno le damos el siguiente folio
    # disponible de la chequera (CAF) de su tipo. Así el tipo 33 gasta folios
    # del CAF 33, el 61 del CAF 61, etc., cada uno con su propia secuencia.
    folio_de_caso = {}   # índice del caso → folio asignado
    folio_por_caso = {}  # numero_caso / sufijo → folio (para las referencias)
    _contador = dict(folio_actual_por_tipo)  # copia para ir avanzando
    for j, c in enumerate(datos.casos):
        folio_asignado = _contador[c.tipo_dte]
        _contador[c.tipo_dte] += 1   # avanzar el contador SOLO de ese tipo
        folio_de_caso[j] = folio_asignado
        folio_por_caso[str(c.numero_caso)] = folio_asignado
        folio_por_caso[str(j + 1)] = folio_asignado

    # Resumen de folios usados por tipo (para la respuesta al cliente):
    # cada tipo informa desde/hasta los folios que consumió de su CAF.
    folios_por_tipo = {}
    for tipo in tipos_set:
        ini = folio_actual_por_tipo[tipo]
        fin = _contador[tipo] - 1   # último folio efectivamente asignado
        folios_por_tipo[str(tipo)] = {"desde": ini, "hasta": fin}

    def _resolver_ref(caso_obj, folio_actual):
        """Construye las referencias del documento. Siempre la referencia al
        SET; además, si es NC/ND con referencia a otro caso, la referencia al
        documento corregido (obligatoria para que el SII no rechace)."""
        from app.services.xml_builder import ReferenciaDTE as _RefDTE
        refs_out = [_RefDTE(
            tipo_doc_ref="SET", folio_ref=folio_actual,
            fecha_ref=fecha_dt, razon_ref=f"CASO-{caso_obj.numero_caso}",
            cod_ref=0,
        )]
        ref = caso_obj.referencia
        if ref and ref.get("caso_ref"):
            # El caso_ref viene como "4841543-1" → tomar el sufijo tras el guión
            sufijo = str(ref["caso_ref"]).split("-")[-1]
            folio_ref = folio_por_caso.get(sufijo)
            if folio_ref:
                # Tipo del documento referido: el indicado, o inferir por el
                # tipo de la nota (NC/ND de factura → 33; de exenta → 34)
                tipo_ref = ref.get("tipo_doc_ref") or 33
                # cod_ref: 1=anula, 2=corrige texto, 3=corrige monto.
                # El SII acepta 1 para anulación; usamos 2 (corrige) por
                # defecto salvo que la razón diga "ANULA".
                razon = (ref.get("razon") or "").upper()
                cod = 1 if "ANULA" in razon else (3 if "MONTO" in razon else 2)
                refs_out.append(_RefDTE(
                    tipo_doc_ref=str(tipo_ref), folio_ref=folio_ref,
                    fecha_ref=fecha_dt, razon_ref=ref.get("razon") or "",
                    cod_ref=cod,
                ))
        return refs_out

    for i, caso in enumerate(datos.casos):
        folio = folio_de_caso[i]   # folio del CAF de SU tipo (33→CAF33, 61→CAF61...)

        tipo_dte = caso.tipo_dte
        es_boleta = tipo_dte in TIPOS_BOLETA
        # CAF correspondiente al tipo de este caso (cada tipo su chequera)
        caf_xml_str = caf_por_tipo[tipo_dte]

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
            # Referencias: al SET + (si es NC/ND) al documento corregido
            refs = _resolver_ref(caso, folio)
            # Receptor completo (con giro/dirección) para evitar reparos del SII
            rcpt = caso.receptor or {}
            input_obj = InputDTE(
                tipo_dte=tipo_dte, folio=folio, fecha_emision=fecha_dt,
                emisor=emisor_dte,
                receptor=ReceptorDTE(
                    rut=rut_recep, razon_social=nom_recep,
                    giro=rcpt.get("giro", "") or "",
                    direccion=rcpt.get("direccion", "") or "",
                    comuna=rcpt.get("comuna", "") or "",
                    ciudad=rcpt.get("ciudad", "") or "",
                    correo=rcpt.get("correo", "") or "",
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
            "folios_por_tipo": folios_por_tipo,
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
            "folios_por_tipo": folios_por_tipo,
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
