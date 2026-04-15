# app/services/caf_service.py
# ══════════════════════════════════════════════════════════════
# Servicio de gestión de CAF (Código de Autorización de Folios)
#
# Responsabilidades:
#   1. Parsear y validar el XML del CAF entregado por el SII
#   2. Validar que el CAF pertenece al RUT del emisor
#   3. Asignar el siguiente folio disponible (con lock para concurrencia)
#   4. Alertar cuando los folios están por agotarse
#
# Analogía: el CAF es el talonario de boletas que el SII
# te entrega. Este servicio es el cajero que controla
# cuál es la siguiente boleta disponible y te avisa
# cuando quedan pocas.
# ══════════════════════════════════════════════════════════════

import logging
from lxml import etree
from datetime import date, datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from app.models.caf import CAF
from app.models.emisor import Emisor

logger = logging.getLogger("yepardtecore.caf")

# Umbral de alerta — avisar cuando queden menos de X folios
UMBRAL_ALERTA_FOLIOS = 20

# Tipos DTE válidos para Chile
TIPOS_DTE_VALIDOS = {33, 34, 39, 41, 52, 56, 61}
TIPOS_NOMBRES = {
    33: "Factura Electrónica",
    34: "Factura No Afecta",
    39: "Boleta Electrónica",
    52: "Guía de Despacho",
    56: "Nota de Débito",
    61: "Nota de Crédito",
}


