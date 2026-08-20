# app/services/email_service.py
# ============================================================
# YeparDTE — Servicio de Email via Resend API
# ============================================================

import os
import json
import base64
import io
import re
import resend
from dotenv import load_dotenv

load_dotenv()

RESEND_API_KEY  = os.getenv("RESEND_API_KEY", "")
EMAIL_FROM      = os.getenv("EMAIL_FROM",      "soporte@yeparsolutions.com")
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "YeparDTE")
FRONTEND_URL    = os.getenv("VITE_FRONTEND_URL", "https://app.yepardte.cl")
BACKEND_URL     = os.getenv("BACKEND_URL", "https://yepardte-backend-production.up.railway.app")


def enviar_email(destinatario: str, asunto: str, html: str, adjuntos: list = None) -> bool:
    """Envía un email HTML via Resend."""
    if not RESEND_API_KEY:
        print("[EMAIL ERROR] RESEND_API_KEY no configurado")
        return False
    if not destinatario or "@" not in destinatario:
        print(f"[EMAIL ERROR] Destinatario inválido: {destinatario}")
        return False
    try:
        resend.api_key = RESEND_API_KEY
        response = resend.Emails.send({
            "from":    f"{EMAIL_FROM_NAME} <{EMAIL_FROM}>",
            "to":      [destinatario],
            "subject": asunto,
            "html":    html,
        })
        if response and response.get("id"):
            print(f"[EMAIL OK] Enviado a {destinatario} — ID: {response['id']}")
            return True
        print(f"[EMAIL ERROR] Respuesta inesperada: {response}")
        return False
    except Exception as e:
        print(f"[EMAIL ERROR] No se pudo enviar a {destinatario}: {e}")
        return False


# ── Helpers PDF417 ────────────────────────────────────────────────────────────

def _extraer_ted_xml(xml_firmado: str) -> str:
    """Extrae el bloque TED del XML firmado del DTE."""
    try:
        match = re.search(r'<TED[^>]*>.*?</TED>', xml_firmado, re.DOTALL)
        if match:
            return match.group(0)
    except Exception:
        pass
    return ""


def _generar_pdf417_base64(ted_xml: str) -> str:
    """Genera imagen PDF417 del TED y retorna base64 PNG. Vacío si falla."""
    if not ted_xml:
        return ""
    try:
        from pdf417gen import encode, render_image
        codes = encode(ted_xml, columns=13, security_level=2)
        img   = render_image(codes, scale=2, ratio=2, padding=2)
        buf   = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"[PDF417] Error generando timbre: {e}")
        return ""


# ── Generador de PDF con formato oficial DTE ──────────────────────────────────

