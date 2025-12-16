import os
import sqlite3
import sys
import urllib.parse
import datetime
import googlemaps
import random
import math
import requests  # NECESARIO PARA OSRM
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
from dotenv import load_dotenv

# Carga variables de entorno si existen
load_dotenv()

basedir = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__, static_folder=basedir, static_url_path='')
CORS(app)

# --- CREDENCIALES ---
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "AIzaSyDByOKScFAJaWzvmDOeAnmlUZsUuIL1Oqo")

FIREBASE_CONFIG = {
    "apiKey": os.environ.get("FIREBASE_API_KEY", "AIzaSyDByOKScFAJaWzvmDOeAnmlUZsUuIL1Oqo"),
    "authDomain": os.environ.get("FIREBASE_AUTH_DOMAIN", "ess-routeplanner-sanbox.firebaseapp.com"),
    "projectId": os.environ.get("FIREBASE_PROJECT_ID", "ess-routeplanner-sanbox"),
    "storageBucket": os.environ.get("FIREBASE_STORAGE_BUCKET", "ess-routeplanner-sanbox.firebasestorage.app"),
    "messagingSenderId": os.environ.get("FIREBASE_MESSAGING_SENDER_ID", "330245876076"),
    "appId": os.environ.get("FIREBASE_APP_ID", "1:330245876076:web:d450c48d7be981fa8d2b49")
}

# --- CONFIGURACIÓN OSRM ---
OSRM_BASE_URL = "http://router.project-osrm.org"

# --- VALIDACIÓN DE INICIO ---
API_KEY = GOOGLE_MAPS_API_KEY
if not API_KEY or API_KEY.startswith("TU_"):
    print("ADVERTENCIA CRÍTICA: No hay API Key válida configurada.", file=sys.stderr, flush=True)
else:
    print(f"✅ API Key cargada: {API_KEY[:5]}... (Oculta)", file=sys.stdout, flush=True)

gmaps = None
try:
    if API_KEY:
        gmaps = googlemaps.Client(key=API_KEY)
except ValueError as e:
    print(f"Error iniciando cliente Google Maps: {e}", file=sys.stderr, flush=True)

# Coordenada del Almacén por defecto (Lat, Lng) - Orlando
ALMACEN_COORD = "28.450324,-81.405368" 

