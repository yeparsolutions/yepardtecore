# app/services/dte_service.py
# ══════════════════════════════════════════════════════════════
# Orquestador principal del motor DTE
#
# Este servicio coordina todo el flujo de emisión:
#   1. Verificar idempotencia (evitar duplicados)
#   2. Obtener folio del CAF
#   3. Construir el XML del DTE
#   4. Firmar digitalmente con certificado del emisor
#   5. Guardar en BD con estado PENDIENTE_ENVIO
#   6. Enviar al SII
#   7. Actualizar estado según respuesta
#
# Analogía: si xml_builder es el escribano y firma_digital
# es el notario, este servicio es el gerente que coordina
# a ambos para que el documento llegue al SII correctamente.
#
# Estados del DTE:
#   BORRADOR          → generado, no se intentó enviar
#   PENDIENTE_ENVIO   → en cola para enviar al SII
#   EN_PROCESO        → enviado, esperando respuesta SII
#   ENVIADO           → sobre recibido por SII (tiene TrackID)
#   ACEPTADO          → SII procesó y aceptó sin problemas
#   ACEPTADO_CON_REPAROS → SII acepta con observaciones (válido)
#   RECHAZADO         → SII rechazó, hay que corregir
#   ERROR_ENVIO       → error de red/timeout al enviar
# ══════════════════════════════════════════════════════════════

import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import date, datetime, timezone

from app.models.dte    import DTE, ItemDTE as ItemDTEModel
from app.models.emisor import Emisor
from app.models.caf    import CAF

from app.services.xml_builder   import XMLBuilder, InputDTE, EmisorDTE, ReceptorDTE
from app.services.xml_builder   import ItemDTE as ItemDTEInput, ReferenciaDTE
from app.services.firma_digital import FirmaDigital
from app.services.caf_service   import CAFService
from app.services.sii_sender    import SIISender

# Logger dedicado — los logs de DTE van separados para fácil debugging
logger = logging.getLogger("yepardtecore.dte")

TIPOS_NOMBRES = {
    33: "Factura",
    34: "FactExenta",
    39: "Boleta",
    52: "Guía",
    56: "ND",
    61: "NC",
}

# Estados válidos del ciclo de vida de un DTE
ESTADOS_DTE = {
    "BORRADOR",
    "PENDIENTE_ENVIO",
    "EN_PROCESO",
    "ENVIADO",
    "ACEPTADO",
    "ACEPTADO_CON_REPAROS",
    "RECHAZADO",
    "ERROR_ENVIO",
    "ANULADO",
}


