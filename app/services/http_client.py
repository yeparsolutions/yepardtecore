# app/services/http_client.py
# ═══════════════════════════════════════════════════════════════════
# Cliente HTTP centralizado que usa el proxy Vultr (Chile) para
# todas las llamadas al SII cuando está configurado.
#
# Uso:
#   from app.services.http_client import get_sii_client
#   async with get_sii_client() as client:
#       r = await client.get(url)
# ═══════════════════════════════════════════════════════════════════

import os
import httpx

def get_sii_client(timeout: float = 30.0) -> httpx.AsyncClient:
    """
    Retorna un AsyncClient de httpx configurado con el proxy Vultr
    si las variables HTTP_PROXY / HTTPS_PROXY están definidas.
    """
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    
    if proxy_url:
        proxies = {
            "http://":  proxy_url,
            "https://": proxy_url,
        }
        return httpx.AsyncClient(timeout=timeout, proxies=proxies)
    
    return httpx.AsyncClient(timeout=timeout)