# --- BASE DE DATOS (CACHE LOCAL) ---
def init_db():
    db_path = os.path.join(basedir, 'economy_routes.db')
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS direcciones_v3
                     (direccion TEXT PRIMARY KEY, latlng TEXT, place_id TEXT, formatted_address TEXT)''')
        conn.commit()
        conn.close()
        print("✅ Base de datos verificada (V3).", flush=True)
    except Exception as e:
        print(f"❌ Error DB: {e}", file=sys.stderr, flush=True)

init_db()

# --- GEOCODING (USA GOOGLE PARA DIRECCIONES) ---
def obtener_datos_geo(direccion):
    if not direccion: return None, None, None
    db_path = os.path.join(basedir, 'economy_routes.db')
    direccion_clean = direccion.strip().lower()
    
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT latlng, formatted_address, place_id FROM direcciones_v3 WHERE direccion=?", (direccion_clean,))
        resultado = c.fetchone()
        if resultado:
            conn.close()
            return resultado[0], resultado[1], resultado[2]
        
        if not gmaps: return None, None, None
        
        try:
            # Esta llamada SÍ usa Google Maps API (Geocoding), costo bajo
            geocode_result = gmaps.geocode(direccion)
        except Exception as api_e:
            print(f"❌ Error API al geocodificar {direccion}: {api_e}", file=sys.stderr, flush=True)
            conn.close()
            return None, None, None

        if geocode_result and len(geocode_result) > 0:
            res = geocode_result[0]
            loc = res['geometry']['location']
            # Aseguramos formato limpio sin espacios extra
            latlng_str = f"{loc['lat']},{loc['lng']}"
            place_id = res.get('place_id', '')
            formatted_addr = res.get('formatted_address', direccion) 
            c.execute("INSERT OR REPLACE INTO direcciones_v3 VALUES (?, ?, ?, ?)", (direccion_clean, latlng_str, place_id, formatted_addr))
            conn.commit()
            conn.close()
            return latlng_str, formatted_addr, place_id
        else:
            conn.close()
            return None, None, None
    except Exception as e:
        try: conn.close()
        except: pass
    return None, None, None

# --- PARSEO DE COORDENADAS ---
def parse_latlng(latlng_str):
    try:
        parts = latlng_str.split(',')
        return float(parts[0]), float(parts[1])
    except:
        return 0.0, 0.0

# --- CLUSTERING (K-MEANS) ---
def simple_kmeans(points, k, max_iterations=100):
    if not points: return []
    if k <= 0: return [points]
    if k > len(points): k = len(points) 

    centroids = random.sample([p['coords'] for p in points], k)
    clusters = [[] for _ in range(k)]

    for _ in range(max_iterations):
        clusters = [[] for _ in range(k)]
        for point in points:
            p_lat, p_lng = point['coords']
            best_dist = float('inf')
            best_idx = 0
            for i, (c_lat, c_lng) in enumerate(centroids):
                dist = (p_lat - c_lat)**2 + (p_lng - c_lng)**2
                if dist < best_dist:
                    best_dist = dist
                    best_idx = i
            clusters[best_idx].append(point)
        
        new_centroids = []
        diff = 0
        for i in range(k):
            cluster = clusters[i]
            if not cluster:
                new_centroids.append(centroids[i])
                continue
            avg_lat = sum(p['coords'][0] for p in cluster) / len(cluster)
            avg_lng = sum(p['coords'][1] for p in cluster) / len(cluster)
            new_centroids.append((avg_lat, avg_lng))
            diff += (avg_lat - centroids[i][0])**2 + (avg_lng - centroids[i][1])**2
        
        centroids = new_centroids
        if diff < 1e-6: break 
        
    return clusters

# --- MATRIZ DE DISTANCIA (SOLO OSRM - CORREGIDO) ---
def obtener_matriz_osrm(puntos):
    """
    Consulta al servicio OSRM para obtener la matriz de tiempos de viaje.
    """
    if not puntos: return []
    if len(puntos) < 2: return [[0]]

    # 1. Preparar coordenadas para OSRM (lng,lat) LIMPIAS
    osrm_coords = []
    for p in puntos:
        try:
            # FIX: strip() es vital porque OSRM falla si hay espacios "lat, lng"
            lat, lng = [x.strip() for x in p.split(',')]
            osrm_coords.append(f"{lng},{lat}")
        except:
            msg = f"❌ Error parseando coord para OSRM: {p}"
            print(msg, file=sys.stderr)
            raise Exception(msg)

    coords_string = ";".join(osrm_coords)
    url = f"{OSRM_BASE_URL}/table/v1/driving/{coords_string}?annotations=duration"

    print(f"🌍 Consultando OSRM ({len(puntos)} puntos)...", file=sys.stdout, flush=True)

    # FIX: Headers para evitar bloqueo de User-Agent genérico
    headers = {
        'User-Agent': 'EssRoutePlanner/1.0 (Internal Tool)',
        'Accept': 'application/json'
    }

    try:
        # Timeout aumentado a 10s para ser seguros con el servidor público
        response = requests.get(url, headers=headers, timeout=10) 
        
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "Ok" and "durations" in data:
                durations = data["durations"]
                clean_matrix = []
                for row in durations:
                    # OSRM devuelve None si no hay ruta, usamos 999999 como infinito
                    clean_row = [int(val) if val is not None else 999999 for val in row]
                    clean_matrix.append(clean_row)
                print("✅ Matriz OSRM recibida con éxito.", file=sys.stdout, flush=True)
                return clean_matrix
            else:
                msg = f"Respuesta OSRM no válida: {data.get('code')}"
                print(f"⚠️ {msg}", file=sys.stderr)
                raise Exception(msg)
        else:
            msg = f"Error HTTP OSRM: {response.status_code} - {response.text}"
            print(f"⚠️ {msg}", file=sys.stderr)
            raise Exception(msg)

    except Exception as e:
        print(f"⚠️ Excepción conectando a OSRM: {str(e)}", file=sys.stderr)
        raise e

# --- LINK GENERATOR ---
def generar_link_puro(origen_obj, destino_obj, waypoints_objs):
    base_url = "https://www.google.com/maps/dir/?api=1"
    def clean_param(text):
        if not text: return ""
        encoded = urllib.parse.quote_plus(text.strip())
        encoded = encoded.replace('%2C', ',')
        return encoded
    
    dest_addr = clean_param(destino_obj['clean_address'])
    link = base_url + f"&destination={dest_addr}"

    if waypoints_objs:
        wp_list = []
        for p in waypoints_objs:
            val = p.get('direccion') or p.get('clean_address')
            if val: wp_list.append(clean_param(val))
        if wp_list:
            link += f"&waypoints={'|'.join(wp_list)}"

    link += "&travelmode=driving"
    return link

# --- MODELO DE DATOS VRP ---
def crear_modelo_datos(lista_paradas, num_vans, base_address_text=None):
    datos = {}
    coord_almacen = ALMACEN_COORD
    fmt_almacen = base_address_text 
    pid_almacen = ""

    if base_address_text:
        c, fmt, pid = obtener_datos_geo(base_address_text)
        if c: 
            coord_almacen = c
            fmt_almacen = fmt
            pid_almacen = pid
        else:
            return {"error_critico": f"No se encontró la dirección BASE: {base_address_text}"}

    puntos = [coord_almacen]
    paradas_validas = [] 
    paradas_erroneas = []
    
    for item in lista_paradas:
        dir_txt = item.get('direccion') or item.get('address')
        nombre_txt = item.get('nombre') or item.get('name') or "Cliente"
        invoices = item.get('invoices', '')
        pieces = item.get('pieces', '')
        if not dir_txt: continue 

        c, fmt, pid = obtener_datos_geo(dir_txt)
        if c:
            puntos.append(c)
            paradas_validas.append({
                "nombre": nombre_txt, "direccion": dir_txt, "clean_address": fmt,
                "place_id": pid, "invoices": invoices, "pieces": pieces
            })
        else:
            paradas_erroneas.append(dir_txt)
    
    if paradas_erroneas: return {"invalidas": paradas_erroneas}
    if len(puntos) <= 1: return None 

    # --- SOLO OSRM ---
    try:
        matriz_tiempos = obtener_matriz_osrm(puntos)
    except Exception as e:
        return {"error_critico": f"Error OSRM: {str(e)}"}

    datos['time_matrix'] = matriz_tiempos
    datos['num_vehicles'] = int(num_vans)
    datos['depot'] = 0 
    datos['coords'] = puntos
    
    base_obj = {"nombre": "Warehouse", "direccion": base_address_text or "Warehouse", "clean_address": fmt_almacen, "place_id": pid_almacen}
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
                        "nombre": info['nombre'], "direccion": info['direccion'], 
                        "clean_address": info['clean_address'], "place_id": info.get('place_id'),
                        "invoices": info.get('invoices', ''), "pieces": info.get('pieces', ''),
                        "coord": datos['coords'][node_index]
                    })
                index = solution.Value(routing.NextVar(index))
            
            nombre_van = f"Van {vehicle_id + 1}"
            
            base_info = datos['paradas_info'][0]
            full_link = ""
            if ruta: full_link = generar_link_puro(base_info, base_info, ruta)
                
            finish_index = routing.End(vehicle_id)
            tiempo_total = solution.Min(time_dimension.CumulVar(finish_index))
            
            rutas_finales[nombre_van] = {
                "paradas": ruta,
                "duracion_estimada": tiempo_total / 60,
                "link": full_link
            }
    return rutas_finales

# --- ENDPOINTS ---
@app.route('/')
def serve_frontend(): return send_from_directory(basedir, 'index.html')

@app.route('/health')
def health_check():
    return jsonify({"status": "healthy"}), 200

@app.route('/config')
def get_config(): return jsonify({"googleApiKey": GOOGLE_MAPS_API_KEY, "firebaseConfig": FIREBASE_CONFIG})

@app.route('/optimizar', methods=['POST'])
def optimizar():
    if not API_KEY: return jsonify({"error": "Falta GOOGLE MAPS API KEY para geocodificar."}), 500
    try:
        data = request.json
        num_vans = int(data.get('num_vans', 1))
        base_address = data.get('base_address')
        dwell_time = int(data.get('dwell_time', 6))
        
        raw_stops = data.get('direcciones', [])
        lista_stops = []
        for item in raw_stops:
            if isinstance(item, str): lista_stops.append({"nombre":"Cliente", "direccion":item})
            else: lista_stops.append(item)

        if num_vans > 1:
            print(f"🌍 Iniciando Zonificación (K-Means) para {num_vans} conductores...", file=sys.stdout, flush=True)
            
            puntos_para_cluster = []
            for stop in lista_stops:
                dir_txt = stop.get('direccion') or stop.get('address')
                if not dir_txt: continue
                c_str, _, _ = obtener_datos_geo(dir_txt) 
                if c_str:
                    lat, lng = parse_latlng(c_str)
                    puntos_para_cluster.append({'coords': (lat, lng), 'data': stop})
            
            clusters = simple_kmeans(puntos_para_cluster, k=num_vans)
            
            rutas_globales = {}
            for i, cluster in enumerate(clusters):
                nombre_driver = f"Van {i + 1}"
                if not cluster:
                    rutas_globales[nombre_driver] = {"paradas": [], "duracion_estimada": 0, "link": ""}
                    continue
                
                paradas_zona = [p['data'] for p in cluster]
                modelo_zona = crear_modelo_datos(paradas_zona, num_vans=1, base_address_text=base_address)
                
                if modelo_zona and not "error_critico" in modelo_zona:
                    resultado_zona = resolver_vrp(modelo_zona, dwell_time)
                    
                    if resultado_zona and len(resultado_zona) > 0:
                        data_zona = list(resultado_zona.values())[0]
                        rutas_globales[nombre_driver] = data_zona
                    else:
                        rutas_globales[nombre_driver] = {"paradas": [], "duracion_estimada": 0, "link": ""}
                else:
                    rutas_globales[nombre_driver] = {"paradas": [], "duracion_estimada": 0, "link": ""}

            return jsonify(rutas_globales)

        else:
            modelo = crear_modelo_datos(lista_stops, num_vans, base_address)
            if modelo is None: return jsonify({"error": "Error al procesar datos."}), 500
            if isinstance(modelo, dict) and "error_critico" in modelo: return jsonify({"error": modelo["error_critico"]}), 400
            resultado = resolver_vrp(modelo, dwell_time)
            return jsonify(resultado)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": str(e)}), 500

# --- RECALCULAR / OPTIMIZAR RESTANTES ---
@app.route('/optimizar_restantes', methods=['POST'])
def optimizar_restantes():
    data = request.json
    paradas = data.get('paradas', [])
    base = data.get('base_address')
    dwell = int(data.get('dwell_time', 6))
    
    p_objs = []
    for p in paradas:
        obj = p if isinstance(p, dict) else {'direccion': p}
        if 'address' in obj: obj['direccion'] = obj['address']
        p_objs.append(obj)
        
    if len(p_objs) < 3: return recalcular_ruta_internal(p_objs, base, dwell)

    fixed = p_objs[0]
    loose = p_objs[1:]
    new_order = resolver_tsp_parcial(fixed, loose, base, dwell)
    
    if not new_order: return jsonify({"error": "Error optimizando"}), 500
    
    return recalcular_ruta_internal(new_order, base, dwell)

@app.route('/recalcular', methods=['POST'])
def recalcular_ruta():
    data = request.json
    return recalcular_ruta_internal(data.get('paradas', []), data.get('base_address'), int(data.get('dwell_time', 6)))

def recalcular_ruta_internal(paradas_objs, base, dwell_time):
    c_base, fmt_base, pid_base = obtener_datos_geo(base)
    if not c_base: return jsonify({"error": "Base inválida"}), 400
    
    coords_limpias = []
    paradas_clean = []
    for p in paradas_objs:
        addr = p.get('direccion') or p.get('address')
        if not addr: continue
        c, fmt, pid = obtener_datos_geo(addr)
        if c:
            coords_limpias.append(c)
            new_p = p.copy()
            new_p.update({'direccion': addr, 'clean_address': fmt, 'place_id': pid})
            paradas_clean.append(new_p)
            
    seq = [c_base] + coords_limpias + [c_base]
    tiempo_total = 0
    
    unique = list(set(seq))
    try:
        # --- SOLO OSRM ---
        matrix = obtener_matriz_osrm(unique)
        idx_map = {coord: i for i, coord in enumerate(unique)}
        for i in range(len(seq)-1):
            u, v = seq[i], seq[i+1]
            val = matrix[idx_map[u]][idx_map[v]]
            tiempo_total += val
    except Exception as e:
        print(f"Error recalculando con OSRM: {e}", file=sys.stderr)
        # Si falla el recálculo, no devolvemos 0, sino error, para que el usuario sepa.
        return jsonify({"error": "Fallo al conectar con OSRM para recalcular."}), 500
        
    tiempo_total += len(coords_limpias) * dwell_time * 60
    base_obj = {'clean_address': fmt_base}
    link = generar_link_puro(base_obj, base_obj, paradas_clean)
    
    return jsonify({"duracion_estimada": tiempo_total/60, "link": link, "paradas": paradas_clean})

def resolver_tsp_parcial(fixed, loose, base, dwell):
    fixed_addr = fixed.get('direccion') or fixed.get('address')
    modelo_loose = crear_modelo_datos(loose, 1, fixed_addr)
    
    if not modelo_loose or "error_critico" in modelo_loose:
        return None
        
    manager = pywrapcp.RoutingIndexManager(len(modelo_loose['time_matrix']), 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        val = modelo_loose['time_matrix'][from_node][to_node]
        if to_node != 0: val += dwell * 60
        return val

    transit_idx = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.seconds = 1

    solution = routing.SolveWithParameters(search_parameters)
    
    if not solution: return None
    
    if not list(modelo_loose['paradas_info']):
         return None

    index = routing.Start(0)
    ruta_ordenada = []
    
    while not routing.IsEnd(index):
        node_index = manager.IndexToNode(index)
        if node_index != 0:
             info = modelo_loose['paradas_info'][node_index]
             ruta_ordenada.append({
                "nombre": info['nombre'], "direccion": info['direccion'], 
                "clean_address": info['clean_address'], "place_id": info.get('place_id'),
                "invoices": info.get('invoices', ''), "pieces": info.get('pieces', '')
             })
        index = solution.Value(routing.NextVar(index))
    
    return [fixed] + ruta_ordenada

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Servidor corriendo en puerto {port}", flush=True)
    app.run(host='0.0.0.0', port=port)
