#!/usr/bin/env python3
"""
Proxy SII para boletas electrónicas — YeparDTEcore v4
Extrae el XML del multipart y lo envía como body directo a api.sii.cl

Puerto: 8080
"""
import http.server
import urllib.request
import urllib.error
import json
import ssl
import logging
import email
import io

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("proxy-sii")

BASE = "https://api.sii.cl/recursos/v1"
CTX  = ssl.create_default_context()

def sii_req(method, url, body=None, headers=None):
    req = urllib.request.Request(url, data=body,
                                  headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
            return r.status, r.read(), r.headers.get("Content-Type","")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), "text/xml"

def extraer_xml_de_multipart(body_bytes, content_type):
    """Extrae el archivo XML del multipart/form-data."""
    # Construir mensaje MIME con headers mínimos
    msg_str = f"Content-Type: {content_type}\r\n\r\n".encode() + body_bytes
    msg = email.message_from_bytes(msg_str)
    for part in msg.walk():
        cd = part.get("Content-Disposition", "")
        name = part.get_param("name", header="content-disposition") or ""
        if name == "archivo" or "xml" in (part.get_content_type() or "").lower():
            payload = part.get_payload(decode=True)
            if payload:
                log.info(f"XML extraído: {len(payload)}b")
                return payload
    log.warning("No se encontró archivo XML en multipart")
    return None

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.info(f"{self.client_address[0]} {fmt % args}")

    def do_GET(self):
        if self.path == "/boleta/semilla":
            s, rb, ct = sii_req("GET", BASE + "/boleta.electronica.semilla")
            log.info(f"semilla -> {s} {len(rb)}b")
            self._resp(s, rb, ct or "application/xml")
        else:
            self._resp(404, b"not found", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else b""

        if self.path == "/boleta/token":
            hdrs = {"Content-Type": "application/xml"}
            auth = self.headers.get("Authorization", "")
            cookie = self.headers.get("Cookie", "")
            if auth: hdrs["Authorization"] = auth
            elif cookie: hdrs["Cookie"] = cookie
            s, rb, ct = sii_req("POST", BASE + "/boleta.electronica.token",
                                  body, hdrs)
            log.info(f"token -> {s} {len(rb)}b")
            self._resp(s, rb, ct or "application/xml")

        elif self.path == "/boleta/envio":
            self._envio(body)
        else:
            self._resp(404, b"not found", "text/plain")

    def _envio(self, body):
        # Extraer token de Cookie
        cookie = self.headers.get("Cookie", "")
        token = ""
        for part in cookie.split(";"):
            p = part.strip()
            if p.startswith("TOKEN="):
                token = p[6:].strip()
                break

        ct_in = self.headers.get("Content-Type", "")
        log.info(f"envio: token={token[:10]}... body={len(body)}b")

        if not token:
            self._resp(400, b'{"error":"sin token"}', "application/json")
            return

        # Extraer XML del multipart
        xml_bytes = None
        if "multipart" in ct_in:
            xml_bytes = extraer_xml_de_multipart(body, ct_in)

        if not xml_bytes:
            # Si no hay multipart o no se pudo extraer, mandar body directo
            xml_bytes = body
            log.warning("Usando body completo como XML")

        # Enviar XML directo a api.sii.cl con Bearer
        hdrs = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/xml",
        }

        log.info(f"POST envio XML {len(xml_bytes)}b -> {BASE}/boleta.electronica.envio")
        s, rb, ct = sii_req("POST", BASE + "/boleta.electronica.envio",
                              xml_bytes, hdrs)
        log.info(f"envio <- {s} {len(rb)}b: {rb[:300]}")
        self._resp(s, rb, ct or "application/json")

    def _resp(self, status, body, ct):
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", 8080), Handler)
    log.info("Proxy SII v4 escuchando en :8080")
    server.serve_forever()