def generar_pdf_documento(doc, empresa) -> bytes:
    """
    Genera el PDF del DTE con formato oficial SII:
    - Header emisor + recuadro tipo/número
    - Sección receptor
    - Tabla de ítems
    - Totales
    - Timbre PDF417 real
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph,
        Spacer, HRFlowable, Image as RLImage,
    )
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER

    # ── Ambiente — mostrar banner solo en no-producción ───────────────────────
    es_produccion = getattr(doc, 'ambiente', 'certificacion') == 'produccion'

    # ── Parsear items ─────────────────────────────────────────────────────────
    items_raw = doc.items
    if isinstance(items_raw, str):
        items_all = json.loads(items_raw or "[]")
    elif isinstance(items_raw, list):
        items_all = items_raw
    else:
        items_all = []
    guia_data  = next((i for i in items_all if i.get("__guia__")), None)
    items_list = [i for i in items_all if not i.get("__guia__")]

    # ── Tipo de documento ─────────────────────────────────────────────────────
    tipo_code_doc     = getattr(doc, 'tipo_code', '') or ''
    tipo_doc          = getattr(doc, 'tipo', '') or ''
    es_boleta         = tipo_code_doc in ("39","41") or tipo_doc in ("Boleta","Boleta Exenta")
    es_exenta_doc     = tipo_code_doc == "41" or tipo_doc == "Boleta Exenta"
    es_factura_exenta = tipo_code_doc == "33" and tipo_doc == "Factura Exenta"
    es_nota_credito   = tipo_code_doc == "61" or tipo_doc == "Nota de Crédito"
    es_nota_debito    = tipo_code_doc == "56" or tipo_doc == "Nota de Débito"
    es_guia           = tipo_code_doc == "52" or tipo_doc == "Guía de Despacho"

    tipo_label = (
        "BOLETA EXENTA ELECTRÓNICA"    if es_exenta_doc else
        "BOLETA ELECTRÓNICA"           if es_boleta else
        "FACTURA EXENTA ELECTRÓNICA"   if es_factura_exenta else
        "NOTA DE CRÉDITO ELECTRÓNICA"  if es_nota_credito else
        "NOTA DE DÉBITO ELECTRÓNICA"   if es_nota_debito else
        "GUÍA DE DESPACHO ELECTRÓNICA" if es_guia else
        "FACTURA ELECTRÓNICA"
    )
    color_tipo = (
        colors.HexColor("#1a56db") if es_boleta else
        colors.HexColor("#10b981") if es_nota_credito else
        colors.HexColor("#f59e0b") if es_nota_debito else
        colors.HexColor("#8b5cf6") if es_guia else
        colors.HexColor("#cc0000")
    )

    neto      = doc.monto_neto  or 0
    iva       = doc.monto_iva   or 0
    total     = doc.monto_total or 0
    folio_str = str(doc.folio or "").zfill(11)

    def fmt(n): return f"${n:,.0f}".replace(",", ".")

    COLOR_ROJO   = colors.HexColor("#cc0000")
    COLOR_OSCURO = colors.HexColor("#333333")
    COLOR_GRIS   = colors.HexColor("#f5f5f5")
    COLOR_BORDE  = colors.HexColor("#dddddd")
    COLOR_MUTED  = colors.HexColor("#555555")
    COLOR_HEADER = colors.HexColor("#333333")

    def estilo(size=9, bold=False, color=colors.black, align=TA_LEFT):
        return ParagraphStyle(
            'x', fontSize=size,
            fontName='Helvetica-Bold' if bold else 'Helvetica',
            textColor=color, alignment=align,
            leading=size * 1.3, spaceAfter=0, spaceBefore=0,
        )

    buffer  = io.BytesIO()
    doc_pdf = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=12*mm, bottomMargin=12*mm,
        leftMargin=12*mm, rightMargin=12*mm,
    )
    elementos = []

    # ── 1. HEADER ─────────────────────────────────────────────────────────────
    empresa_nombre = getattr(empresa, 'razon_social', None) or empresa.nombre or "Empresa"
    empresa_rut    = empresa.rut or "—"
    empresa_giro   = empresa.giro or "—"
    empresa_dir    = f"{empresa.direccion or ''} - {(empresa.comuna or '').upper()} - {(empresa.ciudad or '').upper()}"
    empresa_ciudad = (empresa.ciudad or "SANTIAGO").upper()

    emisor_data = [
        [Paragraph(f"R.U.T. {empresa_rut}", estilo(12, bold=True))],
        [Paragraph(tipo_label, estilo(16, bold=True))],
        [Paragraph(f"N° {folio_str}", estilo(11, bold=True, color=COLOR_OSCURO))],
        [Paragraph(f"S.I.I. — {empresa_ciudad}", estilo(8, color=COLOR_MUTED))],
        [Spacer(1, 4)],
        [Paragraph(empresa_nombre, estilo(11, bold=True))],
        [Paragraph(f"Giro: {empresa_giro}", estilo(9, color=COLOR_OSCURO))],
        [Paragraph(empresa_dir, estilo(9, color=COLOR_OSCURO))],
    ]
    tabla_emisor = Table(emisor_data, colWidths=[120*mm])
    tabla_emisor.setStyle(TableStyle([
        ("PADDING", (0, 0), (-1, -1), 1),
        ("VALIGN",  (0, 0), (-1, -1), "TOP"),
    ]))

    recuadro_data = [
        [Paragraph(tipo_label, estilo(10, bold=True, color=COLOR_ROJO, align=TA_CENTER))],
        [Paragraph(f"N° {folio_str}", estilo(18, bold=True, color=COLOR_ROJO, align=TA_CENTER))],
    ]
    tabla_recuadro = Table(recuadro_data, colWidths=[60*mm])
    tabla_recuadro.setStyle(TableStyle([
        ("BOX",     (0, 0), (-1, -1), 2, COLOR_ROJO),
        ("PADDING", (0, 0), (-1, -1), 8),
        ("VALIGN",  (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",   (0, 0), (-1, -1), "CENTER"),
    ]))

    tabla_header = Table([[tabla_emisor, tabla_recuadro]], colWidths=[125*mm, 61*mm])
    tabla_header.setStyle(TableStyle([
        ("VALIGN",  (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 0),
    ]))
    elementos.append(tabla_header)
    elementos.append(HRFlowable(width="100%", thickness=2, color=colors.black, spaceAfter=6))

    # ── 2. BANNER solo en no-producción ───────────────────────────────────────
    if not es_produccion:
        aviso = Table(
            [[Paragraph(
                "⚠ DOCUMENTO INTERNO — SIN VALIDEZ FISCAL — PENDIENTE CERTIFICACIÓN DTE",
                estilo(8, bold=True, color=colors.HexColor("#856404"), align=TA_CENTER)
            )]],
            colWidths=[186*mm],
        )
        aviso.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fff3cd")),
            ("BOX",        (0, 0), (-1, -1), 1, colors.HexColor("#ffc107")),
            ("PADDING",    (0, 0), (-1, -1), 5),
        ]))
        elementos.append(aviso)
        elementos.append(Spacer(1, 4))

    # ── 3. RECEPTOR ───────────────────────────────────────────────────────────
    fecha_emision = doc.fecha.strftime("%d/%m/%Y") if hasattr(doc.fecha, 'strftime') else str(doc.fecha)[:10]

    receptor_data = [
        [
            Paragraph("SEÑOR(ES):", estilo(7, bold=True, color=COLOR_MUTED)),
            Paragraph(doc.receptor_nombre or "—", estilo(9, bold=True)),
            Paragraph("", estilo(7)),
            Paragraph("", estilo(9)),
        ],
        [
            Paragraph("R.U.T.:", estilo(7, bold=True, color=COLOR_MUTED)),
            Paragraph(doc.receptor_rut or "—", estilo(9, bold=True)),
            Paragraph("GIRO:", estilo(7, bold=True, color=COLOR_MUTED)),
            Paragraph(doc.receptor_giro or "—", estilo(9, bold=True)),
        ],
        [
            Paragraph("DIRECCIÓN:", estilo(7, bold=True, color=COLOR_MUTED)),
            Paragraph(doc.receptor_direccion or "—", estilo(9, bold=True)),
            Paragraph("", estilo(7)),
            Paragraph("", estilo(9)),
        ],
        [
            Paragraph("FECHA EMISIÓN:", estilo(7, bold=True, color=COLOR_MUTED)),
            Paragraph(fecha_emision, estilo(9, bold=True)),
            Paragraph("CONDICIÓN PAGO:", estilo(7, bold=True, color=COLOR_MUTED)),
            Paragraph("Contado", estilo(9, bold=True)),
        ],
    ]
    tabla_receptor = Table(receptor_data, colWidths=[28*mm, 65*mm, 28*mm, 65*mm])
    tabla_receptor.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), COLOR_GRIS),
        ("BOX",        (0, 0), (-1, -1), 1, COLOR_BORDE),
        ("GRID",       (0, 0), (-1, -1), 0.3, COLOR_BORDE),
        ("PADDING",    (0, 0), (-1, -1), 4),
        ("VALIGN",     (0, 0), (-1, -1), "TOP"),
        ("SPAN",       (1, 0), (3, 0)),
        ("SPAN",       (1, 2), (3, 2)),
    ]))
    elementos.append(tabla_receptor)
    elementos.append(Spacer(1, 6))

    # ── 3b. TRASLADO (guía de despacho) ───────────────────────────────────────
    if es_guia and guia_data:
        COLOR_VERDE = colors.HexColor("#1a7a4a")
        COLOR_FONDO = colors.HexColor("#f0fff7")
        COLOR_BORDE_G = colors.HexColor("#c3e6cb")
        traslado_rows = [
            [Paragraph("DATOS DEL TRASLADO", estilo(8, bold=True, color=COLOR_VERDE)), Paragraph("", estilo(8))],
            [Paragraph("ORIGEN:",  estilo(8, bold=True, color=COLOR_MUTED)), Paragraph(guia_data.get("direccionOrigen", "—"), estilo(9, bold=True))],
            [Paragraph("DESTINO:", estilo(8, bold=True, color=COLOR_MUTED)), Paragraph(guia_data.get("direccionDestino", "—"), estilo(9, bold=True))],
            [Paragraph("MOTIVO:",  estilo(8, bold=True, color=COLOR_MUTED)), Paragraph(guia_data.get("motivo", "Venta"), estilo(9))],
        ]
        tabla_traslado = Table(traslado_rows, colWidths=[30*mm, 156*mm])
        tabla_traslado.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), COLOR_FONDO),
            ("BOX",        (0, 0), (-1, -1), 1.5, COLOR_VERDE),
            ("PADDING",    (0, 0), (-1, -1), 4),
        ]))
        elementos.append(tabla_traslado)
        elementos.append(Spacer(1, 6))

    # ── 4. TABLA DE ÍTEMS ─────────────────────────────────────────────────────
    items_header = [
        Paragraph("N°",           estilo(8, bold=True, color=colors.white, align=TA_CENTER)),
        Paragraph("CÓDIGO",       estilo(8, bold=True, color=colors.white)),
        Paragraph("DESCRIPCIÓN",  estilo(8, bold=True, color=colors.white)),
        Paragraph("CANT.",        estilo(8, bold=True, color=colors.white, align=TA_RIGHT)),
        Paragraph("PRECIO UNIT.", estilo(8, bold=True, color=colors.white, align=TA_RIGHT)),
        Paragraph("%DESC.",        estilo(8, bold=True, color=colors.white, align=TA_RIGHT)),
        Paragraph("MTO.DESC.",     estilo(8, bold=True, color=colors.white, align=TA_RIGHT)),
        Paragraph("VALOR",         estilo(8, bold=True, color=colors.white, align=TA_RIGHT)),
    ]
    items_rows = [items_header]
    for i, item in enumerate(items_list):
        qty      = item.get("qty", 1)
        precio   = item.get("precio", 0)
        desc_pct = item.get("descuento", 0) or 0
        subtotal = round(qty * precio * (1 - desc_pct / 100))
        nombre   = item.get("nombre", item.get("desc", ""))
        items_rows.append([
            Paragraph(str(i + 1),          estilo(9, align=TA_CENTER)),
            Paragraph("",                  estilo(9)),
            Paragraph(nombre,              estilo(9)),
            Paragraph(str(qty),            estilo(9, align=TA_RIGHT)),
            Paragraph(fmt(precio),         estilo(9, align=TA_RIGHT)),
            Paragraph(f"{int(desc_pct)}%",                    estilo(9, align=TA_RIGHT)),
            Paragraph(f"-{fmt(round(qty*precio*desc_pct/100))}" if desc_pct > 0 else "$0", estilo(9, align=TA_RIGHT)),
            Paragraph(fmt(subtotal),                           estilo(9, align=TA_RIGHT)),
        ])

    tabla_items = Table(items_rows, colWidths=[10*mm, 18*mm, 80*mm, 15*mm, 25*mm, 15*mm, 23*mm])
    tabla_items.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (-1, 0),  COLOR_HEADER),
        ("TEXTCOLOR",      (0, 0), (-1, 0),  colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafafa")]),
        ("GRID",           (0, 0), (-1, -1), 0.5, COLOR_BORDE),
        ("PADDING",        (0, 0), (-1, -1), 4),
        ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elementos.append(tabla_items)
    elementos.append(Spacer(1, 6))

    # ── 5. TOTALES ────────────────────────────────────────────────────────────
    if es_guia:
        totales_rows = [
            [Paragraph("VALOR TOTAL $", estilo(11, bold=True)), Paragraph(fmt(total), estilo(11, bold=True, align=TA_RIGHT))],
        ]
    elif es_exenta_doc or es_factura_exenta:
        totales_rows = [
            [Paragraph("MONTO EXENTO $", estilo(9, color=COLOR_OSCURO)), Paragraph(fmt(total), estilo(9, align=TA_RIGHT))],
            [Paragraph("I.V.A. 19% $",   estilo(9, color=COLOR_OSCURO)), Paragraph("EXENTO",  estilo(9, align=TA_RIGHT))],
            [Paragraph("TOTAL $",         estilo(11, bold=True)),          Paragraph(fmt(total), estilo(11, bold=True, align=TA_RIGHT))],
        ]
    else:
        totales_rows = [
            [Paragraph("MONTO NETO $", estilo(9, color=COLOR_OSCURO)), Paragraph(fmt(neto),  estilo(9, align=TA_RIGHT))],
            [Paragraph("I.V.A. 19% $", estilo(9, color=COLOR_OSCURO)), Paragraph(fmt(iva),   estilo(9, align=TA_RIGHT))],
            [Paragraph("TOTAL $",       estilo(11, bold=True)),          Paragraph(fmt(total), estilo(11, bold=True, align=TA_RIGHT))],
        ]
    tabla_totales = Table(totales_rows, colWidths=[30*mm, 30*mm], hAlign="RIGHT")
    tabla_totales.setStyle(TableStyle([
        ("GRID",      (0, 0), (-1, -2), 0.5, COLOR_BORDE),
        ("LINEABOVE", (0, -1), (-1, -1), 2, colors.black),
        ("LINEBELOW", (0, -1), (-1, -1), 2, colors.black),
        ("PADDING",   (0, 0),  (-1, -1), 4),
        ("ALIGN",     (1, 0),  (1, -1),  "RIGHT"),
    ]))
    elementos.append(tabla_totales)
    elementos.append(Spacer(1, 12))

    # ── 6. TIMBRE ELECTRÓNICO con PDF417 ──────────────────────────────────────
    elementos.append(HRFlowable(width="100%", thickness=2, color=colors.black, spaceAfter=6))

    xml_firmado = getattr(doc, 'xml_firmado', '') or ''
    ted_xml     = _extraer_ted_xml(xml_firmado)
    pdf417_b64  = _generar_pdf417_base64(ted_xml)

    if pdf417_b64:
        # Decodificar base64 → imagen ReportLab
        img_data = base64.b64decode(pdf417_b64.split(",")[1])
        img_buf  = io.BytesIO(img_data)
        rl_img   = RLImage(img_buf, width=80*mm, height=20*mm)
        timbre_col_izq = Table([
            [Paragraph("TIMBRE ELECTRÓNICO SII", estilo(8, bold=True))],
            [rl_img],
            [Paragraph("Res. 80 de 2014 — Verifique en: www.sii.cl", estilo(7, color=COLOR_MUTED))],
        ], colWidths=[90*mm])
    else:
        timbre_col_izq = Table([
            [Paragraph("TIMBRE ELECTRÓNICO SII", estilo(8, bold=True))],
            [Paragraph("Verifique documento en: www.sii.cl", estilo(8, color=COLOR_MUTED))],
        ], colWidths=[90*mm])

    timbre_col_der = Table([
        [Paragraph(tipo_label,          estilo(8, align=TA_RIGHT))],
        [Paragraph(f"N° {folio_str}",   estilo(8, align=TA_RIGHT))],
        [Paragraph(f"Emisión: {fecha_emision}", estilo(8, align=TA_RIGHT))],
        [Paragraph(f"RUT: {empresa_rut}", estilo(8, align=TA_RIGHT))],
    ], colWidths=[96*mm])

    tabla_timbre = Table([[timbre_col_izq, timbre_col_der]], colWidths=[90*mm, 96*mm])
    tabla_timbre.setStyle(TableStyle([
        ("VALIGN",  (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 0),
    ]))
    elementos.append(tabla_timbre)

    # ── 7. FOOTER ─────────────────────────────────────────────────────────────
    elementos.append(Spacer(1, 8))
    elementos.append(HRFlowable(width="100%", thickness=0.5, color=COLOR_BORDE))
    elementos.append(Spacer(1, 3))
    elementos.append(Paragraph(
        "Generado con YeparDTE · yeparsolutions.com",
        estilo(7, color=colors.HexColor("#999999"), align=TA_CENTER)
    ))

    doc_pdf.build(elementos)
    return buffer.getvalue()


# ── Templates HTML ────────────────────────────────────────────────────────────

def template_codigo_verificacion(nombre: str, codigo: str) -> str:
    logo_url    = "https://app.yepardte.cl/logo-300x130.png"
    isotipo_url = "https://app.yepardte.cl/Isotipo.png"
    return f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Código de verificación — YeparDTE</title></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:40px 20px;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0"
             style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">
        <tr><td style="padding:32px 40px 24px;text-align:center;border-bottom:1px solid #e2e8f0;">
          <img src="{logo_url}" alt="YeparDTE" width="200" height="auto"
               style="display:block;margin:0 auto;" onerror="this.style.display='none'">
        </td></tr>
        <tr><td style="padding:40px 40px 32px;text-align:center;">
          <img src="{isotipo_url}" alt="YeparDTE" width="64" height="64"
               style="display:block;margin:0 auto 24px;" onerror="this.style.display='none'">
          <h1 style="margin:0 0 8px;font-size:22px;font-weight:800;color:#0f172a;">HOLA {nombre.upper()}</h1>
          <p style="margin:0 0 8px;font-size:16px;color:#475569;font-weight:600;">AQUÍ ESTÁ TU CÓDIGO</p>
          <p style="margin:0 0 28px;font-size:15px;color:#64748b;">Tu código de verificación es</p>
          <div style="display:inline-block;background:#f0fdf4;border:2px solid #00C77B;
                      border-radius:14px;padding:18px 40px;margin-bottom:28px;">
            <span style="font-family:monospace;font-size:42px;font-weight:900;
                         color:#00C77B;letter-spacing:10px;">{codigo}</span>
          </div>
          <p style="margin:0 0 32px;font-size:14px;color:#94a3b8;">Este código expira en 15 minutos</p>
          <p style="margin:0;font-size:13px;color:#cbd5e1;">
            Si no creaste una cuenta en <span style="color:#00C77B;font-weight:700;">YeparDTE</span>,
            puedes ignorar este mensaje.
          </p>
        </td></tr>
        <tr><td style="background:#f8fafc;padding:20px 40px;border-top:1px solid #e2e8f0;">
          <table width="100%" cellpadding="0" cellspacing="0"><tr>
            <td style="font-size:12px;color:#94a3b8;">
              <a href="https://yeparsolutions.com" style="color:#94a3b8;text-decoration:none;">https://yeparsolutions.com</a>
            </td>
            <td style="font-size:12px;color:#94a3b8;text-align:right;">© 2026 Yepar Solutions SpA.</td>
          </tr></table>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def template_documento_email(
    empresa_nombre: str,
    empresa_rut: str,
    tipo_doc: str,
    numero_doc: str,
    receptor_nombre: str,
    monto_total: int,
    fecha: str,
    doc_id: str = "",
    token: str = "",
    logo_empresa_base64: str = None,
    es_cedible_tipo: bool = False,
) -> str:
    # Gmail, Zoho y la mayoría de clientes de correo NO renderizan imágenes
    # data:base64 incrustadas (las bloquean por seguridad/peso, aunque sean válidas).
    # Por eso en el correo solo usamos el logo de empresa si es una URL http(s);
    # si viene en base64 o vacío, caemos al logo de Yepar alojado por URL, que sí carga.
    _logo_emp = (logo_empresa_base64 or "").strip()
    logo_url  = _logo_emp if _logo_emp.startswith("http") else "https://app.yepardte.cl/logo-300x130.png"
    monto_fmt = f"${monto_total:,.0f}".replace(",", ".")
    pdf_url_base = f"{BACKEND_URL}/api/dte/{doc_id}/pdf-publico?token={token}" if doc_id and token else ""

    # Facturas, exentas y guías de venta pueden llevar copia Cedible (con
    # acuse de recibo, Ley 19.983) además de la Tributaria. Por correo no
    # importa cuál llegue primero, así que se ofrecen las dos.
    # Facturas, exentas y guías de venta: se manda SOLO la copia Cedible al
    # cliente (es la que necesita si va a usar el documento para factoring).
    # La Tributaria queda disponible para uso interno desde Historial.
    if pdf_url_base and es_cedible_tipo:
        boton_pdf = f"""
        <div style="text-align:center;margin:28px 0;">
          <a href="{pdf_url_base}&copia=cedible"
             style="display:inline-block;background:#00C77B;color:#ffffff;
                    font-size:15px;font-weight:700;text-decoration:none;
                    padding:14px 32px;border-radius:10px;">
            ⬇ Descargar documento PDF
          </a>
        </div>
        """
    elif pdf_url_base:
        boton_pdf = f"""
        <div style="text-align:center;margin:28px 0;">
          <a href="{pdf_url_base}&copia=tributaria"
             style="display:inline-block;background:#00C77B;color:#ffffff;
                    font-size:15px;font-weight:700;text-decoration:none;
                    padding:14px 32px;border-radius:10px;">
            ⬇ Descargar documento PDF
          </a>
        </div>
        """
    else:
        boton_pdf = ""

    return f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{tipo_doc} {numero_doc} — {empresa_nombre}</title></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:40px 20px;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0"
             style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">
        <tr><td style="padding:32px 40px 24px;text-align:center;border-bottom:1px solid #e2e8f0;">
          <img src="{logo_url}" alt="YeparDTE" width="180" height="auto"
               style="display:block;margin:0 auto;" onerror="this.style.display='none'">
        </td></tr>
        <tr><td style="padding:40px 40px 32px;">
          <h2 style="margin:0 0 8px;font-size:20px;font-weight:800;color:#0f172a;">Hola, {receptor_nombre} 👋</h2>
          <p style="margin:0 0 24px;font-size:15px;color:#475569;line-height:1.6;">
            <strong>{empresa_nombre}</strong> (RUT {empresa_rut}) te ha enviado el siguiente documento tributario electrónico:
          </p>
          <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:24px;margin-bottom:28px;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;">
                  <span style="font-size:13px;color:#64748b;">Documento</span>
                </td>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;text-align:right;">
                  <strong style="font-size:14px;color:#0f172a;">{tipo_doc}</strong>
                </td>
              </tr>
              <tr>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;">
                  <span style="font-size:13px;color:#64748b;">Número</span>
                </td>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;text-align:right;">
                  <strong style="font-size:14px;color:#0f172a;font-family:monospace;">{numero_doc}</strong>
                </td>
              </tr>
              <tr>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;">
                  <span style="font-size:13px;color:#64748b;">Fecha emisión</span>
                </td>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;text-align:right;">
                  <span style="font-size:14px;color:#0f172a;">{fecha}</span>
                </td>
              </tr>
              <tr>
                <td style="padding:12px 0 0;">
                  <span style="font-size:15px;font-weight:700;color:#0f172a;">Total</span>
                </td>
                <td style="padding:12px 0 0;text-align:right;">
                  <span style="font-size:20px;font-weight:900;color:#00C77B;">{monto_fmt}</span>
                </td>
              </tr>
            </table>
          </div>
          {boton_pdf}
          <p style="margin:0;font-size:13px;color:#94a3b8;">
            Puedes verificar la autenticidad en
            <a href="https://www.sii.cl" style="color:#00C77B;">www.sii.cl</a>
          </p>
        </td></tr>
        <tr><td style="background:#f8fafc;padding:20px 40px;border-top:1px solid #e2e8f0;">
          <table width="100%" cellpadding="0" cellspacing="0"><tr>
            <td style="font-size:12px;color:#94a3b8;">
              Enviado con <a href="https://yeparsolutions.com" style="color:#00C77B;text-decoration:none;">YeparDTE</a>
            </td>
            <td style="font-size:12px;color:#94a3b8;text-align:right;">© 2026 Yepar Solutions SpA.</td>
          </tr></table>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


# ── HTML carta DTE para pdf-publico ──────────────────────────────────────────

def _html_carta_dte(doc, empresa, logo_base64=None, logo_ancho=70, copia="tributaria"):
    """Genera HTML carta con timbre PDF417 real y banner solo en no-producción.

    copia: "tributaria" (default, sin acuse de recibo) o "cedible" (con el
    recuadro de acuse de recibo — Ley 19.983 — para facturas, exentas y
    guías de despacho de venta). Se entrega físicamente al cliente la copia
    que corresponda; por correo no importa si llega cualquiera de las dos.
    """
    import json as _json

    tipo_code  = getattr(doc, "tipo_code", "") or ""
    doc_tipo   = getattr(doc, "tipo", "") or ""
    es_produccion     = getattr(doc, "ambiente", "certificacion") == "produccion"
    es_exenta         = tipo_code == "41" or doc_tipo == "Boleta Exenta"
    es_factura_exenta = tipo_code == "33" and doc_tipo == "Factura Exenta"
    es_boleta         = tipo_code in ("39", "41") or doc_tipo in ("Boleta", "Boleta Exenta")
    es_nota_credito   = tipo_code == "61" or doc_tipo == "Nota de Crédito"
    es_nota_debito    = tipo_code == "56" or doc_tipo == "Nota de Débito"
    es_guia           = tipo_code == "52" or doc_tipo == "Guía de Despacho"

    tipo_label = (
        "BOLETA EXENTA ELECTRÓNICA"    if es_exenta else
        "BOLETA ELECTRÓNICA"           if es_boleta else
        "FACTURA EXENTA ELECTRÓNICA"   if es_factura_exenta else
        "NOTA DE CRÉDITO ELECTRÓNICA"  if es_nota_credito else
        "NOTA DE DÉBITO ELECTRÓNICA"   if es_nota_debito else
        "GUÍA DE DESPACHO ELECTRÓNICA" if es_guia else
        "FACTURA ELECTRÓNICA"
    )
    color_doc = (
        "#1a56db" if es_boleta else
        "#10b981" if es_nota_credito else
        "#f59e0b" if es_nota_debito else
        "#8b5cf6" if es_guia else
        "#c00"
    )

    neto   = getattr(doc, "monto_neto",   0) or 0
    iva    = getattr(doc, "monto_iva",    0) or 0
    exento = getattr(doc, "monto_exento", 0) or 0
    total  = getattr(doc, "monto_total",  0) or 0
    folio  = str(getattr(doc, "folio", 0) or 0).zfill(11)
    fecha  = doc.fecha.strftime("%d/%m/%Y") if hasattr(doc.fecha, "strftime") else str(doc.fecha)[:10]

    def fmt(n): return f"${n:,.0f}".replace(",", ".")

    # Items
    items_raw = doc.items
    if isinstance(items_raw, str):
        all_items  = _json.loads(items_raw or "[]")
        items_list = [i for i in all_items if not i.get("__guia__")]
        guia_data  = next((i for i in all_items if i.get("__guia__")), None)
    else:
        items_list = []
        guia_data  = None

    # PDF417 del TED
    xml_firmado = getattr(doc, "xml_firmado", "") or ""
    ted_xml     = _extraer_ted_xml(xml_firmado)
    pdf417_b64  = _generar_pdf417_base64(ted_xml)
    timbre_img_html = f'<img src="{pdf417_b64}" style="display:block;height:40px;width:auto;margin:4px 0;" alt="Timbre PDF417"/>' if pdf417_b64 else ""

    # ── Cedible: facturas (33), exentas (34) y guías de despacho DE VENTA ────
    # (no traslado interno) pueden llevar copia Cedible con acuse de recibo.
    # NC/ND nunca son cedibles. Mismo criterio que Muestras Impresas.
    _rut_receptor_limpio = (getattr(doc, "receptor_rut", "") or "").replace(".", "").strip()
    _rut_emisor_limpio   = (empresa.rut or "").replace(".", "").strip()
    es_guia_venta   = es_guia and _rut_receptor_limpio != _rut_emisor_limpio
    es_cedible_tipo = tipo_code in ("33", "34") or es_guia_venta
    mostrar_cedible = (copia == "cedible") and es_cedible_tipo

    # Banner
    banner_html = ""
    if not es_produccion:
        banner_html = """
        <div style="background:#fff3cd;border:1px solid #ffc107;border-radius:3px;padding:6px;
                    text-align:center;font-size:9px;font-weight:bold;color:#856404;margin-bottom:10px;">
          ⚠ DOCUMENTO INTERNO — SIN VALIDEZ FISCAL — PENDIENTE CERTIFICACIÓN DTE
        </div>"""

    # Traslado
    traslado_html = ""
    if es_guia and guia_data:
        traslado_html = f"""
        <table width="100%" cellpadding="0" cellspacing="0"
          style="margin-bottom:10px;border:2px solid #1a7a4a;border-radius:4px;background:#f0fff7;">
          <tr><td colspan="2" style="padding:6px 10px;font-size:10px;font-weight:bold;color:#1a7a4a;
            border-bottom:1px solid #c3e6cb;">DATOS DEL TRASLADO</td></tr>
          <tr><td style="padding:5px 10px;font-size:10px;color:#555;font-weight:600;width:90px;">ORIGEN:</td>
              <td style="padding:5px 10px;font-size:11px;font-weight:700;">{guia_data.get('direccionOrigen','—')}</td></tr>
          <tr><td style="padding:5px 10px;font-size:10px;color:#555;font-weight:600;">DESTINO:</td>
              <td style="padding:5px 10px;font-size:11px;font-weight:700;">{guia_data.get('direccionDestino','—')}</td></tr>
          <tr><td style="padding:5px 10px;font-size:10px;color:#555;font-weight:600;">MOTIVO:</td>
              <td style="padding:5px 10px;font-size:11px;">{guia_data.get('motivo','Venta')}</td></tr>
        </table>"""

    # Totales
    if es_guia:
        totales_html = f'<div style="display:flex;justify-content:space-between;padding:5px 0;font-size:14px;font-weight:800;border-top:2px solid #000;"><span>VALOR TOTAL $</span><span>{fmt(total)}</span></div>'
    elif es_exenta or es_factura_exenta:
        totales_html = f"""
        <div style="display:flex;justify-content:space-between;padding:4px 0;font-size:12px;border-bottom:1px solid #eee;"><span>MONTO EXENTO $</span><span>{fmt(exento or total)}</span></div>
        <div style="display:flex;justify-content:space-between;padding:4px 0;font-size:12px;border-bottom:1px solid #eee;"><span>I.V.A. 19% $</span><span style="font-size:10px;font-weight:bold;color:#1a56db;">EXENTO</span></div>
        <div style="display:flex;justify-content:space-between;padding:5px 0;font-size:14px;font-weight:800;border-top:2px solid #000;"><span>TOTAL $</span><span>{fmt(total)}</span></div>"""
    else:
        totales_html = f"""
        <div style="display:flex;justify-content:space-between;padding:4px 0;font-size:12px;border-bottom:1px solid #eee;"><span>MONTO NETO $</span><span>{fmt(neto)}</span></div>
        <div style="display:flex;justify-content:space-between;padding:4px 0;font-size:12px;border-bottom:1px solid #eee;"><span>I.V.A. 19% $</span><span>{fmt(iva)}</span></div>
        <div style="display:flex;justify-content:space-between;padding:5px 0;font-size:14px;font-weight:800;border-top:2px solid #000;"><span>TOTAL $</span><span>{fmt(total)}</span></div>"""

    # Items HTML
    items_html = ""
    for i, item in enumerate(items_list):
        qty      = item.get("qty", 1)
        precio   = item.get("precio", 0)
        desc_pct = item.get("descuento", 0) or 0
        nombre   = item.get("nombre", item.get("desc", ""))
        subtotal = round(qty * precio * (1 - desc_pct / 100))
        items_html += f"""<tr>
          <td style="padding:5px 6px;font-size:10px;border-bottom:1px solid #eee;text-align:center;">{i+1}</td>
          <td style="padding:5px 6px;font-size:10px;border-bottom:1px solid #eee;"></td>
          <td style="padding:5px 6px;font-size:10px;border-bottom:1px solid #eee;">{nombre}</td>
          <td style="padding:5px 6px;font-size:10px;border-bottom:1px solid #eee;text-align:right;">{qty}</td>
          <td style="padding:5px 6px;font-size:10px;border-bottom:1px solid #eee;text-align:right;">{fmt(precio)}</td>
          <td style="padding:5px 6px;font-size:10px;border-bottom:1px solid #eee;text-align:right;">{int(desc_pct)}%</td>
          <td style="padding:5px 6px;font-size:10px;border-bottom:1px solid #eee;text-align:right;">{"- "+fmt(round(qty*precio*desc_pct/100)) if desc_pct > 0 else "$0"}</td>
          <td style="padding:5px 6px;font-size:10px;border-bottom:1px solid #eee;text-align:right;">{fmt(subtotal)}</td>
        </tr>"""

    logo_html = f'<img src="{logo_base64}" style="height:{logo_ancho}px;width:auto;display:block;margin-bottom:6px;" alt="Logo"/>' if logo_base64 else ""

    # ── Acuse de recibo (solo copia CEDIBLE) — versión compacta para ir
    # junto al timbre (no le quita ancho a la tabla de arriba)
    acuse_html = ""
    if mostrar_cedible:
        leyenda_cedible = "CEDIBLE CON SU FACTURA" if es_guia else "CEDIBLE"
        acuse_html = f"""
