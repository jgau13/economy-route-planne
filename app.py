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
        # Nueva tabla V2 con soporte para Place ID
        c.execute('''CREATE TABLE IF NOT EXISTS direcciones_v2
                     (direccion TEXT PRIMARY KEY, latlng TEXT, place_id TEXT)''')
        conn.commit()
        conn.close()
        print("✅ Base de datos verificada (V2).")
    except Exception as e:
        print(f"❌ Error DB: {e}", file=sys.stderr)

init_db()

def obtener_datos_geo(direccion):
    """
    Retorna una tupla (latlng, place_id)
    """
    db_path = os.path.join(basedir, 'economy_routes.db')
    direccion_clean = direccion.strip().lower()
    
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT latlng, place_id FROM direcciones_v2 WHERE direccion=?", (direccion_clean,))
        resultado = c.fetchone()
        
        if resultado:
            conn.close()
            return resultado[0], resultado[1]
        
        if not API_KEY: return None, None
        
        try:
            # Solicitamos el Place ID explícitamente
            geocode_result = gmaps.geocode(direccion)
        except Exception as api_e:
            print(f"❌ Error API al geocodificar {direccion}: {api_e}", file=sys.stderr)
            conn.close()
            return None, None

        if geocode_result and len(geocode_result) > 0:
            res = geocode_result[0]
            loc = res['geometry']['location']
            latlng_str = f"{loc['lat']},{loc['lng']}"
            place_id = res.get('place_id', '') # Obtenemos el ID único de Google
            
            c.execute("INSERT OR REPLACE INTO direcciones_v2 VALUES (?, ?, ?)", (direccion_clean, latlng_str, place_id))
            conn.commit()
            conn.close()
            return latlng_str, place_id
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
    pid_almacen = ""
    
    if base_address_text:
        c, pid = obtener_datos_geo(base_address_text)
        if c: 
            coord_almacen = c
            pid_almacen = pid
        else:
            return {"error_critico": f"No se encontró la dirección BASE: {base_address_text}"}

    puntos = [coord_almacen]
    place_ids = [pid_almacen] # Lista paralela de Place IDs
    paradas_validas = [] 
    paradas_erroneas = []
    
    for item in lista_paradas:
        dir_txt = item['direccion'] if isinstance(item, dict) else item
        nombre_txt = item['nombre'] if isinstance(item, dict) else "Cliente"
        
        c, pid = obtener_datos_geo(dir_txt)
        if c:
            puntos.append(c)
            place_ids.append(pid)
            paradas_validas.append({"nombre": nombre_txt, "direccion": dir_txt})
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
    datos['place_ids'] = place_ids
    datos['paradas_info'] = [{"nombre": "Warehouse", "direccion": base_address_text or "Warehouse"}] + paradas_validas
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
            ruta_pids = [] # Place IDs de la ruta
            
            while not routing.IsEnd(index):
                node_index = manager.IndexToNode(index)
                if node_index != 0:
                    info = datos['paradas_info'][node_index]
                    ruta.append({
                        "nombre": info['nombre'],
                        "direccion": info['direccion'],
                        "coord": datos['coords'][node_index]
                    })
                    # Guardamos el Place ID
                    ruta_pids.append(datos['place_ids'][node_index])
                    
                index = solution.Value(routing.NextVar(index))
            
            nombre_van = f"Van {vehicle_id + 1}"
            
            if ruta:
                # --- GENERACIÓN DE LINK CON PLACE IDs ---
                base_pid = datos['place_ids'][0]
                base_param = f"origin=place_id:{base_pid}" if base_pid else f"origin={urllib.parse.quote_plus(datos['paradas_info'][0]['direccion'])}"
                dest_param = f"destination=place_id:{base_pid}" if base_pid else f"destination={urllib.parse.quote_plus(datos['paradas_info'][0]['direccion'])}"
                
                # Construimos waypoints usando place_id:XXXX
                # Esto fuerza a Google Maps a mostrar el nombre del negocio oficial
                waypoints_parts = []
                for i, pid in enumerate(ruta_pids):
                    if pid:
                        waypoints_parts.append(f"place_id:{pid}")
                    else:
                        # Fallback a dirección si no hay ID
                        waypoints_parts.append(urllib.parse.quote_plus(ruta[i]['direccion']))
                
                stops_str = "&waypoints=" + "|".join(waypoints_parts)
                
                base_url = "https://www.google.com/maps/dir/?api=1"
                full_link = f"{base_url}&{base_param}&{dest_param}{stops_str}"
                
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
    c_start, pid_start = obtener_datos_geo(fixed_stop['direccion'])
    c_end, pid_end = obtener_datos_geo(base_address_txt)
    
    if not c_start or not c_end: return None

    coords = [c_start]
    place_ids = [pid_start]
    objetos_ordenados = [fixed_stop] 
    
    for s in loose_stops:
        c, pid = obtener_datos_geo(s['direccion'])
        if c:
            coords.append(c)
            place_ids.append(pid)
            objetos_ordenados.append(s)
            
    coords.append(c_end)
    place_ids.append(pid_end)
    
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
            # Adjuntamos el Place ID al objeto para usarlo luego
            obj_con_pid = obj_original.copy()
            obj_con_pid['place_id'] = place_ids[node_index]
            
            nuevo_orden_paradas.append(obj_con_pid)
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
        
    c_fixed, pid_fixed = obtener_datos_geo(fixed_stop['direccion'])
    fixed_completo = {
        "nombre": fixed_stop['nombre'], 
        "direccion": fixed_stop['direccion'], 
        "place_id": pid_fixed
    }
    lista_final = [fixed_completo] + nuevas_loose_ordenadas
    
    return recalcular_ruta_internal(lista_final, base_address, dwell_time)

