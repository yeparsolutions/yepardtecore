# v4-fix-ambiente-produccion
# app/services/dte_service.py.
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import date

from app.models.dte    import DTE, ItemDTE as ItemDTEModel
from app.models.emisor import Emisor

from app.services.xml_builder        import XMLBuilder, InputDTE, EmisorDTE, ReceptorDTE, ItemDTE, ReferenciaDTE
from app.services.xml_builder_boleta import (
    XMLBuilderBoleta, InputBoleta, EmisorBoleta, ReceptorBoleta,
    ItemBoleta, ReferenciaBoleta,
)
from app.services.firma_digital import FirmaDigital
from app.services.caf_service   import CAFService

TIPOS_BOLETA = {39, 41}

logger = logging.getLogger("yepardtecore.dte")

TIPOS_SIGLAS = {
    33: "F", 34: "FE", 39: "B", 41: "BE", 52: "G", 56: "ND", 61: "NC"
}


class DTEService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.caf_service = CAFService(db)

    async def emitir(self, emisor_id: int, datos: dict, auto_enviar: bool = True) -> dict:
        # 1. Validar emisor — usar select() para evitar caché del identity map
        result = await self.db.execute(select(Emisor).where(Emisor.id == emisor_id))
        emisor = result.scalar_one_or_none()
        if not emisor:
            raise ValueError("Emisor no encontrado")

        logger.info(f"[DTE] emisor={emisor.rut} ambiente={emisor.ambiente}")

        cert = emisor.certificado_activo
        if not cert or not cert.certificado_p12:
            raise ValueError(f"Falta Certificado Digital para el emisor {emisor.rut}")

        # 2. Obtener folio y CAF
        tipo_dte = datos["tipo_dte"]
        folio, caf = await self.caf_service.obtener_siguiente_folio(
            emisor_id, tipo_dte, emisor.ambiente
        )

        # 3. Construir XML base — boletas usan builder y dataclasses distintos
        if tipo_dte in TIPOS_BOLETA:
            input_dte = self._construir_input_boleta(datos, folio, emisor)
            builder   = XMLBuilderBoleta(input_dte)
        else:
            input_dte = self._construir_input(datos, folio, emisor)
            builder   = XMLBuilder(input_dte)
        xml_sin_firma = builder.construir()

        # 4. Firma digital — usa AppDTE para timbre + firma XMLDSig
        try:
            firma = FirmaDigital(
                cert.certificado_p12,
                cert.certificado_password or "",
                ambiente=emisor.ambiente or "certificacion",
            )

            it1 = input_dte.items[0].nombre if input_dte.items else "PRODUCTO"

            xml_firmado_bytes = await firma.firmar_dte(
                xml_bytes     = xml_sin_firma,
                folio         = folio,
                tipo_dte      = tipo_dte,
                xml_caf       = caf.xml_caf,
                fecha_emision = input_dte.fecha_emision.isoformat(),
                rut_emisor    = emisor.rut,
                monto_total   = builder.monto_total,
                it1_nombre    = it1,
            )
            xml_firmado_str = xml_firmado_bytes.decode("ISO-8859-1")
        except Exception as e:
            logger.error(f"Falla en firma: {e}", exc_info=True)
            raise RuntimeError(f"Error al firmar digitalmente: Falla en firma individual: {e}. Verifique la clave del certificado.")

        # 5. Persistencia
        sigla     = TIPOS_SIGLAS.get(tipo_dte, "D")
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
            ambiente        = emisor.ambiente,
        )
        self.db.add(nuevo_dte)
        await self.db.flush()

        # 6. Guardar items
        for i, item_data in enumerate(input_dte.items, 1):
            self.db.add(ItemDTEModel(
                dte_id          = nuevo_dte.id,
                numero_linea    = i,
                nombre          = item_data.nombre,
                cantidad        = item_data.cantidad,
                precio_unitario = item_data.precio_unitario,
                monto_item      = item_data.monto_item,
                codigo          = item_data.codigo,
            ))

        await self.db.commit()

        return {
            "id":          nuevo_dte.id,
            "folio":       folio,
            "status":      "success",
            "xml_firmado": xml_firmado_str,
            "monto_total": builder.monto_total,
            "monto_neto":  builder.monto_neto,
            "monto_iva":   builder.monto_iva,
        }

    def _construir_input(self, datos: dict, folio: int, emisor: Emisor) -> InputDTE:
        r_data = datos.get("receptor", {})

        items_input = [
            ItemDTE(
                nombre          = i["nombre"],
                cantidad        = float(i.get("cantidad", 1)),
                precio_unitario = float(i["precio_unitario"]),
                descuento_pct   = float(i.get("descuento_pct", 0)),
                codigo          = i.get("codigo", ""),
                unidad          = i.get("unidad", ""),
                exento          = bool(i.get("exento", False)),
            )
            for i in datos.get("items", [])
        ]

        refs_data = datos.get("referencias", [])
        referencias = [
            ReferenciaDTE(
                tipo_doc_ref = r["tipo_doc_ref"] if str(r["tipo_doc_ref"]).upper() == "SET" else int(r["tipo_doc_ref"]),
                folio_ref    = int(r["folio_ref"]),
                fecha_ref    = date.fromisoformat(r["fecha_ref"]) if isinstance(r.get("fecha_ref"), str) else date.today(),
                razon_ref    = r.get("razon_ref", ""),
                cod_ref      = r.get("cod_ref", 0),
            )
            for r in refs_data
        ]

        es_nota = datos["tipo_dte"] in (56, 61)
        sin_items = len(datos.get("items", [])) == 0
        tiene_codref2 = es_nota and any(
            int(r.get("cod_ref", 0)) == 2
            for r in datos.get("referencias", [])
            if str(r.get("tipo_doc_ref","")).upper() != "SET"
        )
        forzar_cero = bool(datos.get("forzar_monto_cero", False)) or (es_nota and sin_items) or tiene_codref2

        return InputDTE(
            tipo_dte             = datos["tipo_dte"],
            folio                = folio,
            fecha_emision        = date.fromisoformat(datos.get("fecha_emision", date.today().isoformat())),
            forzar_monto_cero    = forzar_cero,
            emisor               = EmisorDTE(
                rut          = emisor.rut,
                razon_social = emisor.razon_social,
                giro         = emisor.giro,
                direccion    = emisor.direccion,
                comuna       = emisor.comuna,
                ciudad       = emisor.ciudad,
                telefono     = getattr(emisor, "telefono", "") or "",
                correo       = getattr(emisor, "correo", "") or "",
                acteco       = getattr(emisor, "acteco", "") or "",
            ),
            receptor             = ReceptorDTE(
                rut          = r_data.get("rut", "66666666-6"),
                razon_social = r_data.get("razon_social", "Consumidor Final"),
                giro         = r_data.get("giro", ""),
                direccion    = r_data.get("direccion", ""),
                comuna       = r_data.get("comuna", ""),
                ciudad       = r_data.get("ciudad", ""),
                correo       = r_data.get("correo", ""),
            ),
            items                = items_input,
            referencias          = referencias,
            ambiente             = emisor.ambiente,
            forma_pago           = int(datos.get("forma_pago", 1)),
            descuento_global_pct = float(datos.get("descuento_global_pct", 0)),
            indicador_traslado   = int(datos.get("indicador_traslado", 0)),
            indicador_despacho   = int(datos.get("indicador_despacho", 0)),
        )

    def _construir_input_boleta(self, datos: dict, folio: int, emisor: Emisor) -> InputBoleta:
        r_data = datos.get("receptor", {})

        items_input = [
            ItemBoleta(
                nombre          = i["nombre"],
                cantidad        = float(i.get("cantidad", 1)),
                precio_unitario = float(i["precio_unitario"]),
                descuento_pct   = float(i.get("descuento_pct", 0)),
                codigo          = i.get("codigo", ""),
                unidad          = i.get("unidad", ""),
                exento          = bool(i.get("exento", False)),
            )
            for i in datos.get("items", [])
        ]

        referencias = [
            ReferenciaBoleta(
                tipo_doc_ref = r["tipo_doc_ref"] if str(r["tipo_doc_ref"]).upper() == "SET" else int(r["tipo_doc_ref"]),
                folio_ref    = int(r["folio_ref"]),
                fecha_ref    = date.fromisoformat(r["fecha_ref"]) if isinstance(r.get("fecha_ref"), str) else date.today(),
                razon_ref    = r.get("razon_ref", ""),
            )
            for r in datos.get("referencias", [])
        ]

        return InputBoleta(
            tipo_dte         = datos["tipo_dte"],
            folio            = folio,
            fecha_emision    = date.fromisoformat(datos.get("fecha_emision", date.today().isoformat())),
            emisor           = EmisorBoleta(
                rut          = emisor.rut,
                razon_social = emisor.razon_social,
                giro         = emisor.giro,
                direccion    = emisor.direccion,
                comuna       = emisor.comuna,
                ciudad       = emisor.ciudad,
                acteco       = getattr(emisor, "acteco", "") or "",
                telefono     = getattr(emisor, "telefono", "") or "",
                correo       = getattr(emisor, "correo", "") or "",
            ),
            receptor         = ReceptorBoleta(
                rut          = r_data.get("rut", "66666666-6"),
                razon_social = r_data.get("razon_social", "Consumidor Final"),
                correo       = r_data.get("correo", ""),
            ),
            items            = items_input,
            referencias      = referencias,
            descuento_global_pct = float(datos.get("descuento_global_pct", 0)),
        )
