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


def _firmar_cof(cof_xml: str, p12_bytes: bytes, password: str) -> str:
    """
    Firma el ConsumoFolios con XMLDSig.
    
    Analogía: es como el contador que pone su firma y sello
    en el cierre de caja — sin eso el SII no lo acepta.
    
    El COF usa enveloped-signature (la firma va DENTRO del documento)
    a diferencia del EnvioBOLETA que usa la firma FUERA del SetDTE.
    """
    from lxml import etree
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend
    import io

    # Cargar certificado
    pwd = password.encode('utf-8') if isinstance(password, str) else password
    priv_key, cert, _ = pkcs12.load_key_and_certificates(
        p12_bytes, pwd, backend=default_backend()
    )
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    pub_key  = cert.public_key()
    pub_nums = pub_key.public_numbers()

    # Parsear el COF
    parser = etree.XMLParser(remove_blank_text=False)
    root   = etree.fromstring(cof_xml.encode('utf-8'), parser)

    # El elemento a firmar es DocumentoConsumoFolios
    doc_el  = root.find(f'{{{NS_SII}}}DocumentoConsumoFolios')
    doc_id  = doc_el.get('ID', 'RCOF_01')

    # Calcular DigestValue del DocumentoConsumoFolios (c14n standalone)
    doc_bytes  = etree.tostring(doc_el)
    doc_alone  = etree.fromstring(doc_bytes)
    doc_c14n   = etree.tostring(doc_alone, method='c14n',
                                exclusive=False, with_comments=False)
    digest_val = base64.b64encode(hashlib.sha1(doc_c14n).digest()).decode()

    # Construir SignedInfo
    signed_info_xml = (
        f'<SignedInfo xmlns="{NS_DS}">'
        f'<CanonicalizationMethod Algorithm="{C14N}"/>'
        f'<SignatureMethod Algorithm="{RSA_SHA1}"/>'
        f'<Reference URI="#{doc_id}">'
        f'<Transforms>'
        f'<Transform Algorithm="{ENVLP}"/>'
        f'</Transforms>'
        f'<DigestMethod Algorithm="{SHA1_URI}"/>'
        f'<DigestValue>{digest_val}</DigestValue>'
        f'</Reference>'
        f'</SignedInfo>'
    )
    si_el   = etree.fromstring(signed_info_xml.encode())
    si_c14n = etree.tostring(
        etree.fromstring(etree.tostring(si_el)),
        method='c14n', exclusive=False, with_comments=False
    )

    # Firmar
    sig_bytes = priv_key.sign(si_c14n, padding.PKCS1v15(), hashes.SHA1())
    sig_b64   = base64.b64encode(sig_bytes).decode()

    # Construir módulo y exponente RSA
    n_bytes = pub_nums.n.to_bytes((pub_nums.n.bit_length() + 7) // 8, 'big')
    e_bytes = pub_nums.e.to_bytes((pub_nums.e.bit_length() + 7) // 8, 'big')
    mod_b64 = base64.b64encode(n_bytes).decode()
    exp_b64 = base64.b64encode(e_bytes).decode()
    cert_b64 = base64.b64encode(cert_der).decode()

    # Agregar Signature al root (enveloped — va dentro del ConsumoFolios)
    sig_xml = (
        f'<Signature xmlns="{NS_DS}">'
        f'<SignedInfo>'
        f'<CanonicalizationMethod Algorithm="{C14N}"/>'
        f'<SignatureMethod Algorithm="{RSA_SHA1}"/>'
        f'<Reference URI="#{doc_id}">'
        f'<Transforms><Transform Algorithm="{ENVLP}"/></Transforms>'
        f'<DigestMethod Algorithm="{SHA1_URI}"/>'
        f'<DigestValue>{digest_val}</DigestValue>'
        f'</Reference>'
        f'</SignedInfo>'
        f'<SignatureValue>{_wrap64(sig_b64)}</SignatureValue>'
        f'<KeyInfo>'
        f'<KeyValue><RSAKeyValue>'
        f'<Modulus>{_wrap64(mod_b64)}</Modulus>'
        f'<Exponent>{exp_b64}</Exponent>'
        f'</RSAKeyValue></KeyValue>'
        f'<X509Data>'
        f'<X509Certificate>{_wrap64(cert_b64)}</X509Certificate>'
        f'</X509Data>'
        f'</KeyInfo>'
        f'</Signature>'
    )
    sig_el = etree.fromstring(sig_xml.encode())
    root.append(sig_el)

    body = etree.tostring(root, encoding='unicode')
    return '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + body


# ── Función principal: construir el XML del COF ───────────────

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

    # Filtrar por fecha de emisión leyendo el XML
    dtes_del_dia = []
    for dte in todos_dtes:
        xml_str = dte.xml_firmado or ''
        fch_emis = re.search(r'<FchEmis>([^<]+)</FchEmis>', xml_str)
        if fch_emis and fch_emis.group(1) == fecha.isoformat():
            dtes_del_dia.append(dte)

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

    # 4. Firmar el COF
    cof_firmado = _firmar_cof(
        cof_xml,
        bytes(cert.certificado_p12),
        cert.certificado_password,
    )

    logger.info(f"[COF] XML firmado OK — {len(dtes_del_dia)} boletas")

    # 5. Enviar al SII si se solicita
    if body.enviar_sii:
        sender  = SIISender(ambiente=emisor.ambiente or 'certificacion')
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
