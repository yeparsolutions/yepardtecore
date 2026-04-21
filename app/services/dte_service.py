# app/services/dte_service.py
# ══════════════════════════════════════════════════════════════
# Orquestador principal del motor DTE
# ══════════════════════════════════════════════════════════════

import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import date, datetime, timezone
from typing import Optional, List, Dict, Any

from app.models.dte import DTE, DTEItem, DTEReferencia
from app.models.emisor import Emisor
from app.models.caf import CAF
from app.models.certificado import Certificado
from app.services.xml_builder import XMLBuilder, InputDTE, EmisorDTE, ReceptorDTE, ItemDTE, ReferenciaDTE
from app.services.firma_digital import FirmaDigital
from app.services.sii_sender import SIISender

logger = logging.getLogger("yepardtecore.services.dte_service")

class DTEService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def emitir_dte(self, datos_input: Dict[str, Any], emisor_id: int) -> Dict[str, Any]:
        """
        Flujo completo de emisión: Validación -> Folio -> XML -> Firma -> Registro
        """
        # 1. Obtener Emisor y su Certificado
        emisor = await self.db.get(Emisor, emisor_id)
        if not emisor:
            raise Exception(f"Emisor con ID {emisor_id} no encontrado")

        stmt = select(Certificado).where(Certificado.emisor_id == emisor_id, Certificado.activo == True)
        result = await self.db.execute(stmt)
        cert = result.scalar_one_or_none()
        if not cert:
            raise Exception("No se encontró un certificado digital activo para este emisor")

        # 2. Validar y obtener Folio (CAF)
        tipo_dte = datos_input.get("tipo_dte")
        caf = await self._obtener_caf_valido(emisor_id, tipo_dte, emisor.ambiente)
        folio = caf.folio_actual
        
        # 3. Preparar datos para el XML Builder
        input_dte = self._mapear_a_input_dte(datos_input, emisor, folio)

        try:
            # 4. Construir XML Base
            builder = XMLBuilder(input_dte)
            xml_sin_firmar = builder.construir() # Retorna bytes en ISO-8859-1

            # 5. Firmar Documento
            # Pasamos los datos necesarios para la firma del Nodo DTE
            signer = FirmaDigital(cert.certificado_p12, cert.certificado_password)
            xml_firmado = signer.firmar_dte(
                xml_bytes=xml_sin_firmar,
                folio=folio,
                tipo_dte=tipo_dte,
                xml_caf=caf.xml_caf,
                fecha_emision=input_dte.fecha_emision,
                rut_emisor=emisor.rut,
                monto_total=builder.monto_total
            )

            # 6. Registrar en Base de Datos
            nuevo_dte = await self._guardar_dte_db(input_dte, builder, emisor_id, xml_firmado)
            
            # 7. Actualizar Folio en el CAF
            caf.folio_actual += 1
            await self.db.commit()

            return {
                "ok": True,
                "dte_id": nuevo_dte.id,
                "folio": folio,
                "xml_base64": xml_firmado.decode('ISO-8859-1')
            }

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Error en proceso de emisión: {str(e)}")
            raise e

    async def _obtener_caf_valido(self, emisor_id: int, tipo_dte: int, ambiente: str) -> CAF:
        stmt = select(CAF).where(
            CAF.emisor_id == emisor_id,
            CAF.tipo_dte == tipo_dte,
            CAF.ambiente == ambiente,
            CAF.activo == True
        )
        result = await self.db.execute(stmt)
        caf = result.scalar_one_or_none()

        if not caf:
            raise Exception(f"No hay CAF activo para tipo {tipo_dte} en ambiente {ambiente}")
        if caf.folio_actual > caf.folio_hasta:
            raise Exception(f"Folios agotados para tipo {tipo_dte}")
        
        return caf

    def _mapear_a_input_dte(self, datos: Dict[str, Any], emisor: Emisor, folio: int) -> InputDTE:
        # Mapeo de lógica de negocio a estructura de XML
        emisor_dte = EmisorDTE(
            rut=emisor.rut,
            razon_social=emisor.razon_social,
            giro=emisor.giro,
            direccion=emisor.direccion,
            comuna=emisor.comuna,
            ciudad=emisor.ciudad
        )

        receptor_data = datos.get("receptor", {})
        receptor = ReceptorDTE(
            rut=receptor_data.get("rut"),
            razon_social=receptor_data.get("razon_social"),
            giro=receptor_data.get("giro", "Particular"),
            direccion=receptor_data.get("direccion", "Ciudad"),
            comuna=receptor_data.get("comuna", "Santiago"),
            ciudad=receptor_data.get("ciudad", "Santiago")
        )

        items = [
            ItemDTE(
                nombre=i["nombre"],
                cantidad=i.get("cantidad", 1),
                precio_unitario=i["precio_unitario"],
                exento=i.get("exento", False)
            ) for i in datos.get("items", [])
        ]

        referencias = [
            ReferenciaDTE(
                tipo_doc_ref=r["tipo_doc_ref"],
                folio_ref=r["folio_ref"],
                fecha_ref=date.fromisoformat(r["fecha_ref"]),
                cod_ref=r.get("cod_ref", 0),
                razon_ref=r.get("razon_ref", "")
            ) for r in datos.get("referencias", [])
        ]

        return InputDTE(
            tipo_dte=datos["tipo_dte"],
            folio=folio,
            fecha_emision=date.today(),
            emisor=emisor_dte,
            receptor=receptor,
            items=items,
            referencias=referencias,
            ambiente=emisor.ambiente
        )

    async def _guardar_dte_db(self, input_dte: InputDTE, builder: Any, emisor_id: int, xml_firmado: bytes) -> DTE:
        nuevo_dte = DTE(
            emisor_id=emisor_id,
            tipo_dte=input_dte.tipo_dte,
            folio=input_dte.folio,
            rut_receptor=input_dte.receptor.rut,
            nombre_receptor=input_dte.receptor.razon_social,
            monto_neto=builder.monto_neto,
            monto_iva=builder.monto_iva,
            monto_total=builder.monto_total,
            xml_firmado=xml_firmado.decode('ISO-8859-1'),
            estado="PENDIENTE_ENVIO"
        )
        self.db.add(nuevo_dte)
        await self.db.flush() # Para obtener el ID antes del commit

        # Guardar items
        for item in input_dte.items:
            db_item = DTEItem(
                dte_id=nuevo_dte.id,
                nombre=item.nombre,
                cantidad=item.cantidad,
                precio_unitario=item.precio_unitario,
                monto_item=int(item.cantidad * item.precio_unitario)
            )
            self.db.add(db_item)

        return nuevo_dte
