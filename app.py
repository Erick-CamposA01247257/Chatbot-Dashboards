from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import anthropic
import psycopg2
import os
import hashlib
import secrets
import csv
import io
import re
import smtplib
from html import escape as html_escape
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import date, datetime, timedelta
from twilio.rest import Client as TwilioClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()

limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response

DASHBOARD_USER = os.environ.get("DASHBOARD_USER") or ""
GMAIL_USER = os.environ.get("GMAIL_USER") or ""
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD") or ""
BACKUP_EMAIL = os.environ.get("BACKUP_EMAIL") or GMAIL_USER
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD") or ""
ASSISTANT_USER = os.environ.get("ASSISTANT_USER") or ""
ASSISTANT_PASSWORD = os.environ.get("ASSISTANT_PASSWORD") or ""
SESSION_SECRET = os.environ.get("SESSION_SECRET") or ""
DOCTOR_PHONE = os.environ.get("DOCTOR_PHONE") or ""

DURACIONES = {
    "limpieza": 30, "limpieza dental": 30,
    "revisión": 30, "revision": 30,
    "revisión general": 30, "revisión y diagnóstico general": 30,
    "blanqueamiento": 60, "blanqueamiento dental": 60,
    "resinas": 60, "resina": 60,
    "restauraciones": 60, "resinas y restauraciones": 60,
    "extracción": 45, "extraccion": 45, "extracciones": 45,
    "ortodoncia": 60, "brackets": 60, "ortodoncia y brackets": 60,
    "implante": 90, "implantes": 90, "implantes dentales": 90,
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

def get_conn():
    return psycopg2.connect(os.environ.get("DATABASE_URL"))

def init_db():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS citas (
            id SERIAL PRIMARY KEY,
            nombre TEXT NOT NULL,
            servicio TEXT NOT NULL,
            fecha TEXT NOT NULL,
            hora TEXT NOT NULL,
            duracion INTEGER NOT NULL,
            fecha_registro TEXT NOT NULL,
            telefono TEXT NOT NULL DEFAULT '',
            confirmada BOOLEAN NOT NULL DEFAULT FALSE,
            cancelacion_token TEXT
        )
    """)
    cursor.execute("ALTER TABLE citas ADD COLUMN IF NOT EXISTS telefono TEXT NOT NULL DEFAULT ''")
    cursor.execute("ALTER TABLE citas ADD COLUMN IF NOT EXISTS confirmada BOOLEAN NOT NULL DEFAULT FALSE")
    cursor.execute("ALTER TABLE citas ADD COLUMN IF NOT EXISTS cancelacion_token TEXT")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS config (
            clave TEXT PRIMARY KEY,
            valor TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bloqueos (
            id SERIAL PRIMARY KEY,
            fecha TEXT NOT NULL,
            hora_inicio TEXT,
            hora_fin TEXT,
            motivo TEXT DEFAULT ''
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS auditoria (
            id SERIAL PRIMARY KEY,
            usuario TEXT NOT NULL,
            rol TEXT NOT NULL,
            accion TEXT NOT NULL,
            detalle TEXT NOT NULL,
            fecha_hora TEXT NOT NULL
        )
    """)
    cursor.execute("ALTER TABLE citas ADD COLUMN IF NOT EXISTS no_show BOOLEAN NOT NULL DEFAULT FALSE")
    conn.commit()
    conn.close()

def get_auto_confirmar() -> bool:
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT valor FROM config WHERE clave = 'auto_confirmar'")
        row = cursor.fetchone()
        conn.close()
        return row is not None and row[0] == "true"
    except Exception:
        return False

def guardar_cita(nombre: str, servicio: str, fecha: str, hora: str, duracion: int, telefono: str, confirmada: bool = False):
    token = secrets.token_urlsafe(16)
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO citas (nombre, servicio, fecha, hora, duracion, fecha_registro, telefono, confirmada, cancelacion_token)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (nombre, servicio, fecha, hora, duracion, str(date.today()), telefono, confirmada, token))
    conn.commit()
    conn.close()

