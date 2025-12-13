import os
import sqlite3
import sys
import urllib.parse
import datetime
import requests # NECESARIO PARA OSRM
import math # NECESARIO PARA CALCULAR ÁNGULOS (ZONAS)
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
        print("✅ Base de datos verificada (V3).")
    except Exception as e:
        print(f"❌ Error DB: {e}", file=sys.stderr)

init_db()

def obtener_datos_geo(direccion):
    """
    Usa GOOGLE GEOCODING API (Mantenemos esto por precisión).
    Retorna una tupla (latlng, formatted_address, place_id).
    """
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
        
        # LLAMADA A GOOGLE (Costosa pero precisa, se mantiene)
        try:
            geocode_result = gmaps.geocode(direccion)
        except Exception as api_e:
            print(f"❌ Error API al geocodificar {direccion}: {api_e}", file=sys.stderr)
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

    except Exception as e:
        print(f"❌ Error DB/General: {e}", file=sys.stderr)
        try: conn.close()
        except: pass
    return None, None, None

def obtener_matriz_segura(puntos):
    """
    SUSTITUCIÓN DE GOOGLE DISTANCE MATRIX POR OSRM (Gratis).
    Entrada: Lista de strings "lat,lon" (Formato Google)
    Salida: Matriz de tiempos simulando formato Google.
    """
    if not puntos: return []

    # OSRM requiere formato "lon,lat", Google usa "lat,lon". Invertimos:
    coords_osrm = []
    for p in puntos:
        try:
            lat, lon = p.split(',')
            coords_osrm.append(f"{lon.strip()},{lat.strip()}")
        except:
            continue

    if not coords_osrm: return None

    # URL del servicio público de OSRM (Gratis)
    coords_string = ";".join(coords_osrm)
    url = f"http://router.project-osrm.org/table/v1/driving/{coords_string}"
    
    params = {
        'annotations': 'duration' # Solo pedimos tiempo
    }

    try:
        # User-Agent es buena práctica para no ser bloqueado por OSRM
        headers = {'User-Agent': 'ESSRoutePlanner/1.0'}
        response = requests.get(url, params=params, headers=headers, timeout=10)
        
        if response.status_code != 200:
            print(f"Error OSRM Status: {response.status_code}")
            return None
            
        data = response.json()

        if data.get('code') != 'Ok':
            print("Error OSRM Response:", data.get('code'))
            return None

        durations = data['durations'] # Matriz NxN en segundos (Floats)

        # CONVERTIR A FORMATO GOOGLE (Para no romper el resto del código)
        matriz_google_style = []
        
        for fila in durations:
            fila_google = {'elements': []}
            for duracion_segundos in fila:
                # CORRECCIÓN CRÍTICA: Convertir Float a Int para OR-Tools
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

# --- GENERADOR DE LINKS ---
def generar_link_puro(origen_obj, destino_obj, waypoints_objs):
    base_url = "https://www.google.com/maps/dir/?api=1"
    
    def clean_param(text):
        if not text: return ""
        encoded = urllib.parse.quote_plus(text.strip())
        encoded = encoded.replace('%2C', ',')
        return encoded

    # 1. Origen: OMITIDO (GPS Actual del chofer)
    link = base_url
    
    # 2. Destino (Warehouse/Base)
    dest_addr = clean_param(destino_obj.get('clean_address') or destino_obj.get('direccion'))
    link += f"&destination={dest_addr}"

    # 3. Waypoints
    if waypoints_objs:
        wp_list = []
        for p in waypoints_objs:
            # Preferimos la dirección limpia formateada, si no la original
            texto = p.get('clean_address') or p.get('direccion')
            if texto:
                wp_list.append(clean_param(texto))
            
        wp_string = "|".join(wp_list)
        link += f"&waypoints={wp_string}"

    link += "&travelmode=driving"
    return link

