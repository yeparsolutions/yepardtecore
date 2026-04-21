def _construir_input(self, datos: dict, folio: int, emisor: Emisor) -> InputDTE:
        # Extraer datos del receptor de forma segura
        r_data = datos.get("receptor", {})
        
        # FIX: Evitar error si 'acteco' no existe en el modelo Emisor
        # Intentamos obtenerlo, si falla (AttributeError), usamos None o un valor de 'datos'
        try:
            codigo_acteco = getattr(emisor, 'acteco', None)
        except AttributeError:
            codigo_acteco = None

        return InputDTE(
            tipo_dte      = datos["tipo_dte"],
            folio         = folio,
            fecha_emision = date.fromisoformat(datos.get("fecha_emision", date.today().isoformat())),
            emisor        = EmisorDTE(
                rut=emisor.rut, 
                razon_social=emisor.razon_social, 
                giro=emisor.giro,
                direccion=emisor.direccion, 
                comuna=emisor.comuna, 
                ciudad=emisor.ciudad,
                acteco=codigo_acteco # <--- Ahora es seguro
            ),
            receptor      = ReceptorDTE(
                rut=r_data.get("rut"),
                razon_social=r_data.get("razon_social"),
                giro=r_data.get("giro", "Particular"),
                direccion=r_data.get("direccion", "Ciudad"),
                comuna=r_data.get("comuna", "Santiago"),
                ciudad=r_data.get("ciudad", "Santiago")
            ),
            items         = [
                ItemDTEInput(
                    nombre          = i["nombre"],
                    cantidad        = float(i.get("cantidad", 1)),
                    precio_unitario = float(i["precio_unitario"]),
                    codigo          = i.get("codigo", ""),
                    exento          = bool(i.get("exento", False))
                ) for i in datos.get("items", [])
            ],
            ambiente      = emisor.ambiente
        )
