# app/api/v1/endpoints/certificacion_facturas.py
# ══════════════════════════════════════════════════════════════
# SET BASICO de Facturas — Número de Atención: 4794671
# 8 documentos: 4 Facturas (33), 3 NC (61), 1 ND (56)
# ══════════════════════════════════════════════════════════════

import logging
from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.base import get_db
from app.models.emisor import Emisor
from app.models.certificado import Certificado
from app.services.dte_service import DTEService
from app.services.firma_digital import FirmaDigital
from app.services.sii_sender import SIISender

logger = logging.getLogger("yepardtecore.cert_facturas")
router = APIRouter(prefix="/certificacion-facturas", tags=["Certificacion Facturas"])

NATENCION = "4816655"

RECEPTOR = {
    "rut":          "77777777-7",
    "razon_social": "EMPRESA LTDA",
    "giro":         "COMPUTACION",
    "direccion":    "SAN DIEGO 2222",
    "comuna":       "LA FLORIDA",
    "ciudad":       "SANTIAGO",
}


def _ref_set(n: int, fecha: str) -> dict:
    # Instrucciones SII certificación: TpoDocRef="SET", RazonRef="CASO NNNNN-N"
    # Esta referencia siempre va en la PRIMERA línea de cada DTE del set
    return {
        "tipo_doc_ref": "SET",   # string "SET", no código numérico
        "folio_ref":    n,       # número de caso (se ignora al escribir SET)
        "fecha_ref":    fecha,
        "razon_ref":    f"CASO {NATENCION}-{n}",
    }


def _ref_doc(tipo: int, folio: int, fecha: str, cod: int, razon: str) -> dict:
    return {
        "tipo_doc_ref": tipo,
        "folio_ref":    folio,
        "fecha_ref":    fecha,
        "cod_ref":      cod,
        "razon_ref":    razon,
    }


@router.post("/generar-xml", summary="Genera EnvioDTE SET BASICO Facturas (N° Atención 4816655)")
async def generar_xml_facturas(
    emisor_id: int,
    fecha_override: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(status_code=404, detail=f"Emisor {emisor_id} no encontrado")

    cert_result = await db.execute(
        select(Certificado).where(
            Certificado.emisor_id == emisor_id,
            Certificado.activo == True
        ).limit(1)
    )
    cert = cert_result.scalar_one_or_none()
    if not cert or not cert.certificado_p12:
        raise HTTPException(status_code=400, detail="Sin certificado .p12 cargado")
    logger.info(f"[CERT FAC] Certificado OK: {cert.rut_firmante}")

    fecha = fecha_override or date.today().isoformat()
    service = DTEService(db)
    xmls_firmados = []
    folios: dict[int, int] = {}
    errores = []

    async def emitir(caso_n: int, datos: dict):
        try:
            r = await service.emitir(
                emisor_id=emisor_id,
                datos={**datos, "emisor_id": emisor_id},
                auto_enviar=False,
            )
            xmls_firmados.append(r["xml_firmado"])
            folios[caso_n] = r["folio"]
            logger.info(f"[CERT FAC] Caso {caso_n} OK folio={r['folio']} total=${r['monto_total']:,.0f}")
        except Exception as e:
            errores.append(f"Caso {caso_n}: {e}")
            logger.error(f"[CERT FAC] Error caso {caso_n}: {e}", exc_info=True)

    # CASO 1 — Factura 2 ítems afectos
    await emitir(1, {
        "tipo_dte": 33, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "Cajón AFECTO",   "cantidad": 133, "precio_unitario": 1489, "exento": False},
            {"nombre": "Relleno AFECTO", "cantidad":  57, "precio_unitario": 2430, "exento": False},
        ],
        "referencias": [_ref_set(1, fecha)],
    })

    # CASO 2 — Factura con descuentos por línea (5% y 9%)
    await emitir(2, {
        "tipo_dte": 33, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "Pañuelo AFECTO", "cantidad": 350, "precio_unitario": 2796, "exento": False, "descuento_pct": 5},
            {"nombre": "ITEM 2 AFECTO",  "cantidad": 281, "precio_unitario": 1857, "exento": False, "descuento_pct": 9},
        ],
        "referencias": [_ref_set(2, fecha)],
    })

    # CASO 3 — Factura afecto + exento
    await emitir(3, {
        "tipo_dte": 33, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "Pintura B&W AFECTO",    "cantidad":  28, "precio_unitario":  3118, "exento": False},
            {"nombre": "ITEM 2 AFECTO",          "cantidad": 168, "precio_unitario":  3137, "exento": False},
            {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad":   1, "precio_unitario": 34834, "exento": True},
        ],
        "referencias": [_ref_set(3, fecha)],
    })

    # CASO 4 — Factura con descuento global 10% (solo ítems afectos)
    await emitir(4, {
        "tipo_dte": 33, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "ITEM 1 AFECTO",          "cantidad": 154, "precio_unitario": 2608, "exento": False},
            {"nombre": "ITEM 2 AFECTO",          "cantidad":  66, "precio_unitario": 2683, "exento": False},
            {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad":   2, "precio_unitario": 6782, "exento": True},
        ],
        "descuento_global_pct": 10,
        "referencias": [_ref_set(4, fecha)],
    })

    # CASO 5 — NC corrige giro receptor (mismos ítems CASO 1)
    if 1 in folios:
        await emitir(5, {
            "tipo_dte": 61, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "Cajón AFECTO",   "cantidad": 133, "precio_unitario": 1489, "exento": False},
                {"nombre": "Relleno AFECTO", "cantidad":  57, "precio_unitario": 2430, "exento": False},
            ],
            "referencias": [
                _ref_set(5, fecha),                                          # línea 1: SET (obligatorio)
                _ref_doc(33, folios[1], fecha, 2, "CORRIGE GIRO DEL RECEPTOR"),  # línea 2: doc referenciado
            ],
        })

    # CASO 6 — NC devolución parcial (cantidades del set: 129 y 190)
    if 2 in folios:
        await emitir(6, {
            "tipo_dte": 61, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "Pañuelo AFECTO", "cantidad": 129, "precio_unitario": 2796, "exento": False, "descuento_pct": 5},
                {"nombre": "ITEM 2 AFECTO",  "cantidad": 190, "precio_unitario": 1857, "exento": False, "descuento_pct": 9},
            ],
            "referencias": [
                _ref_set(6, fecha),                                           # línea 1: SET (obligatorio)
                _ref_doc(33, folios[2], fecha, 3, "DEVOLUCION DE MERCADERIAS"),  # línea 2: doc referenciado
            ],
        })

    # CASO 7 — NC anula factura (mismos ítems CASO 3)
    if 3 in folios:
        await emitir(7, {
            "tipo_dte": 61, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "Pintura B&W AFECTO",    "cantidad":  28, "precio_unitario":  3118, "exento": False},
                {"nombre": "ITEM 2 AFECTO",          "cantidad": 168, "precio_unitario":  3137, "exento": False},
                {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad":   1, "precio_unitario": 34834, "exento": True},
            ],
            "referencias": [
                _ref_set(7, fecha),                              # línea 1: SET (obligatorio)
                _ref_doc(33, folios[3], fecha, 1, "ANULA FACTURA"),  # línea 2: doc referenciado
            ],
        })

    # CASO 8 — ND anula NC caso 5 (mismos ítems CASO 5 = CASO 1)
    if 5 in folios:
        await emitir(8, {
            "tipo_dte": 56, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "Cajón AFECTO",   "cantidad": 133, "precio_unitario": 1489, "exento": False},
                {"nombre": "Relleno AFECTO", "cantidad":  57, "precio_unitario": 2430, "exento": False},
            ],
            "referencias": [
                _ref_set(8, fecha),                                                    # línea 1: SET (obligatorio)
                _ref_doc(61, folios[5], fecha, 1, "ANULA NOTA DE CREDITO ELECTRONICA"),  # línea 2: CodRef=1 (anula)
            ],
        })

    if not xmls_firmados:
        raise HTTPException(
            status_code=500,
            detail=f"No se generó ningún documento. Errores: {'; '.join(errores)}"
        )

    firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    sender = SIISender(ambiente=emisor.ambiente)
    try:
        sobre_xml = await sender.construir_sobre(
            dtes_xml=xmls_firmados,
            rut_emisor=emisor.rut,
            rut_enviador=firma.rut_certificado or emisor.rut,
            firma_service=firma,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error armando sobre: {e}")

    rut_limpio = emisor.rut.replace(".", "").replace("-", "")
    nombre     = f"EnvioDTE_SetBasico_{rut_limpio}_{fecha.replace('-','')}.xml"

    logger.info(
        f"[CERT FAC] Sobre listo {len(xmls_firmados)}/8 docs"
        + (f" — errores: {errores}" if errores else " ✓")
    )

    return Response(
        content=sobre_xml.encode("ISO-8859-1"),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{nombre}"',
            "X-Casos-Generados": str(len(xmls_firmados)),
            "X-Casos-Error":     str(len(errores)),
            "X-Errores-Detalle": " | ".join(errores) if errores else "",
            "X-NroAtencion":     NATENCION,
        }
    )



