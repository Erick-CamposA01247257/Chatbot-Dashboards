from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import anthropic
import sqlite3
import os
from datetime import date, datetime, timedelta

load_dotenv()


app = FastAPI()
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


# ============================================
# INICIO — Configuración
# ============================================

DURACIONES = {
    "limpieza": 30,
    "limpieza dental": 30,
    "revisión": 30,
    "revision": 30,
    "revisión general": 30,
    "revisión y diagnóstico general": 30,
    "blanqueamiento": 60,
    "blanqueamiento dental": 60,
    "resinas": 60,
    "resina": 60,
    "restauraciones": 60,
    "resinas y restauraciones": 60,
    "extracción": 45,
    "extraccion": 45,
    "extracciones": 45,
    "ortodoncia": 60,
    "brackets": 60,
    "ortodoncia y brackets": 60,
    "implante": 90,
    "implantes": 90,
    "implantes dentales": 90,
}

DURACION_DEFAULT = 60
HORA_INICIO = 10
HORA_FIN = 19

SISTEMA_MOSADENT = """Eres el asistente virtual de MOSADENT, un consultorio dental en Guadalupe, Nuevo León.

INFORMACIÓN DEL CONSULTORIO:
- Nombre: MOSADENT
- Dirección: P.° de las Américas 2213, Contry La Silla 9o Sector, Guadalupe, N.L.
- Teléfono: 81 1679 8832
- Horario: Lunes a Domingo de 10:00 AM a 7:00 PM
- Calificación: 5 estrellas en Google

SERVICIOS QUE OFRECEMOS:
- Limpieza dental
- Blanqueamiento dental
- Ortodoncia y brackets
- Implantes dentales
- Extracciones
- Resinas y restauraciones
- Revisión y diagnóstico general

PRECIOS:
- Los precios varían según el caso de cada paciente
- Ofrecemos consulta de diagnóstico para evaluar tu situación
- Para conocer el costo exacto de tu tratamiento, agenda una cita

CÓMO AGENDAR CITA:
- El paciente usa el calendario de la interfaz para elegir fecha y hora disponible
- Cuando el sistema confirme la cita con los datos completos, confirma amablemente
- Si el paciente escribe que quiere agendar, dile que use el calendario que aparece en pantalla

INSTRUCCIONES DE COMPORTAMIENTO:
- Responde siempre en español, de forma amable y profesional
- Si preguntan por precios exactos, di que varían según el caso y ofrece agendar una cita de diagnóstico gratuita
- Si hay una emergencia dental, indica que llamen directamente al 81 1679 8832
- Nunca inventes información que no esté en este documento
- Tu propósito es agendar citas, siempre deberías referir a agendar una cita
- No deberías dar información extra sobre consultas, no estás preparado para dar información médica, solo agenda citas
- Tu lenguaje debe ser siempre formal y profesional, nunca coloquial, informal ni lenguaje de redes sociales
- Mantén respuestas cortas y claras — esto es WhatsApp, no un ensayo"""

conversaciones = {}

# ============================================
# PROCESO — Base de datos SQLite
# ============================================

