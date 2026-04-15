# app/services/pdf_service.py
# ══════════════════════════════════════════════════════════════
# Generación de PDF para DTE Chile — Formato YeparDTE
# Soporta Boleta (39) y Factura (33)
# ══════════════════════════════════════════════════════════════

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm, mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.pdfgen import canvas
import io
from datetime import datetime

# Colores
COLOR_AZUL    = colors.HexColor('#1a3a5c')
COLOR_GRIS    = colors.HexColor('#f5f5f5')
COLOR_BORDE   = colors.HexColor('#cccccc')
COLOR_ROJO    = colors.HexColor('#c0392b')
COLOR_AMARILLO = colors.HexColor('#f39c12')

TIPOS_NOMBRE = {
    33: 'FACTURA ELECTRÓNICA',
    34: 'FACTURA NO AFECTA',
    39: 'BOLETA ELECTRÓNICA',
    52: 'GUÍA DE DESPACHO',
    56: 'NOTA DE DÉBITO',
    61: 'NOTA DE CRÉDITO',
}


# Formatos disponibles
FORMATOS = {
    'a4':       'A4 portrait — factura estándar',
    'carta':    'Carta — 21.6x27.9 cm',
    'ticket80': 'Ticket 80mm — caja registradora',
    'ticket58': 'Ticket 58mm — impresora pequeña',
}

def generar_pdf_dte(dte_data: dict, emisor_data: dict, formato: str = None) -> bytes:
    """
    Genera PDF del DTE en formato YeparDTE.

    Args:
        dte_data:    Datos del DTE
        emisor_data: Datos del emisor
        formato:     Formato de impresión (a4, carta, ticket80, ticket58)
                     Si es None, usa el formato guardado en emisor_data
                     o detecta automáticamente por tipo de DTE

    Formatos disponibles:
        - a4:       A4 portrait (facturas)
        - carta:    Carta 21.6x27.9cm (facturas)
        - ticket80: Ticket 80mm (boletas caja)
        - ticket58: Ticket 58mm (boletas móvil)
    """
    tipo_dte = dte_data.get('tipo_dte', 39)

    # Prioridad: parámetro > preferencia emisor > detección automática
    fmt = formato or emisor_data.get('formato_pdf') or ('ticket80' if tipo_dte == 39 else 'a4')
    fmt = fmt.lower().strip()

    if fmt in ('ticket80', 'ticket58'):
        ancho = 8*cm if fmt == 'ticket80' else 5.8*cm
        return _generar_boleta(dte_data, emisor_data, ancho=ancho)
    elif fmt == 'carta':
        from reportlab.lib.pagesizes import letter
        return _generar_factura(dte_data, emisor_data, pagesize=letter)
    else:
        return _generar_factura(dte_data, emisor_data)


