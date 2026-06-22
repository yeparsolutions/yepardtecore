# app/services/http_client.py
# ═══════════════════════════════════════════════════════════════════
# Cliente HTTP centralizado que enruta las llamadas al SII a través
# del proxy chileno cuando está configurado.
#
# Analogía: el proxy es un "despachador en Santiago". Railway (EE.UU.)
# le entrega el tráfico al despachador, y este lo presenta al SII desde
# su dirección chilena. El SII solo ve la IP de Chile, que sí acepta.
#
# Uso:
#   from app.services.http_client import get_sii_client
#   async with get_sii_client() as client:
#       r = await client.get(url)
#
# Para activarlo, definir en Railway la variable:
#   SII_PROXY_URL = http://USUARIO:CLAVE@IP_CHILENA:PUERTO
# (o las clásicas HTTP_PROXY / HTTPS_PROXY)
# ═══════════════════════════════════════════════════════════════════

import os
import httpx


def _get_proxy_url():
    """
    Busca la URL del proxy en las variables de entorno.
    Prioridad: SII_PROXY_URL (específica) > HTTPS_PROXY > HTTP_PROXY.
    """
    return (
        os.environ.get("SII_PROXY_URL")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
    )


def get_sii_client(timeout: float = 30.0) -> httpx.AsyncClient:
    """
    Retorna un AsyncClient de httpx que sale por el proxy chileno
    si hay una URL de proxy configurada; si no, conexión directa.

    NOTA: en httpx 0.27 el parámetro correcto es `proxy=` (singular,
    string), NO `proxies=` (plural, dict) que está deprecado.
    """
    proxy_url = _get_proxy_url()

    if proxy_url:
        return httpx.AsyncClient(timeout=timeout, proxy=proxy_url)

    return httpx.AsyncClient(timeout=timeout)