def init_db():
    conn = sqlite3.connect("citas.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS citas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            servicio TEXT NOT NULL,
            fecha TEXT NOT NULL,
            hora TEXT NOT NULL,
            duracion INTEGER NOT NULL,
            fecha_registro TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def guardar_cita(nombre: str, servicio: str, fecha: str, hora: str, duracion: int):
    conn = sqlite3.connect("citas.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO citas (nombre, servicio, fecha, hora, duracion, fecha_registro)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (nombre, servicio, fecha, hora, duracion, str(date.today())))
    conn.commit()
    conn.close()

def obtener_citas():
    conn = sqlite3.connect("citas.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, nombre, servicio, fecha, hora, duracion, fecha_registro FROM citas ORDER BY fecha, hora")
    filas = cursor.fetchall()
    conn.close()
    return [
        {"id": f[0], "nombre": f[1], "servicio": f[2], "fecha": f[3], "hora": f[4], "duracion": f[5], "fecha_registro": f[6]}
        for f in filas
    ]

def obtener_citas_por_fecha(fecha: str):
    conn = sqlite3.connect("citas.db")
    cursor = conn.cursor()
    cursor.execute("SELECT hora, duracion FROM citas WHERE fecha = ?", (fecha,))
    filas = cursor.fetchall()
    conn.close()
    return [{"hora": f[0], "duracion": f[1]} for f in filas]

def get_duracion(servicio: str) -> int:
    servicio_lower = servicio.lower().strip()
    for key, val in DURACIONES.items():
        if key in servicio_lower or servicio_lower in key:
            return val
    return DURACION_DEFAULT
def calcular_slots(fecha: str, duracion: int):
    citas_del_dia = obtener_citas_por_fecha(fecha)
    ocupados = []
    for c in citas_del_dia:
        inicio = datetime.strptime(f"{fecha} {c['hora']}", "%Y-%m-%d %H:%M")
        fin = inicio + timedelta(minutes=c["duracion"])
        ocupados.append((inicio, fin))

    ahora_mexico = datetime.now() - timedelta(hours=6)
    hoy = ahora_mexico.strftime("%Y-%m-%d")
    es_hoy = fecha == hoy
    minimo = ahora_mexico + timedelta(hours=2) if es_hoy else None

    slots = []
    cursor_time = datetime.strptime(f"{fecha} {HORA_INICIO}:00", "%Y-%m-%d %H:%M")
    fin_dia = datetime.strptime(f"{fecha} {HORA_FIN}:00", "%Y-%m-%d %H:%M")

    while cursor_time + timedelta(minutes=duracion) <= fin_dia:
        fin_slot = cursor_time + timedelta(minutes=duracion)

        if es_hoy and cursor_time <= ahora_mexico:
            cursor_time += timedelta(minutes=30)
            continue

        if es_hoy and minimo and cursor_time < minimo:
            cursor_time += timedelta(minutes=30)
            continue

        disponible = all(
            fin_slot <= inicio or cursor_time >= fin
            for inicio, fin in ocupados
        )
        if disponible:
            slots.append(cursor_time.strftime("%H:%M"))
        cursor_time += timedelta(minutes=30)

    return slots

init_db()

# ============================================
# PROCESO — Modelos
# ============================================

class Mensaje(BaseModel):
    texto: str
    session_id: str = "default"

class CitaRequest(BaseModel):
    nombre: str
    servicio: str
    fecha: str
    hora: str

# ============================================
# SALIDA — Endpoints
# ============================================

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/citas", response_class=HTMLResponse)
async def panel_citas():
    with open("templates/dashboard.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/dashboard", response_class=HTMLResponse)
async def panel_dashboard():
    with open("templates/dashboard.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/citas")
async def api_citas():
    citas = obtener_citas()
    return JSONResponse(content={"total": len(citas), "citas": citas})

@app.get("/api/slots")
async def api_slots(fecha: str, servicio: str):
    duracion = get_duracion(servicio)
    slots = calcular_slots(fecha, duracion)
    return JSONResponse(content={"fecha": fecha, "servicio": servicio, "duracion": duracion, "slots": slots})

@app.post("/api/agendar")
async def api_agendar(cita: CitaRequest):
    duracion = get_duracion(cita.servicio)
    slots = calcular_slots(cita.fecha, duracion)
    if cita.hora not in slots:
        return JSONResponse(
            status_code=409,
            content={"error": "Ese horario ya no está disponible", "slots_disponibles": slots}
        )
    guardar_cita(cita.nombre, cita.servicio, cita.fecha, cita.hora, duracion)
    return JSONResponse(content={"status": "ok", "mensaje": "Cita registrada correctamente"})

@app.post("/chat")
async def chat(mensaje: Mensaje):
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    
    if mensaje.session_id not in conversaciones:
        conversaciones[mensaje.session_id] = []

    conversaciones[mensaje.session_id].append({
        "role": "user",
        "content": mensaje.texto
    })

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=SISTEMA_MOSADENT,
        messages=conversaciones[mensaje.session_id]
    )

    respuesta = response.content[0].text

    conversaciones[mensaje.session_id].append({
        "role": "assistant",
        "content": respuesta
    })

    return {"respuesta": respuesta}

@app.post("/limpiar")
async def limpiar(session: dict = {"session_id": "default"}):
    session_id = session.get("session_id", "default")
    if session_id in conversaciones:
        del conversaciones[session_id]
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)