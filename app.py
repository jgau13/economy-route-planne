import os
import sqlite3
import sys
import math  # Necesario para calcular los lotes
import urllib.parse
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
        c.execute('''CREATE TABLE IF NOT EXISTS direcciones
                     (direccion TEXT PRIMARY KEY, latlng TEXT)''')
        conn.commit()
        conn.close()
        print("✅ Base de datos verificada.")
    except Exception as e:
        print(f"❌ Error DB: {e}", file=sys.stderr)

init_db()

def obtener_coordenadas_inteligentes(direccion):
    db_path = os.path.join(basedir, 'economy_routes.db')
    direccion_clean = direccion.strip().lower()
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT latlng FROM direcciones WHERE direccion=?", (direccion_clean,))
        resultado = c.fetchone()
        if resultado:
            conn.close()
            return resultado[0] 
        
        if not API_KEY: return None
        
        try:
            geocode_result = gmaps.geocode(direccion)
        except Exception as api_e:
            print(f"❌ Error API al geocodificar {direccion}: {api_e}", file=sys.stderr)
            conn.close()
            return None

        if geocode_result and len(geocode_result) > 0:
            loc = geocode_result[0]['geometry']['location']
            latlng_str = f"{loc['lat']},{loc['lng']}"
            c.execute("INSERT OR REPLACE INTO direcciones VALUES (?, ?)", (direccion_clean, latlng_str))
            conn.commit()
            conn.close()
            return latlng_str
        else:
            conn.close()
            return None

    except Exception as e:
        print(f"❌ Error DB/General: {e}", file=sys.stderr)
        try: conn.close()
        except: pass
    return None

# --- NUEVA FUNCIÓN: OBTENER MATRIZ POR LOTES ---
def obtener_matriz_segura(puntos):
    """
    Divide la petición a Google Matrix en lotes para evitar el error MAX_ELEMENTS_EXCEEDED.
    El límite es 100 elementos (origenes * destinos) por llamada.
    """
    num_puntos = len(puntos)
    
    # Calculamos cuántas filas (orígenes) podemos pedir por cada llamada
    # para no pasarnos de 100 elementos.
    # filas * num_puntos <= 100  => filas <= 100 / num_puntos
    if num_puntos == 0: return []
    
    max_filas_por_lote = max(1, int(100 / num_puntos))
    
    # Google también tiene un límite de 25 orígenes/destinos por llamada
    max_filas_por_lote = min(max_filas_por_lote, 25)
    
    matriz_completa = []
    
    # Iteramos por los puntos en bloques
    for i in range(0, num_puntos, max_filas_por_lote):
        # Lote de orígenes (ej: del 0 al 5)
        origenes_lote = puntos[i : i + max_filas_por_lote]
        
        try:
            # Pedimos: Este lote de orígenes contra TODOS los destinos
            # Esto rellena una "banda" horizontal de la matriz final
            respuesta = gmaps.distance_matrix(
                origins=origenes_lote,
                destinations=puntos,
                mode="driving"
            )
            
            if 'rows' in respuesta:
                matriz_completa.extend(respuesta['rows'])
            else:
                print("Error inesperado en respuesta de Google (sin rows)")
                return None
                
        except Exception as e:
            print(f"Error obteniendo lote de matriz: {e}")
            return None
            
    return matriz_completa