class DTEService:
    """
    Orquestador principal de emisión de DTE.

    Uso:
        service = DTEService(db)
        resultado = await service.emitir(emisor_id, datos_dte)
    """

    def __init__(self, db: AsyncSession):
        self.db          = db
        self.caf_service = CAFService(db)

    # ── Emisión principal ─────────────────────────────────────

    async def emitir(self, emisor_id: int, datos: dict,
                     auto_enviar: bool = True) -> dict:
        """
        Emite un DTE completo con idempotencia.

        Args:
            emisor_id:   ID del emisor en BD
            datos:       Dict con todos los datos del documento
            auto_enviar: Si True, envía al SII automáticamente

        Returns:
            dict con DTE guardado, XML y resultado del envío

        Idempotencia:
            Si se pasa 'idempotency_key' en datos y ya existe un DTE
            con esa clave para este emisor, retorna el DTE existente
            sin crear uno nuevo.
            Analogia: si mandas la misma carta dos veces con el mismo
            número de seguimiento, el correo solo la entrega una vez.
        """
        # ── 0. Verificar idempotencia ─────────────────────────
        idempotency_key = datos.get("idempotency_key") or datos.get("referencia_interna")
        if idempotency_key:
            dte_existente = await self._buscar_por_idempotency(emisor_id, idempotency_key)
            if dte_existente:
                logger.info(
                    f"[IDEMPOTENCIA] DTE ya existe para emisor={emisor_id} "
                    f"key={idempotency_key} → retornando DTE id={dte_existente.id}"
                )
                return self._dte_a_dict(dte_existente)

        # ── 1. Cargar emisor y validar ────────────────────────
        emisor = await self._cargar_emisor(emisor_id)

        # ── 2. Obtener folio del CAF ──────────────────────────
        tipo_dte = datos["tipo_dte"]
        ambiente = emisor.ambiente
        folio, caf = await self.caf_service.obtener_siguiente_folio(
            emisor_id, tipo_dte, ambiente
        )

        logger.info(
            f"[EMITIR] emisor={emisor.rut} tipo={tipo_dte} folio={folio} "
            f"ambiente={ambiente}"
        )

        # ── 3. Construir el XML ───────────────────────────────
        input_dte     = self._construir_input(datos, folio, emisor)
        builder       = XMLBuilder(input_dte)
        xml_sin_firma = builder.construir()

        # ── 4. Obtener certificado activo ─────────────────────
        # Primero busca en tabla certificados, luego fallback a emisor
        cert = emisor.certificado_activo
        if not cert or not cert.certificado_p12:
            raise ValueError(
                f"El emisor {emisor.rut} no tiene certificado digital cargado. "
                "Sube un certificado .p12 antes de emitir."
            )

        firma = FirmaDigital(
            cert.certificado_p12,
            cert.certificado_password or ""
        )

        logger.info(
            f"[FIRMA] Firmando con certificado de {firma.rut_certificado or 'emisor'} "
            f"vigente hasta {firma.vigente_hasta.date()}"
        )

        # ── 5. Firmar el DTE ──────────────────────────────────
        # Obtener nombre del primer item para el TED (IT1)
        it1 = datos.get("items", [{}])[0].get("nombre", "PRODUCTO")[:40]
        xml_firmado_bytes = firma.firmar_dte(
            xml_bytes     = xml_sin_firma,
            folio         = folio,
            tipo_dte      = tipo_dte,
            xml_caf       = caf.xml_caf,
            fecha_emision = datos.get("fecha_emision", date.today().strftime("%Y-%m-%d")),
            rut_emisor    = emisor.rut,
            monto_total   = builder.monto_total,
            it1_nombre    = it1,
        )
        xml_firmado = xml_firmado_bytes.decode("ISO-8859-1")

        # ── 6. Guardar DTE en BD ──────────────────────────────
        folio_fmt = f"{TIPOS_NOMBRES.get(tipo_dte, 'D')}-{folio:08d}"

        # Estado inicial según si se va a enviar o no
        estado_inicial = "PENDIENTE_ENVIO" if auto_enviar else "BORRADOR"

        dte = DTE(
            emisor_id          = emisor_id,
            tipo_dte           = tipo_dte,
            folio              = folio,
            folio_fmt          = folio_fmt,
            rut_receptor       = datos.get("receptor", {}).get("rut"),
            nombre_receptor    = datos.get("receptor", {}).get("razon_social"),
            giro_receptor      = datos.get("receptor", {}).get("giro"),
            direccion_receptor = datos.get("receptor", {}).get("direccion"),
            ciudad_receptor    = datos.get("receptor", {}).get("ciudad"),
            monto_neto         = builder.monto_neto,
            monto_iva          = builder.monto_iva,
            monto_total        = builder.monto_total,
            tasa_iva           = 19,
            estado             = estado_inicial,
            xml_firmado        = xml_firmado,
            referencia_interna = idempotency_key or datos.get("referencia_interna"),
            ambiente           = ambiente,
        )
        self.db.add(dte)
        await self.db.flush()

        logger.info(
            f"[BD] DTE guardado id={dte.id} folio={folio_fmt} "
            f"estado={estado_inicial} monto=${builder.monto_total:,.0f}"
        )

        # ── 7. Guardar items ──────────────────────────────────
        for idx, item_data in enumerate(datos.get("items", []), start=1):
            item = ItemDTEModel(
                dte_id          = dte.id,
                numero_linea    = idx,
                codigo          = item_data.get("codigo"),
                nombre          = item_data["nombre"],
                descripcion     = item_data.get("descripcion"),
                cantidad        = item_data.get("cantidad", 1.0),
                unidad          = item_data.get("unidad", "UN"),
                precio_unitario = item_data["precio_unitario"],
                descuento_pct   = item_data.get("descuento_pct", 0.0),
                monto_item      = ItemDTEInput(
                    nombre          = item_data["nombre"],
                    cantidad        = item_data.get("cantidad", 1.0),
                    precio_unitario = item_data["precio_unitario"],
                    descuento_pct   = item_data.get("descuento_pct", 0.0),
                ).monto_item,
            )
            self.db.add(item)

        # ── 8. Enviar al SII si se solicita ───────────────────
        track_id     = None
        estado_final = "BORRADOR" if not auto_enviar else "PENDIENTE_ENVIO"

        if auto_enviar:
            dte.estado = "EN_PROCESO"
            await self.db.flush()

            try:
                sender = SIISender(ambiente=ambiente)
                sobre  = sender.construir_sobre(
                    dtes_xml      = [xml_firmado],
                    rut_emisor    = emisor.rut,
                    rut_enviador  = firma.rut_certificado or emisor.rut,
                    firma_service = firma,
                )
                resultado_envio = await sender.enviar_sobre(
                    sobre,
                    emisor.rut,
                    firma.rut_certificado or emisor.rut,
                    p12_bytes = cert.certificado_p12,
                    password  = cert.certificado_password or "",
                )

                track_id = resultado_envio.get("track_id")

                if track_id:
                    estado_final = "ENVIADO"
                    logger.info(
                        f"[SII] DTE enviado correctamente id={dte.id} "
                        f"folio={folio_fmt} track_id={track_id}"
                    )
                else:
                    estado_final = "ERROR_ENVIO"
                    logger.warning(
                        f"[SII] Envío sin TrackID id={dte.id} "
                        f"respuesta={resultado_envio}"
                    )

            except Exception as e:
                estado_final = "ERROR_ENVIO"
                logger.error(
                    f"[SII] Error al enviar DTE id={dte.id} folio={folio_fmt}: {e}",
                    exc_info=True
                )

            dte.track_id = track_id
            dte.estado   = estado_final
            await self.db.flush()

        return {
            "dte_id":      dte.id,
            "folio":       folio,
            "folio_fmt":   folio_fmt,
            "tipo_dte":    tipo_dte,
            "monto_total": builder.monto_total,
            "monto_neto":  builder.monto_neto,
            "monto_iva":   builder.monto_iva,
            "estado":      estado_final,
            "track_id":    track_id,
            "xml_firmado": xml_firmado,
            "ambiente":    ambiente,
        }

    # ── Consulta de estado SII ────────────────────────────────

    async def consultar_estado_sii(self, dte_id: int) -> dict:
        """
        Consulta el estado de un DTE en el SII por TrackID.
        Actualiza el estado en BD según la respuesta.
        """
        resultado = await self.db.execute(select(DTE).where(DTE.id == dte_id))
        dte = resultado.scalar_one_or_none()
        if not dte:
            raise ValueError(f"DTE {dte_id} no encontrado")

        if not dte.track_id:
            return {
                "estado":  dte.estado,
                "mensaje": "DTE sin TrackID — no fue enviado al SII"
            }

        emisor  = await self._cargar_emisor(dte.emisor_id)
        sender  = SIISender(ambiente=dte.ambiente)
        estado  = await sender.consultar_estado(dte.track_id, emisor.rut)

        # Mapear estados SII a estados internos
        mapeo = {
            "ACEPTADO":  "ACEPTADO",
            "REPAROS":   "ACEPTADO_CON_REPAROS",
            "RECHAZADO": "RECHAZADO",
            "PENDIENTE": "ENVIADO",
        }
        nuevo_estado = mapeo.get(estado.get("estado"), dte.estado)

        if nuevo_estado != dte.estado:
            logger.info(
                f"[ESTADO] DTE id={dte_id} folio={dte.folio_fmt} "
                f"{dte.estado} → {nuevo_estado}"
            )
            dte.estado = nuevo_estado
            await self.db.flush()

        return {**estado, "dte_id": dte_id, "folio": dte.folio, "estado_bd": nuevo_estado}

    # ── Reenvío ───────────────────────────────────────────────

    async def reenviar(self, dte_id: int) -> dict:
        """
        Reenvía un DTE al SII.
        Reutiliza el XML firmado ya guardado — no genera uno nuevo.
        Útil cuando el primer envío falló por timeout o error de red.
        """
        resultado = await self.db.execute(select(DTE).where(DTE.id == dte_id))
        dte = resultado.scalar_one_or_none()
        if not dte:
            raise ValueError(f"DTE {dte_id} no encontrado")

        if not dte.xml_firmado:
            raise ValueError("DTE sin XML firmado — no se puede reenviar")

        if dte.estado == "ACEPTADO":
            raise ValueError("DTE ya fue aceptado por el SII — no se puede reenviar")

        emisor = await self._cargar_emisor(dte.emisor_id)
        cert   = emisor.certificado_activo
        if not cert or not cert.certificado_p12:
            raise ValueError("El emisor no tiene certificado digital")

        firma  = FirmaDigital(cert.certificado_p12, cert.certificado_password or "")
        sender = SIISender(ambiente=dte.ambiente)

        dte.estado = "EN_PROCESO"
        await self.db.flush()

        sobre = sender.construir_sobre(
            dtes_xml      = [dte.xml_firmado],
            rut_emisor    = emisor.rut,
            rut_enviador  = firma.rut_certificado or emisor.rut,
            firma_service = firma,
        )
        resultado_envio = await sender.enviar_sobre(
            sobre,
            emisor.rut,
            firma.rut_certificado or emisor.rut,
            p12_bytes = cert.certificado_p12,
            password  = cert.certificado_password or "",
        )

        track_id = resultado_envio.get("track_id")
        if track_id:
            dte.track_id = track_id
            dte.estado   = "ENVIADO"
            logger.info(f"[REENVIO] DTE id={dte_id} reenviado track_id={track_id}")
        else:
            dte.estado = "ERROR_ENVIO"
            logger.warning(f"[REENVIO] DTE id={dte_id} falló reenvío: {resultado_envio}")

        await self.db.flush()
        return resultado_envio

    # ── Helpers ───────────────────────────────────────────────

    async def _cargar_emisor(self, emisor_id: int) -> Emisor:
        """Carga y valida el emisor. Lanza ValueError si no existe o está inactivo."""
        emisor = await self.db.get(Emisor, emisor_id)
        if not emisor:
            raise ValueError(f"Emisor {emisor_id} no encontrado")
        if not emisor.activo:
            raise ValueError(f"Emisor {emisor_id} está desactivado")
        return emisor

    async def _buscar_por_idempotency(self, emisor_id: int, key: str) -> DTE | None:
        """
        Busca un DTE existente por su clave de idempotencia.
        Retorna None si no existe — se debe crear uno nuevo.
        """
        resultado = await self.db.execute(
            select(DTE).where(
                DTE.emisor_id          == emisor_id,
                DTE.referencia_interna == key,
            ).order_by(DTE.id.desc()).limit(1)
        )
        return resultado.scalar_one_or_none()

    def _dte_a_dict(self, dte: DTE) -> dict:
        """Convierte un DTE a dict de respuesta estándar."""
        return {
            "dte_id":      dte.id,
            "folio":       dte.folio,
            "folio_fmt":   dte.folio_fmt,
            "tipo_dte":    dte.tipo_dte,
            "monto_total": dte.monto_total,
            "monto_neto":  dte.monto_neto,
            "monto_iva":   dte.monto_iva,
            "estado":      dte.estado,
            "track_id":    dte.track_id,
            "xml_firmado": dte.xml_firmado,
            "ambiente":    dte.ambiente,
            "idempotente": True,  # indica que fue retornado por idempotencia
        }

    def _construir_input(self, datos: dict, folio: int, emisor: Emisor) -> InputDTE:
        """Construye el InputDTE a partir del dict de la request."""
        receptor_data = datos.get("receptor", {})
        items_data    = datos.get("items", [])
        refs_data     = datos.get("referencias", [])

        receptor = ReceptorDTE(
            rut          = receptor_data.get("rut", "66.666.666-6"),
            razon_social = receptor_data.get("razon_social", "Sin Nombre"),
            giro         = receptor_data.get("giro", ""),
            direccion    = receptor_data.get("direccion", ""),
            comuna       = receptor_data.get("comuna", ""),
            ciudad       = receptor_data.get("ciudad", ""),
            correo       = receptor_data.get("correo", ""),
        )

        emisor_dte = EmisorDTE(
            rut          = emisor.rut,
            razon_social = emisor.razon_social,
            giro         = emisor.giro,
            direccion    = emisor.direccion,
            comuna       = emisor.comuna,
            ciudad       = emisor.ciudad,
            telefono     = emisor.telefono or "",
        )

        items = [
            ItemDTEInput(
                nombre          = i["nombre"],
                cantidad        = float(i.get("cantidad", 1)),
                precio_unitario = float(i["precio_unitario"]),
                descuento_pct   = float(i.get("descuento_pct", 0)),
                codigo          = i.get("codigo", ""),
                unidad          = i.get("unidad", "UN"),
                exento          = bool(i.get("exento", False)),
            )
            for i in items_data
        ]

        referencias = [
            ReferenciaDTE(
                tipo_doc_ref = r["tipo_doc_ref"],
                folio_ref    = r["folio_ref"],
                fecha_ref    = date.fromisoformat(r["fecha_ref"]),
                razon_ref    = r.get("razon_ref", ""),
                cod_ref      = r.get("cod_ref", 0),
            )
            for r in refs_data
        ]

        fecha_str = datos.get("fecha_emision", date.today().isoformat())

        return InputDTE(
            tipo_dte             = datos["tipo_dte"],
            folio                = folio,
            fecha_emision        = date.fromisoformat(fecha_str),
            emisor               = emisor_dte,
            receptor             = receptor,
            items                = items,
            ambiente             = emisor.ambiente,
            referencias          = referencias,
            forma_pago           = datos.get("forma_pago", 1),
            observacion          = datos.get("observacion", ""),
            descuento_global_pct = float(datos.get("descuento_global_pct", 0.0)),
        )
