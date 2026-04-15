#!/bin/bash
# Generar los 5 casos de certificacion Boleta Electronica
# Uso: bash /tmp/generar_be.sh

API_KEY="yek_e15eda1650ba8fca5b2d5872d7e8a4f1ddaa748a060d9a1785017d936bc8b2ed"
URL="http://localhost:8000/v1/dte/emitir"

echo "=== Generando 5 casos BE ==="

echo "--- BE-1: Cambio aceite + Alineacion ---"
curl -s -X POST $URL \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"emisor_id":1,"tipo_dte":39,"items":[{"nombre":"Cambio de aceite","cantidad":1,"precio_unitario":19900},{"nombre":"Alineacion y balanceo","cantidad":1,"precio_unitario":9900}],"referencias":[{"tipo_doc_ref":808,"folio_ref":"1","fecha_ref":"2026-04-13","razon_ref":"CASO-1"}],"auto_enviar":false}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(f'folio={d.get(\"folio\")} id={d.get(\"dte_id\")} err={d.get(\"detail\")}')"

echo "--- BE-2: Papel de regalo ---"
curl -s -X POST $URL \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"emisor_id":1,"tipo_dte":39,"items":[{"nombre":"Papel de regalo","cantidad":17,"precio_unitario":120}],"referencias":[{"tipo_doc_ref":808,"folio_ref":"2","fecha_ref":"2026-04-13","razon_ref":"CASO-2"}],"auto_enviar":false}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(f'folio={d.get(\"folio\")} id={d.get(\"dte_id\")} err={d.get(\"detail\")}')"

echo "--- BE-3: Sandwic + Bebida ---"
curl -s -X POST $URL \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"emisor_id":1,"tipo_dte":39,"items":[{"nombre":"Sandwic","cantidad":2,"precio_unitario":1500},{"nombre":"Bebida","cantidad":2,"precio_unitario":550}],"referencias":[{"tipo_doc_ref":808,"folio_ref":"3","fecha_ref":"2026-04-13","razon_ref":"CASO-3"}],"auto_enviar":false}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(f'folio={d.get(\"folio\")} id={d.get(\"dte_id\")} err={d.get(\"detail\")}')"

echo "--- BE-4: Afecto + Exento ---"
curl -s -X POST $URL \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"emisor_id":1,"tipo_dte":39,"items":[{"nombre":"item afecto 1","cantidad":8,"precio_unitario":1590,"exento":false},{"nombre":"item exento 2","cantidad":2,"precio_unitario":1000,"exento":true}],"referencias":[{"tipo_doc_ref":808,"folio_ref":"4","fecha_ref":"2026-04-13","razon_ref":"CASO-4"}],"auto_enviar":false}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(f'folio={d.get(\"folio\")} id={d.get(\"dte_id\")} err={d.get(\"detail\")}')"

echo "--- BE-5: Arroz Kg ---"
curl -s -X POST $URL \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"emisor_id":1,"tipo_dte":39,"items":[{"nombre":"Arroz","cantidad":5,"precio_unitario":700,"unidad":"Kg"}],"referencias":[{"tipo_doc_ref":808,"folio_ref":"5","fecha_ref":"2026-04-13","razon_ref":"CASO-5"}],"auto_enviar":false}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(f'folio={d.get(\"folio\")} id={d.get(\"dte_id\")} err={d.get(\"detail\")}')"

echo "=== Listo ==="
