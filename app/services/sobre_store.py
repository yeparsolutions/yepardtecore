# app/services/sobre_store.py
# ══════════════════════════════════════════════════════════════
# Guardarropa temporal de sobres firmados
#
# Analogía: el guardarropa de un teatro. Cuando DTEcore firma un
# sobre (EnvioDTE/EnvioBOLETA), lo cuelga aquí y entrega un ticket
# (sobre_id). El cliente (YeparDTE) pasea solo con el ticket; cuando
# dice "enviar", DTEcore retira el sobre ORIGINAL del gancho y lo
# sube al SII.
#
# ¿Por qué? Los bytes firmados son delicados: si viajan por
# JSON → base64 → JSON entre servicios, un solo byte recodificado
# rompe la firma digital. Guardándolos aquí, los bytes que firmó
# el motor son EXACTAMENTE los que llegan a maullin/palena.
#
# Es memoria de proceso (no BD) a propósito:
#   - Son datos efímeros de una sesión de certificación
#   - Cero migraciones, cero persistencia de datos de clientes
#   - Si Railway reinicia, el mensaje de error pide regenerar (claro)
# ══════════════════════════════════════════════════════════════

import uuid
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("yepardtecore.sobre_store")

# El perchero: {sobre_id: {"bytes": ..., "emisor_rut": ..., "expira": ...}}
_perchero: dict[str, dict] = {}

TTL_HORAS    = 6     # cuánto vive un sobre colgado sin que lo retiren
MAX_SOBRES   = 50    # capacidad del perchero — al llenarse, cae el más viejo


def _limpiar_vencidos() -> None:
    """Saca del perchero los sobres cuyo ticket ya venció."""
    ahora = datetime.now(timezone.utc)
    vencidos = [sid for sid, item in _perchero.items() if item["expira"] <= ahora]
    for sid in vencidos:
        del _perchero[sid]
    if vencidos:
        logger.info(f"[STORE] Limpiados {len(vencidos)} sobres vencidos")


def guardar(sobre_bytes: bytes, emisor_rut: str = "") -> str:
    """
    Cuelga un sobre firmado y devuelve el ticket (sobre_id).

    sobre_bytes debe ser el XML YA codificado en ISO-8859-1 —
    los mismos bytes exactos que se subirán al SII.
    """
    _limpiar_vencidos()

    # Perchero lleno → cae el sobre más antiguo (FIFO)
    if len(_perchero) >= MAX_SOBRES:
        mas_viejo = min(_perchero, key=lambda k: _perchero[k]["expira"])
        del _perchero[mas_viejo]
        logger.warning("[STORE] Perchero lleno — descartado el sobre más antiguo")

    sobre_id = uuid.uuid4().hex
    _perchero[sobre_id] = {
        "bytes":      sobre_bytes,
        "emisor_rut": emisor_rut,
        "expira":     datetime.now(timezone.utc) + timedelta(hours=TTL_HORAS),
    }
    logger.info(f"[STORE] Sobre guardado id={sobre_id[:8]}... "
                f"bytes={len(sobre_bytes)} ttl={TTL_HORAS}h")
    return sobre_id


def obtener(sobre_id: str) -> bytes | None:
    """
    Retira el sobre del gancho con el ticket. Devuelve None si el
    ticket no existe o venció (p.ej. el servicio se reinició).
    El sobre NO se borra al retirarlo — permite reintentar el envío
    si maullin corta la conexión.
    """
    _limpiar_vencidos()
    item = _perchero.get(sobre_id)
    if item is None:
        logger.warning(f"[STORE] Ticket no encontrado: {sobre_id[:8]}...")
        return None
    return item["bytes"]


def descartar(sobre_id: str) -> None:
    """Elimina un sobre tras un envío exitoso (el abrigo ya se retiró)."""
    if sobre_id in _perchero:
        del _perchero[sobre_id]
        logger.info(f"[STORE] Sobre descartado tras envío: {sobre_id[:8]}...")