@router.post("/enviar", summary="Genera Y envía directo al SII (sin descargar)")
async def enviar_xml_facturas(
    emisor_id: int,
    fecha_override: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Genera el EnvioDTE del Set Básico de Facturas y lo envía
    directamente al SII via API. Retorna track_id y estado.
    """
    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(status_code=404, detail=f"Emisor {emisor_id} no encontrado")

    cert_result = await db.execute(
        select(Certificado).where(
            Certificado.emisor_id == emisor_id,
            Certificado.activo == True
        ).limit(1)
    )
    cert = cert_result.scalar_one_or_none()
    if not cert or not cert.certificado_p12:
        raise HTTPException(status_code=400, detail="Sin certificado .p12 cargado")

    fecha = fecha_override or date.today().isoformat()
    service = DTEService(db)
    xmls_firmados = []
    folios: dict[int, int] = {}
    errores = []

    async def emitir(caso_n: int, datos: dict):
        try:
            r = await service.emitir(
                emisor_id=emisor_id,
                datos={**datos, "emisor_id": emisor_id},
                auto_enviar=False,
            )
            xmls_firmados.append(r["xml_firmado"])
            folios[caso_n] = r["folio"]
            logger.info(f"[ENVIAR] Caso {caso_n} OK folio={r['folio']}")
        except Exception as e:
            errores.append(f"Caso {caso_n}: {e}")
            logger.error(f"[ENVIAR] Error caso {caso_n}: {e}", exc_info=True)

    # Casos 1-4: Facturas (tipo 33)
    await emitir(1, {
        "tipo_dte": 33, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "Cajón AFECTO",   "cantidad": 133, "precio_unitario": 1489, "exento": False},
            {"nombre": "Relleno AFECTO", "cantidad":  57, "precio_unitario": 2430, "exento": False},
        ],
        "referencias": [_ref_set(1, fecha)],
    })
    await emitir(2, {
        "tipo_dte": 33, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "Pañuelo AFECTO", "cantidad": 350, "precio_unitario": 2796, "exento": False, "descuento_pct": 5},
            {"nombre": "ITEM 2 AFECTO",  "cantidad": 281, "precio_unitario": 1857, "exento": False, "descuento_pct": 9},
        ],
        "referencias": [_ref_set(2, fecha)],
    })
    await emitir(3, {
        "tipo_dte": 33, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "Pintura B&W AFECTO",    "cantidad":  28, "precio_unitario":  3118, "exento": False},
            {"nombre": "ITEM 2 AFECTO",          "cantidad": 168, "precio_unitario":  3137, "exento": False},
            {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad":   1, "precio_unitario": 34834, "exento": True},
        ],
        "referencias": [_ref_set(3, fecha)],
    })
    await emitir(4, {
        "tipo_dte": 33, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "ITEM 1 AFECTO",          "cantidad": 154, "precio_unitario": 2608, "exento": False},
            {"nombre": "ITEM 2 AFECTO",          "cantidad":  66, "precio_unitario": 2683, "exento": False},
            {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad":   2, "precio_unitario": 6782, "exento": True},
        ],
        "descuento_global_pct": 10,
        "referencias": [_ref_set(4, fecha)],
    })

    # Casos 5-7: NC (tipo 61) — dependen de folios anteriores
    if 1 in folios:
        await emitir(5, {
            "tipo_dte": 61, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "Cajón AFECTO",   "cantidad": 133, "precio_unitario": 1489, "exento": False},
                {"nombre": "Relleno AFECTO", "cantidad":  57, "precio_unitario": 2430, "exento": False},
            ],
            "referencias": [
                _ref_set(5, fecha),
                _ref_doc(33, folios[1], fecha, 2, "CORRIGE GIRO DEL RECEPTOR"),
            ],
        })
    if 2 in folios:
        await emitir(6, {
            "tipo_dte": 61, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "Pañuelo AFECTO", "cantidad": 129, "precio_unitario": 2796, "exento": False, "descuento_pct": 5},
                {"nombre": "ITEM 2 AFECTO",  "cantidad": 190, "precio_unitario": 1857, "exento": False, "descuento_pct": 9},
            ],
            "referencias": [
                _ref_set(6, fecha),
                _ref_doc(33, folios[2], fecha, 3, "DEVOLUCION DE MERCADERIAS"),
            ],
        })
    if 3 in folios:
        await emitir(7, {
            "tipo_dte": 61, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "Pintura B&W AFECTO",    "cantidad":  28, "precio_unitario":  3118, "exento": False},
                {"nombre": "ITEM 2 AFECTO",          "cantidad": 168, "precio_unitario":  3137, "exento": False},
                {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad":   1, "precio_unitario": 34834, "exento": True},
            ],
            "referencias": [
                _ref_set(7, fecha),
                _ref_doc(33, folios[3], fecha, 1, "ANULA FACTURA"),
            ],
        })

    # Caso 8: ND (tipo 56)
    if 5 in folios:
        await emitir(8, {
            "tipo_dte": 56, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "Cajón AFECTO",   "cantidad": 133, "precio_unitario": 1489, "exento": False},
                {"nombre": "Relleno AFECTO", "cantidad":  57, "precio_unitario": 2430, "exento": False},
            ],
            "referencias": [
                _ref_set(8, fecha),
                _ref_doc(61, folios[5], fecha, 1, "ANULA NOTA DE CREDITO ELECTRONICA"),
            ],
        })

    if not xmls_firmados:
        raise HTTPException(
            status_code=500,
            detail=f"No se generó ningún documento. Errores: {'; '.join(errores)}"
        )

    # Construir sobre
    firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    sender = SIISender(ambiente=emisor.ambiente)
    try:
        sobre_xml = await sender.construir_sobre(
            dtes_xml=xmls_firmados,
            rut_emisor=emisor.rut,
            rut_enviador=firma.rut_certificado or emisor.rut,
            firma_service=firma,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error armando sobre: {e}")

    # Enviar al SII via API
    try:
        # Log de diagnostico
        logger.info(f"[ENVIAR] rut_enviador={firma.rut_certificado or emisor.rut}")
        logger.info(f"[ENVIAR] cert.certificado_p12={'SI' if cert.certificado_p12 else 'NO'}")
        logger.info(f"[ENVIAR] cert.certificado_auth_p12={'SI' if cert.certificado_auth_p12 else 'NO'}")
        resultado = await sender.enviar_sobre(
            sobre_xml=sobre_xml,
            rut_emisor=emisor.rut,
            rut_enviador=firma.rut_certificado or emisor.rut,
            p12_bytes=cert.certificado_p12,
            password=cert.certificado_password or "",
            # Si existe certificado de auth separado (ej: E-Sign), usarlo para el token
            auth_p12_bytes=cert.certificado_auth_p12 or None,
            auth_password=cert.certificado_auth_password or None,
        )
        logger.info(f"[ENVIAR SII] Resultado: {resultado}")
        return {
            "estado": resultado.get("estado"),
            "track_id": resultado.get("track_id"),
            "mensaje": resultado.get("mensaje"),
            "docs_generados": len(xmls_firmados),
            "errores_generacion": errores,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error enviando al SII: {e}")


@router.post("/enviar-appdte", summary="Firma con AppDTE Java y envía al SII")
async def enviar_xml_appdte(
    emisor_id: int,
    fecha_override: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Genera los DTEs, los firma usando el servicio Java de AppDTE,
    y envía al SII. Endpoint de diagnóstico para comparar métodos de firma.
    """
    import httpx as _httpx
    import base64 as _b64

    emisor = await db.get(Emisor, emisor_id)
    if not emisor:
        raise HTTPException(status_code=404, detail="Emisor no encontrado")

    cert_result = await db.execute(
        select(Certificado).where(
            Certificado.emisor_id == emisor_id,
            Certificado.activo == True
        ).limit(1)
    )
    cert = cert_result.scalar_one_or_none()
    if not cert or not cert.certificado_p12:
        raise HTTPException(status_code=400, detail="Sin certificado .p12")

    fecha = fecha_override or date.today().isoformat()
    service = DTEService(db)
    xmls_firmados = []
    folios: dict[int, int] = {}
    errores = []

    async def emitir(caso_n: int, datos: dict):
        try:
            r = await service.emitir(
                emisor_id=emisor_id,
                datos={**datos, "emisor_id": emisor_id},
                auto_enviar=False,
            )
            # El XML viene firmado por nuestro codigo
            # Lo re-firmamos con AppDTE para comparar
            xml_original = r["xml_firmado"].encode("ISO-8859-1") if isinstance(r["xml_firmado"], str) else r["xml_firmado"]
            
            # Extraer el DTE sin firma para enviarlo a AppDTE
            from lxml import etree as _et
            NS_SII = 'http://www.sii.cl/SiiDte'
            NS_SIG = 'http://www.w3.org/2000/09/xmldsig#'
            
            root = _et.fromstring(xml_original)
            # Si es un DTE, quitar la Signature
            for sig in root.findall(f'{{{NS_SIG}}}Signature'):
                root.remove(sig)
            
            # Obtener el doc_id
            doc_el = root.find(f'{{{NS_SII}}}Documento')
            doc_id = doc_el.get('ID', '') if doc_el is not None else ''
            
            # Serializar sin firma
            xml_sin_firma = _et.tostring(root, encoding='ISO-8859-1', xml_declaration=True)
            
            # Enviar a AppDTE para firmar
            pfx_b64 = _b64.b64encode(cert.certificado_p12).decode()
            xml_b64 = _b64.b64encode(xml_sin_firma).decode()
            
            async with _httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://apicert.appdte.cl/api/firmaxml",
                    json={
                        "xmlBase64": xml_b64,
                        "pfxBase64": pfx_b64,
                        "pass_cert": cert.certificado_password or "",
                        "nodo_xml": "Documento",
                        "id_referencia": doc_id,
                    }
                )
            
            if resp.status_code == 200:
                data = resp.json()
                if "xmlFirmado" in data:
                    xml_appdte = _b64.b64decode(data["xmlFirmado"]).decode("ISO-8859-1")
                    xmls_firmados.append(xml_appdte)
                    folios[caso_n] = r["folio"]
                    logger.info(f"[APPDTE] Caso {caso_n} firmado por AppDTE ✅")
                else:
                    raise Exception(f"AppDTE no devolvio xmlFirmado: {data}")
            else:
                raise Exception(f"AppDTE error {resp.status_code}: {resp.text[:200]}")
                
        except Exception as e:
            errores.append(f"Caso {caso_n}: {e}")
            logger.error(f"[APPDTE] Error caso {caso_n}: {e}")

    # Generar los 8 casos
    await emitir(1, {
        "tipo_dte": 33, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "Cajón AFECTO", "cantidad": 133, "precio_unitario": 1489, "exento": False},
            {"nombre": "Relleno AFECTO", "cantidad": 57, "precio_unitario": 2430, "exento": False},
        ],
        "referencias": [_ref_set(1, fecha)],
    })
    await emitir(2, {
        "tipo_dte": 33, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "Pañuelo AFECTO", "cantidad": 350, "precio_unitario": 2796, "exento": False, "descuento_pct": 5},
            {"nombre": "ITEM 2 AFECTO", "cantidad": 281, "precio_unitario": 1857, "exento": False, "descuento_pct": 9},
        ],
        "referencias": [_ref_set(2, fecha)],
    })
    await emitir(3, {
        "tipo_dte": 33, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "Pintura B&W AFECTO", "cantidad": 28, "precio_unitario": 3118, "exento": False},
            {"nombre": "ITEM 2 AFECTO", "cantidad": 168, "precio_unitario": 3137, "exento": False},
            {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad": 1, "precio_unitario": 34834, "exento": True},
        ],
        "referencias": [_ref_set(3, fecha)],
    })
    await emitir(4, {
        "tipo_dte": 33, "fecha_emision": fecha, "receptor": RECEPTOR,
        "items": [
            {"nombre": "ITEM 1 AFECTO", "cantidad": 154, "precio_unitario": 2608, "exento": False},
            {"nombre": "ITEM 2 AFECTO", "cantidad": 66, "precio_unitario": 2683, "exento": False},
            {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad": 2, "precio_unitario": 6782, "exento": True},
        ],
        "descuento_global_pct": 10,
        "referencias": [_ref_set(4, fecha)],
    })
    if 1 in folios:
        await emitir(5, {
            "tipo_dte": 61, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "Cajón AFECTO", "cantidad": 133, "precio_unitario": 1489, "exento": False},
                {"nombre": "Relleno AFECTO", "cantidad": 57, "precio_unitario": 2430, "exento": False},
            ],
            "referencias": [_ref_set(5, fecha), _ref_doc(33, folios[1], fecha, 2, "CORRIGE GIRO DEL RECEPTOR")],
        })
    if 2 in folios:
        await emitir(6, {
            "tipo_dte": 61, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "Pañuelo AFECTO", "cantidad": 129, "precio_unitario": 2796, "exento": False, "descuento_pct": 5},
                {"nombre": "ITEM 2 AFECTO", "cantidad": 190, "precio_unitario": 1857, "exento": False, "descuento_pct": 9},
            ],
            "referencias": [_ref_set(6, fecha), _ref_doc(33, folios[2], fecha, 3, "DEVOLUCION DE MERCADERIAS")],
        })
    if 3 in folios:
        await emitir(7, {
            "tipo_dte": 61, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "Pintura B&W AFECTO", "cantidad": 28, "precio_unitario": 3118, "exento": False},
                {"nombre": "ITEM 2 AFECTO", "cantidad": 168, "precio_unitario": 3137, "exento": False},
                {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad": 1, "precio_unitario": 34834, "exento": True},
            ],
            "referencias": [_ref_set(7, fecha), _ref_doc(33, folios[3], fecha, 1, "ANULA FACTURA")],
        })
    if 5 in folios:
        await emitir(8, {
            "tipo_dte": 56, "fecha_emision": fecha, "receptor": RECEPTOR,
            "items": [
                {"nombre": "Cajón AFECTO", "cantidad": 133, "precio_unitario": 1489, "exento": False},
                {"nombre": "Relleno AFECTO", "cantidad": 57, "precio_unitario": 2430, "exento": False},
            ],
            "referencias": [_ref_set(8, fecha), _ref_doc(61, folios[5], fecha, 1, "ANULA NOTA DE CREDITO ELECTRONICA")],
        })

    if not xmls_firmados:
        raise HTTPException(status_code=500, detail=f"Sin DTEs firmados. Errores: {'; '.join(errores)}")

    # Construir y enviar sobre
    firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
    sender = SIISender(ambiente=emisor.ambiente)
    try:
        sobre_xml = await sender.construir_sobre(
            dtes_xml=xmls_firmados,
            rut_emisor=emisor.rut,
            rut_enviador=firma.rut_certificado or emisor.rut,
            firma_service=firma,
        )
        resultado = await sender.enviar_sobre(
            sobre_xml=sobre_xml,
            rut_emisor=emisor.rut,
            rut_enviador=firma.rut_certificado or emisor.rut,
            p12_bytes=cert.certificado_p12,
            password=cert.certificado_password or "",
            auth_p12_bytes=cert.certificado_auth_p12 or None,
            auth_password=cert.certificado_auth_password or None,
        )
        return {
            "estado": resultado.get("estado"),
            "track_id": resultado.get("track_id"),
            "mensaje": resultado.get("mensaje"),
            "docs_firmados_appdte": len(xmls_firmados),
            "errores": errores,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error sobre/envío: {e}")


@router.get("/test-appdte")
async def test_appdte(emisor_id: int, db: AsyncSession = Depends(get_db)):
    """Test: envia resultado.xml de AppDTE a su servicio Java con nuestro certificado."""
    import httpx, base64 as _b64
    
    cert_result = await db.execute(
        select(Certificado).where(
            Certificado.emisor_id == emisor_id,
            Certificado.activo == True
        ).limit(1)
    )
    cert = cert_result.scalar_one_or_none()
    if not cert:
        raise HTTPException(400, "Sin certificado")
    
    xml_b64 = "PD94bWwgdmVyc2lvbj0iMS4wIiBlbmNvZGluZz0iSVNPLTg4NTktMSI/Pgo8RFRFIHZlcnNpb249IjEuMCI+CjxEb2N1bWVudG8gSUQ9IkY5NVQzMyI+CjxFbmNhYmV6YWRvPgo8SWREb2M+CjxUaXBvRFRFPjMzPC9UaXBvRFRFPgo8Rm9saW8+OTU8L0ZvbGlvPgo8RmNoRW1pcz4yMDI1LTExLTEyPC9GY2hFbWlzPgo8Rm1hUGFnbz4xPC9GbWFQYWdvPgo8L0lkRG9jPgo8RW1pc29yPgo8UlVURW1pc29yPjc2MDQwMzA4LTM8L1JVVEVtaXNvcj4KPFJ6blNvYz5FR0dBIElORk9STUFUSUNBIEVJUkw8L1J6blNvYz4KPEdpcm9FbWlzPlNFUlZJQ0lPUyBJTkZPUk1BVElDT1M8L0dpcm9FbWlzPgo8QWN0ZWNvPjYyMDIwMDwvQWN0ZWNvPgo8Q2RnU0lJU3VjdXI+MTwvQ2RnU0lJU3VjdXI+CjxEaXJPcmlnZW4+UkFGQUVMIENBU0FOT1ZBIDI5NzwvRGlyT3JpZ2VuPgo8Q21uYU9yaWdlbj5TQU5UQSBDUlVaPC9DbW5hT3JpZ2VuPgo8Q2l1ZGFkT3JpZ2VuPlNBTlRBIENSVVo8L0NpdWRhZE9yaWdlbj4KPC9FbWlzb3I+CjxSZWNlcHRvcj4KPFJVVFJlY2VwPjkzNzU4NTUtMjwvUlVUUmVjZXA+CjxSem5Tb2NSZWNlcD5MVVpNSVJBIENFU1BFREVTIE5BVkFSUk88L1J6blNvY1JlY2VwPgo8R2lyb1JlY2VwLz4KPERpclJlY2VwPkFEUklBTk8gRElBWiA1NjA8L0RpclJlY2VwPgo8Q21uYVJlY2VwPlNhbnRhIENydXo8L0NtbmFSZWNlcD4KPENpdWRhZFJlY2VwPlNhbnRhIENydXo8L0NpdWRhZFJlY2VwPgo8L1JlY2VwdG9yPgo8VG90YWxlcz4KPE1udE5ldG8+MTc2NTwvTW50TmV0bz4KPFRhc2FJVkE+MTk8L1Rhc2FJVkE+CjxJVkE+MzM1PC9JVkE+CjxNbnRUb3RhbD4yMTAwPC9NbnRUb3RhbD4KPC9Ub3RhbGVzPgo8L0VuY2FiZXphZG8+CjxEZXRhbGxlPgo8TnJvTGluRGV0PjE8L05yb0xpbkRldD4KPENkZ0l0ZW0+CjxUcG9Db2RpZ28+SU5UPC9UcG9Db2RpZ28+CjxWbHJDb2RpZ28+MDEwMDE8L1ZsckNvZGlnbz4KPC9DZGdJdGVtPgo8Tm1iSXRlbT5QQU4gQ09SUklFTlRFPC9ObWJJdGVtPgo8RHNjSXRlbS8+CjxRdHlJdGVtPjE8L1F0eUl0ZW0+CjxQcmNJdGVtPjE3NjU8L1ByY0l0ZW0+CjxNb250b0l0ZW0+MTc2NTwvTW9udG9JdGVtPgo8L0RldGFsbGU+CjwvRG9jdW1lbnRvPgo8L0RURT4K"
    pfx_b64 = _b64.b64encode(cert.certificado_p12).decode()
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            "https://apicert.appdte.cl/api/firmaxml",
            json={
                "xmlBase64": xml_b64,
                "pfxBase64": pfx_b64,
                "pass_cert": cert.certificado_password or "",
                "nodo_xml": "Documento",
                "id_referencia": "F95T33",
            }
        )
    
    if r.status_code == 200:
        data = r.json()
        if "xmlFirmado" in data:
            xml_firmado = _b64.b64decode(data["xmlFirmado"]).decode("ISO-8859-1")
            return {"status": "OK", "xml_firmado": xml_firmado[:3000]}
    
    return {"status": r.status_code, "response": r.text[:500]}

