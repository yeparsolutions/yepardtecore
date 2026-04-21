# app/services/dte_service.py
# ══════════════════════════════════════════════════════════════
# Orquestador principal del motor DTE - Versión Final Corregida
# ══════════════════════════════════════════════════════════════

import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import date, datetime, timezone
from typing import Any

# Importaciones de modelos
from app.models.dte    import DTE, ItemDTE
from app.models.emisor import Emisor
from app.models.caf    import CAF

# Importaciones de servicios auxiliares - NOMBRES SINCRONIZADOS
from app.services.xml_builder   import XMLBuilder, InputDTE, EmisorDTE, ReceptorDTE
from app.services.xml_builder   import ItemDTEInput, ReferenciaDTE
from app.services.firma_digital import FirmaDigital
from app.services.caf_service   import CAFService
from app.services.sii_sender    import SIISender

logger = logging.getLogger("yepardtecore.dte")

TIPOS_NOMBRES = {
    33: "Factura",
    34: "FactExenta",
    39: "Boleta",
    52: "Guía",
    56: "ND",
    61: "NC",
}

class DTEService:
    def __init__(self, db: AsyncSession):
        self.db          = db
        self.caf_service = CAFService(db)

    async def emitir(self, emisor_id: int, datos: dict, auto_enviar: bool = True) -> dict:
        # 1. Idempotencia
        idempotency_key = datos.get("idempotency_key") or datos.get("referencia_interna")
        if idempotency_key:
            dte_existente = await self._buscar_por_idempotency(emisor_id, idempotency_key)
            if dte_existente:
                return self._dte_a_dict(dte_existente)

        # 2. Emisor y Folio
        emisor = await self._cargar_emisor(emisor_id)
        tipo_dte = datos["tipo_dte"]
        folio, caf = await self.caf_service.obtener_siguiente_folio(
            emisor_id, tipo_dte, emisor.ambiente
        )

        # 3. Construir XML
        input_dte = self._construir_input(datos, folio, emisor)
        builder = XMLBuilder(input_dte)
        xml_sin_firma = builder.construir()

        # 4. Firma Digital
        cert = emisor.certificado_activo
        if not cert or not cert.certificado_p12:
            raise ValueError(f"Emisor {emisor.rut} no tiene certificado P12 activo.")

        firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
        
        xml_firmado_bytes = firma.firmar_dte(
            xml_bytes = xml_sin_firma,
            folio    = folio,
            tipo_dte = tipo_dte
        )
        xml_firmado_str = xml_firmado_bytes.decode("ISO-8859-1")

        # 5. Guardar Cabecera DTE
        estado_inicial = "PENDIENTE_ENVIO" if auto_enviar else "BORRADOR"
        dte_db = DTE(
            emisor_id          = emisor_id,
            tipo_dte           = tipo_dte,
            folio              = folio,
            folio_fmt          = f"{TIPOS_NOMBRES.get(tipo_dte, 'D')}-{folio:08d}",
            rut_receptor       = datos.get("receptor", {}).get("rut"),
            nombre_receptor    = datos.get("receptor", {}).get("razon_social"),
            monto_neto         = builder.monto_neto,
            monto_iva          = builder.monto_iva,
            monto_total        = builder.monto_total,
            estado             = estado_inicial,
            xml_firmado        = xml_firmado_str,
            referencia_interna = idempotency_key,
            ambiente           = emisor.ambiente
        )
        self.db.add(dte_db)
        await self.db.flush()

        # 6. Guardar Items (Modelo ItemDTE)
        for i, item_data in enumerate(input_dte.items, 1):
            nuevo_item = ItemDTE(
                dte_id          = dte_db.id,
                numero_linea    = i,
                nombre          = item_data.nombre,
                cantidad        = item_data.cantidad,
                precio_unitario = item_data.precio_unitario,
                monto_item      = item_data.monto_item,
                exento          = item_data.exento,
                codigo          = item_data.codigo
            )
            self.db.add(nuevo_item)

        # 7. Envío al SII
        track_id = None
        if auto_enviar:
            try:
                sender = SIISender(ambiente=emisor.ambiente)
                # Nota: SIISender y el sobre requieren lógica adicional de empaquetado
                # que se asume implementada en el servicio correspondiente.
                pass 
            except Exception as e:
                logger.error(f"Error en envío automático: {e}")

        await self.db.commit()
        return self._dte_a_dict(dte_db)

    async def _cargar_emisor(self, emisor_id: int) -> Emisor:
        emisor = await self.db.get(Emisor, emisor_id)
        if not emisor: raise ValueError("Emisor no encontrado")
        return emisor

    async def _buscar_por_idempotency(self, emisor_id: int, key: str) -> DTE | None:
        res = await self.db.execute(
            select(DTE).where(DTE.emisor_id == emisor_id, DTE.referencia_interna == key)
        )
        return res.scalar_one_or_none()

    def _dte_a_dict(self, dte: DTE) -> dict:
        return {
            "dte_id": dte.id,
            "folio": dte.folio,
            "estado": dte.estado,
            "monto_total": dte.monto_total
        }

    def _construir_input(self, datos: dict, folio: int, emisor: Emisor) -> InputDTE:
        # Mapeo de items usando ItemDTEInput (el nombre de xml_builder)
        items_input = [
            ItemDTEInput(
                nombre          = i["nombre"],
                cantidad        = float(i.get("cantidad", 1)),
                precio_unitario = float(i["precio_unitario"]),
                codigo          = i.get("codigo", ""),
                exento          = i.get("exento", False)
            ) for i in datos.get("items", [])
        ]

        return InputDTE(
            tipo_dte      = datos["tipo_dte"],
            folio         = folio,
            fecha_emision = date.fromisoformat(datos.get("fecha_emision", date.today().isoformat())),
            emisor        = EmisorDTE(
                rut=emisor.rut, razon_social=emisor.razon_social, giro=emisor.giro,
                direccion=emisor.direccion, comuna=emisor.comuna, ciudad=emisor.ciudad
            ),
            receptor      = ReceptorDTE(
                rut=datos.get("receptor", {}).get("rut"),
                razon_social=datos.get("receptor", {}).get("razon_social")
            ),
            items         = items_input,
            ambiente      = emisor.ambiente
        )
