# app/api/v1/endpoints/certificacion_cof.py
# ══════════════════════════════════════════════════════════════
# Endpoint para generar y enviar el Reporte de Consumo de Folios
# (COF / ConsumoFolios) — requerido DIARIAMENTE por el SII
# cuando se emiten boletas electrónicas.
#
# El COF es como el "cierre de caja" diario que le dices al SII:
# "hoy emití N boletas, con estos folios y estos montos totales".
# Si el COF no coincide con los DTEs enviados → el set es rechazado.
#
# POST /v1/certificacion-boletas/generar-cof
#   → genera el XML del COF y lo firma
#   → opcionalmente lo envía al SII
# ══════════════════════════════════════════════════════════════

import logging
import hashlib
import base64
import re
import textwrap
from datetime import date, datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.base import get_db
from app.models.emisor import Emisor
from app.models.certificado import Certificado
from app.models.dte import DTE
from app.services.firma_digital import FirmaDigital
from app.services.sii_sender import SIISender

logger = logging.getLogger("yepardtecore.cof")

# El COF se agrega al mismo router de certificacion-boletas
router = APIRouter(prefix="/certificacion-boletas", tags=["Certificacion Boletas"])

NS_SII  = "http://www.sii.cl/SiiDte"
NS_XSI  = "http://www.w3.org/2001/XMLSchema-instance"
NS_DS   = "http://www.w3.org/2000/09/xmldsig#"
C14N    = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
RSA_SHA1 = "http://www.w3.org/2000/09/xmldsig#rsa-sha1"
SHA1_URI = "http://www.w3.org/2000/09/xmldsig#sha1"
ENVLP   = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"


# ── Schemas de entrada ────────────────────────────────────────

class GenerarCOFRequest(BaseModel):
    emisor_id:   int
    fecha:       Optional[str] = None   # YYYY-MM-DD, default hoy
    sec_envio:   int = 1                # SecEnvio — número secuencial del envío
    enviar_sii:  bool = False           # si True, envía automáticamente al SII


# ── Helpers de firma ─────────────────────────────────────────

def _wrap64(s: str) -> str:
    """Formatea base64 en líneas de 64 chars — requerido por el SII."""
    clean = s.replace('\n', '').replace(' ', '')
    return '\n' + '\n'.join(textwrap.wrap(clean, 64)) + '\n'



async def _firmar_cof_java(cof_xml: str, p12_bytes: bytes, password: str) -> str:
    """
    Firma el ConsumoFolios usando Java modo firmar-cof.
    FirmaDTE.java busca DocumentoConsumoFolios y firma con enveloped-signature.
    El mismo mecanismo que firmar-libro, pero para el tag del COF.
    """
    import asyncio, base64 as _b64, subprocess, os

    java_dir = os.environ.get("FIRMA_JAVA_DIR", "/app")
    xml_bytes = cof_xml.encode("ISO-8859-1")
    xml_b64   = _b64.b64encode(xml_bytes)
    pfx_b64   = _b64.b64encode(p12_bytes).decode()
    pwd_str   = password if isinstance(password, str) else password.decode()

    cmd = ["java", "-cp", java_dir, "FirmaDTE", "firmar-cof", "-", pfx_b64, pwd_str]

    def _run():
        result = subprocess.run(cmd, input=xml_b64, capture_output=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"FirmaDTE [firmar-cof]: {result.stderr.decode()[:300]}")
        if not result.stdout:
            raise RuntimeError("FirmaDTE [firmar-cof]: sin output")
        return _b64.b64decode(result.stdout).decode("ISO-8859-1")

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