# --- LÓGICA VRP (Igual que antes, pero llama a la nueva matriz) ---
def crear_modelo_datos(lista_paradas, num_vans, base_address_text=None):
    datos = {}
    
    coord_almacen = ALMACEN_COORD
    fmt_almacen = base_address_text 
    pid_almacen = ""

    # 1. Obtener Base Geocodificada
    if base_address_text:
        c, fmt, pid = obtener_datos_geo(base_address_text)
        if c: 
            coord_almacen = c
            fmt_almacen = fmt
            pid_almacen = pid
        else:
            print("⚠️ No se pudo geocodificar la base, usando default.")
    
    # Parsing de coordenadas base para cálculos angulares
    try:
        base_lat, base_lon = map(float, coord_almacen.split(','))
    except:
        base_lat, base_lon = 0.0, 0.0

    # 2. Recolectar todas las paradas válidas en una lista temporal
    paradas_temp = [] 
    paradas_erroneas = []
    
    for item in lista_paradas:
        dir_txt = item.get('direccion') or item.get('address')
        nombre_txt = item.get('nombre') or item.get('name') or "Cliente"
        invoices = item.get('invoices', '')
        pieces = item.get('pieces', '')

        if not dir_txt: continue

        c, fmt, pid = obtener_datos_geo(dir_txt)
        if c:
            # Calcular Ángulo Polar (0 a 360 grados) respecto a la base
            # Esto es clave para la zonificación (Norte, Sur, Este, Oeste)
            try:
                p_lat, p_lon = map(float, c.split(','))
                # atan2 devuelve radianes entre -pi y pi
                angle = math.atan2(p_lat - base_lat, p_lon - base_lon)
            except:
                angle = 0
            
            paradas_temp.append({
                "nombre": nombre_txt, 
                "direccion": dir_txt,
                "clean_address": fmt,
                "place_id": pid,
                "invoices": invoices,
                "pieces": pieces,
                "coord_str": c,
                "angle": angle # Guardamos el ángulo para ordenar
            })
        else:
            paradas_erroneas.append(dir_txt)
    
    if paradas_erroneas:
        return {"invalidas": paradas_erroneas}

    if not paradas_temp: return None

    # 3. ORDENAR PARADAS POR ÁNGULO (SWEEP)
    # Esto pre-agrupa las paradas por zonas geográficas antes de optimizar
    paradas_temp.sort(key=lambda x: x['angle'])

    # 4. Construir listas finales para el solver
    puntos = [coord_almacen] + [p['coord_str'] for p in paradas_temp]
    paradas_validas = paradas_temp # Ya ordenadas

    # AQUÍ LLAMAMOS A OSRM EN VEZ DE GOOGLE
    rows_matriz = obtener_matriz_segura(puntos)
    
    if not rows_matriz: return None
    
    matriz_tiempos = []
    for fila in rows_matriz:
        fila_tiempos = []
        for elemento in fila['elements']:
            val = int(elemento.get('duration', {}).get('value', 999999))
            fila_tiempos.append(val)
        matriz_tiempos.append(fila_tiempos)

    datos['time_matrix'] = matriz_tiempos
    datos['num_vehicles'] = int(num_vans)
    datos['depot'] = 0 
    datos['coords'] = puntos
    
    base_obj = {
        "nombre": "Base", 
        "direccion": base_address_text or "Warehouse", 
        "clean_address": fmt_almacen,
        "place_id": pid_almacen
    }
    datos['paradas_info'] = [base_obj] + paradas_validas
    return datos

