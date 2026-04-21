# app/services/dte_service.py
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import date
from app.models.dte import DTE, ItemDTE
from app.models.emisor import Emisor
from app.services.xml_builder import XMLBuilder, InputDTE, EmisorDTE, ReceptorDTE, ItemDTEInput
from app.services.firma_digital import FirmaDigital
from app.services.caf_service import CAFService

logger = logging.getLogger("yepardtecore.dte")

class DTEService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.caf_service = CAFService(db)

    async def emitir(self, emisor_id: int, datos: dict, auto_enviar: bool = True) -> dict:
        # 1. Cargar Emisor y su Certificado
        emisor = await self.db.get(Emisor, emisor_id)
        if not emisor:
            raise ValueError("Emisor no encontrado")

        # VALIDACIÓN CRÍTICA: ¿Tiene certificado?
        cert = emisor.certificado_activo
        if not cert or not cert.certificado_p12:
            # Este mensaje aparecerá en tu log de Railway para confirmarte el problema
            logger.error(f"EMISOR {emisor.rut} SIN CERTIFICADO P12")
            raise ValueError(f"El emisor {emisor.rut} no tiene un certificado digital (.p12) cargado en la base de datos.")

        # 2. Obtener Folio y CAF
        tipo_dte = datos["tipo_dte"]
        folio, caf = await self.caf_service.obtener_siguiente_folio(
            emisor_id, tipo_dte, emisor.ambiente
        )

        # 3. Construir XML
        input_dte = self._construir_input(datos, folio, emisor)
        builder = XMLBuilder(input_dte)
        xml_sin_firma = builder.construir()

        # 4. Firma Digital
        try:
            firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
            xml_firmado_bytes = firma.firmar_dte(
                xml_bytes = xml_sin_firma,
                folio    = folio,
                tipo_dte = tipo_dte,
                xml_caf  = caf.xml_caf
            )
            xml_firmado_str = xml_firmado_bytes.decode("ISO-8859-1")
        except Exception as e:
            logger.error(f"Error al firmar: {str(e)}")
            raise RuntimeError(f"La firma digital falló. Verifica la contraseña del certificado. Error: {str(e)}")

        # 5. Guardar en Base de Datos
        nuevo_dte = DTE(
            emisor_id       = emisor_id,
            tipo_dte        = tipo_dte,
            folio           = folio,
            folio_fmt       = f"{tipo_dte}-{folio}",
            rut_receptor    = datos.get("receptor", {}).get("rut"),
            nombre_receptor = datos.get("receptor", {}).get("razon_social"),
            monto_neto      = builder.monto_neto,
            monto_iva       = builder.monto_iva,
            monto_total     = builder.monto_total,
            xml_firmado     = xml_firmado_str, # Se guarda el XML ya firmado
            estado          = "PENDIENTE_ENVIO",
            ambiente        = emisor.ambiente
        )
        
        self.db.add(nuevo_dte)
        await self.db.flush() 

        # 6. Guardar Items
        for i, item_data in enumerate(input_dte.items, 1):
            self.db.add(ItemDTE(
                dte_id=nuevo_dte.id, numero_linea=i, nombre=item_data.nombre,
                cantidad=item_data.cantidad, precio_unitario=item_data.precio_unitario,
                monto_item=item_data.monto_item
            ))

        await self.db.commit()
        
        return {
            "id": nuevo_dte.id,
            "folio": folio,
            "status": "success",
            "xml_firmado": xml_firmado_str  # <--- Ahora garantizamos que esto existe
        }

    def _construir_input(self, datos: dict, folio: int, emisor: Emisor) -> InputDTE:
        # (Se mantiene igual que la versión anterior con la protección de acteco)
        pass