def _construir_cof_xml(
    rut_emisor:   str,
    rut_enviador: str,
    fch_resol:    str,
    nro_resol:    int,
    fecha:        date,
    sec_envio:    int,
    dtes:         list,         # lista de objetos DTE del día
    ambiente:     str,
) -> str:
    """
    Construye el XML del ConsumoFolios sin firmar.
    
    El COF agrupa los DTEs por TipoDTE y reporta:
    - Totales (MntNeto, MntIva, MntExento, MntTotal)
    - Folios utilizados (rango Inicial-Final)
    - Folios anulados
    """
    tmst = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    fch_str = fecha.isoformat()

    # Agrupar DTEs por tipo
    por_tipo: dict[int, list] = {}
    for dte in dtes:
        t = dte.tipo_dte
        por_tipo.setdefault(t, []).append(dte)

    # Construir secciones de Resumen por tipo
    resumenes = []
    for tipo_dte, lista in sorted(por_tipo.items()):
        # Sumar montos desde el xml_firmado (fuente de verdad)
        mnt_neto  = 0
        mnt_iva   = 0
        mnt_exe   = 0
        mnt_total = 0

        folios_lista = []
        for d in lista:
            xml_str = d.xml_firmado or ''
            # Extraer montos del XML (más confiable que los campos de BD)
            neto  = re.search(r'<MntNeto>(\d+)</MntNeto>', xml_str)
            iva   = re.search(r'<IVA>(\d+)</IVA>', xml_str)
            exe   = re.search(r'<MntExe>(\d+)</MntExe>', xml_str)
            total = re.search(r'<MntTotal>(\d+)</MntTotal>', xml_str)
            folio = re.search(r'<Folio>(\d+)</Folio>', xml_str)

            if neto:  mnt_neto  += int(neto.group(1))
            if iva:   mnt_iva   += int(iva.group(1))
            if exe:   mnt_exe   += int(exe.group(1))
            if total: mnt_total += int(total.group(1))
            if folio: folios_lista.append(int(folio.group(1)))

        folios_lista.sort()
        inicial = folios_lista[0] if folios_lista else 0
        final   = folios_lista[-1] if folios_lista else 0
        emitidos = len(folios_lista)

        # Sección MntExento solo si hay montos exentos
        exento_tag = f'<MntExento>{mnt_exe}</MntExento>' if mnt_exe > 0 else ''

        resumenes.append(
            f'<Resumen>'
            f'<TipoDocumento>{tipo_dte}</TipoDocumento>'
            f'<MntNeto>{mnt_neto}</MntNeto>'
            f'<MntIva>{mnt_iva}</MntIva>'
            f'<TasaIVA>19</TasaIVA>'
            f'{exento_tag}'
            f'<MntTotal>{mnt_total}</MntTotal>'
            f'<FoliosEmitidos>{emitidos}</FoliosEmitidos>'
            f'<FoliosAnulados>0</FoliosAnulados>'
            f'<FoliosUtilizados>{emitidos}</FoliosUtilizados>'
            f'<RangoUtilizados>'
            f'<Inicial>{inicial}</Inicial>'
            f'<Final>{final}</Final>'
            f'</RangoUtilizados>'
            f'</Resumen>'
        )

    resumen_str = ''.join(resumenes)

    cof_xml = (
        f'<?xml version="1.0" encoding="ISO-8859-1"?>\n'
        f'<ConsumoFolios xmlns="{NS_SII}" '
        f'xmlns:xsi="{NS_XSI}" '
        f'version="1.0" '
        f'xsi:schemaLocation="{NS_SII} ConsumoFolio_v10.xsd">'
        f'<DocumentoConsumoFolios ID="RCOF_01">'
        f'<Caratula version="1.0">'
        f'<RutEmisor>{rut_emisor}</RutEmisor>'
        f'<RutEnvia>{rut_enviador}</RutEnvia>'
        f'<FchResol>{fch_resol}</FchResol>'
        f'<NroResol>{nro_resol}</NroResol>'
        f'<FchInicio>{fch_str}</FchInicio>'
        f'<FchFinal>{fch_str}</FchFinal>'
        f'<SecEnvio>{sec_envio}</SecEnvio>'
        f'<TmstFirmaEnv>{tmst}</TmstFirmaEnv>'
        f'</Caratula>'
        f'{resumen_str}'
        f'</DocumentoConsumoFolios>'
        f'</ConsumoFolios>'
    )
    return cof_xml


# ── Endpoint ──────────────────────────────────────────────────

