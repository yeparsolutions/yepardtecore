# app/services/dte_service.py
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import date

from app.models.dte    import DTE, ItemDTE
from app.models.emisor import Emisor

from app.services.xml_builder   import XMLBuilder, InputDTE, EmisorDTE, ReceptorDTE, ItemDTEInput
from app.services.firma_digital import FirmaDigital
from app.services.caf_service   import CAFService

logger = logging.getLogger("yepardtecore.dte")

TIPOS_SIGLAS = {
    33: "F", 34: "FE", 39: "B", 41: "BE", 52: "G", 56: "ND", 61: "NC"
}

class DTEService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.caf_service = CAFService(db)

    async def emitir(self, emisor_id: int, datos: dict, auto_enviar: bool = True) -> dict:
        # 1. Validar Emisor
        emisor = await self.db.get(Emisor, emisor_id)
        if not emisor:
            raise ValueError("Emisor no encontrado")

        cert = emisor.certificado_activo
        if not cert or not cert.certificado_p12:
            raise ValueError(f"Falta Certificado Digital para el emisor {emisor.rut}")

        # 2. Obtener Folio y CAF
        tipo_dte = datos["tipo_dte"]
        folio, caf = await self.caf_service.obtener_siguiente_folio(
            emisor_id, tipo_dte, emisor.ambiente
        )

        # 3. Construir XML base
        input_dte = self._construir_input(datos, folio, emisor)
        builder = XMLBuilder(input_dte)
        xml_sin_firma = builder.construir()

        # 4. Proceso de Firma
        try:
            firma = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
            xml_firmado_bytes = firma.firmar_dte(
                xml_bytes      = xml_sin_firma,
                folio         = folio,
                tipo_dte      = tipo_dte,
                fecha_emision = input_dte.fecha_emision,
                rut_emisor    = emisor.rut,
                monto_total   = builder.monto_total
            )
            xml_firmado_str = xml_firmado_bytes.decode("ISO-8859-1")
        except Exception as e:
            logger.error(f"Error en paso de firma: {str(e)}")
            raise RuntimeError(f"Error al firmar digitalmente: {str(e)}")

        # 5. Persistencia en Base de Datos
        sigla = TIPOS_SIGLAS.get(tipo_dte, "D")
        nuevo_dte = DTE(
            emisor_id       = emisor_id,
            tipo_dte        = tipo_dte,
            folio           = folio,
            folio_fmt       = f"{sigla}-{folio:08d}",
            rut_receptor    = datos.get("receptor", {}).get("rut"),
            nombre_receptor = datos.get("receptor", {}).get("razon_social"),
            monto_neto      = builder.monto_neto,
            monto_iva       = builder.monto_iva,
            monto_total     = builder.monto_total,
            xml_firmado     = xml_firmado_str,
            estado          = "PENDIENTE_ENVIO" if auto_enviar else "BORRADOR",
            ambiente        = emisor.ambiente
        )
        
        self.db.add(nuevo_dte)
        await self.db.flush() 

        # 6. Guardar Items detallados
        for i, item_data in enumerate(input_dte.items, 1):
            self.db.add(ItemDTE(
                dte_id          = nuevo_dte.id,
                numero_linea    = i,
                nombre          = item_data.nombre,
                cantidad        = item_data.cantidad,
                precio_unitario = item_data.precio_unitario,
                monto_item      = item_data.monto_item,
                codigo          = item_data.codigo
            ))

        await self.db.commit()
        
        return {
            "id": nuevo_dte.id,
            "folio": folio,
            "status": "success",
            "xml_firmado": xml_firmado_str
        }

    def _construir_input(self, datos: dict, folio: int, emisor: Emisor) -> InputDTE:
        r_data = datos.get("receptor", {})
        items_input = [
            ItemDTEInput(
                nombre          = i["nombre"],
                cantidad        = float(i.get("cantidad", 1)),
                precio_unitario = float(i["precio_unitario"]),
                codigo          = i.get("codigo", ""),
                exento          = bool(i.get("exento", False))
            ) for i in datos.get("items", [])
        ]

        return InputDTE(
            tipo_dte      = datos["tipo_dte"],
            folio         = folio,
            fecha_emision = date.fromisoformat(datos.get("fecha_emision", date.today().isoformat())),
            emisor        = EmisorDTE(
                rut=emisor.rut, razon_social=emisor.razon_social, giro=emisor.giro,
                direccion=emisor.direccion, comuna=emisor.comuna, ciudad=emisor.ciudad,
                acteco=getattr(emisor, 'acteco', None)
            ),
            receptor      = ReceptorDTE(
                rut=r_data.get("rut"),
                razon_social=r_data.get("razon_social"),
                giro=r_data.get("giro", "Particular"),
                direccion=r_data.get("direccion", "Ciudad"),
                comuna=r_data.get("comuna", "Santiago"),
                ciudad=r_data.get("ciudad", "Santiago")
            ),
            items         = items_input,
            ambiente      = emisor.ambiente
        )