# --- LÓGICA VRP ---
def crear_modelo_datos(direcciones_texto, num_vans, base_address_text=None):
    datos = {}
    coord_almacen = ALMACEN_COORD
    
    if base_address_text:
        coord_buscada = obtener_coordenadas_inteligentes(base_address_text)
        if coord_buscada: 
            coord_almacen = coord_buscada
        else:
            return {"error_critico": f"No se encontró la dirección BASE: {base_address_text}"}

    puntos = [coord_almacen]
    direcciones_validas = []
    direcciones_erroneas = []
    
    for dir_txt in direcciones_texto:
        coord = obtener_coordenadas_inteligentes(dir_txt)
        if coord:
            puntos.append(coord)
            direcciones_validas.append(dir_txt)
        else:
            direcciones_erroneas.append(dir_txt)
    
    if direcciones_erroneas:
        return {"invalidas": direcciones_erroneas}

    if len(puntos) <= 1: return None

    # --- USAMOS LA NUEVA FUNCIÓN SEGURA ---
    if not API_KEY: return None
    rows_matriz = obtener_matriz_segura(puntos)
    
    if not rows_matriz:
        return None
    
    # Procesamos la respuesta (que ahora ya está completa y unida)
    matriz_tiempos = []
    for fila in rows_matriz:
        fila_tiempos = []
        for elemento in fila['elements']:
            # Valor por defecto alto si no hay ruta
            valor = elemento.get('duration', {}).get('value', 999999)
            fila_tiempos.append(valor)
        matriz_tiempos.append(fila_tiempos)

    datos['time_matrix'] = matriz_tiempos
    datos['num_vehicles'] = int(num_vans)
    datos['depot'] = 0 
    datos['coords'] = puntos
    datos['nombres_originales'] = [base_address_text if base_address_text else "Warehouse"] + direcciones_validas
    datos['base_coord'] = coord_almacen
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

    solution = routing.SolveWithParameters(search_parameters)
    rutas_finales = {}
    
    if solution:
        for vehicle_id in range(datos['num_vehicles']):
            index = routing.Start(vehicle_id)
            ruta = []
            while not routing.IsEnd(index):
                node_index = manager.IndexToNode(index)
                if node_index != 0:
                    ruta.append({
                        "direccion": datos['nombres_originales'][node_index],
                        "coord": datos['coords'][node_index]
                    })
                index = solution.Value(routing.NextVar(index))
            
            nombre_van = f"Van {vehicle_id + 1}"
            
            if ruta:
                base_txt = urllib.parse.quote(datos['nombres_originales'][0])
                base_url = "https://www.google.com/maps/dir/?api=1"
                stops_str = "&waypoints=" + "|".join([urllib.parse.quote(p["direccion"]) for p in ruta])
                full_link = f"{base_url}&origin={base_txt}&destination={base_txt}{stops_str}"
                
                finish_index = routing.End(vehicle_id)
                tiempo_total = solution.Min(time_dimension.CumulVar(finish_index))
                
                rutas_finales[nombre_van] = {
                    "paradas": ruta,
                    "duracion_estimada": tiempo_total / 60,
                    "link": full_link
                }
            else:
                rutas_finales[nombre_van] = {
                    "paradas": [],
                    "duracion_estimada": 0,
                    "link": ""
                }

    return rutas_finales

# --- OPTIMIZACIÓN PARCIAL ---
def resolver_tsp_parcial(fixed_stop_txt, loose_stops_txt, base_address_txt, dwell_time):
    coord_start = obtener_coordenadas_inteligentes(fixed_stop_txt)
    coord_end = obtener_coordenadas_inteligentes(base_address_txt)
    
    if not coord_start or not coord_end: return None

    coords = [coord_start]
    nombres = [fixed_stop_txt]
    
    for s in loose_stops_txt:
        c = obtener_coordenadas_inteligentes(s)
        if c:
            coords.append(c)
            nombres.append(s)
            
    coords.append(coord_end)
    nombres.append(base_address_txt) 
    
    # USAR LA NUEVA FUNCIÓN SEGURA AQUÍ TAMBIÉN
    if not API_KEY: return None
    rows_matriz = obtener_matriz_segura(coords)
    if not rows_matriz: return None

    time_matrix = []
    for r in rows_matriz:
        row = []
        for el in r['elements']:
            row.append(el.get('duration', {}).get('value', 999999))
        time_matrix.append(row)

    num_locations = len(coords)
    manager = pywrapcp.RoutingIndexManager(num_locations, 1, [0], [num_locations-1])
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        val = time_matrix[from_node][to_node]
        if to_node != num_locations - 1: 
            val += dwell_time * 60
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
            nuevo_orden_paradas.append({
                "direccion": nombres[node_index],
                "coord": coords[node_index]
            })
            index = solution.Value(routing.NextVar(index))
            
    return nuevo_orden_paradas

# --- RUTAS ---

@app.route('/')
def serve_frontend():
    return send_from_directory(basedir, 'index.html')

@app.route('/config')
def get_config():
    return jsonify({
        "googleApiKey": API_KEY,
        "firebaseConfig": FIREBASE_CONFIG
    })