def _generar_factura(dte_data: dict, emisor_data: dict, pagesize=A4) -> bytes:
    """Genera factura en formato A4."""
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=pagesize,
        rightMargin=1.5*cm,
        leftMargin=1.5*cm,
        topMargin=1*cm,
        bottomMargin=1.5*cm,
    )

    styles = getSampleStyleSheet()

    def estilo(size=8, bold=False, align=TA_LEFT, color=colors.black):
        return ParagraphStyle(
            f'e{size}{bold}{align}',
            parent=styles['Normal'],
            fontSize=size,
            fontName='Helvetica-Bold' if bold else 'Helvetica',
            alignment=align,
            textColor=color,
            leading=size + 2,
        )

    tipo_nombre = TIPOS_NOMBRE.get(dte_data.get('tipo_dte', 33), 'DOCUMENTO TRIBUTARIO')
    folio       = dte_data.get('folio', 0)
    ambiente    = dte_data.get('ambiente', 'certificacion')

    contenido = []

    # ── Banner advertencia ─────────────────────────────────────
    if ambiente == 'certificacion':
        banner = Table([[
            Paragraph(
                '⚠ DOCUMENTO INTERNO — SIN VALIDEZ FISCAL — PENDIENTE CERTIFICACIÓN DTE',
                estilo(7, bold=True, align=TA_CENTER, color=colors.white)
            )
        ]], colWidths=[18*cm])
        banner.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), COLOR_ROJO),
            ('TOPPADDING', (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ]))
        contenido.append(banner)
        contenido.append(Spacer(1, 3*mm))

    # ── Encabezado: emisor + recuadro documento ────────────────
    rut_emisor  = emisor_data.get('rut', '')
    razon_social = emisor_data.get('razon_social', '')
    giro        = emisor_data.get('giro', '')
    direccion   = emisor_data.get('direccion', '')
    comuna      = emisor_data.get('comuna', '')
    ciudad      = emisor_data.get('ciudad', '')

    dir_completa = f"{direccion} - {comuna} - {ciudad}".upper()

    col_emisor = [
        [Paragraph(f'R.U.T. {rut_emisor}', estilo(9, bold=True))],
        [Paragraph(razon_social.upper(), estilo(11, bold=True, color=COLOR_AZUL))],
        [Paragraph(f'Giro: {giro}', estilo(8))],
        [Paragraph(dir_completa, estilo(8))],
    ]

    col_doc = [
        [Paragraph(tipo_nombre, estilo(10, bold=True, align=TA_CENTER, color=COLOR_AZUL))],
        [Paragraph(f'N° {folio:08d}', estilo(12, bold=True, align=TA_CENTER))],
        [Paragraph(f'S.I.I. — {ciudad or "Santiago"}', estilo(8, align=TA_CENTER))],
    ]

    tabla_emisor = Table([[
        Table(col_emisor, colWidths=[11*cm]),
        Table(col_doc, colWidths=[6*cm]),
    ]], colWidths=[11*cm, 6*cm])

    tabla_emisor.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOX', (1,0), (1,0), 1, COLOR_AZUL),
        ('TOPPADDING', (1,0), (1,0), 6),
        ('BOTTOMPADDING', (1,0), (1,0), 6),
    ]))
    contenido.append(tabla_emisor)
    contenido.append(Spacer(1, 3*mm))

    # ── Datos receptor ─────────────────────────────────────────
    fecha_emision = dte_data.get('fecha_emision', datetime.now().strftime('%Y-%m-%d'))
    fecha_fmt     = '-'.join(reversed(fecha_emision.split('-'))) if fecha_emision else ''
    rut_receptor  = dte_data.get('rut_receptor', '66.666.666-6')
    nombre_rec    = dte_data.get('nombre_receptor', 'Sin Nombre')
    giro_rec      = dte_data.get('giro_receptor', '')
    dir_rec       = dte_data.get('direccion_receptor', '')
    forma_pago    = 'Contado' if dte_data.get('forma_pago', 1) == 1 else 'Crédito'

    datos_receptor = [
        ['SEÑOR(ES):', nombre_rec, 'FECHA EMISIÓN:', fecha_fmt],
        ['R.U.T.:', rut_receptor, 'CONDICIÓN PAGO:', forma_pago],
        ['GIRO:', giro_rec, '', ''],
        ['DIRECCIÓN:', dir_rec, '', ''],
    ]

    tabla_receptor = Table(datos_receptor, colWidths=[2.5*cm, 7*cm, 3.5*cm, 4*cm])
    tabla_receptor.setStyle(TableStyle([
        ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'),
        ('FONTNAME', (2,0), (2,-1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 8),
        ('BOX', (0,0), (-1,-1), 0.5, COLOR_BORDE),
        ('INNERGRID', (0,0), (-1,-1), 0.25, COLOR_BORDE),
        ('TOPPADDING', (0,0), (-1,-1), 2),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ('LEFTPADDING', (0,0), (-1,-1), 4),
        ('BACKGROUND', (0,0), (-1,-1), COLOR_GRIS),
    ]))
    contenido.append(tabla_receptor)
    contenido.append(Spacer(1, 3*mm))

    # ── Tabla de items ─────────────────────────────────────────
    items = dte_data.get('items_json', [])
    filas = [['N°', 'CODIGO', 'DESCRIPCION', 'CANT.', 'PRECIO UNIT.', '%DESC.', 'VALOR']]

    for i, item in enumerate(items, 1):
        nombre   = item.get('nombre', '')
        codigo   = item.get('codigo', '')
        cantidad = item.get('cantidad', 1)
        precio   = item.get('precio_unitario', 0)
        desc_pct = item.get('descuento_pct', 0)
        total    = item.get('monto_item', cantidad * precio)
        filas.append([
            str(i), codigo, nombre,
            f"{cantidad:.0f}",
            f"${precio:,.0f}",
            f"{desc_pct:.0f}%",
            f"${total:,.0f}",
        ])

    tabla_items = Table(filas, colWidths=[0.8*cm, 2*cm, 6.5*cm, 1.5*cm, 2.5*cm, 1.5*cm, 2.2*cm])
    tabla_items.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), COLOR_AZUL),
        ('TEXTCOLOR',  (0,0), (-1,0), colors.white),
        ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTNAME',   (0,1), (-1,-1), 'Helvetica'),
        ('FONTSIZE',   (0,0), (-1,-1), 7),
        ('ALIGN',      (3,0), (-1,-1), 'RIGHT'),
        ('ALIGN',      (0,0), (2,-1), 'LEFT'),
        ('BOX',        (0,0), (-1,-1), 0.5, COLOR_BORDE),
        ('INNERGRID',  (0,0), (-1,-1), 0.25, COLOR_BORDE),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, COLOR_GRIS]),
        ('TOPPADDING',    (0,0), (-1,-1), 2),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ('LEFTPADDING',   (0,0), (-1,-1), 3),
        ('RIGHTPADDING',  (0,0), (-1,-1), 3),
    ]))
    contenido.append(tabla_items)
    contenido.append(Spacer(1, 3*mm))

    # ── Totales ────────────────────────────────────────────────
    monto_neto  = float(dte_data.get('monto_neto', 0))
    monto_iva   = float(dte_data.get('monto_iva', 0))
    monto_total = float(dte_data.get('monto_total', 0))

    filas_totales = []
    if monto_neto:
        filas_totales.append(['', 'MONTO NETO $', f'${monto_neto:,.0f}'])
        filas_totales.append(['', 'I.V.A. 19% $', f'${monto_iva:,.0f}'])
    filas_totales.append(['', 'TOTAL $', f'${monto_total:,.0f}'])

    tabla_totales = Table(filas_totales, colWidths=[10*cm, 4*cm, 4*cm])
    tabla_totales.setStyle(TableStyle([
        ('FONTNAME',  (1,0), (-1,-2), 'Helvetica-Bold'),
        ('FONTNAME',  (1,-1), (-1,-1), 'Helvetica-Bold'),
        ('FONTSIZE',  (0,0), (-1,-1), 8),
        ('ALIGN',     (1,0), (-1,-1), 'RIGHT'),
        ('BOX',       (1,0), (-1,-1), 0.5, COLOR_BORDE),
        ('INNERGRID', (1,0), (-1,-1), 0.25, COLOR_BORDE),
        ('BACKGROUND',(1,-1), (-1,-1), COLOR_AZUL),
        ('TEXTCOLOR', (1,-1), (-1,-1), colors.white),
        ('TOPPADDING',    (0,0), (-1,-1), 2),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
    ]))
    contenido.append(tabla_totales)
    contenido.append(Spacer(1, 5*mm))

    # ── Pie ────────────────────────────────────────────────────
    contenido.append(HRFlowable(width='100%', thickness=0.5, color=COLOR_BORDE))
    contenido.append(Spacer(1, 2*mm))
    contenido.append(Paragraph(
        'TIMBRE ELECTRÓNICO SII',
        estilo(7, bold=True, align=TA_CENTER, color=COLOR_AZUL)
    ))
    contenido.append(Paragraph(
        'Verifique documento en: www.sii.cl',
        estilo(7, align=TA_CENTER)
    ))
    contenido.append(Spacer(1, 2*mm))
    contenido.append(Paragraph(
        f'Generado con YeparDTE · by YeparSolutions · yepardte.yeparsolutions.com',
        estilo(6, align=TA_CENTER, color=colors.grey)
    ))

    doc.build(contenido)
    buffer.seek(0)
    return buffer.read()


