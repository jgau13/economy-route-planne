import os
import sqlite3
import sys
import urllib.parse
import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import googlemaps
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
from dotenv import load_dotenv

load_dotenv()

basedir = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__, static_folder=basedir, static_url_path='')
CORS(app) 

# --- CONFIGURACIÓN ---
API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
FIREBASE_CONFIG = {
    "apiKey": os.environ.get("FIREBASE_API_KEY"),
    "authDomain": os.environ.get("FIREBASE_AUTH_DOMAIN"),
    "projectId": os.environ.get("FIREBASE_PROJECT_ID"),
    "storageBucket": os.environ.get("FIREBASE_STORAGE_BUCKET"),
    "messagingSenderId": os.environ.get("FIREBASE_MESSAGING_SENDER_ID"),
    "appId": os.environ.get("FIREBASE_APP_ID")
}

if not API_KEY:
    print("ADVERTENCIA: No se detectó GOOGLE_MAPS_API_KEY.", file=sys.stderr)

try:
    if API_KEY:
        gmaps = googlemaps.Client(key=API_KEY)
except ValueError as e:
    print(f"Error iniciando Google Maps: {e}", file=sys.stderr)

ALMACEN_COORD = "25.7617,-80.1918" 

# --- BASE DE DATOS ---
def init_db():
    db_path = os.path.join(basedir, 'economy_routes.db')
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        # TABLA V3: Agregamos 'formatted_address' para guardar la dirección limpia oficial
        c.execute('''CREATE TABLE IF NOT EXISTS direcciones_v3
                     (direccion TEXT PRIMARY KEY, latlng TEXT, place_id TEXT, formatted_address TEXT)''')
        conn.commit()
        conn.close()
        print("✅ Base de datos verificada (V3).")
    except Exception as e:
        print(f"❌ Error DB: {e}", file=sys.stderr)

init_db()

def obtener_datos_geo(direccion):
    """
    Retorna una tupla (latlng, formatted_address).
    formatted_address es la dirección postal LIMPIA (sin nombre de negocio).
    """
    db_path = os.path.join(basedir, 'economy_routes.db')
    direccion_clean = direccion.strip().lower()
    
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT latlng, formatted_address FROM direcciones_v3 WHERE direccion=?", (direccion_clean,))
        resultado = c.fetchone()
        
        if resultado:
            conn.close()
            return resultado[0], resultado[1]
        
        if not API_KEY: return None, None
        
        try:
            geocode_result = gmaps.geocode(direccion)
        except Exception as api_e:
            print(f"❌ Error API al geocodificar {direccion}: {api_e}", file=sys.stderr)
            conn.close()
            return None, None

        if geocode_result and len(geocode_result) > 0:
            res = geocode_result[0]
            loc = res['geometry']['location']
            latlng_str = f"{loc['lat']},{loc['lng']}"
            place_id = res.get('place_id', '')
            # CAPTURAMOS LA DIRECCIÓN LIMPIA OFICIAL
            formatted_addr = res.get('formatted_address', direccion) 
            
            c.execute("INSERT OR REPLACE INTO direcciones_v3 VALUES (?, ?, ?, ?)", (direccion_clean, latlng_str, place_id, formatted_addr))
            conn.commit()
            conn.close()
            return latlng_str, formatted_addr
        else:
            conn.close()
            return None, None

    except Exception as e:
        print(f"❌ Error DB/General: {e}", file=sys.stderr)
        try: conn.close()
        except: pass
    return None, None

def obtener_matriz_segura(puntos):
    num_puntos = len(puntos)
    if num_puntos == 0: return []
    
    max_filas_por_lote = max(1, int(100 / num_puntos))
    max_filas_por_lote = min(max_filas_por_lote, 25)
    
    matriz_completa = []
    
    for i in range(0, num_puntos, max_filas_por_lote):
        origenes_lote = puntos[i : i + max_filas_por_lote]
        try:
            respuesta = gmaps.distance_matrix(
                origins=origenes_lote,
                destinations=puntos,
                mode="driving",
                departure_time=datetime.datetime.now() 
            )
            
            if 'rows' in respuesta:
                matriz_completa.extend(respuesta['rows'])
            else:
                return None
        except Exception as e:
            print(f"Error matriz: {e}")
            return None
            
    return matriz_completa

