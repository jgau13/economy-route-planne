import os
import sqlite3
import sys
import urllib.parse
import datetime
import random
import math
import traceback 
import time
import re # IMPORTANTE: Para validar Zip Codes con Regex
# IMPORTANTE: Asegúrate de que requirements.txt tenga: requests, python-dotenv, googlemaps
import requests 
from requests.exceptions import Timeout, ConnectionError 
import concurrent.futures
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
from dotenv import load_dotenv
import googlemaps

# Carga variables de entorno locales
load_dotenv()

basedir = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__, static_folder=basedir, static_url_path='')
CORS(app)

# =============================================================================
# MANEJADORES DE ERROR
# =============================================================================
@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Error Interno (500)", "details": str(error)}), 500

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint no encontrado (404)", "details": str(error)}), 404

@app.errorhandler(Exception)
def handle_exception(e):
    print(f"❌ Error Crítico: {str(e)}", file=sys.stderr)
    traceback.print_exc()
    return jsonify({"error": "Error del Sistema", "details": str(e)}), 500

# =============================================================================
# CONFIGURACIÓN
# =============================================================================
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
DISTANCEMATRIX_AI_KEY = os.environ.get("DISTANCEMATRIX_AI_KEY")

FIREBASE_CONFIG = {
    "apiKey": os.environ.get("FIREBASE_API_KEY"),
    "authDomain": os.environ.get("FIREBASE_AUTH_DOMAIN"),
    "projectId": os.environ.get("FIREBASE_PROJECT_ID"),
    "storageBucket": os.environ.get("FIREBASE_STORAGE_BUCKET"),
    "messagingSenderId": os.environ.get("FIREBASE_MESSAGING_SENDER_ID"),
    "appId": os.environ.get("FIREBASE_APP_ID")
}

ALMACEN_COORD = "28.450324,-81.405368" 

if not GOOGLE_MAPS_API_KEY:
    print("⚠️ ADVERTENCIA: GOOGLE_MAPS_API_KEY no detectada.", file=sys.stderr)

if not DISTANCEMATRIX_AI_KEY:
    print("⚠️ ADVERTENCIA: DISTANCEMATRIX_AI_KEY no detectada.", file=sys.stderr)

gmaps = None
try:
    if GOOGLE_MAPS_API_KEY:
        gmaps = googlemaps.Client(key=GOOGLE_MAPS_API_KEY)
except Exception as e:
    print(f"❌ Error iniciando Google Maps: {e}", file=sys.stderr)