@router.get("/get-appdte-xml")
async def get_appdte_xml(emisor_id: int, db: AsyncSession = Depends(get_db)):
    """Llama al servicio Java de AppDTE para firmar y devuelve el XML firmado."""
    import httpx, base64 as _b64
    
    cert_result = await db.execute(
        select(Certificado).where(
            Certificado.emisor_id == emisor_id,
            Certificado.activo == True
        ).limit(1)
    )
    cert = cert_result.scalar_one_or_none()
    if not cert:
        raise HTTPException(400, "Sin certificado")
    
    xml_b64 = "PD94bWwgdmVyc2lvbj0iMS4wIiBlbmNvZGluZz0iSVNPLTg4NTktMSI/Pgo8RFRFIHZlcnNpb249IjEuMCI+CjxEb2N1bWVudG8gSUQ9IkY5NVQzMyI+CjxFbmNhYmV6YWRvPgo8SWREb2M+CjxUaXBvRFRFPjMzPC9UaXBvRFRFPgo8Rm9saW8+OTU8L0ZvbGlvPgo8RmNoRW1pcz4yMDI1LTExLTEyPC9GY2hFbWlzPgo8Rm1hUGFnbz4xPC9GbWFQYWdvPgo8L0lkRG9jPgo8RW1pc29yPgo8UlVURW1pc29yPjc2MDQwMzA4LTM8L1JVVEVtaXNvcj4KPFJ6blNvYz5FR0dBIElORk9STUFUSUNBIEVJUkw8L1J6blNvYz4KPEdpcm9FbWlzPlNFUlZJQ0lPUyBJTkZPUk1BVElDT1M8L0dpcm9FbWlzPgo8QWN0ZWNvPjYyMDIwMDwvQWN0ZWNvPgo8Q2RnU0lJU3VjdXI+MTwvQ2RnU0lJU3VjdXI+CjxEaXJPcmlnZW4+UkFGQUVMIENBU0FOT1ZBIDI5NzwvRGlyT3JpZ2VuPgo8Q21uYU9yaWdlbj5TQU5UQSBDUlVaPC9DbW5hT3JpZ2VuPgo8Q2l1ZGFkT3JpZ2VuPlNBTlRBIENSVVo8L0NpdWRhZE9yaWdlbj4KPC9FbWlzb3I+CjxSZWNlcHRvcj4KPFJVVFJlY2VwPjkzNzU4NTUtMjwvUlVUUmVjZXA+CjxSem5Tb2NSZWNlcD5MVVpNSVJBIENFU1BFREVTIE5BVkFSUk88L1J6blNvY1JlY2VwPgo8R2lyb1JlY2VwLz4KPERpclJlY2VwPkFEUklBTk8gRElBWiA1NjA8L0RpclJlY2VwPgo8Q21uYVJlY2VwPlNhbnRhIENydXo8L0NtbmFSZWNlcD4KPENpdWRhZFJlY2VwPlNhbnRhIENydXo8L0NpdWRhZFJlY2VwPgo8L1JlY2VwdG9yPgo8VG90YWxlcz4KPE1udE5ldG8+MTc2NTwvTW50TmV0bz4KPFRhc2FJVkE+MTk8L1Rhc2FJVkE+CjxJVkE+MzM1PC9JVkE+CjxNbnRUb3RhbD4yMTAwPC9NbnRUb3RhbD4KPC9Ub3RhbGVzPgo8L0VuY2FiZXphZG8+CjxEZXRhbGxlPgo8TnJvTGluRGV0PjE8L05yb0xpbkRldD4KPENkZ0l0ZW0+CjxUcG9Db2RpZ28+SU5UPC9UcG9Db2RpZ28+CjxWbHJDb2RpZ28+MDEwMDE8L1ZsckNvZGlnbz4KPC9DZGdJdGVtPgo8Tm1iSXRlbT5QQU4gQ09SUklFTlRFPC9ObWJJdGVtPgo8RHNjSXRlbS8+CjxRdHlJdGVtPjE8L1F0eUl0ZW0+CjxQcmNJdGVtPjE3NjU8L1ByY0l0ZW0+CjxNb250b0l0ZW0+MTc2NTwvTW9udG9JdGVtPgo8L0RldGFsbGU+CjwvRG9jdW1lbnRvPgo8L0RURT4K"
    pfx_b64 = _b64.b64encode(cert.certificado_p12).decode()
    
    logger.info("[APPDTE] Llamando a apicert.appdte.cl/api/firmaxml...")
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            "https://apicert.appdte.cl/api/firmaxml",
            json={
                "xmlBase64": xml_b64,
                "pfxBase64": pfx_b64,
                "pass_cert": cert.certificado_password or "",
                "nodo_xml": "Documento",
                "id_referencia": "F95T33",
            }
        )
    
    logger.info(f"[APPDTE] Status: {r.status_code} Response: {r.text[:200]}")
    
    if r.status_code == 200:
        data = r.json()
        if "xmlFirmado" in data:
            xml_firmado = _b64.b64decode(data["xmlFirmado"]).decode("ISO-8859-1")
            return {"status": "OK", "xml_firmado": xml_firmado}
    
    return {"status": r.status_code, "response": r.text[:500]}