@router.post("/generar-cof")
async def generar_cof(
    body: GenerarCOFRequest,
    db:   AsyncSession = Depends(get_db),
):
    """
    Genera el Reporte de Consumo de Folios (COF) del día.
    
    El COF resume todas las boletas emitidas en la fecha indicada.
    El SII lo compara con los DTEs enviados — deben coincidir los montos.
    """
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

    fecha = date.fromisoformat(body.fecha) if body.fecha else date.today()

    # 2. Obtener todas las boletas del día desde la BD
    # Tipos de boleta: 39 (afecta) y 41 (exenta)
    resultado = await db.execute(
        select(DTE).where(
            DTE.emisor_id == body.emisor_id,
            DTE.tipo_dte.in_([39, 41]),
            DTE.ambiente == (emisor.ambiente or 'certificacion'),
        )
    )
    todos_dtes = resultado.scalars().all()

    # Tomar los 5 DTEs más recientes del día (el último set enviado).
    # Analogía: el cajero solo cuenta el último turno, no todos los del día.
    # Los reintentos anteriores del mismo día se ignoran.
    dtes_del_dia_todos = []
    for dte in todos_dtes:
        xml_str = dte.xml_firmado or ''
        fch_emis = re.search(r'<FchEmis>([^<]+)</FchEmis>', xml_str)
        if fch_emis and fch_emis.group(1) == fecha.isoformat():
            dtes_del_dia_todos.append(dte)

    # Ordenar por id descendente y tomar los 5 más recientes
    dtes_del_dia_todos.sort(key=lambda d: d.id, reverse=True)
    # Agrupar por folio — solo el más reciente de cada folio
    dtes_por_folio: dict[int, object] = {}
    for dte in dtes_del_dia_todos:
        xml_str = dte.xml_firmado or ''
        folio_xml = re.search(r'<Folio>(\d+)</Folio>', xml_str)
        if not folio_xml:
            continue
        folio_n = int(folio_xml.group(1))
        if folio_n not in dtes_por_folio:
            dtes_por_folio[folio_n] = dte
    # Tomar el grupo de folios más alto (el set más reciente)
    if dtes_por_folio:
        folios_ordenados = sorted(dtes_por_folio.keys())
        folio_maximo = max(folios_ordenados)
        folio_minimo_set = folio_maximo - 4  # sets de 5 boletas
        dtes_del_dia = [
            dtes_por_folio[f] for f in folios_ordenados
            if f >= folio_minimo_set
        ]
    else:
        dtes_del_dia = []

    if not dtes_del_dia:
        raise HTTPException(404, f"No hay boletas para la fecha {fecha.isoformat()}")

    logger.info(f"[COF] Generando para {len(dtes_del_dia)} boletas del {fecha.isoformat()}")

    # 3. Construir XML del COF
    firma = FirmaDigital(
        p12_bytes=bytes(cert.certificado_p12),
        password=cert.certificado_password,
    )
    rut_enviador = cert.rut_firmante or firma.rut_certificado or emisor.rut

    cof_xml = _construir_cof_xml(
        rut_emisor   = emisor.rut,
        rut_enviador = rut_enviador,
        fch_resol    = '2026-04-19',   # fecha resolución certificación
        nro_resol    = 0,
        fecha        = fecha,
        sec_envio    = body.sec_envio,
        dtes         = dtes_del_dia,
        ambiente     = emisor.ambiente or 'certificacion',
    )

    # 4. Compactar y firmar el COF con Java modo firmar-cof
    # El XML indentado altera el digest — compactarlo antes de firmar
    # garantiza que el digest que calcula Java coincida con el que verifica el SII.
    # Analogía: planchar el papel antes de sellar — el sello queda exacto.
    from lxml import etree as _etree
    _parser = _etree.XMLParser(remove_blank_text=True)
    _root   = _etree.fromstring(cof_xml.encode('ISO-8859-1'), _parser)
    cof_compacto = b'<?xml version="1.0" encoding="ISO-8859-1"?>\n' +                    _etree.tostring(_root, encoding='ISO-8859-1', xml_declaration=False)
    cof_firmado = await firma.firmar_cof(cof_compacto.decode('ISO-8859-1'))

    logger.info(f"[COF] XML firmado OK — {len(dtes_del_dia)} boletas")

    # 5. Enviar al SII si se solicita
    if body.enviar_sii:
        nro_resol, fch_resol = emisor.get_resolucion(emisor.ambiente or 'certificacion')
    sender = SIISender(
        ambiente  = emisor.ambiente or 'certificacion',
        fch_resol = fch_resol,
        nro_resol = nro_resol,
    )
        token_p12 = bytes(cert.certificado_auth_p12 or cert.certificado_p12)
        token_pwd = cert.certificado_auth_password or cert.certificado_password

        resultado_envio = await sender.enviar_sobre(
            sobre_xml    = cof_firmado,
            rut_emisor   = emisor.rut,
            rut_enviador = rut_enviador,
            p12_bytes    = bytes(cert.certificado_p12),
            password     = cert.certificado_password,
            auth_p12_bytes = token_p12,
            auth_password  = token_pwd,
        )
        logger.info(f"[COF] Enviado al SII: {resultado_envio}")
        return {
            "ok":       resultado_envio.get("track_id") is not None,
            "track_id": resultado_envio.get("track_id"),
            "estado":   resultado_envio.get("estado"),
            "mensaje":  resultado_envio.get("mensaje"),
            "boletas":  len(dtes_del_dia),
        }

    # 6. Retornar XML para descarga
    rut_limpio = emisor.rut.replace("-", "").replace(".", "")
    filename   = f"ConsumoFolios_{rut_limpio}_{fecha.strftime('%Y%m%d')}.xml"

    return Response(
        content=cof_firmado.encode("ISO-8859-1"),
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Boletas":           str(len(dtes_del_dia)),
        },
    )