def resolver_vrp(datos, dwell_time_minutos):
    manager = pywrapcp.RoutingIndexManager(len(datos['time_matrix']), datos['num_vehicles'], datos['depot'])
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        val = datos['time_matrix'][from_node][to_node]
        if to_node != 0: val += int(dwell_time_minutos * 60) # Asegurar int
        return val

    transit_callback_index = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)
    routing.AddDimension(transit_callback_index, 3600 * 24, 3600 * 24, True, 'Tiempo')
    
    time_dimension = routing.GetDimensionOrDie('Tiempo')
    time_dimension.SetGlobalSpanCostCoefficient(100)
    
    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    
    # --- CAMBIO CRÍTICO PARA ZONIFICACIÓN ---
    # Usamos PATH_CHEAPEST_ARC en lugar de PARALLEL_CHEAPEST_INSERTION.
    # PATH_CHEAPEST_ARC construye ruta por ruta secuencialmente.
    # Combinado con el ordenamiento angular (Sweep) hecho arriba, esto fuerza
    # al algoritmo a llenar un vehículo en una zona (ej. Norte) antes de pasar a la siguiente (ej. Este).
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.seconds = 3 # Un segundo extra para mejor cálculo local

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
                        "direccion": info['direccion'], 
                        "clean_address": info['clean_address'],
                        "place_id": info.get('place_id'),
                        "invoices": info.get('invoices', ''),
                        "pieces": info.get('pieces', ''),
                        "coord": datos['coords'][node_index]
                    })
                
                index = solution.Value(routing.NextVar(index))
            
            nombre_van = f"Van {vehicle_id + 1}"
            
            if ruta:
                base_info = datos['paradas_info'][0]
                full_link = generar_link_puro(base_info, base_info, ruta)
                
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

# --- RECALCULO SIMPLE Y TSP PARCIAL ---
def recalcular_ruta_internal(paradas_objs, base, dwell_time):
    # Usamos OSRM para recalcular también
    c_base, fmt_base, pid_base = obtener_datos_geo(base)
    
    coords_paradas = []
    paradas_con_clean = []

    for p in paradas_objs:
        p_addr = p.get('direccion') or p.get('address')
        if not p_addr: continue
        
        c, fmt, pid = obtener_datos_geo(p_addr)
        if c:
            coords_paradas.append(c)
            p_copy = p.copy()
            p_copy['direccion'] = p_addr
            p_copy['clean_address'] = fmt 
            p_copy['place_id'] = pid
            paradas_con_clean.append(p_copy)
            
    coords_limpias = [c for c in coords_paradas if c]

    if not c_base: return jsonify({"error": "Error base"}), 400

    puntos_secuencia = [c_base] + coords_limpias + [c_base]
    tiempo_total_segundos = 0
    puntos_unicos = list(set(puntos_secuencia))
    
    rows_matriz = obtener_matriz_segura(puntos_unicos)
    
    if rows_matriz:
        mapa_indices = {coord: i for i, coord in enumerate(puntos_unicos)}
        
        for i in range(len(puntos_secuencia) - 1):
            origen = puntos_secuencia[i]
            destino = puntos_secuencia[i+1]
            idx_origen = mapa_indices[origen]
            idx_destino = mapa_indices[destino]
            try:
                el = rows_matriz[idx_origen]['elements'][idx_destino]
                val = int(el.get('duration', {}).get('value', 0)) # Asegurar Int
                tiempo_total_segundos += val
            except: pass
        
    tiempo_total_segundos += (len(coords_limpias) * dwell_time * 60)
    
    base_obj = {"clean_address": fmt_base, "place_id": pid_base, "direccion": base}
    full_link = generar_link_puro(base_obj, base_obj, paradas_con_clean)
    
    return jsonify({
        "duracion_estimada": tiempo_total_segundos / 60,
        "link": full_link,
        "paradas": paradas_con_clean
    })

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
        lista_raw = data['direcciones']
        lista_normalizada = []
        for item in lista_raw:
            if isinstance(item, str): 
                lista_normalizada.append({"nombre": "Cliente", "direccion": item, "invoices": "", "pieces": ""})
            else: 
                lista_normalizada.append(item)

        modelo = crear_modelo_datos(lista_normalizada, data.get('num_vans', 1), data.get('base_address'))
        
        if isinstance(modelo, dict):
            if "invalidas" in modelo: return jsonify({"error": "Direcciones no encontradas", "invalid_addresses": modelo["invalidas"]}), 400
        
        if not modelo: return jsonify({"error": "No hay direcciones válidas."}), 400
        
        resultado = resolver_vrp(modelo, dwell_time)
        return jsonify(resultado)
        
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return jsonify({"error": f"Error interno: {str(e)}"}), 500

@app.route('/optimizar_restantes', methods=['POST'])
def optimizar_restantes():
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
            
    return recalcular_ruta_internal(paradas_objs, base, dwell_time)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