def obtener_citas():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, nombre, servicio, fecha, hora, duracion, fecha_registro, telefono, confirmada, no_show FROM citas ORDER BY fecha, hora")
    filas = cursor.fetchall()
    # Teléfonos con al menos un no-show previo
    cursor.execute("SELECT DISTINCT telefono FROM citas WHERE no_show = TRUE AND telefono != ''")
    telefonos_irregulares = {r[0] for r in cursor.fetchall()}
    conn.close()
    return [
        {
            "id": f[0], "nombre": f[1], "servicio": f[2], "fecha": f[3], "hora": f[4],
            "duracion": f[5], "fecha_registro": f[6], "telefono": f[7],
            "confirmada": f[8], "no_show": f[9],
            "irregular": f[7] in telefonos_irregulares and f[7] != ""
        }
        for f in filas
    ]

def obtener_citas_por_fecha(fecha: str):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT hora, duracion FROM citas WHERE fecha = %s", (fecha,))
    filas = cursor.fetchall()
    conn.close()
    return [{"hora": f[0], "duracion": f[1]} for f in filas]

def get_duracion(servicio: str) -> int:
    servicio_lower = servicio.lower().strip()
    for key, val in DURACIONES.items():
        if key in servicio_lower or servicio_lower in key:
            return val
    return DURACION_DEFAULT

def obtener_bloqueos_fecha(fecha: str):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, hora_inicio, hora_fin, motivo FROM bloqueos WHERE fecha = %s", (fecha,))
    filas = cursor.fetchall()
    conn.close()
    return [{"id": f[0], "hora_inicio": f[1], "hora_fin": f[2], "motivo": f[3]} for f in filas]

def calcular_slots(fecha: str, duracion: int):
    citas_del_dia = obtener_citas_por_fecha(fecha)
    bloqueos_dia = obtener_bloqueos_fecha(fecha)

    # Día completo bloqueado
    if any(b["hora_inicio"] is None for b in bloqueos_dia):
        return []

    ocupados = []
    for c in citas_del_dia:
        inicio = datetime.strptime(f"{fecha} {c['hora']}", "%Y-%m-%d %H:%M")
        fin = inicio + timedelta(minutes=c["duracion"])
        ocupados.append((inicio, fin))

    for b in bloqueos_dia:
        if b["hora_inicio"] and b["hora_fin"]:
            inicio = datetime.strptime(f"{fecha} {b['hora_inicio']}", "%Y-%m-%d %H:%M")
            fin = datetime.strptime(f"{fecha} {b['hora_fin']}", "%Y-%m-%d %H:%M")
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

# ============================================
# PROCESO — Auth determinístico
# ============================================

def get_twilio_client():
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    api_key = os.environ.get("TWILIO_API_KEY")
    api_secret = os.environ.get("TWILIO_API_SECRET")
    from_number = os.environ.get("TWILIO_WHATSAPP_FROM")
    if not all([account_sid, api_key, api_secret, from_number]):
        return None, None
    return TwilioClient(api_key, api_secret, account_sid), from_number

def enviar_whatsapp(telefono: str, mensaje: str):
    try:
        cliente, from_number = get_twilio_client()
        if not cliente or not telefono:
            return
        cliente.messages.create(
            from_=from_number,
            to=f"whatsapp:+521{telefono}",
            body=mensaje
        )
    except Exception as e:
        print(f"[TWILIO ERROR] {e}")

def enviar_whatsapp_confirmacion(telefono: str, nombre: str, servicio: str, fecha: str, hora: str, token: str):
    fecha_leg = datetime.strptime(fecha, "%Y-%m-%d").strftime("%d/%m/%Y")
    hora_leg = datetime.strptime(hora, "%H:%M").strftime("%I:%M %p").lstrip("0")
    base_url = os.environ.get("BASE_URL", "https://mosadent.up.railway.app")
    enviar_whatsapp(telefono, (
        f"✅ Hola {nombre}, su cita en MOSADENT ha sido *confirmada*.\n\n"
        f"📅 Fecha: {fecha_leg}\n"
        f"⏰ Hora: {hora_leg}\n"
        f"🦷 Servicio: {servicio}\n\n"
        f"Le esperamos en Paseo de las Américas 2213, Guadalupe N.L.\n"
        f"Cualquier duda llámenos al 81 1679 8832.\n\n"
        f"¿Necesita cancelar o reagendar? Entre aquí:\n{base_url}/cancelar/{token}"
    ))

