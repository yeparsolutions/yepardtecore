#!/usr/bin/env python3
"""
Proxy SII para boletas electrónicas — YeparDTEcore
Convierte las llamadas de Railway al formato que espera api.sii.cl

Puerto: 8080
"""
import http.server
import urllib.request
import urllib.error
import json
import ssl
import cgi
import io
import logging

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("proxy-sii")

BASE = "https://api.sii.cl/recursos/v1"

class ProxyHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        logger.info(f"{self.client_address[0]} {format % args}")

    def do_GET(self):
        if self.path == "/boleta/semilla":
            self._proxy_get(BASE + "/boleta.electronica.semilla")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/boleta/token":
            self._proxy_post_xml(BASE + "/boleta.electronica.token")
        elif self.path == "/boleta/envio":
            self._proxy_envio(BASE + "/boleta.electronica.envio")
        else:
            self.send_response(404)
            self.end_headers()

    def _proxy_get(self, url):
        try:
            ctx = ssl.create_default_context()
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                rb = resp.read()
                ct = resp.headers.get("Content-Type", "application/xml")
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(rb)))
            self.end_headers()
            self.wfile.write(rb)
            logger.info(f"GET {url} -> 200 {len(rb)}b")
        except Exception as ex:
            logger.error(f"GET error: {ex}")
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(ex).encode())

    def _proxy_post_xml(self, url):
        """Reenvía el body XML tal cual con Authorization del header."""
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else b""
        # Token viene en Authorization o Cookie
        auth = self.headers.get("Authorization", "")
        cookie = self.headers.get("Cookie", "")
        headers = {"Content-Type": "application/xml"}
        if auth:
            headers["Authorization"] = auth
        elif cookie:
            headers["Cookie"] = cookie
        try:
            ctx = ssl.create_default_context()
            req = urllib.request.Request(url, data=body,
                                          headers=headers, method="POST")
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                rb = resp.read()
                ct = resp.headers.get("Content-Type", "application/xml")
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(rb)))
            self.end_headers()
            self.wfile.write(rb)
            logger.info(f"POST token -> 200 {len(rb)}b")
        except urllib.error.HTTPError as e:
            rb = e.read()
            logger.warning(f"POST token HTTP {e.code}")
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(rb)
        except Exception as ex:
            logger.error(f"POST token error: {ex}")
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(ex).encode())

    def _proxy_envio(self, url):
        """
        Railway manda multipart/form-data con:
          rutSender, dvSender, rutCompany, dvCompany, archivo (XML)
          Token en Cookie: TOKEN=xxx

        api.sii.cl espera:
          Authorization: Bearer TOKEN
          Content-Type: multipart/form-data
          Mismos campos multipart
        """
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else b""

        # Extraer token de la Cookie
        cookie = self.headers.get("Cookie", "")
        token = ""
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("TOKEN="):
                token = part[6:]
                break

        ct_original = self.headers.get("Content-Type", "")

        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        headers["Content-Type"] = ct_original

        logger.info(f"POST envio -> {url} token={token[:8]}... {len(body)}b")

        try:
            ctx = ssl.create_default_context()
            req = urllib.request.Request(url, data=body,
                                          headers=headers, method="POST")
            with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
                rb = resp.read()
                ct = resp.headers.get("Content-Type", "application/json")
                status = resp.status
            logger.info(f"POST envio <- {status} {len(rb)}b: {rb[:200]}")
            self.send_response(status)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(rb)))
            self.end_headers()
            self.wfile.write(rb)
        except urllib.error.HTTPError as e:
            rb = e.read()
            logger.warning(f"POST envio HTTP {e.code}: {rb[:300]}")
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(rb)
        except Exception as ex:
            logger.error(f"POST envio error: {ex}")
            self.send_response(502)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(ex)}).encode())


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", 8080), ProxyHandler)
    logger.info("Proxy SII escuchando en :8080")
    server.serve_forever()
