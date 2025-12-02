import os
import sqlite3
import sys
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import googlemaps
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
from dotenv import load_dotenv

load_dotenv()

# Configuración de rutas absolutas para evitar errores en la nube
basedir = os.path.abspath(os.path.dirname(__file__))

# Configuramos Flask para servir archivos estáticos directamente desde la raíz
app = Flask(__name__, static_folder=basedir, static_url_path='')
CORS(app) 

# --- CONFIGURACIÓN DE LLAVES Y VARIABLES DE ENTORNO ---
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
    print("ADVERTENCIA: No se detectó GOOGLE_MAPS_API_KEY en las variables de entorno.", file=sys.stderr)

try:
    if API_KEY:
        gmaps = googlemaps.Client(key=API_KEY)
except ValueError as e:
    print(f"Error iniciando Google Maps: {e}", file=sys.stderr)

ALMACEN_COORD = "25.7617,-80.1918" 

# --- BASE DE DATOS (AUTO-INICIALIZACIÓN) ---
def init_db():
    """Inicializa la base de datos y crea la tabla si no existe."""
    db_path = os.path.join(basedir, 'economy_routes.db')
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS direcciones
                     (direccion TEXT PRIMARY KEY, latlng TEXT)''')
        conn.commit()
        conn.close()
        print("✅ Base de datos verificada y tabla 'direcciones' lista.")
    except Exception as e:
        print(f"❌ Error crítico inicializando base de datos: {e}", file=sys.stderr)

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
            print(f"⚠️ Geocodificación fallida para: {direccion}. No hay resultados.", file=sys.stderr)
            conn.close()
            return None

    except Exception as e:
        print(f"❌ Error DB/General al geocodificar {direccion}: {e}", file=sys.stderr)
        try: conn.close()
        except: pass
    return None

# --- LÓGICA VRP (OPTIMIZACIÓN GLOBAL) ---
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

    try:
        if not API_KEY: return None
        matriz_respuesta = gmaps.distance_matrix(origins=puntos, destinations=puntos, mode="driving")
    except Exception as e:
        print(f"Error API Google Matrix: {e}", file=sys.stderr)
        return None
    
    matriz_tiempos = []
    for fila in matriz_respuesta['rows']:
        fila_tiempos = []
        for elemento in fila['elements']:
            valor = elemento.get('duration', {}).get('value', 999999)
            fila_tiempos.append(valor)
        matriz_tiempos.append(fila_tiempos)

    datos['time_matrix'] = matriz_tiempos
    datos['num_vehicles'] = int(num_vans)
    datos['depot'] = 0 
    datos['coords'] = puntos
    datos['nombres_originales'] = ["Warehouse"] + direcciones_validas
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
                base_coord = datos['base_coord']
                base_url = "https://www.google.com/maps/dir/?api=1"
                stops_str = "&waypoints=" + "|".join([p["coord"] for p in ruta])
                full_link = f"{base_url}&origin={base_coord}&destination={base_coord}{stops_str}"
                
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

# --- NUEVA LÓGICA: OPTIMIZACIÓN PARCIAL (TSP CON INICIO FIJO) ---
def resolver_tsp_parcial(fixed_stop_txt, loose_stops_txt, base_address_txt, dwell_time):
    # 1. Obtener coordenadas
    coord_start = obtener_coordenadas_inteligentes(fixed_stop_txt)
    coord_end = obtener_coordenadas_inteligentes(base_address_txt)
    
    if not coord_start or not coord_end:
        return None

    # Lista completa: [Inicio (Fijo), ...Libres..., Fin (Base)]
    coords = [coord_start]
    nombres = [fixed_stop_txt]
    
    for s in loose_stops_txt:
        c = obtener_coordenadas_inteligentes(s)
        if c:
            coords.append(c)
            nombres.append(s)
            
    # Añadimos la base al final para cerrar la matriz, pero es el destino
    coords.append(coord_end)
    nombres.append("Warehouse") 
    
    # 2. Matriz de distancias
    try:
        if not API_KEY: return None
        matriz_res = gmaps.distance_matrix(origins=coords, destinations=coords, mode="driving")
    except Exception as e:
        print(f"Error TSP Matrix: {e}")
        return None

    time_matrix = []
    for r in matriz_res['rows']:
        row = []
        for el in r['elements']:
            row.append(el.get('duration', {}).get('value', 999999))
        time_matrix.append(row)

    # 3. Solver TSP (Inicio=0, Fin=Ultimo)
    num_locations = len(coords)
    # En OR-Tools, start y end son listas de indices de nodos
    # Inicio es el nodo 0 (Fixed Stop), Fin es el nodo num_locations-1 (Warehouse)
    manager = pywrapcp.RoutingIndexManager(num_locations, 1, [0], [num_locations-1])
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        val = time_matrix[from_node][to_node]
        # Sumar dwell time si no es el depósito final
        if to_node != num_locations - 1: 
            val += dwell_time * 60
        return val

    transit_idx = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)

    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC

    solution = routing.SolveWithParameters(search_params)
    
    nuevo_orden_paradas = []
    
    if solution:
        index = routing.Start(0)
        # Saltamos el Start porque ya es el fixed stop que tenemos en el frontend
        # Pero necesitamos el orden de los siguientes
        
        # El primero es el fixed stop
        # nuevo_orden_paradas.append(nombres[manager.IndexToNode(index)]) 
        
        index = solution.Value(routing.NextVar(index)) # Pasamos al siguiente
        
        while not routing.IsEnd(index):
            node_index = manager.IndexToNode(index)
            # Guardamos nombre y coord para devolver al frontend
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
    if not API_KEY: 
        return jsonify({"error": "Error: Falta configurar la API KEY de Google Maps en Render."}), 500
    
    try:
        data = request.json
        if not data or 'direcciones' not in data: 
            return jsonify({"error": "Faltan datos de direcciones"}), 400
        
        dwell_time = int(data.get('dwell_time', 10))
        
        modelo = crear_modelo_datos(data['direcciones'], data.get('num_vans', 1), data.get('base_address'))
        
        if isinstance(modelo, dict):
            if "invalidas" in modelo:
                return jsonify({"error": "Direcciones no encontradas", "invalid_addresses": modelo["invalidas"]}), 400
            if "error_critico" in modelo:
                return jsonify({"error": modelo["error_critico"]}), 400
        
        if not modelo: 
            return jsonify({"error": "No se encontraron suficientes direcciones válidas."}), 400
        
        resultado = resolver_vrp(modelo, dwell_time)
        return jsonify(resultado)
        
    except Exception as e:
        print(f"❌ ERROR FATAL EN /optimizar: {e}", file=sys.stderr)
        return jsonify({"error": f"Error interno: {str(e)}"}), 500

@app.route('/optimizar_restantes', methods=['POST'])
def optimizar_restantes():
    if not API_KEY: return jsonify({"error": "Falta API Key"}), 500
    data = request.json
    
    # Recibimos lista completa: [Fixed, A, B, C...]
    paradas_actuales = data.get('paradas', [])
    base_address = data.get('base_address')
    dwell_time = int(data.get('dwell_time', 10))
    
    if len(paradas_actuales) < 3:
        # Si hay menos de 3 (Fixed + 1 + ...), no hay nada que reordenar realmente (o solo 1)
        # Simplemente devolvemos la lista tal cual o calculamos
        return recalcular_ruta() 

    fixed_stop = paradas_actuales[0] # La #1 se queda fija
    loose_stops = paradas_actuales[1:] # El resto se baraja
    
    # Extraer solo el texto de la dirección si vienen objetos
    fixed_txt = fixed_stop['direccion'] if isinstance(fixed_stop, dict) else fixed_stop
    loose_txts = [p['direccion'] if isinstance(p, dict) else p for p in loose_stops]
    
    # Llamamos al solver TSP parcial
    nuevas_loose_ordenadas = resolver_tsp_parcial(fixed_txt, loose_txts, base_address, dwell_time)
    
    if nuevas_loose_ordenadas is None:
        return jsonify({"error": "Error optimizando restantes"}), 500
        
    # Reconstruimos la lista: [Fixed] + [Nuevas Ordenadas]
    # Ojo: resolver_tsp_parcial devuelve objetos con {direccion, coord}
    
    # Reconstruimos el objeto del fijo para que coincida formato
    c_fixed = obtener_coordenadas_inteligentes(fixed_txt)
    lista_final = [{"direccion": fixed_txt, "coord": c_fixed}] + nuevas_loose_ordenadas
    
    # Ahora recalculamos tiempos finales para esa secuencia
    # Usamos la lógica de recalcular_ruta internamente
    return recalcular_ruta_internal(lista_final, base_address, dwell_time)

def recalcular_ruta_internal(paradas_objs, base, dwell_time):
    # Función auxiliar para no duplicar código
    base_coord = obtener_coordenadas_inteligentes(base)
    coords_paradas = [p['coord'] for p in paradas_objs]
    
    if not base_coord: return jsonify({"error": "Error base"}), 400

    puntos_secuencia = [base_coord] + coords_paradas + [base_coord]
    tiempo_total_segundos = 0
    puntos_unicos = list(set(puntos_secuencia))
    
    try:
        matriz = gmaps.distance_matrix(origins=puntos_unicos, destinations=puntos_unicos, mode="driving")
    except Exception as e:
        return jsonify({"error": str(e)}), 500
        
    mapa_indices = {coord: i for i, coord in enumerate(puntos_unicos)}
    rows = matriz['rows']
    
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
    
    base_url = "https://www.google.com/maps/dir/?api=1"
    stops_str = "&waypoints=" + "|".join(coords_paradas)
    full_link = f"{base_url}&origin={base_coord}&destination={base_coord}{stops_str}"
    
    return jsonify({
        "duracion_estimada": tiempo_total_segundos / 60,
        "link": full_link,
        "paradas": paradas_objs # Devolvemos la nueva lista ordenada
    })

@app.route('/recalcular', methods=['POST'])
def recalcular_ruta():
    if not API_KEY: return jsonify({"error": "Error: Falta Configurar la API KEY"}), 500
    data = request.json
    paradas = data.get('paradas', [])
    base = data.get('base_address')
    dwell_time = int(data.get('dwell_time', 10))
    
    # Normalizar paradas a objetos si no lo son
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