@router.get("/get-appdte-xml2")  
async def get_appdte_xml2(emisor_id: int, db: AsyncSession = Depends(get_db)):
    """Login en AppDTE y firma el XML."""
    import httpx, base64 as _b64
    
    cert_result = await db.execute(
        select(Certificado).where(
            Certificado.emisor_id == emisor_id,
            Certificado.activo == True
        ).limit(1)
    )
    cert = cert_result.scalar_one_or_none()
    if not cert:
        raise HTTPException(400, "Sin certificado")
    
    base_url = "https://apicert.appdte.cl"
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Paso 1: Login
        login_r = await client.post(
            f"{base_url}/AppDTE/api/login",
            json={"username": "demo", "password": "demo"}
        )
        logger.info(f"[APPDTE] Login: {login_r.status_code} {login_r.text[:200]}")
        
        if login_r.status_code != 200:
            # Probar sin /AppDTE
            login_r = await client.post(
                f"{base_url}/api/login",
                json={"username": "demo", "password": "demo"}
            )
            logger.info(f"[APPDTE] Login2: {login_r.status_code} {login_r.text[:200]}")
        
        # Paso 2: Intentar firmar con diferentes rutas
        xml_b64 = "PD94bWwgdmVyc2lvbj0iMS4wIiBlbmNvZGluZz0iSVNPLTg4NTktMSI/Pgo8RFRFIHZlcnNpb249IjEuMCI+CjxEb2N1bWVudG8gSUQ9IkY5NVQzMyI+CjxFbmNhYmV6YWRvPgo8SWREb2M+CjxUaXBvRFRFPjMzPC9UaXBvRFRFPgo8Rm9saW8+OTU8L0ZvbGlvPgo8RmNoRW1pcz4yMDI1LTExLTEyPC9GY2hFbWlzPgo8Rm1hUGFnbz4xPC9GbWFQYWdvPgo8L0lkRG9jPgo8RW1pc29yPgo8UlVURW1pc29yPjc2MDQwMzA4LTM8L1JVVEVtaXNvcj4KPFJ6blNvYz5FR0dBIElORk9STUFUSUNBIEVJUkw8L1J6blNvYz4KPEdpcm9FbWlzPlNFUlZJQ0lPUyBJTkZPUk1BVElDT1M8L0dpcm9FbWlzPgo8QWN0ZWNvPjYyMDIwMDwvQWN0ZWNvPgo8Q2RnU0lJU3VjdXI+MTwvQ2RnU0lJU3VjdXI+CjxEaXJPcmlnZW4+UkFGQUVMIENBU0FOT1ZBIDI5NzwvRGlyT3JpZ2VuPgo8Q21uYU9yaWdlbj5TQU5UQSBDUlVaPC9DbW5hT3JpZ2VuPgo8Q2l1ZGFkT3JpZ2VuPlNBTlRBIENSVVo8L0NpdWRhZE9yaWdlbj4KPC9FbWlzb3I+CjxSZWNlcHRvcj4KPFJVVFJlY2VwPjkzNzU4NTUtMjwvUlVUUmVjZXA+CjxSem5Tb2NSZWNlcD5MVVpNSVJBIENFU1BFREVTIE5BVkFSUk88L1J6blNvY1JlY2VwPgo8R2lyb1JlY2VwLz4KPERpclJlY2VwPkFEUklBTk8gRElBWiA1NjA8L0RpclJlY2VwPgo8Q21uYVJlY2VwPlNhbnRhIENydXo8L0NtbmFSZWNlcD4KPENpdWRhZFJlY2VwPlNhbnRhIENydXo8L0NpdWRhZFJlY2VwPgo8L1JlY2VwdG9yPgo8VG90YWxlcz4KPE1udE5ldG8+MTc2NTwvTW50TmV0bz4KPFRhc2FJVkE+MTk8L1Rhc2FJVkE+CjxJVkE+MzM1PC9JVkE+CjxNbnRUb3RhbD4yMTAwPC9NbnRUb3RhbD4KPC9Ub3RhbGVzPgo8L0VuY2FiZXphZG8+CjxEZXRhbGxlPgo8TnJvTGluRGV0PjE8L05yb0xpbkRldD4KPENkZ0l0ZW0+CjxUcG9Db2RpZ28+SU5UPC9UcG9Db2RpZ28+CjxWbHJDb2RpZ28+MDEwMDE8L1ZsckNvZGlnbz4KPC9DZGdJdGVtPgo8Tm1iSXRlbT5QQU4gQ09SUklFTlRFPC9ObWJJdGVtPgo8RHNjSXRlbS8+CjxRdHlJdGVtPjE8L1F0eUl0ZW0+CjxQcmNJdGVtPjE3NjU8L1ByY0l0ZW0+CjxNb250b0l0ZW0+MTc2NTwvTW9udG9JdGVtPgo8L0RldGFsbGU+CjwvRG9jdW1lbnRvPgo8L0RURT4K"
        pfx_b64 = _b64.b64encode(cert.certificado_p12).decode()
        
        results = {}
        for path in ["/AppDTE/api/firmaxml", "/api/firmaxml", "/AppDTE/api/firma"]:
            try:
                r = await client.post(
                    f"{base_url}{path}",
                    json={
                        "xmlBase64": xml_b64,
                        "pfxBase64": pfx_b64,
                        "pass_cert": cert.certificado_password or "",
                        "nodo_xml": "Documento",
                        "id_referencia": "F95T33",
                    }
                )
                results[path] = {"status": r.status_code, "body": r.text[:200]}
                logger.info(f"[APPDTE] {path}: {r.status_code}")
            except Exception as e:
                results[path] = {"error": str(e)}
        
        return {"login": login_r.text[:200], "firma_attempts": results}


