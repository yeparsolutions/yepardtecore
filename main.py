import os
import requests
import time
import sqlite3
from lxml import etree
from google.cloud import secretmanager
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.backends import default_backend

# --- CONFIGURACIÓN ---
PROJECT_ID = "yepardte-motor-2026"
SECRET_ID = "CERTIFICADO_SII"
CERT_PASSWORD = "N4buc0n0d0s0r"
DB_NAME = "yepardte.db"

# URL del SII (Pruebas)
URL_SEMILLA = "https://maullin.sii.cl/DTEWS/CrSeed.jws"

def init_db():
    """Crea la base de datos local y la tabla de folios si no existen."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS folios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo_dte INTEGER,
            folio_desde INTEGER,
            folio_hasta INTEGER,
            ultimo_utilizado INTEGER,
            fecha_carga DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Insertar un rango de prueba para Boletas (Tipo 39) si está vacío
    cursor.execute("SELECT count(*) FROM folios WHERE tipo_dte = 39")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO folios (tipo_dte, folio_desde, folio_hasta, ultimo_utilizado) VALUES (39, 1, 100, 0)")
    
    conn.commit()
    conn.close()

def get_certificate_from_secret(project_id, secret_id, version_id="latest"):
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version_id}"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data

def unlock_certificate(pfx_data, password):
    private_key, certificate, additional_certificates = pkcs12.load_key_and_certificates(
        pfx_data, password.encode(), default_backend()
    )
    return private_key, certificate

def get_sii_seed():
    payload = '<?xml version="1.0" encoding="UTF-8"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns1="http://DefaultNamespace"><SOAP-ENV:Body><ns1:getSeed/></SOAP-ENV:Body></SOAP-ENV:Envelope>'
    headers = {'Content-Type': 'text/xml;charset=UTF-8', 'SOAPAction': ''}
    try:
        response = requests.post(URL_SEMILLA, data=payload, headers=headers, timeout=10)
        if response.status_code == 200:
            xml_res = etree.fromstring(response.content)
            seed_text = xml_res.xpath("//getSeedReturn/text()", namespaces={'ns1': 'http://DefaultNamespace'})
            if seed_text:
                inner_xml = etree.fromstring(seed_text[0].encode('utf-8'))
                return inner_xml.xpath("//SEMILLA/text()")[0]
    except:
        return None
    return None

if __name__ == "__main__":
    print("\n--- 🚀 MOTOR YEPARDTECORE + DB LOCAL ---")
    try:
        # 1. Inicializar Base de Datos
        print("💾 Configurando base de datos local...")
        init_db()
        print("✅ Base de Datos lista (yepardte.db).")

        # 2. Identidad
        pfx_blob = get_certificate_from_secret(PROJECT_ID, SECRET_ID)
        key, cert = unlock_certificate(pfx_blob, CERT_PASSWORD)
        print(f"✅ Firma de {cert.subject.get_attributes_for_oid(pkcs12.x509.NameOID.COMMON_NAME)[0].value} lista.")

        # 3. SII
        print("🌐 Intentando contacto con el SII...")
        seed = get_sii_seed()
        if seed:
            print(f"✨ Semilla obtenida: {seed}")
        else:
            print("⚠️ El SII sigue sin responder (500). El motor reintentará más tarde.")

    except Exception as e:
        print(f"💥 ERROR: {e}")