def recalcular_ruta_internal(paradas_objs, base, dwell_time):
    c_base, pid_base = obtener_datos_geo(base)
    
    coords_paradas = []
    place_ids_paradas = []
    
    for p in paradas_objs:
        # Intentamos usar lo que ya traiga, si no, buscamos
        if 'place_id' in p and p['place_id']:
            # Ya tenemos el ID (viene de optimizar_restantes)
            # Necesitamos coord para matriz? Sí.
            # Pero obtener_datos_geo es rápido (caché).
            c, pid = obtener_datos_geo(p['direccion']) # Re-fetch seguro de caché
            coords_paradas.append(c)
            place_ids_paradas.append(pid)
        else:
            c, pid = obtener_datos_geo(p['direccion'])
            coords_paradas.append(c)
            place_ids_paradas.append(pid)
            
    # Filtrar nulos
    coords_limpias = [c for c in coords_paradas if c]
    pids_limpios = [pid for pid in place_ids_paradas] # Mantiene indices alineados con paradas_objs (asumiendo éxito previo)

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
    
    # --- LINK GENERATION WITH PLACE ID ---
    base_param = f"origin=place_id:{pid_base}" if pid_base else f"origin={urllib.parse.quote_plus(base)}"
    dest_param = f"destination=place_id:{pid_base}" if pid_base else f"destination={urllib.parse.quote_plus(base)}"
    
    waypoints_parts = []
    for i, pid in enumerate(pids_limpios):
        if pid:
            waypoints_parts.append(f"place_id:{pid}")
        else:
            waypoints_parts.append(urllib.parse.quote_plus(paradas_objs[i]['direccion']))
            
    stops_str = "&waypoints=" + "|".join(waypoints_parts)
    
    base_url = "https://www.google.com/maps/dir/?api=1"
    full_link = f"{base_url}&{base_param}&{dest_param}{stops_str}"
    
    return jsonify({
        "duracion_estimada": tiempo_total_segundos / 60,
        "link": full_link,
        "paradas": paradas_objs
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