def notificar_doctora(nombre: str, servicio: str, fecha: str, hora: str):
    if not DOCTOR_PHONE:
        return
    fecha_leg = datetime.strptime(fecha, "%Y-%m-%d").strftime("%d/%m/%Y")
    hora_leg = datetime.strptime(hora, "%H:%M").strftime("%I:%M %p").lstrip("0")
    enviar_whatsapp(DOCTOR_PHONE, (
        f"📅 Nueva cita agendada\n\n"
        f"👤 Paciente: {nombre}\n"
        f"🦷 Servicio: {servicio}\n"
        f"📆 Fecha: {fecha_leg}\n"
        f"⏰ Hora: {hora_leg}"
    ))

def enviar_recordatorios():
    ahora_mexico = datetime.now() - timedelta(hours=6)
    manana = (ahora_mexico + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT nombre, servicio, fecha, hora, telefono, cancelacion_token
            FROM citas WHERE fecha = %s AND confirmada = TRUE AND telefono != ''
        """, (manana,))
        citas = cursor.fetchall()
        conn.close()
    except Exception:
        return
    base_url = os.environ.get("BASE_URL", "https://mosadent.up.railway.app")
    for cita in citas:
        nombre, servicio, fecha, hora, telefono, token = cita
        fecha_leg = datetime.strptime(fecha, "%Y-%m-%d").strftime("%d/%m/%Y")
        hora_leg = datetime.strptime(hora, "%H:%M").strftime("%I:%M %p").lstrip("0")
        enviar_whatsapp(telefono, (
            f"⏰ Recordatorio MOSADENT\n\n"
            f"Hola {nombre}, le recordamos que mañana tiene una cita:\n\n"
            f"📅 Fecha: {fecha_leg}\n"
            f"⏰ Hora: {hora_leg}\n"
            f"🦷 Servicio: {servicio}\n\n"
            f"Le esperamos en Paseo de las Américas 2213, Guadalupe N.L.\n\n"
            f"¿Necesita cancelar o reagendar?\n{base_url}/cancelar/{token}"
        ))

def enviar_recordatorio_irregulares():
    """Corre a las 7 AM México — manda recordatorio extra a pacientes irregulares con cita hoy."""
    ahora_mexico = datetime.now() - timedelta(hours=6)
    hoy = ahora_mexico.strftime("%Y-%m-%d")
    try:
        conn = get_conn()
        cursor = conn.cursor()
        # Pacientes con cita hoy, confirmada, con teléfono, y que tienen historial de no-show
        cursor.execute("""
            SELECT c.nombre, c.servicio, c.fecha, c.hora, c.telefono
            FROM citas c
            WHERE c.fecha = %s AND c.confirmada = TRUE AND c.telefono != '' AND c.no_show = FALSE
              AND EXISTS (
                SELECT 1 FROM citas prev
                WHERE prev.telefono = c.telefono AND prev.no_show = TRUE
              )
        """, (hoy,))
        citas = cursor.fetchall()
        conn.close()
    except Exception:
        return
    for cita in citas:
        nombre, servicio, fecha, hora, telefono = cita
        hora_leg = datetime.strptime(hora, "%H:%M").strftime("%I:%M %p").lstrip("0")
        enviar_whatsapp(telefono, (
            f"⚠️ Recordatorio MOSADENT\n\n"
            f"Hola {nombre}, le recordamos que *hoy* tiene una cita:\n\n"
            f"⏰ Hora: {hora_leg}\n"
            f"🦷 Servicio: {servicio}\n\n"
            f"Por favor confírme su asistencia. Si no puede asistir, avísenos con anticipación.\n"
            f"Llámenos al 81 1679 8832."
        ))

def enviar_backup_semanal():
    if not GMAIL_USER or not GMAIL_APP_PASSWORD or not BACKUP_EMAIL:
        return
    try:
        conn = get_conn()
        cursor = conn.cursor()
        archivos = {}

        # Exportar cada tabla como CSV en memoria
        tablas = {
            "citas": "SELECT id, nombre, servicio, fecha, hora, duracion, fecha_registro, telefono, confirmada, no_show FROM citas ORDER BY fecha, hora",
            "bloqueos": "SELECT id, fecha, hora_inicio, hora_fin, motivo FROM bloqueos ORDER BY fecha",
            "auditoria": "SELECT id, usuario, rol, accion, detalle, fecha_hora FROM auditoria ORDER BY id DESC",
        }
        for nombre_tabla, query in tablas.items():
            cursor.execute(query)
            filas = cursor.fetchall()
            headers = [desc[0] for desc in cursor.description]
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(headers)
            writer.writerows(filas)
            archivos[f"{nombre_tabla}.csv"] = buf.getvalue()

        conn.close()

        fecha_hoy = (datetime.now() - timedelta(hours=6)).strftime("%d/%m/%Y")
        msg = MIMEMultipart()
        msg["From"] = GMAIL_USER
        msg["To"] = BACKUP_EMAIL
        msg["Subject"] = f"Backup semanal MOSADENT — {fecha_hoy}"
        msg.attach(MIMEText(
            f"Backup automático semanal del sistema MOSADENT.\n"
            f"Fecha: {fecha_hoy}\n\n"
            f"Se adjuntan los archivos CSV con citas, bloqueos y auditoría.",
            "plain"
        ))

        for nombre_archivo, contenido in archivos.items():
            parte = MIMEBase("application", "octet-stream")
            parte.set_payload(contenido.encode("utf-8"))
            encoders.encode_base64(parte)
            parte.add_header("Content-Disposition", f"attachment; filename={nombre_archivo}")
            msg.attach(parte)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as servidor:
            servidor.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            servidor.sendmail(GMAIL_USER, BACKUP_EMAIL, msg.as_string())

    except Exception as e:
        print(f"[Backup Error] {repr(e)}")

def get_token_para(usuario: str, password: str) -> str:
    data = f"{usuario}:{password}:{SESSION_SECRET}"
    return hashlib.sha256(data.encode()).hexdigest()

def verificar_sesion(request: Request) -> str | None:
    """Retorna 'doctor', 'asistente', o None si no hay sesión válida."""
    token = request.cookies.get("session_token")
    if not token:
        return None
    if DASHBOARD_USER and token == get_token_para(DASHBOARD_USER, DASHBOARD_PASSWORD):
        return "doctor"
    if ASSISTANT_USER and token == get_token_para(ASSISTANT_USER, ASSISTANT_PASSWORD):
        return "asistente"
    return None

def registrar_auditoria(usuario: str, rol: str, accion: str, detalle: str):
    try:
        ahora = (datetime.now() - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO auditoria (usuario, rol, accion, detalle, fecha_hora) VALUES (%s, %s, %s, %s, %s)",
            (usuario, rol, accion, detalle, ahora)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

init_db()

scheduler = AsyncIOScheduler()
scheduler.add_job(enviar_recordatorios, "cron", hour=14, minute=0)                        # 8 AM México
scheduler.add_job(enviar_recordatorio_irregulares, "cron", hour=13, minute=0)              # 7 AM México
scheduler.add_job(enviar_backup_semanal, "cron", day_of_week="mon", hour=14, minute=30)   # Lunes 8:30 AM México
scheduler.start()

# ============================================
# PROCESO — Modelos
# ============================================

class Mensaje(BaseModel):
    texto: str = Field(..., max_length=1000)
    session_id: str = Field("default", max_length=100)

class CitaRequest(BaseModel):
    nombre: str = Field(..., max_length=100, min_length=2)
    servicio: str = Field(..., max_length=100)
    fecha: str = Field(..., max_length=10)
    hora: str = Field(..., max_length=5)
    telefono: str = Field("", max_length=10)

    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    def __init__(self, **data):
        # Sanitizar newlines de campos de texto libre
        if "nombre" in data:
            data["nombre"] = re.sub(r"[\r\n\t]", " ", data["nombre"]).strip()
        if "servicio" in data:
            data["servicio"] = re.sub(r"[\r\n\t]", " ", data["servicio"]).strip()
        super().__init__(**data)
        # Validar formato fecha YYYY-MM-DD
        try:
            datetime.strptime(self.fecha, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Formato de fecha inválido (use YYYY-MM-DD)")
        # Validar formato hora HH:MM
        if not re.match(r'^([01][0-9]|2[0-3]):[0-5][0-9]$', self.hora):
            raise ValueError("Formato de hora inválido (use HH:MM)")

class LoginRequest(BaseModel):
    usuario: str = Field(..., max_length=50)
    password: str = Field(..., max_length=100)

# ============================================
# SALIDA — Endpoints públicos
# ============================================

@app.head("/")
async def health_head():
    return Response()


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    with open("templates/login.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/login")
@limiter.limit("10/minute")
async def api_login(request: Request, datos: LoginRequest):
    if datos.usuario == DASHBOARD_USER and datos.password == DASHBOARD_PASSWORD:
        token = get_token_para(DASHBOARD_USER, DASHBOARD_PASSWORD)
        resp = JSONResponse(content={"status": "ok", "rol": "doctor"})
        resp.set_cookie(key="session_token", value=token, httponly=True, max_age=43200, samesite="strict", secure=True)
        return resp
    if ASSISTANT_USER and datos.usuario == ASSISTANT_USER and datos.password == ASSISTANT_PASSWORD:
        token = get_token_para(ASSISTANT_USER, ASSISTANT_PASSWORD)
        resp = JSONResponse(content={"status": "ok", "rol": "asistente"})
        resp.set_cookie(key="session_token", value=token, httponly=True, max_age=43200, samesite="strict", secure=True)
        return resp
    return JSONResponse(status_code=401, content={"error": "Credenciales incorrectas"})

@app.get("/api/logout")
async def logout(response: Response):
    response.delete_cookie("session_token")
    return RedirectResponse(url="/login")

# ============================================
# SALIDA — Endpoints protegidos
# ============================================

@app.get("/citas", response_class=HTMLResponse)
async def panel_citas(request: Request):
    if not verificar_sesion(request):
        return RedirectResponse(url="/login")
    with open("templates/dashboard.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/dashboard", response_class=HTMLResponse)
async def panel_dashboard(request: Request):
    if not verificar_sesion(request):
        return RedirectResponse(url="/login")
    with open("templates/dashboard.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/me")
async def api_me(request: Request):
    rol = verificar_sesion(request)
    if not rol:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    usuario = DASHBOARD_USER if rol == "doctor" else ASSISTANT_USER
    return JSONResponse(content={"rol": rol, "usuario": usuario})

@app.get("/api/citas")
async def api_citas(request: Request):
    if not verificar_sesion(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    citas = obtener_citas()
    return JSONResponse(content={"total": len(citas), "citas": citas})

@app.get("/api/slots")
async def api_slots(fecha: str, servicio: str):
    duracion = get_duracion(servicio)
    slots = calcular_slots(fecha, duracion)
    return JSONResponse(content={"fecha": fecha, "servicio": servicio, "duracion": duracion, "slots": slots})

@app.post("/api/agendar")
@limiter.limit("10/hour")
async def api_agendar(request: Request, cita: CitaRequest):
    duracion = get_duracion(cita.servicio)
    slots = calcular_slots(cita.fecha, duracion)
    if cita.hora not in slots:
        return JSONResponse(
            status_code=409,
            content={"error": "Ese horario ya no está disponible", "slots_disponibles": slots}
        )
    auto = get_auto_confirmar()
    guardar_cita(cita.nombre, cita.servicio, cita.fecha, cita.hora, duracion, cita.telefono, confirmada=auto)
    notificar_doctora(cita.nombre, cita.servicio, cita.fecha, cita.hora)
    return JSONResponse(content={"status": "ok", "mensaje": "Cita registrada correctamente"})

@app.post("/api/agendar-dashboard")
async def api_agendar_dashboard(cita: CitaRequest, request: Request):
    rol = verificar_sesion(request)
    if not rol:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    duracion = get_duracion(cita.servicio)
    slots = calcular_slots(cita.fecha, duracion)
    if cita.hora not in slots:
        return JSONResponse(
            status_code=409,
            content={"error": "Ese horario ya no está disponible", "slots_disponibles": slots}
        )
    guardar_cita(cita.nombre, cita.servicio, cita.fecha, cita.hora, duracion, cita.telefono, confirmada=True)
    notificar_doctora(cita.nombre, cita.servicio, cita.fecha, cita.hora)
    usuario = DASHBOARD_USER if rol == "doctor" else ASSISTANT_USER
    registrar_auditoria(usuario, rol, "crear_cita", f"{cita.nombre} — {cita.servicio} — {cita.fecha} {cita.hora}")
    return JSONResponse(content={"status": "ok", "mensaje": "Cita registrada correctamente"})

@app.patch("/api/citas/{cita_id}/confirmar")
async def confirmar_cita(cita_id: int, request: Request):
    rol = verificar_sesion(request)
    if not rol:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE citas SET confirmada = TRUE WHERE id = %s", (cita_id,))
    conn.commit()
    cursor.execute("SELECT nombre, servicio, fecha, hora, telefono, cancelacion_token FROM citas WHERE id = %s", (cita_id,))
    cita = cursor.fetchone()
    conn.close()
    if cita:
        if cita[4]:
            enviar_whatsapp_confirmacion(cita[4], cita[0], cita[1], cita[2], cita[3], cita[5] or "")
        usuario = DASHBOARD_USER if rol == "doctor" else ASSISTANT_USER
        registrar_auditoria(usuario, rol, "confirmar_cita", f"{cita[0]} — {cita[1]} — {cita[2]} {cita[3]}")
    return JSONResponse(content={"status": "ok"})

@app.delete("/api/citas/{cita_id}")
async def eliminar_cita(cita_id: int, request: Request):
    rol = verificar_sesion(request)
    if not rol:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT nombre, servicio, fecha, hora FROM citas WHERE id = %s", (cita_id,))
    cita = cursor.fetchone()
    cursor.execute("DELETE FROM citas WHERE id = %s", (cita_id,))
    conn.commit()
    conn.close()
    if cita:
        usuario = DASHBOARD_USER if rol == "doctor" else ASSISTANT_USER
        registrar_auditoria(usuario, rol, "eliminar_cita", f"{cita[0]} — {cita[1]} — {cita[2]} {cita[3]}")
    return JSONResponse(content={"status": "ok"})

@app.get("/api/config")
async def get_config(request: Request):
    if not verificar_sesion(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    return JSONResponse(content={"auto_confirmar": get_auto_confirmar()})

@app.post("/api/config")
async def set_config(request: Request):
    rol = verificar_sesion(request)
    if not rol:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    body = await request.json()
    valor = "true" if body.get("auto_confirmar") else "false"
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO config (clave, valor) VALUES ('auto_confirmar', %s) ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor",
        (valor,)
    )
    conn.commit()
    conn.close()
    return JSONResponse(content={"status": "ok"})

@app.post("/api/citas/confirmar-lote")
async def confirmar_lote(request: Request):
    rol = verificar_sesion(request)
    if not rol:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    body = await request.json()
    ids = body.get("ids", [])
    if not ids:
        return JSONResponse(content={"status": "ok"})
    conn = get_conn()
    cursor = conn.cursor()
    usuario = DASHBOARD_USER if rol == "doctor" else ASSISTANT_USER
    for cita_id in ids:
        cursor.execute("UPDATE citas SET confirmada = TRUE WHERE id = %s", (cita_id,))
        cursor.execute("SELECT nombre, servicio, fecha, hora, telefono, cancelacion_token FROM citas WHERE id = %s", (cita_id,))
        cita = cursor.fetchone()
        if cita:
            if cita[4]:
                enviar_whatsapp_confirmacion(cita[4], cita[0], cita[1], cita[2], cita[3], cita[5] or "")
            registrar_auditoria(usuario, rol, "confirmar_cita", f"{cita[0]} — {cita[1]} — {cita[2]} {cita[3]}")
    conn.commit()
    conn.close()
    return JSONResponse(content={"status": "ok"})

@app.patch("/api/citas/{cita_id}/no-show")
async def marcar_no_show(cita_id: int, request: Request):
    rol = verificar_sesion(request)
    if not rol:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE citas SET no_show = TRUE WHERE id = %s", (cita_id,))
    conn.commit()
    cursor.execute("SELECT nombre, servicio, fecha, hora FROM citas WHERE id = %s", (cita_id,))
    cita = cursor.fetchone()
    conn.close()
    if cita:
        usuario = DASHBOARD_USER if rol == "doctor" else ASSISTANT_USER
        registrar_auditoria(usuario, rol, "no_show", f"{cita[0]} — {cita[1]} — {cita[2]} {cita[3]}")
    return JSONResponse(content={"status": "ok"})

@app.get("/api/auditoria")
async def get_auditoria(request: Request):
    rol = verificar_sesion(request)
    if rol != "doctor":
        return JSONResponse(status_code=403, content={"error": "Solo el doctor puede ver la auditoría"})
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, usuario, rol, accion, detalle, fecha_hora FROM auditoria ORDER BY id DESC LIMIT 100")
    filas = cursor.fetchall()
    conn.close()
    acciones_label = {
        "crear_cita": "Creó cita",
        "confirmar_cita": "Confirmó cita",
        "eliminar_cita": "Eliminó cita",
        "no_show": "No se presentó",
        "crear_bloqueo": "Bloqueó horario",
        "eliminar_bloqueo": "Quitó bloqueo",
    }
    return JSONResponse(content={"registros": [
        {
            "id": f[0], "usuario": f[1], "rol": f[2],
            "accion": acciones_label.get(f[3], f[3]),
            "detalle": f[4], "fecha_hora": f[5]
        } for f in filas
    ]})

@app.get("/api/bloqueos")
async def get_bloqueos(request: Request):
    if not verificar_sesion(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, fecha, hora_inicio, hora_fin, motivo FROM bloqueos ORDER BY fecha, hora_inicio")
    filas = cursor.fetchall()
    conn.close()
    return JSONResponse(content={"bloqueos": [
        {"id": f[0], "fecha": f[1], "hora_inicio": f[2], "hora_fin": f[3], "motivo": f[4]} for f in filas
    ]})

@app.post("/api/bloqueos")
async def crear_bloqueo(request: Request):
    rol = verificar_sesion(request)
    if not rol:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    body = await request.json()
    fecha = body.get("fecha", "")
    todo_el_dia = body.get("todo_el_dia", False)
    hora_inicio = None if todo_el_dia else body.get("hora_inicio")
    hora_fin = None if todo_el_dia else body.get("hora_fin")
    motivo = body.get("motivo", "")
    if not fecha:
        return JSONResponse(status_code=400, content={"error": "Fecha requerida"})
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO bloqueos (fecha, hora_inicio, hora_fin, motivo) VALUES (%s, %s, %s, %s) RETURNING id",
        (fecha, hora_inicio, hora_fin, motivo)
    )
    new_id = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    usuario = DASHBOARD_USER if rol == "doctor" else ASSISTANT_USER
    detalle = f"{fecha} — {'Todo el día' if todo_el_dia else f'{hora_inicio}–{hora_fin}'}" + (f" ({motivo})" if motivo else "")
    registrar_auditoria(usuario, rol, "crear_bloqueo", detalle)
    return JSONResponse(content={"status": "ok", "id": new_id})

@app.delete("/api/bloqueos/{bloqueo_id}")
async def eliminar_bloqueo(bloqueo_id: int, request: Request):
    rol = verificar_sesion(request)
    if not rol:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT fecha, hora_inicio, hora_fin, motivo FROM bloqueos WHERE id = %s", (bloqueo_id,))
    bloqueo = cursor.fetchone()
    cursor.execute("DELETE FROM bloqueos WHERE id = %s", (bloqueo_id,))
    conn.commit()
    conn.close()
    if bloqueo:
        usuario = DASHBOARD_USER if rol == "doctor" else ASSISTANT_USER
        detalle = f"{bloqueo[0]} — {'Todo el día' if not bloqueo[1] else f'{bloqueo[1]}–{bloqueo[2]}'}" + (f" ({bloqueo[3]})" if bloqueo[3] else "")
        registrar_auditoria(usuario, rol, "eliminar_bloqueo", detalle)
    return JSONResponse(content={"status": "ok"})

@app.get("/cancelar/{token}", response_class=HTMLResponse)
async def cancelar_page(token: str):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT nombre, servicio, fecha, hora FROM citas WHERE cancelacion_token = %s", (token,))
    cita = cursor.fetchone()
    conn.close()
    if not cita:
        return HTMLResponse("<h2 style='font-family:sans-serif;padding:40px'>Esta cita ya fue cancelada o el link no es válido.</h2>")
    nombre, servicio, fecha, hora = cita
    fecha_leg = datetime.strptime(fecha, "%Y-%m-%d").strftime("%d/%m/%Y")
    hora_leg = datetime.strptime(hora, "%H:%M").strftime("%I:%M %p").lstrip("0")
    with open("templates/cancelar.html", "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("{{nombre}}", html_escape(nombre)).replace("{{servicio}}", html_escape(servicio))
    html = html.replace("{{fecha}}", html_escape(fecha_leg)).replace("{{hora}}", html_escape(hora_leg))
    html = html.replace("{{token}}", html_escape(token))
    return html

@app.post("/api/cancelar/{token}")
@limiter.limit("5/minute")
async def api_cancelar(token: str, request: Request):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM citas WHERE cancelacion_token = %s", (token,))
    cita = cursor.fetchone()
    if not cita:
        conn.close()
        return JSONResponse(status_code=404, content={"error": "Cita no encontrada"})
    cursor.execute("DELETE FROM citas WHERE cancelacion_token = %s", (token,))
    conn.commit()
    conn.close()
    return JSONResponse(content={"status": "ok"})

@app.post("/chat")
@limiter.limit("30/minute")
async def chat(request: Request, mensaje: Mensaje):
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