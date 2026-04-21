# app/services/dte_service.py
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import date, datetime
from typing import Any

from app.models.dte    import DTE, ItemDTE
from app.models.emisor import Emisor

from app.services.xml_builder   import XMLBuilder, InputDTE, EmisorDTE, ReceptorDTE, ItemDTEInput
from app.services.firma_digital import FirmaDigital
from app.services.caf_service   import CAFService

logger = logging.getLogger("yepardtecore.dte")

class DTEService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.caf_service = CAFService(db)

    async def emitir(self, emisor_id: int, datos: dict, auto_enviar: bool = True) -> dict:
        # 1. Cargar Emisor
        emisor = await self.db.get(Emisor, emisor_id)
        if not emisor:
            raise ValueError("Emisor no encontrado")

        # 2. Obtener Folio y CAF
        tipo_dte = datos["tipo_dte"]
        folio, caf = await self.caf_service.obtener_siguiente_folio(
            emisor_id, tipo_dte, emisor.ambiente
        )

        # 3. Construir XML base (Siguiendo estructura del Ejemplo SII)
        input_dte = self._construir_input(datos, folio, emisor)
        builder = XMLBuilder(input_dte)
        xml_sin_firma = builder.construir()

        # 4. Proceso de Firma Digital
        cert = emisor.certificado_activo
        if not cert or not cert.certificado_p12:
            raise ValueError("Falta certificado digital P12 para firmar.")

        firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
        
        # Generar el XML firmado
        try:
            xml_firmado_bytes = firma.firmar_dte(
                xml_bytes = xml_sin_firma,
                folio    = folio,
                tipo_dte = tipo_dte,
                xml_caf  = caf.xml_caf  # Importante para el TED
            )
            xml_firmado_str = xml_firmado_bytes.decode("ISO-8859-1")
        except Exception as e:
            logger.error(f"Error crítico en firma digital: {e}")
            raise RuntimeError(f"Falla al firmar documento: {str(e)}")

        # 5. Guardar en Base de Datos (Cabecera)
        # Aseguramos que todos los campos existan para evitar el Error 500
        nuevo_dte = DTE(
            emisor_id       = emisor_id,
            tipo_dte        = tipo_dte,
            folio           = folio,
            rut_receptor    = datos.get("receptor", {}).get("rut"),
            nombre_receptor = datos.get("receptor", {}).get("razon_social"),
            monto_neto      = builder.monto_neto,
            monto_iva       = builder.monto_iva,
            monto_total     = builder.monto_total,
            xml_firmado     = xml_firmado_str,  # <-- Aquí estaba el error
            estado          = "PENDIENTE_ENVIO" if auto_enviar else "BORRADOR",
            ambiente        = emisor.ambiente
        )
        
        self.db.add(nuevo_dte)
        await self.db.flush() # Para obtener el ID

        # 6. Guardar Items
        for i, item_data in enumerate(input_dte.items, 1):
            db_item = ItemDTE(
                dte_id          = nuevo_dte.id,
                numero_linea    = i,
                nombre          = item_data.nombre,
                cantidad        = item_data.cantidad,
                precio_unitario = item_data.precio_unitario,
                monto_item      = item_data.monto_item,
                codigo          = item_data.codigo
            )
            self.db.add(db_item)

        await self.db.commit()
        
        return {
            "id": nuevo_dte.id,
            "folio": folio,
            "status": "success",
            "xml": xml_firmado_str[:100] + "..." # Solo para log
        }

    def _construir_input(self, datos: dict, folio: int, emisor: Emisor) -> InputDTE:
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
            items         = [
                ItemDTEInput(
                    nombre          = i["nombre"],
                    cantidad        = float(i.get("cantidad", 1)),
                    precio_unitario = float(i["precio_unitario"]),
                    codigo          = i.get("codigo", ""),
                    exento          = bool(i.get("exento", False))
                ) for i in datos.get("items", [])
            ],
            ambiente      = emisor.ambiente
        )