<div style="border:1px solid #000;border-radius:3px;padding:6px 8px;width:230px;">
  <div style="text-align:right;font-size:10px;font-weight:bold;margin-bottom:3px;">{leyenda_cedible}</div>
  <div style="font-size:7px;margin-bottom:3px;">Nombre: _____________________</div>
  <div style="font-size:7px;margin-bottom:3px;">R.U.T.: __________ Fecha: __________</div>
  <div style="font-size:7px;margin-bottom:4px;">Firma: _____________ Recinto: _______</div>
  <div style="font-size:5.5px;color:#333;line-height:1.3;">
    El acuse de recibo que se declara en este acto, de acuerdo a lo dispuesto en la letra b) del
    Art. 4, y la letra c) del Art. 5 de la Ley 19.983, acredita que la entrega de mercaderías o
    servicio(s) prestado(s) ha(n) sido recibido(s).
  </div>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<title>{tipo_label} N° {folio}</title>
<style>
  @page {{ size: A4 portrait; margin: 12mm; }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html, body {{ font-family:Arial,Helvetica,sans-serif; font-size:11px; color:#000; background:#fff; width:210mm; }}
  /* body = columna flex con la altura útil de la hoja, para poder empujar
     el bloque del timbre al fondo con margin-top:auto. El min-height se
     ajustó a 287mm (con padding inferior chico) para que el timbre baje
     hasta el pie de la página en la impresión del navegador. */
  body {{ padding: 12mm 12mm 4mm 12mm; min-height: 300mm; display: flex; flex-direction: column; }}
</style>
</head><body>
{banner_html}
<div style="border-bottom:2px solid #000;padding-bottom:8px;margin-bottom:10px;display:flex;justify-content:space-between;align-items:flex-start;">
  <div>
    {logo_html}
    <div style="font-size:10px;color:#555;">S.I.I. — {empresa.ciudad or 'SANTIAGO'}</div>
    <div style="font-size:18px;font-weight:900;margin:2px 0;">{empresa.nombre}</div>
    <div style="font-size:11px;font-weight:500;">Giro: {empresa.giro}</div>
    <div style="font-size:11px;">{empresa.direccion} - {(empresa.comuna or '').upper()} - {(empresa.ciudad or '').upper()}</div>
    {f'<div style="font-size:10px;color:#555;">{empresa.correo}</div>' if getattr(empresa, "correo", None) else ""}
    {f'<div style="font-size:10px;color:#555;">Tel: {empresa.telefono}</div>' if getattr(empresa, "telefono", None) else ""}
  </div>
  <div style="border:2px solid {color_doc};border-radius:4px;padding:8px 14px;text-align:center;min-width:160px;">
    <div style="font-size:11px;font-weight:bold;color:{color_doc};margin-bottom:2px;">R.U.T. {empresa.rut}</div>
    <div style="font-size:12px;font-weight:bold;color:{color_doc};margin-bottom:4px;">{tipo_label}</div>
    <div style="font-size:22px;font-weight:bold;color:{color_doc};">N° {folio}</div>
  </div>
</div>
<div style="background:#f5f5f5;border:1px solid #ddd;border-radius:3px;padding:8px 10px;margin-bottom:10px;">
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;">
    <div style="grid-column:1/-1;display:flex;gap:4px;">
      <span style="font-size:9px;font-weight:bold;text-transform:uppercase;color:#666;">SEÑOR(ES):</span>
      <span style="font-size:11px;font-weight:600;border-bottom:1px solid #ccc;flex:1;">{doc.receptor_nombre}</span>
    </div>
    <div style="display:flex;gap:4px;">
      <span style="font-size:9px;font-weight:bold;text-transform:uppercase;color:#666;">R.U.T.:</span>
      <span style="font-size:11px;font-weight:600;border-bottom:1px solid #ccc;flex:1;">{doc.receptor_rut}</span>
    </div>
    <div style="display:flex;gap:4px;">
      <span style="font-size:9px;font-weight:bold;text-transform:uppercase;color:#666;">GIRO:</span>
      <span style="font-size:11px;font-weight:600;border-bottom:1px solid #ccc;flex:1;">{doc.receptor_giro or ''}</span>
    </div>
    <div style="grid-column:1/-1;display:flex;gap:4px;">
      <span style="font-size:9px;font-weight:bold;text-transform:uppercase;color:#666;">DIRECCIÓN:</span>
      <span style="font-size:11px;font-weight:600;border-bottom:1px solid #ccc;flex:1;">{doc.receptor_direccion or ''}</span>
    </div>
    <div style="display:flex;gap:4px;">
      <span style="font-size:9px;font-weight:bold;text-transform:uppercase;color:#666;">FECHA EMISIÓN:</span>
      <span style="font-size:11px;font-weight:600;border-bottom:1px solid #ccc;flex:1;">{fecha}</span>
    </div>
    <div style="display:flex;gap:4px;">
      <span style="font-size:9px;font-weight:bold;text-transform:uppercase;color:#666;">CONDICIÓN PAGO:</span>
      <span style="font-size:11px;font-weight:600;border-bottom:1px solid #ccc;flex:1;">{doc.condicion_pago or 'Contado'}</span>
    </div>
  </div>
</div>
{traslado_html}
<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-bottom:10px;">
  <thead><tr>
    <th style="background:#333;color:#fff;font-size:9px;padding:5px 6px;width:30px;text-align:center;">N°</th>
    <th style="background:#333;color:#fff;font-size:9px;padding:5px 6px;">CÓDIGO</th>
    <th style="background:#333;color:#fff;font-size:9px;padding:5px 6px;">DESCRIPCIÓN</th>
    <th style="background:#333;color:#fff;font-size:9px;padding:5px 6px;text-align:right;">CANT.</th>
    <th style="background:#333;color:#fff;font-size:9px;padding:5px 6px;text-align:right;">PRECIO UNIT.</th>
    <th style="background:#333;color:#fff;font-size:9px;padding:5px 6px;text-align:right;">%DESC.</th>
    <th style="background:#333;color:#fff;font-size:9px;padding:5px 6px;text-align:right;">MTO.DESC.</th>
    <th style="background:#333;color:#fff;font-size:9px;padding:5px 6px;text-align:right;">VALOR</th>
  </tr></thead>
  <tbody>{items_html}</tbody>
</table>
<div style="display:flex;justify-content:flex-end;margin-bottom:12px;">
  <div style="width:220px;">{totales_html}</div>
</div>
<div style="margin-top:auto;border-top:2px solid #000;padding-top:8px;display:flex;justify-content:space-between;align-items:flex-start;gap:10px;">
  <div>
    <div style="font-size:9px;font-weight:bold;text-transform:uppercase;">Timbre Electrónico SII</div>
    {timbre_img_html}
    <div style="font-size:8px;color:#555;">Res. 80 de 2014 — Verifique en: www.sii.cl</div>
  </div>
  {acuse_html}
  <div style="text-align:right;font-size:9px;">
    {tipo_label}<br/>N° {folio}<br/>Emisión: {fecha}<br/>RUT: {empresa.rut}
  </div>
</div>
<div style="margin-top:16px;border-top:1px solid #ccc;padding-top:6px;font-size:8px;color:#999;text-align:center;">
  Generado con YeparDTE · yeparsolutions.com
</div>
</body></html>"""