def _generar_boleta(dte_data: dict, emisor_data: dict, ancho=None) -> bytes:
    """Genera boleta en formato ticket angosto."""
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=((ancho or 8*cm), 25*cm),
        rightMargin=0.3*cm,
        leftMargin=0.3*cm,
        topMargin=0.3*cm,
        bottomMargin=0.3*cm,
    )

    styles = getSampleStyleSheet()

    def estilo(size=7, bold=False, align=TA_CENTER, color=colors.black):
        return ParagraphStyle(
            f'b{size}{bold}{align}',
            parent=styles['Normal'],
            fontSize=size,
            fontName='Helvetica-Bold' if bold else 'Helvetica',
            alignment=align,
            textColor=color,
            leading=size + 2,
        )

    folio       = dte_data.get('folio', 0)
    ambiente    = dte_data.get('ambiente', 'certificacion')
    razon_social = emisor_data.get('razon_social', '')
    rut_emisor  = emisor_data.get('rut', '')
    giro        = emisor_data.get('giro', '')
    direccion   = emisor_data.get('direccion', '')
    comuna      = emisor_data.get('comuna', '')
    fecha_emision = dte_data.get('fecha_emision', datetime.now().strftime('%Y-%m-%d'))
    fecha_fmt   = '-'.join(reversed(fecha_emision.split('-'))) if fecha_emision else ''

    contenido = []

    # Banner certificacion
    if ambiente == 'certificacion':
        contenido.append(Paragraph(
            '⚠ SIN VALIDEZ FISCAL',
            estilo(6, bold=True, color=COLOR_ROJO)
        ))
        contenido.append(Spacer(1, 1*mm))

    # Emisor
    contenido.append(Paragraph(razon_social.upper(), estilo(9, bold=True, color=COLOR_AZUL)))
    contenido.append(Paragraph(f'R.U.T. {rut_emisor}', estilo(7, bold=True)))
    contenido.append(Paragraph(giro, estilo(6)))
    contenido.append(Paragraph(f'{direccion} - {comuna}', estilo(6)))
    contenido.append(Spacer(1, 2*mm))
    contenido.append(HRFlowable(width='100%', thickness=1, color=COLOR_AZUL))
    contenido.append(Spacer(1, 1*mm))

    # Tipo y folio
    contenido.append(Paragraph('BOLETA ELECTRÓNICA', estilo(9, bold=True, color=COLOR_AZUL)))
    contenido.append(Paragraph(f'N° {folio:08d}', estilo(8, bold=True)))
    contenido.append(Paragraph(f'Fecha: {fecha_fmt}', estilo(6)))
    contenido.append(Spacer(1, 2*mm))
    contenido.append(HRFlowable(width='100%', thickness=0.5, color=COLOR_BORDE))
    contenido.append(Spacer(1, 1*mm))

    # Items
    items = dte_data.get('items_json', [])
    for item in items:
        nombre   = item.get('nombre', '')
        cantidad = item.get('cantidad', 1)
        precio   = item.get('precio_unitario', 0)
        total    = item.get('monto_item', cantidad * precio)
        contenido.append(Paragraph(nombre, estilo(7, align=TA_LEFT)))
        contenido.append(Paragraph(
            f'{cantidad:.0f} x ${precio:,.0f} = ${total:,.0f}',
            estilo(6, align=TA_RIGHT)
        ))
        contenido.append(Spacer(1, 1*mm))

    contenido.append(HRFlowable(width='100%', thickness=0.5, color=COLOR_BORDE))
    contenido.append(Spacer(1, 1*mm))

    # Total
    monto_total = float(dte_data.get('monto_total', 0))
    contenido.append(Paragraph(f'TOTAL: ${monto_total:,.0f}', estilo(10, bold=True, color=COLOR_AZUL)))
    contenido.append(Spacer(1, 3*mm))

    # Pie
    contenido.append(HRFlowable(width='100%', thickness=0.5, color=COLOR_BORDE))
    contenido.append(Paragraph('TIMBRE ELECTRÓNICO SII', estilo(6, bold=True)))
    contenido.append(Paragraph('Verifique en: www.sii.cl', estilo(6)))
    contenido.append(Spacer(1, 1*mm))
    contenido.append(Paragraph(
        'Generado con YeparDTE · by YeparSolutions',
        estilo(5, color=colors.grey)
    ))

    doc.build(contenido)
    buffer.seek(0)
    return buffer.read()
