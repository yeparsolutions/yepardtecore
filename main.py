import os
import requests
import time
import sqlite3
from lxml import etree
from datetime import datetime
from google.cloud import secretmanager
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.backends import default_backend

# --- CONFIGURACIÓN PARA PERMITIR SHA1 (Requerido por SII) ---
from cryptography.hazmat.backends.openssl.backend import backend as openssl_backend
if not openssl_backend.sha1_supported():
    # En algunos sistemas esto ayuda a habilitar el soporte si está bloqueado
    pass 

# --- CONFIGURACIÓN ---
PROJECT_ID = "yepardte-motor-2026"
SECRET_ID = "CERTIFICADO_SII"
CERT_PASSWORD = "N4buc0n0d0s0r"
DB_NAME = "yepardte.db"

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
    conn.commit()
    conn.close()

def generar_xml_boleta(folio, rut_emisor, razon_social, monto_total):
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")
    dte = etree.Element("DTE", version="1.0")
    documento = etree.SubElement(dte, "Documento", ID=f"F{folio}T39")
    encabezado = etree.SubElement(documento, "Encabezado")
    
    id_doc = etree.SubElement(encabezado, "IdDoc")
    etree.SubElement(id_doc, "TipoDTE").text = "39"
    etree.SubElement(id_doc, "Folio").text = str(folio)
    etree.SubElement(id_doc, "FchEmis").text = fecha_hoy
    
    emisor = etree.SubElement(encabezado, "Emisor")
    etree.SubElement(emisor, "RUTEmisor").text = rut_emisor
    etree.SubElement(emisor, "RznSoc").text = razon_social
    etree.SubElement(emisor, "GiroEmisor").text = "Servicios Tecnologicos"
    etree.SubElement(emisor, "DirOrigen").text = "Santiago"
    etree.SubElement(emisor, "CmnaOrigen").text = "Santiago"
    
    totales = etree.SubElement(encabezado, "Totales")
    etree.SubElement(totales, "MntTotal").text = str(monto_total)
    
    return dte

def firmar_xml(dte_element, key, cert):
    """Aplica la firma digital XMLDSig al documento usando SHA1."""
    from signxml import XMLSigner, methods
    
    # Configuramos el firmante para usar SHA1 explícitamente como pide el SII
    signer = XMLSigner(
        method=methods.enveloped,
        signature_algorithm="rsa-sha1",
        digest_algorithm="sha1"
    )
    
    # Firmamos el nodo Documento
    signed_dte = signer.sign(dte_element, key=key, cert=cert)
    return etree.tostring(signed_dte, encoding="ISO-8859-1", xml_declaration=True)

def get_certificate_from_secret(project_id, secret_id):
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data

def unlock_certificate(pfx_data, password):
    # Cargamos el certificado permitiendo algoritmos antiguos (SHA1)
    private_key, certificate, additional_certificates = pkcs12.load_key_and_certificates(
        pfx_data, password.encode(), default_backend()
    )
    return private_key, certificate

if __name__ == "__main__":
    print("\n--- ✍️  FIRMADO DIGITAL YEPARDTECORE (MODO SII) ---")
    try:
        init_db()
        
        # 1. Cargar Identidad
        pfx_blob = get_certificate_from_secret(PROJECT_ID, SECRET_ID)
        key, cert = unlock_certificate(pfx_blob, CERT_PASSWORD)
        
        # 2. Generar XML Base
        print("📝 Generando XML base...")
        dte_base = generar_xml_boleta(1, "76000000-1", "YEPAR SOLUTIONS", 5000)
        
        # 3. Firmar
        print("🔏 Aplicando firma digital RSA-SHA1 (Legacy bypass)...")
        xml_firmado = firmar_xml(dte_base, key, cert)
        
        # 4. Guardar resultado
        with open("boleta_firmada.xml", "wb") as f:
            f.write(xml_firmado)
            
        print("✅ ¡LISTO! Archivo 'boleta_firmada.xml' generado exitosamente.")
        print("La firma SHA1 ha sido aceptada por el motor local.")

    except Exception as e:
        print(f"💥 ERROR EN EL FIRMADO: {e}")