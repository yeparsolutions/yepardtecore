# tools/tests/test_xml_builder_totales.py
# ══════════════════════════════════════════════════════════════════
# Test de regresión para lo más sensible del sistema: el cálculo de
# neto/IVA/total que va en el XML que firma y envía al SII.
#
# No prueba la firma ni el envío (necesitan certificado real y red).
# Prueba la aritmética: que MntNeto + IVA + MntExento == MntTotal
# SIEMPRE, incluso en los casos límite (.5 exactos) donde un redondeo
# ingenuo se desalinea — que es justo el bug que el comentario
# "FIX REDONDEO (2026-07-14)" en xml_builder.py describe haber
# corregido. Este test existe para que ese bug no vuelva sin que
# alguien se entere antes de que el SII rechace un DTE real.
#
# Cómo correrlo:
#   cd yepardtecore-main && pip install pytest && pytest tools/tests -q
# ══════════════════════════════════════════════════════════════════
import importlib.util
import os
import sys
from datetime import date

# Se carga xml_builder.py directo desde su archivo (en vez de
# "from app.services.xml_builder import ...") para NO disparar
# app/services/__init__.py, que importa dte_service.py y arrastra
# sqlalchemy/httpx/etc. — dependencias pesadas que este test de
# aritmética pura no necesita.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_PATH = os.path.join(_ROOT, "app", "services", "xml_builder.py")
_spec = importlib.util.spec_from_file_location("xml_builder", _PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["xml_builder"] = _mod  # @dataclass necesita resolver el módulo por nombre
_spec.loader.exec_module(_mod)

XMLBuilder    = _mod.XMLBuilder
InputDTE      = _mod.InputDTE
EmisorDTE     = _mod.EmisorDTE
ReceptorDTE   = _mod.ReceptorDTE
ItemDTE       = _mod.ItemDTE
_round_half_up = _mod._round_half_up

EMISOR = EmisorDTE(
    rut="76123456-7", razon_social="Empresa Test", giro="Servicios",
    direccion="Calle Falsa 123", comuna="Santiago", ciudad="Santiago",
)
RECEPTOR = ReceptorDTE(rut="66666666-6", razon_social="Consumidor Final")


def _totales(tipo_dte, items, **kwargs):
    datos = InputDTE(
        tipo_dte=tipo_dte, folio=1, fecha_emision=date.today(),
        emisor=EMISOR, receptor=RECEPTOR, items=items, **kwargs,
    )
    b = XMLBuilder(datos)
    return b


def test_factura_afecta_neto_iva_exento_suman_total():
    """Caso simple: factura afecta (33), un ítem con IVA. Debe cuadrar exacto."""
    items = [ItemDTE(nombre="Producto", cantidad=1, precio_unitario=10000)]
    b = _totales(33, items)
    assert b.monto_neto + b.monto_iva + b.monto_exento == b.monto_total
    assert b.monto_neto == 10000
    assert b.monto_iva == 1900          # 19% de 10000
    assert b.monto_total == 11900


def test_boleta_neto_mas_iva_es_monto_afecto():
    """Boleta (39): el precio YA incluye IVA — se separa neto/IVA por división."""
    items = [ItemDTE(nombre="Producto", cantidad=1, precio_unitario=11900)]
    b = _totales(39, items)
    assert b.monto_neto + b.monto_iva == 11900
    assert b.monto_total == 11900


def test_factura_exenta_no_calcula_iva():
    """Factura exenta (34): todo va a MntExento, IVA en cero."""
    items = [ItemDTE(nombre="Producto exento", cantidad=1, precio_unitario=5000, exento=True)]
    b = _totales(34, items)
    assert b.monto_iva == 0
    assert b.monto_neto == 0
    assert b.monto_total == 5000


def test_descuento_en_punto_5_no_desalinea_montos():
    """
    Caso límite que motivó el FIX REDONDEO: cuando bruto*pct/100 cae
    exactamente en .5, redondear el descuento Y el neto de forma
    INDEPENDIENTE puede pasarse por 1 peso del bruto real. El fix
    calcula MontoItem = bruto_int - descuento_ya_redondeado, así
    siempre suman exacto. Este test fija ese comportamiento.
    """
    # cantidad * precio_unitario = 40250; descuento 3% → 1207.5 exacto
    item = ItemDTE(nombre="Item límite", cantidad=1, precio_unitario=40250, descuento_pct=3)
    bruto_int = _round_half_up(item.cantidad * item.precio_unitario)
    assert item.monto_item_int + item.descuento_monto_int == bruto_int, (
        "MontoItem + DescuentoMonto debe sumar exactamente el bruto entero "
        "(si esto falla, el bug de redondeo del 2026-07-14 volvió)"
    )


def test_multiples_items_afectos_y_exentos_cuadran():
    items = [
        ItemDTE(nombre="Afecto 1", cantidad=3, precio_unitario=1990),
        ItemDTE(nombre="Afecto 2", cantidad=2, precio_unitario=3333),
        ItemDTE(nombre="Exento",   cantidad=1, precio_unitario=1500, exento=True),
    ]
    b = _totales(33, items)
    assert b.monto_neto + b.monto_iva + b.monto_exento == b.monto_total


def test_descuento_global_porcentual_cuadra():
    items = [ItemDTE(nombre="Producto", cantidad=10, precio_unitario=1234)]
    b = _totales(33, items, descuento_global_pct=7.5)
    assert b.monto_neto + b.monto_iva + b.monto_exento == b.monto_total
    assert b.monto_neto > 0 and b.monto_neto < 12340  # con descuento aplicado


def test_construir_xml_no_lanza_excepcion_y_usa_los_mismos_totales():
    """El XML final debe reflejar los mismos montos calculados en Python."""
    items = [ItemDTE(nombre="Producto", cantidad=2, precio_unitario=5000)]
    b = _totales(33, items)
    xml_bytes = b.construir()
    assert b'<MntNeto>10000</MntNeto>' in xml_bytes
    assert b'<IVA>1900</IVA>' in xml_bytes
    assert b'<MntTotal>11900</MntTotal>' in xml_bytes


def test_round_half_up_redondea_como_excel():
    assert _round_half_up(1207.5) == 1208
    assert _round_half_up(1207.4) == 1207
    assert _round_half_up(0.5) == 1
    assert _round_half_up(-0.5) == -1  # ROUND_HALF_UP redondea .5 alejándose de cero