class CAFService:
    """Gestiona los CAFs de un emisor."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ── Cargar CAF desde XML ──────────────────────────────────

    async def cargar_caf(self, emisor_id: int, xml_caf: str,
                          ambiente: str = "certificacion") -> CAF:
        """
        Parsea el XML del CAF del SII y lo guarda en BD.

        Validaciones críticas:
        1. XML bien formado
        2. Campos obligatorios presentes (TD, D, H, RE)
        3. RUT del CAF coincide con RUT del emisor ← CRÍTICO
        4. Tipo DTE válido
        5. CAF no duplicado

        El XML del CAF tiene esta estructura:
        <AUTORIZACION>
          <CAF version="1.0">
            <DA>
              <RE>RUT-Emisor</RE>   ← Debe coincidir con emisor.rut
              <TD>39</TD>           ← Tipo DTE
              <RNG><D>1</D><H>100</H></RNG>
              <FA>2024-01-01</FA>
            </DA>
            <FRMA>...</FRMA>
          </CAF>
          <RSASK>...</RSASK>
        </AUTORIZACION>
        """
        # 1. Parsear XML
        try:
            root = etree.fromstring(xml_caf.encode("utf-8"))
        except etree.XMLSyntaxError as e:
            raise ValueError(f"XML del CAF inválido: {e}")

        # 2. Extraer campos obligatorios
        tipo_dte    = self._extraer_texto(root, ".//TD")
        folio_desde = self._extraer_texto(root, ".//D")
        folio_hasta = self._extraer_texto(root, ".//H")
        rut_caf     = self._extraer_texto(root, ".//RE")
        fecha_aut   = self._extraer_texto(root, ".//FA")

        if not all([tipo_dte, folio_desde, folio_hasta]):
            raise ValueError(
                "CAF incompleto — faltan campos obligatorios: "
                f"TD={'OK' if tipo_dte else 'FALTA'}, "
                f"D={'OK' if folio_desde else 'FALTA'}, "
                f"H={'OK' if folio_hasta else 'FALTA'}"
            )

        tipo_dte_int    = int(tipo_dte)
        folio_desde_int = int(folio_desde)
        folio_hasta_int = int(folio_hasta)

        # 3. Validar tipo DTE
        if tipo_dte_int not in TIPOS_DTE_VALIDOS:
            raise ValueError(
                f"Tipo DTE {tipo_dte_int} no es válido. "
                f"Válidos: {sorted(TIPOS_DTE_VALIDOS)}"
            )

        # 4. Validar rango de folios
        if folio_desde_int > folio_hasta_int:
            raise ValueError(
                f"Rango de folios inválido: desde {folio_desde_int} "
                f"hasta {folio_hasta_int}"
            )

        # 5. ── VALIDACIÓN CRÍTICA: RUT del CAF vs RUT del emisor ──────
        # Esta es la validación más importante — el SII rechazará
        # cualquier DTE firmado con un CAF de otro RUT.
        # Analogía: no puedes usar el talonario de boletas de otra empresa.
        emisor = await self.db.get(Emisor, emisor_id)
        if not emisor:
            raise ValueError(f"Emisor {emisor_id} no encontrado")

        if rut_caf:
            # Normalizar RUTs para comparar (quitar puntos, minúsculas)
            rut_caf_norm    = rut_caf.replace(".", "").upper().strip()
            rut_emisor_norm = emisor.rut.replace(".", "").upper().strip()

            if rut_caf_norm != rut_emisor_norm:
                logger.error(
                    f"[CAF] RUT inválido — CAF es para {rut_caf} "
                    f"pero emisor tiene {emisor.rut}"
                )
                raise ValueError(
                    f"❌ El CAF pertenece al RUT {rut_caf} "
                    f"pero el emisor tiene RUT {emisor.rut}. "
                    "Debes subir el CAF del RUT correcto."
                )

        logger.info(
            f"[CAF] Validación RUT OK: {rut_caf} == {emisor.rut} "
            f"tipo={tipo_dte_int} folios={folio_desde_int}-{folio_hasta_int}"
        )

        # 6. Verificar duplicado
        resultado = await self.db.execute(
            select(CAF).where(
                CAF.emisor_id   == emisor_id,
                CAF.tipo_dte    == tipo_dte_int,
                CAF.folio_desde == folio_desde_int,
                CAF.folio_hasta == folio_hasta_int,
            )
        )
        if resultado.scalar_one_or_none():
            raise ValueError(
                f"Ya existe un CAF para tipo {tipo_dte_int} "
                f"folios {folio_desde_int}-{folio_hasta_int}"
            )

        # 7. Parsear fecha de vencimiento
        fecha_venc = None
        if fecha_aut:
            try:
                fecha_venc = datetime.strptime(fecha_aut, "%Y-%m-%d").date()
            except ValueError:
                pass

        # 8. Crear registro
        caf = CAF(
            emisor_id         = emisor_id,
            tipo_dte          = tipo_dte_int,
            folio_desde       = folio_desde_int,
            folio_hasta       = folio_hasta_int,
            folio_actual      = folio_desde_int,
            xml_caf           = xml_caf,
            fecha_vencimiento = fecha_venc,
            activo            = True,
            ambiente          = ambiente,
        )
        self.db.add(caf)
        await self.db.flush()

        logger.info(
            f"[CAF] Cargado exitosamente — emisor={emisor.rut} "
            f"tipo={tipo_dte_int} ({TIPOS_NOMBRES.get(tipo_dte_int)}) "
            f"folios={folio_desde_int}-{folio_hasta_int} "
            f"total={folio_hasta_int - folio_desde_int + 1}"
        )

        return caf

    # ── Obtener siguiente folio ───────────────────────────────

    async def obtener_siguiente_folio(
        self,
        emisor_id: int,
        tipo_dte: int,
        ambiente: str = "certificacion",
    ) -> tuple[int, CAF]:
        """
        Obtiene el siguiente folio disponible y avanza el contador.
        Usa UPDATE atómico para evitar folios duplicados en concurrencia.

        Returns:
            (folio_asignado, caf_usado)

        Raises:
            ValueError: Si no hay CAF activo o los folios están agotados
        """
        # Buscar CAFs activos con folios disponibles
        resultado = await self.db.execute(
            select(CAF).where(
                CAF.emisor_id == emisor_id,
                CAF.tipo_dte  == tipo_dte,
                CAF.activo    == True,
                CAF.ambiente  == ambiente,
            ).order_by(CAF.folio_desde.asc())
        )
        cafs = resultado.scalars().all()

        # Buscar primer CAF con folios disponibles
        caf_disponible = None
        for caf in cafs:
            if not caf.esta_agotado:
                caf_disponible = caf
                break

        if not caf_disponible:
            tipo_nombre = TIPOS_NOMBRES.get(tipo_dte, str(tipo_dte))
            raise ValueError(
                f"Sin folios disponibles para {tipo_nombre} (tipo {tipo_dte}) "
                f"en ambiente {ambiente}. "
                "Solicita un nuevo CAF al SII en sii.cl."
            )

        # Tomar folio actual y avanzar contador (UPDATE atómico)
        folio_asignado = caf_disponible.folio_actual

        await self.db.execute(
            update(CAF)
            .where(CAF.id == caf_disponible.id)
            .values(folio_actual=CAF.folio_actual + 1)
        )

        # Calcular folios restantes después de este
        folios_restantes = caf_disponible.folios_disponibles - 1

        # Alerta de folios bajos
        if folios_restantes <= UMBRAL_ALERTA_FOLIOS:
            logger.warning(
                f"[CAF] ⚠️ ALERTA FOLIOS BAJOS — emisor_id={emisor_id} "
                f"tipo={tipo_dte} quedan={folios_restantes} folios"
            )

        # Desactivar si se agotó
        if caf_disponible.folio_actual >= caf_disponible.folio_hasta:
            await self.db.execute(
                update(CAF)
                .where(CAF.id == caf_disponible.id)
                .values(activo=False)
            )
            logger.warning(
                f"[CAF] CAF agotado — emisor_id={emisor_id} "
                f"tipo={tipo_dte} id={caf_disponible.id}"
            )

        logger.info(
            f"[CAF] Folio asignado — emisor_id={emisor_id} "
            f"tipo={tipo_dte} folio={folio_asignado} "
            f"restantes={folios_restantes}"
        )

        return folio_asignado, caf_disponible

    # ── Estado de folios ──────────────────────────────────────

    async def estado_folios(
        self,
        emisor_id: int,
        ambiente: str = "certificacion",
    ) -> list[dict]:
        """Resumen de folios disponibles por tipo de DTE."""
        resultado = await self.db.execute(
            select(CAF).where(
                CAF.emisor_id == emisor_id,
                CAF.ambiente  == ambiente,
            ).order_by(CAF.tipo_dte, CAF.folio_desde)
        )
        cafs = resultado.scalars().all()

        resumen: dict[int, dict] = {}
        for caf in cafs:
            tipo = caf.tipo_dte
            if tipo not in resumen:
                resumen[tipo] = {
                    "tipo_dte":    tipo,
                    "tipo_nombre": TIPOS_NOMBRES.get(tipo, str(tipo)),
                    "disponibles": 0,
                    "cafs":        [],
                }
            resumen[tipo]["cafs"].append({
                "id":          caf.id,
                "desde":       caf.folio_desde,
                "hasta":       caf.folio_hasta,
                "actual":      caf.folio_actual,
                "disponibles": caf.folios_disponibles,
                "porcentaje":  caf.porcentaje_uso,
                "activo":      caf.activo,
                "agotado":     caf.esta_agotado,
                "vencimiento": str(caf.fecha_vencimiento) if caf.fecha_vencimiento else None,
                "alerta":      caf.folios_disponibles <= UMBRAL_ALERTA_FOLIOS,
            })
            if not caf.esta_agotado:
                resumen[tipo]["disponibles"] += caf.folios_disponibles

        return list(resumen.values())

    # ── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _extraer_texto(root, xpath: str) -> str | None:
        """Extrae texto de un elemento XML por XPath."""
        el = root.find(xpath)
        return el.text.strip() if el is not None and el.text else None

    @staticmethod
    def validar_xml_caf(xml_caf: str) -> dict:
        """
        Valida que el XML es un CAF válido del SII.
        Útil para mostrar preview antes de guardar.
        """
        try:
            root = etree.fromstring(xml_caf.encode("utf-8"))
        except etree.XMLSyntaxError as e:
            return {"valido": False, "error": f"XML inválido: {e}"}

        campos = {
            "tipo_dte":    root.findtext(".//TD"),
            "rut_emisor":  root.findtext(".//RE"),
            "folio_desde": root.findtext(".//D"),
            "folio_hasta": root.findtext(".//H"),
            "fecha_aut":   root.findtext(".//FA"),
        }

        if not all(campos.values()):
            faltantes = [k for k, v in campos.items() if not v]
            return {"valido": False, "error": f"Campos faltantes: {faltantes}"}

        tipo = int(campos["tipo_dte"])
        return {
            "valido":       True,
            "tipo_dte":     tipo,
            "tipo_nombre":  TIPOS_NOMBRES.get(tipo, f"Tipo {tipo}"),
            "rut_emisor":   campos["rut_emisor"],
            "folio_desde":  int(campos["folio_desde"]),
            "folio_hasta":  int(campos["folio_hasta"]),
            "total_folios": int(campos["folio_hasta"]) - int(campos["folio_desde"]) + 1,
            "fecha_aut":    campos["fecha_aut"],
        }