# --- LÓGICA VRP ---
def crear_modelo_datos(lista_paradas, num_vans, base_address_text=None):
    datos = {}
    
    # Datos de la base
    coord_almacen = ALMACEN_COORD
    fmt_almacen = base_address_text # Fallback
    
    if base_address_text:
        c, fmt = obtener_datos_geo(base_address_text)
        if c: 
            coord_almacen = c
            fmt_almacen = fmt
        else:
            return {"error_critico": f"No se encontró la dirección BASE: {base_address_text}"}

    puntos = [coord_almacen]
    direcciones_limpias = [fmt_almacen] # Lista para guardar las versiones limpias
    paradas_validas = [] 
    paradas_erroneas = []
    
    for item in lista_paradas:
        dir_txt = item['direccion'] if isinstance(item, dict) else item
        nombre_txt = item['nombre'] if isinstance(item, dict) else "Cliente"
        
        c, fmt = obtener_datos_geo(dir_txt)
        if c:
            puntos.append(c)
            direcciones_limpias.append(fmt)
            # Guardamos la dirección original Y la limpia
            paradas_validas.append({
                "nombre": nombre_txt, 
                "direccion": dir_txt, # Mantenemos lo que el usuario escribió para mostrarlo
                "clean_address": fmt  # Usaremos esta para el link
            })
        else:
            paradas_erroneas.append(dir_txt)
    
    if paradas_erroneas:
        return {"invalidas": paradas_erroneas}

    if len(puntos) <= 1: return None

    if not API_KEY: return None
    rows_matriz = obtener_matriz_segura(puntos)
    
    if not rows_matriz: return None
    
    matriz_tiempos = []
    for fila in rows_matriz:
        fila_tiempos = []
        for elemento in fila['elements']:
            val_traffic = elemento.get('duration_in_traffic', {}).get('value')
            val_normal = elemento.get('duration', {}).get('value', 999999)
            valor = val_traffic if val_traffic else val_normal
            fila_tiempos.append(valor)
        matriz_tiempos.append(fila_tiempos)

    datos['time_matrix'] = matriz_tiempos
    datos['num_vehicles'] = int(num_vans)
    datos['depot'] = 0 
    datos['coords'] = puntos
    
    # Guardamos la base limpia en paradas_info para usarla
    base_obj = {"nombre": "Warehouse", "direccion": base_address_text or "Warehouse", "clean_address": fmt_almacen}
    datos['paradas_info'] = [base_obj] + paradas_validas
    return datos

def resolver_vrp(datos, dwell_time_minutos):
    manager = pywrapcp.RoutingIndexManager(len(datos['time_matrix']), datos['num_vehicles'], datos['depot'])
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        val = datos['time_matrix'][from_node][to_node]
        if to_node != 0: val += dwell_time_minutos * 60
        return val

    transit_callback_index = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)
    routing.AddDimension(transit_callback_index, 3600 * 24, 3600 * 24, True, 'Tiempo')
    
    time_dimension = routing.GetDimensionOrDie('Tiempo')
    time_dimension.SetGlobalSpanCostCoefficient(100)
    
    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.seconds = 1

    solution = routing.SolveWithParameters(search_parameters)
    rutas_finales = {}
    
    if solution:
        for vehicle_id in range(datos['num_vehicles']):
            index = routing.Start(vehicle_id)
            ruta = []
            
            while not routing.IsEnd(index):
                node_index = manager.IndexToNode(index)
                if node_index != 0:
                    info = datos['paradas_info'][node_index]
                    ruta.append({
                        "nombre": info['nombre'],
                        "direccion": info['direccion'], # Mostramos la original
                        "clean_address": info['clean_address'], # Guardamos la limpia
                        "coord": datos['coords'][node_index]
                    })
                    
                index = solution.Value(routing.NextVar(index))
            
            nombre_van = f"Van {vehicle_id + 1}"
            
            if ruta:
                # --- GENERACIÓN DE LINK LIMPIO (V3) ---
                # Usamos 'clean_address' que viene de Google, garantizado sin nombres de negocio extraños
                
                base_clean = datos['paradas_info'][0]['clean_address']
                base_encoded = urllib.parse.quote_plus(base_clean)
                base_url = "https://www.google.com/maps/dir/?api=1"
                
                stops_str = "&waypoints=" + "|".join([
                    urllib.parse.quote_plus(p['clean_address']) 
                    for p in ruta
                ])
                
                full_link = f"{base_url}&origin={base_encoded}&destination={base_encoded}{stops_str}"
                
                finish_index = routing.End(vehicle_id)
                tiempo_total = solution.Min(time_dimension.CumulVar(finish_index))
                
                rutas_finales[nombre_van] = {
                    "paradas": ruta,
                    "duracion_estimada": tiempo_total / 60,
                    "link": full_link
                }
            else:
                rutas_finales[nombre_van] = {"paradas": [], "duracion_estimada": 0, "link": ""}

    return rutas_finales

