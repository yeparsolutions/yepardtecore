# app/services/dte_service.py
# ══════════════════════════════════════════════════════════════
# Orquestador principal del motor DTE - Versión Sincronizada
# ══════════════════════════════════════════════════════════════

import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import date, datetime, timezone
from typing import Any

# Importaciones corregidas según app/models/dte.py
from app.models.dte    import DTE, ItemDTE
from app.models.emisor import Emisor
from app.models.caf    import CAF

# Importaciones de servicios auxiliares
from app.services.xml_builder   import XMLBuilder, InputDTE, EmisorDTE, ReceptorDTE
from app.services.xml_builder   import ItemDTE as ItemDTEInput, ReferenciaDTE
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
        # 1. Verificar idempotencia
        idempotency_key = datos.get("idempotency_key") or datos.get("referencia_interna")
        if idempotency_key:
            dte_existente = await self._buscar_por_idempotency(emisor_id, idempotency_key)
            if dte_existente:
                return self._dte_a_dict(dte_existente)

        # 2. Cargar emisor y obtener folio
        emisor = await self._cargar_emisor(emisor_id)
        tipo_dte = datos["tipo_dte"]
        folio, caf = await self.caf_service.obtener_siguiente_folio(
            emisor_id, tipo_dte, emisor.ambiente
        )

        # 3. Construir XML
        input_dte = self._construir_input(datos, folio, emisor)
        builder = XMLBuilder(input_dte)
        xml_sin_firma = builder.construir()

        # 4. Firmar DTE
        cert = emisor.certificado_activo
        if not cert or not cert.certificado_p12:
            raise ValueError(f"Emisor {emisor.rut} sin certificado .p12")

        firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
        
        # Obtener primer item para el TED
        it1 = datos.get("items", [{}])[0].get("nombre", "PRODUCTO")[:40]
        
        xml_firmado_bytes = firma.firmar_dte(
            xml_bytes     = xml_sin_firma,
            folio         = folio,
            tipo_dte      = tipo_dte,
            xml_caf       = caf.xml_caf,
            fecha_emision = datos.get("fecha_emision", date.today().isoformat()),
            rut_emisor    = emisor.rut,
            monto_total   = builder.monto_total,
            it1_nombre    = it1,
        )
        xml_firmado_str = xml_firmado_bytes.decode("ISO-8859-1")

        # 5. Guardar en BD (Cabecera)
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

        # 6. Guardar Items (Relación corregida a ItemDTE)
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

        # 7. Envío automático si aplica
        track_id = None
        if auto_enviar:
            try:
                sender = SIISender(ambiente=emisor.ambiente)
                sobre = sender.construir_sobre(
                    dtes_xml=[xml_firmado_str],
                    rut_emisor=emisor.rut,
                    rut_enviador=firma.rut_certificado or emisor.rut,
                    firma_service=firma
                )
                res = await sender.enviar_sobre(
                    sobre, emisor.rut, firma.rut_certificado or emisor.rut,
                    p12_bytes=cert.certificado_p12, password=cert.certificado_password
                )
                track_id = res.get("track_id")
                dte_db.estado = "ENVIADO" if track_id else "ERROR_ENVIO"
                dte_db.track_id = track_id
            except Exception as e:
                logger.error(f"Error envío: {e}")
                dte_db.estado = "ERROR_ENVIO"

        await self.db.commit()
        return self._dte_a_dict(dte_db)

    async def _cargar_emisor(self, emisor_id: int) -> Emisor:
        emisor = await self.db.get(Emisor, emisor_id)
        if not emisor or not emisor.activo:
            raise ValueError("Emisor no encontrado o inactivo")
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
            "track_id": dte.track_id,
            "monto_total": dte.monto_total
        }

    def _construir_input(self, datos: dict, folio: int, emisor: Emisor) -> InputDTE:
        return InputDTE(
            tipo_dte=datos["tipo_dte"],
            folio=folio,
            fecha_emision=date.fromisoformat(datos.get("fecha_emision", date.today().isoformat())),
            emisor=EmisorDTE(
                rut=emisor.rut, razon_social=emisor.razon_social, giro=emisor.giro,
                direccion=emisor.direccion, comuna=emisor.comuna, ciudad=emisor.ciudad
            ),
            receptor=ReceptorDTE(
                rut=datos.get("receptor", {}).get("rut", "66666666-6"),
                razon_social=datos.get("receptor", {}).get("razon_social", "Consumidor Final")
            ),
            items=[
                ItemDTEInput(
                    nombre=i["nombre"],
                    cantidad=float(i.get("cantidad", 1)),
                    precio_unitario=float(i["precio_unitario"]),
                    codigo=i.get("codigo", ""),
                    exento=i.get("exento", False)
                ) for i in datos.get("items", [])
            ],
            ambiente=emisor.ambiente
        )