@router.get("/get-appdte-xml3")  
async def get_appdte_xml3(emisor_id: int, db: AsyncSession = Depends(get_db)):
    """Login en AppDTE con token y firma el XML."""
    import httpx, base64 as _b64
    
    cert_result = await db.execute(
        select(Certificado).where(
            Certificado.emisor_id == emisor_id,
            Certificado.activo == True
        ).limit(1)
    )
    cert = cert_result.scalar_one_or_none()
    if not cert:
        raise HTTPException(400, "Sin certificado")
    
    base_url = "https://apicert.appdte.cl"
    
    async with httpx.AsyncClient(timeout=20.0) as client:
        # Paso 1: Login para obtener token
        login_r = await client.post(
            f"{base_url}/api/login",
            json={"username": "demo", "password": "demo"},
            headers={"Content-Type": "application/json"}
        )
        logger.info(f"[APPDTE] Login: {login_r.status_code} {login_r.text[:300]}")
        
        if login_r.status_code != 200:
            return {"error": f"Login failed: {login_r.status_code}", "body": login_r.text[:300]}
        
        token = login_r.json().get("token", "")
        logger.info(f"[APPDTE] Token: {token[:30]}...")
        
        # Paso 2: Firmar con token en Authorization header
        xml_b64 = "PD94bWwgdmVyc2lvbj0iMS4wIiBlbmNvZGluZz0iSVNPLTg4NTktMSI/Pgo8RFRFIHZlcnNpb249IjEuMCI+CjxEb2N1bWVudG8gSUQ9IkY5NVQzMyI+CjxFbmNhYmV6YWRvPgo8SWREb2M+CjxUaXBvRFRFPjMzPC9UaXBvRFRFPgo8Rm9saW8+OTU8L0ZvbGlvPgo8RmNoRW1pcz4yMDI1LTExLTEyPC9GY2hFbWlzPgo8Rm1hUGFnbz4xPC9GbWFQYWdvPgo8L0lkRG9jPgo8RW1pc29yPgo8UlVURW1pc29yPjc2MDQwMzA4LTM8L1JVVEVtaXNvcj4KPFJ6blNvYz5FR0dBIElORk9STUFUSUNBIEVJUkw8L1J6blNvYz4KPEdpcm9FbWlzPlNFUlZJQ0lPUyBJTkZPUk1BVElDT1M8L0dpcm9FbWlzPgo8QWN0ZWNvPjYyMDIwMDwvQWN0ZWNvPgo8Q2RnU0lJU3VjdXI+MTwvQ2RnU0lJU3VjdXI+CjxEaXJPcmlnZW4+UkFGQUVMIENBU0FOT1ZBIDI5NzwvRGlyT3JpZ2VuPgo8Q21uYU9yaWdlbj5TQU5UQSBDUlVaPC9DbW5hT3JpZ2VuPgo8Q2l1ZGFkT3JpZ2VuPlNBTlRBIENSVVo8L0NpdWRhZE9yaWdlbj4KPC9FbWlzb3I+CjxSZWNlcHRvcj4KPFJVVFJlY2VwPjkzNzU4NTUtMjwvUlVUUmVjZXA+CjxSem5Tb2NSZWNlcD5MVVpNSVJBIENFU1BFREVTIE5BVkFSUk88L1J6blNvY1JlY2VwPgo8R2lyb1JlY2VwLz4KPERpclJlY2VwPkFEUklBTk8gRElBWiA1NjA8L0RpclJlY2VwPgo8Q21uYVJlY2VwPlNhbnRhIENydXo8L0NtbmFSZWNlcD4KPENpdWRhZFJlY2VwPlNhbnRhIENydXo8L0NpdWRhZFJlY2VwPgo8L1JlY2VwdG9yPgo8VG90YWxlcz4KPE1udE5ldG8+MTc2NTwvTW50TmV0bz4KPFRhc2FJVkE+MTk8L1Rhc2FJVkE+CjxJVkE+MzM1PC9JVkE+CjxNbnRUb3RhbD4yMTAwPC9NbnRUb3RhbD4KPC9Ub3RhbGVzPgo8L0VuY2FiZXphZG8+CjxEZXRhbGxlPgo8TnJvTGluRGV0PjE8L05yb0xpbkRldD4KPENkZ0l0ZW0+CjxUcG9Db2RpZ28+SU5UPC9UcG9Db2RpZ28+CjxWbHJDb2RpZ28+MDEwMDE8L1ZsckNvZGlnbz4KPC9DZGdJdGVtPgo8Tm1iSXRlbT5QQU4gQ09SUklFTlRFPC9ObWJJdGVtPgo8RHNjSXRlbS8+CjxRdHlJdGVtPjE8L1F0eUl0ZW0+CjxQcmNJdGVtPjE3NjU8L1ByY0l0ZW0+CjxNb250b0l0ZW0+MTc2NTwvTW9udG9JdGVtPgo8L0RldGFsbGU+CjwvRG9jdW1lbnRvPgo8L0RURT4K"
        pfx_b64 = _b64.b64encode(cert.certificado_p12).decode()
        
        firma_r = await client.post(
            f"{base_url}/api/firmaxml",
            json={
                "xmlBase64": xml_b64,
                "pfxBase64": pfx_b64,
                "pass_cert": cert.certificado_password or "",
                "nodo_xml": "Documento",
                "id_referencia": "F95T33",
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}"
            }
        )
        logger.info(f"[APPDTE] Firma: {firma_r.status_code} {firma_r.text[:200]}")
        
        if firma_r.status_code == 200:
            data = firma_r.json()
            if "xmlFirmado" in data:
                xml_firmado = _b64.b64decode(data["xmlFirmado"]).decode("ISO-8859-1")
                return {"status": "OK", "xml_firmado": xml_firmado}
        
        return {
            "token": token[:20] + "...",
            "firma_status": firma_r.status_code,
            "firma_response": firma_r.text[:500]
        }

