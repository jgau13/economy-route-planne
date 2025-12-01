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

# --- CONFIGURACIÓN ---
API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")

if not API_KEY:
    print("ADVERTENCIA: No se detectó GOOGLE_MAPS_API_KEY.")

try:
    if API_KEY:
        gmaps = googlemaps.Client(key=API_KEY)
except ValueError as e:
    print(f"Error iniciando Google Maps: {e}")

ALMACEN_COORD = "25.7617,-80.1918" 

# --- BASE DE DATOS (CORREGIDA) ---
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
        print(f"❌ Error crítico inicializando base de datos: {e}")

# ¡ESTA ES LA SOLUCIÓN! 
# Ejecutamos esto INMEDIATAMENTE al cargar el código, no al final.
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
        
        # Si no está en DB, pedimos a Google
        if not API_KEY: return None
        geocode_result = gmaps.geocode(direccion)
        if geocode_result:
            loc = geocode_result[0]['geometry']['location']
            latlng_str = f"{loc['lat']},{loc['lng']}"
            
            # Guardamos en caché
            c.execute("INSERT OR REPLACE INTO direcciones VALUES (?, ?)", (direccion_clean, latlng_str))
            conn.commit()
            conn.close()
            return latlng_str
    except Exception as e:
        print(f"Error geocodificando {direccion}: {e}")
        try: conn.close()
        except: pass
    return None

# --- LÓGICA VRP ---
def crear_modelo_datos(direcciones_texto, num_vans, base_address_text=None):
    datos = {}
    coord_almacen = ALMACEN_COORD
    if base_address_text:
        coord_buscada = obtener_coordenadas_inteligentes(base_address_text)
        if coord_buscada: coord_almacen = coord_buscada

    puntos = [coord_almacen]
    direcciones_validas = []
    
    for dir_txt in direcciones_texto:
        coord = obtener_coordenadas_inteligentes(dir_txt)
        if coord:
            puntos.append(coord)
            direcciones_validas.append(dir_txt)
    
    if len(puntos) <= 1: return None

    try:
        if not API_KEY: return None
        matriz_respuesta = gmaps.distance_matrix(origins=puntos, destinations=puntos, mode="driving")
    except Exception as e:
        print(f"Error API Google Matrix: {e}")
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
    
    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC

    solution = routing.SolveWithParameters(search_parameters)
    rutas_finales = {}
    
    if solution:
        time_dimension = routing.GetDimensionOrDie('Tiempo')
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
            
            if ruta:
                base_coord = datos['base_coord']
                base_url = "https://www.google.com/maps/dir/?api=1"
                # Usamos el formato universal de Google Maps
                stops_str = "&waypoints=" + "|".join([p["coord"] for p in ruta])
                full_link = f"{base_url}&origin={base_coord}&destination={base_coord}{stops_str}"
                
                finish_index = routing.End(vehicle_id)
                tiempo_total = solution.Min(time_dimension.CumulVar(finish_index))
                rutas_finales[f"Van {vehicle_id + 1}"] = {
                    "paradas": ruta,
                    "duracion_estimada": tiempo_total / 60,
                    "link": full_link
                }
    return rutas_finales

# --- RUTAS ---

@app.route('/')
def serve_frontend():
    return send_from_directory(basedir, 'index.html')

@app.route('/config')
def get_config():
    return jsonify({"apiKey": API_KEY})

@app.route('/optimizar', methods=['POST'])
def optimizar():
    if not API_KEY: return jsonify({"error": "Error: Falta Configurar la API KEY en Render"}), 500
    data = request.json
    if not data or 'direcciones' not in data: return jsonify({"error": "Faltan datos"}), 400
    dwell_time = int(data.get('dwell_time', 10))
    modelo = crear_modelo_datos(data['direcciones'], data.get('num_vans', 1), data.get('base_address'))
    if not modelo: return jsonify({"error": "No se encontraron direcciones válidas"}), 400
    resultado = resolver_vrp(modelo, dwell_time)
    return jsonify(resultado)

@app.route('/recalcular', methods=['POST'])
def recalcular_ruta():
    if not API_KEY: return jsonify({"error": "Error: Falta Configurar la API KEY en Render"}), 500
    data = request.json
    paradas = data.get('paradas', [])
    base = data.get('base_address')
    dwell_time = int(data.get('dwell_time', 10))
    
    if not base: return jsonify({"error": "Falta direccion base"}), 400
    if not paradas: return jsonify({"duracion_estimada": 0, "link": ""})

    base_coord = obtener_coordenadas_inteligentes(base)
    coords_paradas = []
    for p in paradas:
        dir_txt = p['direccion'] if isinstance(p, dict) else p
        c = obtener_coordenadas_inteligentes(dir_txt)
        if c: coords_paradas.append(c)
        
    if not base_coord: return jsonify({"error": "Error al localizar base"}), 400

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
        duracion = rows[idx_origen]['elements'][idx_destino]['duration']['value']
        tiempo_total_segundos += duracion
        
    tiempo_total_segundos += (len(coords_paradas) * dwell_time * 60)
    
    # URL Universal de Google Maps para mejor compatibilidad móvil
    base_url = "https://www.google.com/maps/dir/?api=1"
    stops_str = "&waypoints=" + "|".join(coords_paradas)
    full_link = f"{base_url}&origin={base_coord}&destination={base_coord}{stops_str}"
    
    return jsonify({ "duracion_estimada": tiempo_total_segundos / 60, "link": full_link })

if __name__ == '__main__':
    # Esto solo corre en tu PC, no en Render
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
