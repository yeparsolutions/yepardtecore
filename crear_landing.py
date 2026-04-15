import base64, os

# SVG Isotipo embebido (logo pequeño para nav y favicon)
isotipo_b64 = open('/opt/yepardtecore/static/IsotipoDTEcore.svg', 'rb').read() if os.path.exists('/opt/yepardtecore/static/IsotipoDTEcore.svg') else b''

html = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YeparDTEcore — API Facturación Electrónica Chile</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;600;700;800&family=Inter:wght@300;400;500&display=swap" rel="stylesheet">
</head>
<body>
<h1>Hola mundo</h1>
</body>
</html>"""

os.makedirs('/var/www/yepardtecore', exist_ok=True)
with open('/var/www/yepardtecore/index.html', 'w') as f:
    f.write(html)
print('Creado')