@router.get("/test-integradte")
async def test_integradte():
    import httpx, json
    payload = {"user_id":"6a0632f7ff18240dc6004aed","business_id":"6a0632f7ff18240dc6004aed","code_sii":"33","data_dte":json.dumps({"Encabezado":{"IdDoc":{"TipoDTE":33,"FchEmis":"2026-05-14","FmaPago":1},"Emisor":{"RUTEmisor":"78377021-0","RznSoc":"YEPAR SOLUTIONS SPA","GiroEmis":"SERVICIOS INFORMATICOS","Acteco":[620200],"DirOrigen":"AV PRUEBA 123","CmnaOrigen":"SANTIAGO"},"Receptor":{"RUTRecep":"77777777-7","RznSocRecep":"EMPRESA LTDA","GiroRecep":"COMPUTACION","DirRecep":"SAN DIEGO 2222","CmnaRecep":"LA FLORIDA"},"Totales":{"MntNeto":100000,"TasaIVA":19.0,"IVA":19000,"MntTotal":119000}},"Detalle":[{"NroLinDet":1,"NmbItem":"Servicio prueba","QtyItem":1,"PrcItem":100000,"MontoItem":100000}]})}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post("https://api.integradte.cl/api/v1/documents/",json=payload,headers={"x-api-key":"6a0632f7ff18240dc6004aed","idempotency-key":"550e8400-e29b-41d4-a716-446655440000"})
    return {"status":r.status_code,"response":r.text[:3000]}