# --- OPTIMIZACIÓN PARCIAL ---
def resolver_tsp_parcial(fixed_stop, loose_stops, base_address_txt, dwell_time):
    c_start, fmt_start = obtener_datos_geo(fixed_stop['direccion'])
    c_end, fmt_end = obtener_datos_geo(base_address_txt)
    
    if not c_start or not c_end: return None

    coords = [c_start]
    # Guardamos objetos con la dirección limpia
    obj_start = fixed_stop.copy()
    obj_start['clean_address'] = fmt_start
    objetos_ordenados = [obj_start] 
    
    for s in loose_stops:
        c, fmt = obtener_datos_geo(s['direccion'])
        if c:
            coords.append(c)
            s_copy = s.copy()
            s_copy['clean_address'] = fmt
            objetos_ordenados.append(s_copy)
            
    coords.append(c_end)
    
    if not API_KEY: return None
    rows_matriz = obtener_matriz_segura(coords) 
    if not rows_matriz: return None

    time_matrix = []
    for r in rows_matriz:
        row = []
        for el in r['elements']:
            val_traffic = el.get('duration_in_traffic', {}).get('value')
            val_normal = el.get('duration', {}).get('value', 999999)
            row.append(val_traffic if val_traffic else val_normal)
        time_matrix.append(row)

    num_locations = len(coords)
    manager = pywrapcp.RoutingIndexManager(num_locations, 1, [0], [num_locations-1])
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        val = time_matrix[from_node][to_node]
        if to_node != num_locations - 1: val += dwell_time * 60
        return val

    transit_idx = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)

    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_params.time_limit.seconds = 1 

    solution = routing.SolveWithParameters(search_params)
    
    nuevo_orden_paradas = []
    
    if solution:
        index = routing.Start(0)
        index = solution.Value(routing.NextVar(index)) 
        
        while not routing.IsEnd(index):
            node_index = manager.IndexToNode(index)
            obj_original = objetos_ordenados[node_index]
            nuevo_orden_paradas.append(obj_original)
            index = solution.Value(routing.NextVar(index))
            
    return nuevo_orden_paradas

# --- RUTAS ---

@app.route('/')
def serve_frontend():
    return send_from_directory(basedir, 'index.html')

@app.route('/config')
def get_config():
    return jsonify({"googleApiKey": API_KEY, "firebaseConfig": FIREBASE_CONFIG})

@app.route('/optimizar', methods=['POST'])
def optimizar():
    if not API_KEY: return jsonify({"error": "Falta API KEY."}), 500
    try:
        data = request.json
        if not data or 'direcciones' not in data: return jsonify({"error": "Faltan datos"}), 400
        
        dwell_time = int(data.get('dwell_time', 10))
        
        lista_raw = data['direcciones']
        lista_normalizada = []
        for item in lista_raw:
            if isinstance(item, str): lista_normalizada.append({"nombre": "Cliente", "direccion": item})
            else: lista_normalizada.append(item)

        modelo = crear_modelo_datos(lista_normalizada, data.get('num_vans', 1), data.get('base_address'))
        
        if isinstance(modelo, dict):
            if "invalidas" in modelo: return jsonify({"error": "Direcciones no encontradas", "invalid_addresses": modelo["invalidas"]}), 400
            if "error_critico" in modelo: return jsonify({"error": modelo["error_critico"]}), 400
        
        if not modelo: return jsonify({"error": "No hay direcciones válidas."}), 400
        
        resultado = resolver_vrp(modelo, dwell_time)
        return jsonify(resultado)
        
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return jsonify({"error": f"Error interno: {str(e)}"}), 500

