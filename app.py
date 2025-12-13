import os
import sqlite3
import sys
import urllib.parse
import datetime
import requests
import math
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

ALMACEN_COORD = "28.450325,-81.396924" # Coordenadas aproximadas de ESS Orlando

# --- BASE DE DATOS ---
def init_db():
    db_path = os.path.join(basedir, 'economy_routes.db')
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS direcciones_v3
                     (direccion TEXT PRIMARY KEY, latlng TEXT, place_id TEXT, formatted_address TEXT)''')
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ Error DB: {e}", file=sys.stderr)

init_db()

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
        
        if not API_KEY: 
            conn.close()
            return None, None, None
        
        try:
            geocode_result = gmaps.geocode(direccion)
        except Exception:
            conn.close()
            return None, None, None

        if geocode_result and len(geocode_result) > 0:
            res = geocode_result[0]
            loc = res['geometry']['location']
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

    except Exception:
        try: conn.close()
        except: pass
    return None, None, None

def obtener_matriz_segura(puntos):
    if not puntos: return []

    coords_osrm = []
    for p in puntos:
        try:
            lat, lon = p.split(',')
            coords_osrm.append(f"{lon.strip()},{lat.strip()}")
        except:
            continue

    if not coords_osrm: return None

    coords_string = ";".join(coords_osrm)
    url = f"http://router.project-osrm.org/table/v1/driving/{coords_string}"
    params = {'annotations': 'duration'}

    try:
        headers = {'User-Agent': 'ESSRoutePlanner/1.0'}
        response = requests.get(url, params=params, headers=headers, timeout=15)
        
        if response.status_code != 200: return None
        data = response.json()
        if data.get('code') != 'Ok': return None

        durations = data['durations'] 
        matriz_google_style = []
        
        for fila in durations:
            fila_google = {'elements': []}
            for duracion_segundos in fila:
                if duracion_segundos is not None:
                    val = int(round(duracion_segundos))
                else:
                    val = 9999999
                
                fila_google['elements'].append({
                    'duration': {'value': val},
                    'duration_in_traffic': {'value': val} 
                })
            matriz_google_style.append(fila_google)
            
        return matriz_google_style

    except Exception as e:
        print(f"Error llamando a OSRM: {e}")
        return None

def generar_link_puro(origen_obj, destino_obj, waypoints_objs):
    base_url = "https://www.google.com/maps/dir/?api=1"
    def clean_param(text):
        if not text: return ""
        encoded = urllib.parse.quote_plus(text.strip())
        encoded = encoded.replace('%2C', ',')
        return encoded

    link = base_url
    dest_addr = clean_param(destino_obj.get('clean_address') or destino_obj.get('direccion'))
    link += f"&destination={dest_addr}"

    if waypoints_objs:
        wp_list = []
        for p in waypoints_objs:
            texto = p.get('clean_address') or p.get('direccion')
            if texto:
                wp_list.append(clean_param(texto))
        wp_string = "|".join(wp_list)
        link += f"&waypoints={wp_string}"

    link += "&travelmode=driving"
    return link

# --- LÓGICA CVRP (Capacitated Vehicle Routing Problem) ---

def crear_modelo_datos_cvrp(lista_paradas, num_vans, base_address_text):
    datos = {}
    
    # 1. Base
    c_base = ALMACEN_COORD
    fmt_base = base_address_text
    pid_base = ""
    
    if base_address_text:
        c, fmt, pid = obtener_datos_geo(base_address_text)
        if c: 
            c_base = c
            fmt_base = fmt
            pid_base = pid

    puntos = [c_base]
    paradas_validas = []
    
    # Demanda de cada nodo (la base tiene 0)
    demands = [0] 
    
    for item in lista_paradas:
        dir_txt = item.get('direccion') or item.get('address')
        if not dir_txt: continue
        
        c, fmt, pid = obtener_datos_geo(dir_txt)
        if c:
            puntos.append(c)
            demands.append(1) # Cada parada cuenta como 1 unidad de capacidad
            
            paradas_validas.append({
                "nombre": item.get('nombre') or item.get('name') or "Cliente",
                "direccion": dir_txt,
                "clean_address": fmt,
                "place_id": pid,
                "invoices": item.get('invoices', ''),
                "pieces": item.get('pieces', ''),
                "coord": c
            })

    if not paradas_validas: return None

    # 2. Matriz de Distancias (Tiempos)
    rows_matriz = obtener_matriz_segura(puntos)
    if not rows_matriz: return None
    
    matriz_tiempos = []
    for fila in rows_matriz:
        fila_tiempos = []
        for el in fila['elements']:
            val = int(el.get('duration', {}).get('value', 999999))
            fila_tiempos.append(val)
        matriz_tiempos.append(fila_tiempos)

    datos['time_matrix'] = matriz_tiempos
    datos['demands'] = demands
    datos['num_vehicles'] = int(num_vans)
    datos['depot'] = 0
    datos['paradas_info'] = [{"nombre":"Base", "direccion":base_address_text, "clean_address":fmt_base}] + paradas_validas
    
    # 3. CÁLCULO DE CAPACIDAD DINÁMICA
    # Calculamos cuántas paradas caben por van para forzar la división de zonas densas.
    total_paradas = len(paradas_validas)
    
    # Margen de holgura: permitimos un 20% más del promedio para flexibilidad
    avg_stops = math.ceil(total_paradas / datos['num_vehicles'])
    vehicle_capacity = int(avg_stops * 1.3) # 30% de buffer
    
    # Aseguramos un mínimo razonable
    vehicle_capacity = max(vehicle_capacity, 3) 
    
    datos['vehicle_capacities'] = [vehicle_capacity] * datos['num_vehicles']
    
    return datos

def resolver_cvrp(datos, dwell_time_minutos):
    manager = pywrapcp.RoutingIndexManager(len(datos['time_matrix']), datos['num_vehicles'], datos['depot'])
    routing = pywrapcp.RoutingModel(manager)

    # 1. Costo de Tiempo (Distancia/Duración)
    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        val = datos['time_matrix'][from_node][to_node]
        if from_node != to_node:
             # Penalizamos ligeramente distancias largas para mantener la compacidad
             val = int(val * 1.0) 
        if to_node != 0: 
            val += int(dwell_time_minutos * 60)
        return val

    transit_callback_index = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    # 2. RESTRICTIVE TIME DIMENSION (4:30 HOURS LIMIT)
    # 4.5 horas = 270 minutos = 16200 segundos
    MAX_ROUTE_DURATION_SECONDS = 270 * 60
    
    routing.AddDimension(
        transit_callback_index,
        3600,  # Slack (Tiempo de espera permitido en ruta, 1h buffer)
        MAX_ROUTE_DURATION_SECONDS, # Horizonte máximo estricto (4h 30m)
        True,  # Start cumul to zero
        'Time'
    )
    time_dimension = routing.GetDimensionOrDie('Time')
    
    # Esto ayuda a que, si una ruta es muy corta y otra muy larga, intente equilibrar,
    # pero el límite de 4:30 es MANDATORIO (Hard Constraint).
    time_dimension.SetGlobalSpanCostCoefficient(100)

    # 3. Dimensión de Capacidad (La clave para tu problema de volumen)
    def demand_callback(from_index):
        from_node = manager.IndexToNode(from_index)
        return datos['demands'][from_node]

    demand_callback_index = routing.RegisterUnaryTransitCallback(demand_callback)
    
    routing.AddDimensionWithVehicleCapacity(
        demand_callback_index,
        0,  # null capacity slack
        datos['vehicle_capacities'],  # vector de capacidades por vehículo
        True,  # start cumul to zero
        'Capacity'
    )

    # Configuración de búsqueda: PATH_CHEAPEST_ARC suele generar rutas más naturales visualmente
    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.seconds = 3

    solution = routing.SolveWithParameters(search_parameters)
    rutas_finales = {}

    if solution:
        for vehicle_id in range(datos['num_vehicles']):
            index = routing.Start(vehicle_id)
            ruta = []
            tiempo_acumulado = 0
            
            while not routing.IsEnd(index):
                node_index = manager.IndexToNode(index)
                if node_index != 0: # Ignorar base en la lista visual
                    info = datos['paradas_info'][node_index]
                    ruta.append(info)
                
                previous_index = index
                index = solution.Value(routing.NextVar(index))
                
                # Sumar tiempo real para estimación
                tiempo_acumulado += datos['time_matrix'][manager.IndexToNode(previous_index)][manager.IndexToNode(index)]
                if manager.IndexToNode(index) != 0:
                    tiempo_acumulado += (dwell_time_minutos * 60)

            nombre_van = f"Van {vehicle_id + 1}"
            
            if ruta:
                base_info = datos['paradas_info'][0]
                # Reconstruir objetos completos para el frontend
                ruta_completa = []
                for r in ruta:
                    ruta_completa.append({
                        "nombre": r['nombre'],
                        "direccion": r['direccion'],
                        "clean_address": r.get('clean_address'),
                        "invoices": r.get('invoices'),
                        "pieces": r.get('pieces'),
                        "place_id": r.get('place_id')
                    })

                full_link = generar_link_puro(base_info, base_info, ruta_completa)
                
                rutas_finales[nombre_van] = {
                    "paradas": ruta_completa,
                    "duracion_estimada": tiempo_acumulado / 60,
                    "link": full_link
                }
            else:
                rutas_finales[nombre_van] = {"paradas": [], "duracion_estimada": 0, "link": ""}
    
    return rutas_finales

# --- RUTAS FLASK ---

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
        num_vans = int(data.get('num_vans', 1))
        base_address = data.get('base_address')
        lista_raw = data['direcciones']
        
        # 1. Crear Modelo CVRP (Capacitated Vehicle Routing Problem)
        modelo = crear_modelo_datos_cvrp(lista_raw, num_vans, base_address)
        
        if not modelo or not modelo['paradas_info']:
            return jsonify({"error": "No se pudieron procesar las direcciones"}), 400
            
        # 2. Resolver
        resultado = resolver_cvrp(modelo, dwell_time)
        return jsonify(resultado)
        
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return jsonify({"error": f"Error interno: {str(e)}"}), 500

@app.route('/optimizar_restantes', methods=['POST'])
def optimizar_restantes():
    # Recalculo simple TSP
    return recalcular_ruta()

@app.route('/recalcular', methods=['POST'])
def recalcular_ruta():
    if not API_KEY: return jsonify({"error": "Error: Falta API KEY"}), 500
    data = request.json
    paradas = data.get('paradas', [])
    base = data.get('base_address')
    dwell_time = int(data.get('dwell_time', 10))
    
    paradas_objs = []
    for p in paradas:
        obj = {"nombre": "Cliente", "direccion": "", "invoices": "", "pieces": ""}
        if isinstance(p, dict):
            obj.update(p)
            if 'name' in p: obj['nombre'] = p['name']
            if 'address' in p: obj['direccion'] = p['address']
        else:
            obj['direccion'] = p
        paradas_objs.append(obj)
    
    # Geocodificación y TSP simple para 1 vehículo
    # Reutilizamos lógica simple
    c_base, fmt_base, pid_base = obtener_datos_geo(base)
    base_obj = {"coord": c_base, "clean_address": fmt_base, "direccion": base}
    
    puntos = [c_base]
    paradas_validas = []
    for p in paradas_objs:
        c, fmt, pid = obtener_datos_geo(p.get('direccion'))
        if c:
            p['clean_address'] = fmt
            paradas_validas.append(p)
            puntos.append(c)
            
    # Matriz para TSP
    rows = obtener_matriz_segura(puntos)
    if not rows: return jsonify({"error": "Error calculando ruta"}), 500
    
    # OR Tools TSP
    matriz_tiempos = []
    for fila in rows:
        fila_tiempos = []
        for el in fila['elements']:
            fila_tiempos.append(int(el.get('duration', {}).get('value', 999999)))
        matriz_tiempos.append(fila_tiempos)
        
    manager = pywrapcp.RoutingIndexManager(len(matriz_tiempos), 1, 0)
    routing = pywrapcp.RoutingModel(manager)
    
    def time_cb(from_i, to_i):
        return matriz_tiempos[manager.IndexToNode(from_i)][manager.IndexToNode(to_i)]
        
    transit_idx = routing.RegisterTransitCallback(time_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)
    
    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    
    solution = routing.SolveWithParameters(search_params)
    
    ruta_final = []
    tiempo_total = 0
    if solution:
        index = routing.Start(0)
        index = solution.Value(routing.NextVar(index))
        while not routing.IsEnd(index):
            idx = manager.IndexToNode(index)
            ruta_final.append(paradas_validas[idx-1]) # -1 porque 0 es base
            previous_index = index
            index = solution.Value(routing.NextVar(index))
            # Sumar tiempos
            # ... (Simplificado para recalculo rápido)
    else:
        ruta_final = paradas_validas # Fallback

    # Recalcular tiempo real linealmente
    if not ruta_final: return jsonify({"duracion_estimada": 0, "link": "", "paradas": []})
    
    # Generar link
    link = generar_link_puro(base_obj, base_obj, ruta_final)
    
    return jsonify({
        "duracion_estimada": 0, # Placeholder simplificado
        "link": link,
        "paradas": ruta_final
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
