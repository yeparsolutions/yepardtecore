# app/services/planes_dtecore.py
# ══════════════════════════════════════════════════════════════
# Catálogo de planes de YeparDTEcore.
# Cada plan define cuántas apps (emisores) puede tener UNA cuenta
# y su precio anual. El precio guardado es NETO; Mercado Pago cobra
# el total con IVA (x1.19), igual que la suscripción individual actual.
# ══════════════════════════════════════════════════════════════

IVA = 0.19

PLANES = {
    "developer": {"nombre": "Developer", "apps": 1,  "precio_neto": 150000},
    "partner":   {"nombre": "Partner",   "apps": 3,  "precio_neto": 350000},
    "business":  {"nombre": "Business",  "apps": 5,  "precio_neto": 500000},
    "scale":     {"nombre": "Scale",     "apps": 10, "precio_neto": 850000},
}

PLAN_DEFAULT = "developer"


def plan_info(clave: str) -> dict:
    """Devuelve la definición del plan (o Developer si la clave no existe)."""
    return PLANES.get((clave or "").lower(), PLANES[PLAN_DEFAULT])


def precio_total(clave: str) -> int:
    """Total con IVA que se cobra en Mercado Pago para ese plan."""
    return round(plan_info(clave)["precio_neto"] * (1 + IVA))


def limite_apps(clave: str) -> int:
    """Cuántas apps (emisores) permite el plan."""
    return plan_info(clave)["apps"]