# =============================================================================
# BASE DE DATOS
# =============================================================================
def init_db():
    db_path = os.path.join(basedir, 'economy_routes.db')
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS direcciones_v3
                     (direccion TEXT PRIMARY KEY, latlng TEXT, place_id TEXT, formatted_address TEXT)''')
        conn.commit()
        conn.close()
        print("✅ Base de datos verificada.", flush=True)
    except Exception as e:
        print(f"❌ Error DB: {e}", file=sys.stderr)

init_db()

# =============================================================================
# FUNCIONES LÓGICAS
# =============================================================================

def obtener_datos_geo(direccion):
    """
    Obtiene Lat/Lng. 
    ESTRICTO: Retorna None si no hay Zip Code en la dirección o resultado.
    """
    if not direccion: return None, None, None
    direccion_clean = direccion.strip().lower()
    
    db_path = os.path.join(basedir, 'economy_routes.db')
    conn = None
    try:
        # Timeout para evitar bloqueos en SQLite
        conn = sqlite3.connect(db_path, timeout=30)
        c = conn.cursor()
        c.execute("SELECT latlng, formatted_address, place_id FROM direcciones_v3 WHERE direccion=?", (direccion_clean,))
        res_db = c.fetchone()
        
        if res_db:
            # VALIDACIÓN ESTRICTA EN CACHÉ
            # Si lo que tenemos guardado no tiene un número de 5 dígitos, lo consideramos inválido
            formatted_addr = res_db[1]
            if not re.search(r'\b\d{5}\b', formatted_addr):
                print(f"⚠️ Dirección en caché rechazada (Falta Zip): {formatted_addr}", file=sys.stderr)
                return None, None, None
            
            return res_db[0], res_db[1], res_db[2]
        
        if not gmaps: return None, None, None
        
        try:
            geocode_result = gmaps.geocode(direccion)
        except Exception as e:
            print(f"Error API Geocoding: {e}")
            return None, None, None

        if geocode_result and len(geocode_result) > 0:
            res = geocode_result[0]
            
            # VALIDACIÓN ESTRICTA EN API
            # Debe tener un componente 'postal_code'
            has_zip = False
            for component in res.get('address_components', []):
                if 'postal_code' in component.get('types', []):
                    has_zip = True
                    break
            
            formatted_addr = res.get('formatted_address', direccion)
            
            if not has_zip:
                 print(f"⚠️ Dirección API rechazada (Falta Zip): {formatted_addr}", file=sys.stderr)
                 return None, None, None

            loc = res['geometry']['location']
            latlng_str = f"{loc['lat']},{loc['lng']}"
            place_id = res.get('place_id', '')
            
            c.execute("INSERT OR REPLACE INTO direcciones_v3 VALUES (?, ?, ?, ?)", (direccion_clean, latlng_str, place_id, formatted_addr))
            conn.commit()
            return latlng_str, formatted_addr, place_id
            
    except Exception as e:
        print(f"Error Geocoding General: {e}", file=sys.stderr)
    finally:
        if conn: conn.close()
        
    return None, None, None

def parse_latlng(latlng_str):
    try:
        if not latlng_str: return 0.0, 0.0
        parts = latlng_str.split(',')
        return float(parts[0]), float(parts[1])
    except:
        return 0.0, 0.0

def simple_kmeans_plus(points, k, max_iter=100):
    """
    K-Means con inicialización inteligente (K-Means++) pero SIN forzar balanceo.
    Esto agrupa de forma natural por cercanía geográfica.
    """
    if not points: return []
    if k <= 0: return [points]
    if k > len(points): k = len(points) # Ajuste si hay más vans que paradas

    # 1. Elegir primer centroide al azar
    centroids = [random.choice([p['coords'] for p in points])]
    
    # 2. Elegir el resto de centroides basados en la distancia (K-Means++ Init)
    for _ in range(k - 1):
        dists = []
        for p in points:
            coords = p['coords']
            min_dist_sq = min((coords[0]-c[0])**2 + (coords[1]-c[1])**2 for c in centroids)
            dists.append(min_dist_sq)
        
        total_dist = sum(dists)
        if total_dist == 0:
            remaining = [p['coords'] for p in points if p['coords'] not in centroids]
            if remaining: centroids.append(random.choice(remaining))
            else: break
        else:
            r = random.uniform(0, total_dist)
            current = 0
            for i, d in enumerate(dists):
                current += d
                if current >= r:
                    centroids.append(points[i]['coords'])
                    break
    
    # 3. Iterar
    clusters = [[] for _ in range(len(centroids))]
    for _ in range(max_iter):
        clusters = [[] for _ in range(len(centroids))]
        for p in points:
            plat, plng = p['coords']
            best_idx = 0
            min_dist = float('inf')
            for i, (clat, clng) in enumerate(centroids):
                d = (plat - clat)**2 + (plng - clng)**2
                if d < min_dist:
                    min_dist = d
                    best_idx = i
            clusters[best_idx].append(p)
        
        new_centroids = []
        diff = 0
        for i in range(len(centroids)):
            cluster = clusters[i]
            if not cluster:
                new_centroids.append(centroids[i])
                continue
            
            avg_lat = sum(x['coords'][0] for x in cluster) / len(cluster)
            avg_lng = sum(x['coords'][1] for x in cluster) / len(cluster)
            new_centroids.append((avg_lat, avg_lng))
            diff += (avg_lat - centroids[i][0])**2 + (avg_lng - centroids[i][1])**2
        
        centroids = new_centroids
        if diff < 1e-6: break
        
    return clusters

def obtener_matriz_dm_ai(puntos):
    """
    Consulta a DistanceMatrix.ai con PAGINACIÓN (Chunking) para evitar el límite de 100 elementos.
    """
    if not puntos or len(puntos) < 2: return [[0]]
    if not DISTANCEMATRIX_AI_KEY: raise Exception("Falta configurar DISTANCEMATRIX_AI_KEY")

    n = len(puntos)
    full_matrix = [[0] * n for _ in range(n)]
    clean_points = [p.replace(" ", "") for p in puntos]
    
    # Límite seguro para la API "Fast" (10x10 = 100 elementos)
    BATCH_SIZE = 10
    
    print(f"🌍 Consultando DM.ai paginado para {n} puntos (Total: {n*n} elementos)...", file=sys.stdout)

    for i in range(0, n, BATCH_SIZE):
        chunk_origins = clean_points[i : i + BATCH_SIZE]
        for j in range(0, n, BATCH_SIZE):
            chunk_dests = clean_points[j : j + BATCH_SIZE]
            
            origins_str = "|".join(chunk_origins)
            dests_str = "|".join(chunk_dests)
            
            url = f"https://api.distancematrix.ai/maps/api/distancematrix/json?origins={origins_str}&destinations={dests_str}&key={DISTANCEMATRIX_AI_KEY}"
            
            try:
                r = requests.get(url, timeout=20)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("status") == "OK":
                        rows = data.get("rows", [])
                        for r_idx, row in enumerate(rows):
                            elements = row.get("elements", [])
                            for c_idx, element in enumerate(elements):
                                val = 999999
                                if element.get("status") == "OK":
                                    val = element["duration"]["value"]
                                global_row = i + r_idx
                                global_col = j + c_idx
                                full_matrix[global_row][global_col] = val
                    else:
                        err = data.get("error_message") or data.get("status")
                        print(f"⚠️ Error API DM.ai en lote {i},{j}: {err}", file=sys.stderr)
                else:
                    print(f"⚠️ HTTP Error {r.status_code} en lote {i},{j}", file=sys.stderr)
                
                # Pequeña pausa si hay muchos datos
                if n > 20: time.sleep(0.1)

            except Exception as e:
                print(f"⚠️ Excepción en lote {i},{j}: {str(e)}", file=sys.stderr)

    print("✅ Matriz DM.ai completada.", file=sys.stdout)
    return full_matrix

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

def crear_modelo_datos(items, n_vans, base_addr):
    base_coord = ALMACEN_COORD
    base_fmt = base_addr
    base_id = ""
    
    if base_addr:
        c, f, i = obtener_datos_geo(base_addr)
        if c: 
            base_coord, base_fmt, base_id = c, f, i
        else:
            return {"invalidas": [f"BASE: {base_addr}"]}

    puntos_coords = [base_coord]
    paradas_validas = []
    paradas_malas = []
    
    for it in items:
        # Optimización: Usar datos pre-calculados si existen
        if 'latlng' in it and it['latlng']:
            c = it['latlng']
            f = it.get('clean_address', '')
            i = it.get('place_id', '')
            addr = it.get('direccion') or it.get('address')
        else:
            addr = it.get('direccion') or it.get('address')
            if not addr: continue
            c, f, i = obtener_datos_geo(addr)
        
        if c:
            puntos_coords.append(c)
            paradas_validas.append({
                "nombre": it.get('nombre') or it.get('name') or "Cliente",
                "direccion": addr,
                "clean_address": f,
                "place_id": i,
                "invoices": it.get('invoices',''),
                "pieces": it.get('pieces','')
            })
        else:
            paradas_malas.append(addr)
            
    if paradas_malas:
        return {"invalidas": paradas_malas}

    if len(puntos_coords) < 2: return None 
    
    try:
        matriz = obtener_matriz_dm_ai(puntos_coords)
    except Exception as e:
        return {"error_critico": f"Error calculando rutas: {str(e)}"}
        
    return {
        "time_matrix": matriz,
        "num_vehicles": int(n_vans),
        "depot": 0,
        "coords": puntos_coords,
        "paradas_info": [{"nombre":"Base","direccion":base_addr,"clean_address":base_fmt}] + paradas_validas
    }

def resolver_vrp(data_model, dwell_min):
    manager = pywrapcp.RoutingIndexManager(len(data_model['time_matrix']), data_model['num_vehicles'], data_model['depot'])
    routing = pywrapcp.RoutingModel(manager)
    
    def time_cb(from_i, to_i):
        fn = manager.IndexToNode(from_i)
        tn = manager.IndexToNode(to_i)
        val = data_model['time_matrix'][fn][tn]
        if tn != 0: val += (dwell_min * 60)
        return val
        
    transit_cb_idx = routing.RegisterTransitCallback(time_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_cb_idx)
    routing.AddDimension(transit_cb_idx, 86400, 86400, True, 'Time')
    
    time_dim = routing.GetDimensionOrDie('Time')
    time_dim.SetGlobalSpanCostCoefficient(100)
    
    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    search_params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_params.time_limit.seconds = 1 
    
    sol = routing.SolveWithParameters(search_params)
    resultado = {}
    
    if sol:
        for v_id in range(data_model['num_vehicles']):
            index = routing.Start(v_id)
            ruta = []
            while not routing.IsEnd(index):
                node = manager.IndexToNode(index)
                if node != 0:
                    info = data_model['paradas_info'][node]
                    ruta.append({
                        **info,
                        "coord": data_model['coords'][node]
                    })
                index = sol.Value(routing.NextVar(index))
                
            end_idx = routing.End(v_id)
            total_time_sec = sol.Min(time_dim.CumulVar(end_idx))
            
            base = data_model['paradas_info'][0]
            link = "https://www.google.com/maps/dir/?api=1"
            def q(s): return urllib.parse.quote_plus(str(s)).replace('%2C', ',')
            
            link += f"&destination={q(base.get('clean_address') or base.get('direccion'))}"
            
            if ruta:
                wps = "|".join([q(r.get('clean_address') or r.get('direccion')) for r in ruta])
                link += f"&waypoints={wps}"
            
            link += "&travelmode=driving"

            resultado[f"Van {v_id+1}"] = {
                "paradas": ruta,
                "duracion_estimada": total_time_sec / 60,
                "link": link
            }
            
    return resultado

# =============================================================================
# ENDPOINTS
# =============================================================================

@app.route('/')
def index(): return send_from_directory(basedir, 'index.html')

@app.route('/health')
def health(): return jsonify({"status":"ok"}), 200

@app.route('/config')
def config():
    return jsonify({
        "googleApiKey": GOOGLE_MAPS_API_KEY, 
        "firebaseConfig": FIREBASE_CONFIG
    })

def procesar_geocoding(s):
    addr = s.get('direccion') or s.get('address')
    if not addr: return None
    c, f, i = obtener_datos_geo(addr) 
    if c:
        lat, lng = parse_latlng(c)
        s_enriched = s.copy()
        s_enriched.update({
            'latlng': c, 
            'clean_address': f, 
            'place_id': i
        })
        return {'coords': (lat, lng), 'data': s_enriched}
    return {'error': addr}

def resolver_cluster_wrapper(i, clust, base, dwell):
    d_name = f"Van {i+1}"
    if not clust:
        return d_name, {"paradas":[], "duracion_estimada":0, "link":""}
    
    sub_stops = [x['data'] for x in clust]
    model = crear_modelo_datos(sub_stops, 1, base)
    
    if not model:
        return d_name, {"error": "Modelo de datos vacío"}
    
    if "error_critico" in model:
        return d_name, {"error": model["error_critico"]}
        
    if "invalidas" in model:
        return d_name, {"error": f"Direcciones inválidas en sub-cluster: {model['invalidas']}"}

    res = resolver_vrp(model, dwell)
    if res:
        return d_name, list(res.values())[0]
    
    return d_name, {"paradas":[], "duracion_estimada":0, "link":""}

@app.route('/optimizar', methods=['POST'])
def optimizar():
    req = request.json
    n_vans = int(req.get('num_vans', 1))
    base = req.get('base_address')
    dwell = int(req.get('dwell_time', 6))
    raw_stops = req.get('direcciones', [])
    
    stops = []
    for s in raw_stops:
        if isinstance(s, str): stops.append({"direccion": s, "nombre": "Cliente"})
        else: stops.append(s)
        
    if not stops: return jsonify({"error": "Sin paradas"}), 400
    
    try:
        final_routes = {}
        
        if n_vans > 1:
            points_cluster = []
            malas = []
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                futures = {executor.submit(procesar_geocoding, s): s for s in stops}
                for future in concurrent.futures.as_completed(futures):
                    res = future.result()
                    if res:
                        if 'error' in res:
                            malas.append(res['error'])
                        else:
                            points_cluster.append(res)
            
            # CHECK ESTRICTO: Si hay alguna mala, abortamos todo
            if malas:
                 return jsonify({"error": f"Direcciones no válidas (Falta Zip o no encontrada): {', '.join(malas)}"}), 400
            
            if not points_cluster:
                 return jsonify({"error": "No hay direcciones válidas."}), 400
            
            # 2. CLUSTERS (K-MEANS PLUS PLUS - NATURAL)
            clusters = simple_kmeans_plus(points_cluster, n_vans)
            
            # 3. RUTAS PARALELAS
            with concurrent.futures.ThreadPoolExecutor(max_workers=n_vans if n_vans < 4 else 3) as executor:
                futures_vrp = [executor.submit(resolver_cluster_wrapper, i, clust, base, dwell) for i, clust in enumerate(clusters)]
                for future in concurrent.futures.as_completed(futures_vrp):
                    d_name, result = future.result()
                    if "error" in result:
                         return jsonify({"error": f"Error calculando {d_name}: {result['error']}"}), 500
                    final_routes[d_name] = result
                    
        else:
            model = crear_modelo_datos(stops, 1, base)
            if model and "invalidas" in model:
                return jsonify({"error": f"Direcciones no válidas (Falta Zip o no encontrada): {', '.join(model['invalidas'])}"}), 400
            if model and "error_critico" in model:
                return jsonify({"error": model["error_critico"]}), 400
            if model:
                final_routes = resolver_vrp(model, dwell)
            else:
                return jsonify({"error": "Error creando modelo"}), 400
                
        return jsonify(final_routes)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/recalcular', methods=['POST'])
def recalcular():
    req = request.json
    return recalcular_ruta_internal(req.get('paradas', []), req.get('base_address'), int(req.get('dwell_time', 6)))

@app.route('/optimizar_restantes', methods=['POST'])
def optimizar_restantes():
    data = request.json
    paradas = data.get('paradas', [])
    base = data.get('base_address')
    dwell = int(data.get('dwell_time', 6))
    
    if len(paradas) < 3: return recalcular_ruta_internal(paradas, base, dwell)

    fixed = paradas[0]
    loose = paradas[1:]
    
    new_order = resolver_tsp_parcial(fixed, loose, base, dwell)
    if not new_order: return jsonify({"error": "Fallo re-optimizando"}), 500
    
    return recalcular_ruta_internal(new_order, base, dwell)

def recalcular_ruta_internal(paradas_objs, base, dwell_time):
    c_base, fmt_base, _ = obtener_datos_geo(base)
    if not c_base: return jsonify({"error": "Base invalida"}), 400
    
    coords = [c_base]
    clean_stops = []
    
    for s in paradas_objs:
        addr = s.get('direccion') or s.get('address')
        c, f, i = obtener_datos_geo(addr)
        if c:
            coords.append(c)
            s_new = s.copy()
            s_new.update({"clean_address": f, "place_id": i, "coord": c})
            clean_stops.append(s_new)
            
    coords.append(c_base)
    
    total_min = 0
    try:
        uniques = list(set(coords))
        if len(uniques) > 1:
            matrix = obtener_matriz_dm_ai(uniques)
            idx_map = {u: i for i, u in enumerate(uniques)}
            
            for i in range(len(coords)-1):
                u, v = coords[i], coords[i+1]
                t_sec = matrix[idx_map[u]][idx_map[v]]
                if t_sec < 900000:
                    total_min += (t_sec / 60)
    except:
        pass 
        
    total_min += (len(clean_stops) * dwell_time)
    
    link = generar_link_puro({'clean_address': fmt_base}, {'clean_address': fmt_base}, clean_stops)
    
    return jsonify({
        "duracion_estimada": total_min,
        "link": link,
        "paradas": clean_stops
    })

def resolver_tsp_parcial(fixed, loose, base, dwell):
    model = crear_modelo_datos(loose, 1, fixed.get('direccion'))
    if not model or "error" in model or "invalidas" in model: return None
    
    manager = pywrapcp.RoutingIndexManager(len(model['time_matrix']), 1, 0)
    routing = pywrapcp.RoutingModel(manager)
    
    def time_cb(i, j):
        return model['time_matrix'][manager.IndexToNode(i)][manager.IndexToNode(j)]
        
    routing.SetArcCostEvaluatorOfAllVehicles(routing.RegisterTransitCallback(time_cb))
    
    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    
    sol = routing.SolveWithParameters(search_params)
    if not sol: return None
    
    ordered = []
    index = routing.Start(0)
    while not routing.IsEnd(index):
        node = manager.IndexToNode(index)
        if node != 0:
            info = model['paradas_info'][node]
            ordered.append({
                "nombre": info['nombre'], "direccion": info['direccion'],
                "clean_address": info['clean_address'], "place_id": info.get('place_id'),
                "invoices": info.get('invoices',''), "pieces": info.get('pieces','')
            })
        index = sol.Value(routing.NextVar(index))
        
    return [fixed] + ordered

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Servidor corriendo en puerto {port}")
    app.run(host='0.0.0.0', port=port)
