import os
import requests
import time
import sqlite3
from lxml import etree
from datetime import datetime
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
    cursor.execute("SELECT count(*) FROM folios WHERE tipo_dte = 39")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO folios (tipo_dte, folio_desde, folio_hasta, ultimo_utilizado) VALUES (39, 1, 100, 0)")
    conn.commit()
    conn.close()

def generar_xml_boleta(folio, rut_emisor, razon_social, monto_total):
    """Genera la estructura XML de una Boleta Electrónica (Tipo 39)."""
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")
    
    # Estructura simplificada obligatoria del SII
    dte = etree.Element("DTE", version="1.0")
    documento = etree.SubElement(dte, "Documento", ID=f"F{folio}T39")
    encabezado = etree.SubElement(documento, "Encabezado")
    
    # Identificación del DTE
    id_doc = etree.SubElement(encabezado, "IdDoc")
    etree.SubElement(id_doc, "TipoDTE").text = "39"
    etree.SubElement(id_doc, "Folio").text = str(folio)
    etree.SubElement(id_doc, "FchEmis").text = fecha_hoy
    etree.SubElement(id_doc, "IndServicio").text = "3" # Boleta de servicios
    
    # Emisor (Yepar Solutions)
    emisor = etree.SubElement(encabezado, "Emisor")
    etree.SubElement(emisor, "RUTEmisor").text = rut_emisor
    etree.SubElement(emisor, "RznSoc").text = razon_social
    etree.SubElement(emisor, "GiroEmisor").text = "Servicios Tecnologicos"
    etree.SubElement(emisor, "DirOrigen").text = "Santiago"
    etree.SubElement(emisor, "CmnaOrigen").text = "Santiago"
    
    # Totales
    totales = etree.SubElement(encabezado, "Totales")
    etree.SubElement(totales, "MntTotal").text = str(monto_total)
    
    return etree.tostring(dte, encoding="ISO-8859-1", xml_declaration=True).decode("ISO-8859-1")

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

if __name__ == "__main__":
    print("\n--- 🚀 GENERADOR DE DTE YEPARDTECORE ---")
    try:
        init_db()
        
        # 1. Preparar Identidad
        pfx_blob = get_certificate_from_secret(PROJECT_ID, SECRET_ID)
        key, cert = unlock_certificate(pfx_blob, CERT_PASSWORD)
        print(f"✅ Firma validada para el proceso.")

        # 2. Generar XML de prueba
        print("📝 Generando XML de Boleta Folio 1...")
        xml_generado = generar_xml_boleta(1, "76000000-1", "YEPAR SOLUTIONS", 5000)
        
        # Guardamos el XML para verlo
        with open("boleta_prueba.xml", "w", encoding="ISO-8859-1") as f:
            f.write(xml_generado)
        
        print(f"✅ XML generado y guardado en: boleta_prueba.xml")
        print("\nPróximo paso: Integrar la firma electrónica sobre este XML.")

    except Exception as e:
        print(f"💥 ERROR: {e}")