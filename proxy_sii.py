#!/usr/bin/env python3
"""
Proxy SII para boletas electrónicas — YeparDTEcore v3
El proxy hace TODO desde Chile: obtiene token y envía a api.sii.cl

Endpoints:
  GET  /boleta/semilla  → semilla desde api.sii.cl
  POST /boleta/token    → token desde api.sii.cl
  POST /boleta/envio    → envía EnvioBOLETA a api.sii.cl con token fresco

Puerto: 8080
"""
import http.server
import urllib.request
import urllib.error
import urllib.parse
import json
import ssl
import logging
import email
import io

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("proxy-sii")

BASE_PROD = "https://api.sii.cl/recursos/v1"
CTX = ssl.create_default_context()

def sii_get(url):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
        return r.status, r.read(), r.headers.get("Content-Type","application/xml")

def sii_post(url, body, headers):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
            return r.status, r.read(), r.headers.get("Content-Type","application/json")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), "application/xml"

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.info(f"{self.client_address[0]} {fmt%args}")

    def do_GET(self):
        if self.path == "/boleta/semilla":
            s, rb, ct = sii_get(BASE_PROD + "/boleta.electronica.semilla")
            log.info(f"semilla -> {s} {len(rb)}b")
            self._resp(s, rb, ct)
        else:
            self._resp(404, b"not found", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else b""

        if self.path == "/boleta/token":
            hdrs = {"Content-Type": "application/xml"}
            auth = self.headers.get("Authorization","")
            cookie = self.headers.get("Cookie","")
            if auth: hdrs["Authorization"] = auth
            elif cookie: hdrs["Cookie"] = cookie
            s, rb, ct = sii_post(BASE_PROD + "/boleta.electronica.token", body, hdrs)
            log.info(f"token -> {s} {len(rb)}b")
            self._resp(s, rb, ct)

        elif self.path == "/boleta/envio":
            self._envio(body)

        else:
            self._resp(404, b"not found", "text/plain")

    def _envio(self, body):
        """
        Recibe el sobre XML firmado (EnvioBOLETA) de Railway.
        1. Obtiene semilla de api.sii.cl
        2. Firma la semilla con el certificado embebido en el sobre
           (no necesitamos — el token ya viene del header o lo pedimos nosotros)
        3. Envía a api.sii.cl con token fresco obtenido aquí mismo

        El token viene en Cookie: TOKEN=xxx desde Railway.
        Lo usamos directamente como Bearer en Authorization.
        """
        # Extraer token del header Cookie
        cookie = self.headers.get("Cookie", "")
        token = ""
        for part in cookie.split(";"):
            p = part.strip()
            if p.startswith("TOKEN="):
                token = p[6:].strip()
                break

        ct_in = self.headers.get("Content-Type", "")
        log.info(f"envio: token={token[:10]}... body={len(body)}b ct={ct_in[:40]}")

        if not token:
            log.warning("envio: sin token en Cookie")
            self._resp(400, b'{"error":"sin token"}', "application/json")
            return

        # Enviar a api.sii.cl con Authorization Bearer
        hdrs = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  ct_in,
        }

        s, rb, ct = sii_post(BASE_PROD + "/boleta.electronica.envio", body, hdrs)
        log.info(f"envio <- {s} {len(rb)}b: {rb[:300]}")
        self._resp(s, rb, ct)

    def _resp(self, status, body, ct):
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", 8080), Handler)
    log.info("Proxy SII v3 escuchando en :8080")
    server.serve_forever()