@app.route('/optimizar', methods=['POST'])
def optimizar():
    if not API_KEY: return jsonify({"error": "Error: Falta API KEY."}), 500
    try:
        data = request.json
        if not data or 'direcciones' not in data: return jsonify({"error": "Faltan datos"}), 400
        
        dwell_time = int(data.get('dwell_time', 10))
        modelo = crear_modelo_datos(data['direcciones'], data.get('num_vans', 1), data.get('base_address'))
        
        if isinstance(modelo, dict):
            if "invalidas" in modelo:
                return jsonify({"error": "Direcciones no encontradas", "invalid_addresses": modelo["invalidas"]}), 400
            if "error_critico" in modelo:
                return jsonify({"error": modelo["error_critico"]}), 400
        
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
    
    if len(paradas_actuales) < 3:
        return recalcular_ruta_internal(paradas_actuales, base_address, dwell_time) 

    fixed_stop = paradas_actuales[0]
    loose_stops = paradas_actuales[1:]
    
    fixed_txt = fixed_stop['direccion'] if isinstance(fixed_stop, dict) else fixed_stop
    loose_txts = [p['direccion'] if isinstance(p, dict) else p for p in loose_stops]
    
    nuevas_loose_ordenadas = resolver_tsp_parcial(fixed_txt, loose_txts, base_address, dwell_time)
    
    if nuevas_loose_ordenadas is None:
        return jsonify({"error": "Error optimizando restantes"}), 500
        
    c_fixed = obtener_coordenadas_inteligentes(fixed_txt)
    lista_final = [{"direccion": fixed_txt, "coord": c_fixed}] + nuevas_loose_ordenadas
    
    return recalcular_ruta_internal(lista_final, base_address, dwell_time)

def recalcular_ruta_internal(paradas_objs, base, dwell_time):
    base_coord = obtener_coordenadas_inteligentes(base)
    coords_paradas = [p['coord'] for p in paradas_objs]
    textos_paradas = [p['direccion'] for p in paradas_objs]
    
    if not base_coord: return jsonify({"error": "Error base"}), 400

    puntos_secuencia = [base_coord] + coords_paradas + [base_coord]
    tiempo_total_segundos = 0
    puntos_unicos = list(set(puntos_secuencia))
    
    # --- USAR NUEVA FUNCIÓN SEGURA ---
    rows_matriz = obtener_matriz_segura(puntos_unicos)
    if not rows_matriz: return jsonify({"error": "Error calculando tiempos"}), 500
        
    mapa_indices = {coord: i for i, coord in enumerate(puntos_unicos)}
    rows = rows_matriz # Ya viene completa
    
    for i in range(len(puntos_secuencia) - 1):
        origen = puntos_secuencia[i]
        destino = puntos_secuencia[i+1]
        idx_origen = mapa_indices[origen]
        idx_destino = mapa_indices[destino]
        try:
            val = rows[idx_origen]['elements'][idx_destino]['duration']['value']
            tiempo_total_segundos += val
        except:
            pass
        
    tiempo_total_segundos += (len(coords_paradas) * dwell_time * 60)
    
    base_txt = urllib.parse.quote(base)
    base_url = "https://www.google.com/maps/dir/?api=1"
    stops_str = "&waypoints=" + "|".join([urllib.parse.quote(t) for t in textos_paradas])
    full_link = f"{base_url}&origin={base_txt}&destination={base_txt}{stops_str}"
    
    return jsonify({
        "duracion_estimada": tiempo_total_segundos / 60,
        "link": full_link,
        "paradas": paradas_objs
    })

@app.route('/recalcular', methods=['POST'])
def recalcular_ruta():
    if not API_KEY: return jsonify({"error": "Error: Falta Configurar la API KEY"}), 500
    data = request.json
    paradas = data.get('paradas', [])
    base = data.get('base_address')
    dwell_time = int(data.get('dwell_time', 10))
    
    paradas_objs = []
    for p in paradas:
        if isinstance(p, dict):
            paradas_objs.append(p)
        else:
            c = obtener_coordenadas_inteligentes(p)
            if c: paradas_objs.append({"direccion": p, "coord": c})
            
    return recalcular_ruta_internal(paradas_objs, base, dwell_time)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
