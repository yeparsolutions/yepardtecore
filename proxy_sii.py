#!/usr/bin/env python3
"""
Proxy HTTP simple para el SII — YeparDTEcore
Recibe requests de Railway y las reenvía al SII como si fuera Chile.

Endpoints:
  POST /boleta/semilla  → GET  api.sii.cl/recursos/v1/boleta.electronica.semilla
  POST /boleta/token    → POST api.sii.cl/recursos/v1/boleta.electronica.token
  POST /boleta/envio    → POST api.sii.cl/recursos/v1/boleta.electronica.envio

Puerto: 8080
"""
import http.server
import urllib.request
import urllib.error
import json
import ssl
import logging

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("proxy-sii")

SII_BASE = "https://api.sii.cl/recursos/v1"

RUTAS = {
    "/boleta/semilla": ("GET",  f"{SII_BASE}/boleta.electronica.semilla"),
    "/boleta/token":   ("POST", f"{SII_BASE}/boleta.electronica.token"),
    "/boleta/envio":   ("POST", f"{SII_BASE}/boleta.electronica.envio"),
}

class ProxyHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        logger.info(f"{self.client_address[0]} {format % args}")

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        ruta = RUTAS.get(self.path)
        if not ruta:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"ruta no encontrada"}')
            return

        metodo, url_sii = ruta

        # Leer body si viene
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length) if length else None

        # Copiar headers relevantes
        headers = {}
        for h in ("Authorization", "Content-Type", "Accept", "Cookie"):
            v = self.headers.get(h)
            if v:
                headers[h] = v

        logger.info(f"→ {metodo} {url_sii} body={len(body or b'')}b")

        try:
            ctx = ssl.create_default_context()
            req = urllib.request.Request(url_sii, data=body,
                                         headers=headers, method=metodo)
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                status  = resp.status
                rbody   = resp.read()
                rct     = resp.headers.get("Content-Type", "application/json")

            logger.info(f"← {status} {len(rbody)}b")
            self.send_response(status)
            self.send_header("Content-Type", rct)
            self.send_header("Content-Length", str(len(rbody)))
            self.end_headers()
            self.wfile.write(rbody)

        except urllib.error.HTTPError as e:
            rbody = e.read()
            logger.warning(f"← HTTP {e.code}: {rbody[:200]}")
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(rbody)

        except Exception as ex:
            logger.error(f"Error: {ex}")
            self.send_response(502)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(ex)}).encode())


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", 8080), ProxyHandler)
    logger.info("Proxy SII escuchando en :8080")
    server.serve_forever()