@app.route('/optimizar_restantes', methods=['POST'])
def optimizar_restantes():
    if not API_KEY: return jsonify({"error": "Falta API Key"}), 500
    data = request.json
    paradas_actuales = data.get('paradas', [])
    base_address = data.get('base_address')
    dwell_time = int(data.get('dwell_time', 10))
    
    paradas_objs = []
    for p in paradas_actuales:
        if isinstance(p, dict) and 'nombre' in p: paradas_objs.append(p)
        elif isinstance(p, dict): paradas_objs.append({"nombre": "Cliente", "direccion": p.get('direccion', '')})
        else: paradas_objs.append({"nombre": "Cliente", "direccion": p})

    if len(paradas_objs) < 3:
        return recalcular_ruta_internal(paradas_objs, base_address, dwell_time) 

    fixed_stop = paradas_objs[0]
    loose_stops = paradas_objs[1:]
    
    nuevas_loose_ordenadas = resolver_tsp_parcial(fixed_stop, loose_stops, base_address, dwell_time)
    
    if nuevas_loose_ordenadas is None: return jsonify({"error": "Error optimizando"}), 500
        
    c_fixed, fmt_fixed = obtener_datos_geo(fixed_stop['direccion'])
    fixed_completo = {
        "nombre": fixed_stop['nombre'], 
        "direccion": fixed_stop['direccion'], 
        "clean_address": fmt_fixed,
        "coord": c_fixed
    }
    lista_final = [fixed_completo] + nuevas_loose_ordenadas
    
    return recalcular_ruta_internal(lista_final, base_address, dwell_time)

def recalcular_ruta_internal(paradas_objs, base, dwell_time):
    c_base, fmt_base = obtener_datos_geo(base)
    
    coords_paradas = []
    paradas_con_clean = []

    for p in paradas_objs:
        c, fmt = obtener_datos_geo(p['direccion'])
        if c:
            coords_paradas.append(c)
            # Aseguramos que el objeto tenga clean_address
            p_copy = p.copy()
            p_copy['clean_address'] = fmt
            paradas_con_clean.append(p_copy)
            
    coords_limpias = [c for c in coords_paradas if c]

    if not c_base: return jsonify({"error": "Error base"}), 400

    puntos_secuencia = [c_base] + coords_limpias + [c_base]
    tiempo_total_segundos = 0
    puntos_unicos = list(set(puntos_secuencia))
    
    rows_matriz = obtener_matriz_segura(puntos_unicos)
    if not rows_matriz: return jsonify({"error": "Error calculando tiempos"}), 500
        
    mapa_indices = {coord: i for i, coord in enumerate(puntos_unicos)}
    rows = rows_matriz
    
    for i in range(len(puntos_secuencia) - 1):
        origen = puntos_secuencia[i]
        destino = puntos_secuencia[i+1]
        idx_origen = mapa_indices[origen]
        idx_destino = mapa_indices[destino]
        try:
            el = rows[idx_origen]['elements'][idx_destino]
            val = el.get('duration_in_traffic', {}).get('value') or el.get('duration', {}).get('value', 0)
            tiempo_total_segundos += val
        except:
            pass
        
    tiempo_total_segundos += (len(coords_limpias) * dwell_time * 60)
    
    # --- LINK LIMPIO (V3): USAR 'clean_address' ---
    # Usamos la dirección oficial devuelta por Google, que suele estar limpia.
    
    base_encoded = urllib.parse.quote_plus(fmt_base)
    base_url = "https://www.google.com/maps/dir/?api=1"
    
    stops_str = "&waypoints=" + "|".join([
        urllib.parse.quote_plus(p['clean_address']) 
        for p in paradas_con_clean
    ])
    full_link = f"{base_url}&origin={base_encoded}&destination={base_encoded}{stops_str}"
    
    return jsonify({
        "duracion_estimada": tiempo_total_segundos / 60,
        "link": full_link,
        "paradas": paradas_con_clean
    })

@app.route('/recalcular', methods=['POST'])
def recalcular_ruta():
    if not API_KEY: return jsonify({"error": "Error: Falta API KEY"}), 500
    data = request.json
    paradas = data.get('paradas', [])
    base = data.get('base_address')
    dwell_time = int(data.get('dwell_time', 10))
    
    paradas_objs = []
    for p in paradas:
        if isinstance(p, dict) and 'nombre' in p: paradas_objs.append(p)
        elif isinstance(p, dict): paradas_objs.append({"nombre": "Cliente", "direccion": p.get('direccion', '')})
        else: paradas_objs.append({"nombre": "Cliente", "direccion": p})
            
    return recalcular_ruta_internal(paradas_objs, base, dwell_time)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
