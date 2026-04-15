import os
from lxml import etree
from datetime import datetime
from cryptography.hazmat.primitives.serialization import pkcs12

# CONFIGURACIÓN LOCAL EN VULTR
CERT_PATH = "tu_certificado.pfx"  # Asegúrate de subir tu .pfx a la misma carpeta
CERT_PASSWORD = "N4buc0n0d0s0r"

def generar_xml_boleta(folio, rut_emisor, razon_social, monto_total):
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")
    dte = etree.Element("DTE", version="1.0")
    doc = etree.SubElement(dte, "Documento", ID=f"F{folio}T39")
    enc = etree.SubElement(doc, "Encabezado")
    id_doc = etree.SubElement(enc, "IdDoc")
    etree.SubElement(id_doc, "TipoDTE").text = "39"
    etree.SubElement(id_doc, "Folio").text = str(folio)
    etree.SubElement(id_doc, "FchEmis").text = fecha_hoy
    emisor = etree.SubElement(enc, "Emisor")
    etree.SubElement(emisor, "RUTEmisor").text = rut_emisor
    etree.SubElement(emisor, "RznSoc").text = razon_social
    totales = etree.SubElement(enc, "Totales")
    etree.SubElement(totales, "MntTotal").text = str(monto_total)
    return dte

def firmar_xml(dte_element, pfx_path, password):
    from signxml import XMLSigner, methods
    
    # Leer el archivo PFX desde el disco
    with open(pfx_path, "rb") as f:
        pfx_blob = f.read()
    
    # CARGA COMPATIBLE (Evita el error 'object is not iterable')
    p12 = pkcs12.load_key_and_certificates(pfx_blob, password.encode())
    
    # Acceso robusto a los atributos
    key = getattr(p12, 'key', p12[0])
    cert = getattr(p12, 'cert', p12[1])
    
    signer = XMLSigner(
        method=methods.enveloped, 
        signature_algorithm="rsa-sha1", 
        digest_algorithm="sha1"
    )
    
    signed_node = signer.sign(dte_element, key=key, cert=cert)
    # El SII requiere codificación ISO-8859-1
    return etree.tostring(signed_node, encoding="ISO-8859-1", xml_declaration=True)

if __name__ == "__main__":
    print("\n--- 🚀 MOTOR DTE YEPAR: EJECUTANDO EN VULTR (CHILE) ---")
    try:
        if not os.path.exists(CERT_PATH):
            raise FileNotFoundError(f"No se encontró el certificado en: {CERT_PATH}")

        print(f"📝 Generando XML de prueba...")
        dte_base = generar_xml_boleta(1, "76000000-1", "YEPAR SOLUTIONS", 1000)
        
        print(f"🔏 Firmando con {CERT_PATH}...")
        xml_firmado = firmar_xml(dte_base, CERT_PATH, CERT_PASSWORD)
        
        with open("boleta_firmada.xml", "wb") as f:
            f.write(xml_firmado)
            
        print("\n✅ ¡LOGRADO! Archivo 'boleta_firmada.xml' generado exitosamente.")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
