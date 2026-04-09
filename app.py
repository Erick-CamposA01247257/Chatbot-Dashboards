from fastapi import FastAPI, Request, Response, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import anthropic
import psycopg2
import requests
import os
import uuid
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
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()

limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Sesiones en memoria: {token: {username, rol, doctor_id}}
sessions = {}

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    return response

DASHBOARD_USER     = os.environ.get("DASHBOARD_USER") or ""
GMAIL_USER         = os.environ.get("GMAIL_USER") or ""
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD") or ""
BACKUP_EMAIL       = os.environ.get("BACKUP_EMAIL") or GMAIL_USER
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD") or ""
ASSISTANT_USER     = os.environ.get("ASSISTANT_USER") or ""
ASSISTANT_PASSWORD = os.environ.get("ASSISTANT_PASSWORD") or ""
OWNER_USER         = os.environ.get("OWNER_USER") or ""
OWNER_PASSWORD     = os.environ.get("OWNER_PASSWORD") or ""
SESSION_SECRET     = os.environ.get("SESSION_SECRET") or ""
DOCTOR_PHONE       = os.environ.get("DOCTOR_PHONE") or ""

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

# Mapeo servicio → username del doctor asignado
SERVICIOS_DOCTOR = {
    "limpieza": "dr.garcia", "limpieza dental": "dr.garcia",
    "revisión": "dr.garcia", "revision": "dr.garcia",
    "revisión general": "dr.garcia", "revisión y diagnóstico general": "dr.garcia",
    "blanqueamiento": "dr.garcia", "blanqueamiento dental": "dr.garcia",
    "ortodoncia": "dra.martinez", "brackets": "dra.martinez", "ortodoncia y brackets": "dra.martinez",
    "extracción": "dr.lopez", "extraccion": "dr.lopez", "extracciones": "dr.lopez",
    "implante": "dr.lopez", "implantes": "dr.lopez", "implantes dentales": "dr.lopez",
    "resinas": "dra.rodriguez", "resina": "dra.rodriguez",
    "restauraciones": "dra.rodriguez", "resinas y restauraciones": "dra.rodriguez",
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
# PROCESO — Base de datos
# ============================================

def get_conn():
    return psycopg2.connect(os.environ.get("DATABASE_URL"))

def hash_password(password: str) -> str:
    return hashlib.sha256(f"{password}:{SESSION_SECRET}".encode()).hexdigest()

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
            cancelacion_token TEXT,
            no_show BOOLEAN NOT NULL DEFAULT FALSE,
            doctor_id INTEGER
        )
    """)
    cursor.execute("ALTER TABLE citas ADD COLUMN IF NOT EXISTS telefono TEXT NOT NULL DEFAULT ''")
    cursor.execute("ALTER TABLE citas ADD COLUMN IF NOT EXISTS confirmada BOOLEAN NOT NULL DEFAULT FALSE")
    cursor.execute("ALTER TABLE citas ADD COLUMN IF NOT EXISTS cancelacion_token TEXT")
    cursor.execute("ALTER TABLE citas ADD COLUMN IF NOT EXISTS no_show BOOLEAN NOT NULL DEFAULT FALSE")
    cursor.execute("ALTER TABLE citas ADD COLUMN IF NOT EXISTS doctor_id INTEGER")

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
            motivo TEXT DEFAULT '',
            doctor_id INTEGER
        )
    """)
    cursor.execute("ALTER TABLE bloqueos ADD COLUMN IF NOT EXISTS doctor_id INTEGER")
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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS doctores (
            id SERIAL PRIMARY KEY,
            nombre TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#3B82F6',
            hora_inicio INTEGER NOT NULL DEFAULT 10,
            hora_fin INTEGER NOT NULL DEFAULT 19,
            whatsapp TEXT DEFAULT '',
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    """)

    # Seed doctores de prueba — contraseñas desde env vars con fallback
    doctores_seed = [
        ("Dr. García",      "#3B82F6", 10, 15, "dr.garcia",      hash_password(os.environ.get("PASS_DR_GARCIA",     "garcia123"))),
        ("Dra. Martínez",   "#8B5CF6", 11, 19, "dra.martinez",   hash_password(os.environ.get("PASS_DRA_MARTINEZ",  "martinez123"))),
        ("Dr. López",       "#F59E0B", 10, 17, "dr.lopez",       hash_password(os.environ.get("PASS_DR_LOPEZ",      "lopez123"))),
        ("Dra. Rodríguez",  "#10B981", 12, 19, "dra.rodriguez",  hash_password(os.environ.get("PASS_DRA_RODRIGUEZ", "rodriguez123"))),
    ]
    for doc in doctores_seed:
        cursor.execute("""
            INSERT INTO doctores (nombre, color, hora_inicio, hora_fin, username, password_hash)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (username) DO UPDATE SET password_hash = EXCLUDED.password_hash
        """, doc)

    conn.commit()
    conn.close()

def obtener_doctores():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, nombre, color, hora_inicio, hora_fin, whatsapp, username FROM doctores ORDER BY id")
    filas = cursor.fetchall()
    conn.close()
    return [
        {"id": f[0], "nombre": f[1], "color": f[2],
         "hora_inicio": f[3], "hora_fin": f[4],
         "whatsapp": f[5], "username": f[6]}
        for f in filas
    ]

def asignar_doctor(servicio: str) -> int | None:
    """Retorna doctor_id según el servicio. None si no hay mapeo."""
    username = SERVICIOS_DOCTOR.get(servicio.lower().strip())
    if not username:
        return None
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM doctores WHERE username = %s", (username,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

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

def guardar_cita(nombre: str, servicio: str, fecha: str, hora: str, duracion: int,
                 telefono: str, confirmada: bool = False, doctor_id: int = None) -> str:
    token = secrets.token_urlsafe(16)
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO citas (nombre, servicio, fecha, hora, duracion, fecha_registro, telefono, confirmada, cancelacion_token, doctor_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (nombre, servicio, fecha, hora, duracion, str(date.today()), telefono, confirmada, token, doctor_id))
    conn.commit()
    conn.close()
    return token

def obtener_citas(doctor_id: int = None):
    conn = get_conn()
    cursor = conn.cursor()
    if doctor_id:
        cursor.execute("""
            SELECT c.id, c.nombre, c.servicio, c.fecha, c.hora, c.duracion, c.fecha_registro,
                   c.telefono, c.confirmada, c.no_show, c.doctor_id,
                   d.nombre, d.color
            FROM citas c
            LEFT JOIN doctores d ON c.doctor_id = d.id
            WHERE c.doctor_id = %s
            ORDER BY c.fecha, c.hora
        """, (doctor_id,))
    else:
        cursor.execute("""
            SELECT c.id, c.nombre, c.servicio, c.fecha, c.hora, c.duracion, c.fecha_registro,
                   c.telefono, c.confirmada, c.no_show, c.doctor_id,
                   d.nombre, d.color
            FROM citas c
            LEFT JOIN doctores d ON c.doctor_id = d.id
            ORDER BY c.fecha, c.hora
        """)
    filas = cursor.fetchall()
    cursor.execute("SELECT DISTINCT telefono FROM citas WHERE no_show = TRUE AND telefono != ''")
    telefonos_irregulares = {r[0] for r in cursor.fetchall()}
    conn.close()
    return [
        {
            "id": f[0], "nombre": f[1], "servicio": f[2], "fecha": f[3], "hora": f[4],
            "duracion": f[5], "fecha_registro": f[6], "telefono": f[7],
            "confirmada": f[8], "no_show": f[9],
            "doctor_id": f[10], "doctor_nombre": f[11] or "", "doctor_color": f[12] or "#94A3B8",
            "irregular": f[7] in telefonos_irregulares and f[7] != ""
        }
        for f in filas
    ]

def obtener_citas_por_fecha(fecha: str, doctor_id: int = None):
    conn = get_conn()
    cursor = conn.cursor()
    if doctor_id:
        cursor.execute("SELECT hora, duracion FROM citas WHERE fecha = %s AND doctor_id = %s", (fecha, doctor_id))
    else:
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

def obtener_bloqueos_fecha(fecha: str, doctor_id: int = None):
    conn = get_conn()
    cursor = conn.cursor()
    if doctor_id:
        # Bloqueos globales (doctor_id IS NULL) + bloqueos del doctor específico
        cursor.execute(
            "SELECT id, hora_inicio, hora_fin, motivo, doctor_id FROM bloqueos WHERE fecha = %s AND (doctor_id IS NULL OR doctor_id = %s)",
            (fecha, doctor_id)
        )
    else:
        cursor.execute("SELECT id, hora_inicio, hora_fin, motivo, doctor_id FROM bloqueos WHERE fecha = %s", (fecha,))
    filas = cursor.fetchall()
    conn.close()
    return [{"id": f[0], "hora_inicio": f[1], "hora_fin": f[2], "motivo": f[3], "doctor_id": f[4]} for f in filas]

def calcular_slots(fecha: str, duracion: int, doctor_id: int = None,
                   hora_inicio: int = None, hora_fin: int = None):
    """Calcula slots disponibles. Si doctor_id se provee, usa horario del doctor."""
    h_inicio = hora_inicio if hora_inicio is not None else HORA_INICIO
    h_fin    = hora_fin    if hora_fin    is not None else HORA_FIN

    # Si tenemos doctor_id pero no horas, buscar horas del doctor
    if doctor_id and hora_inicio is None:
        try:
            conn = get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT hora_inicio, hora_fin FROM doctores WHERE id = %s", (doctor_id,))
            row = cursor.fetchone()
            conn.close()
            if row:
                h_inicio, h_fin = row[0], row[1]
        except Exception:
            pass

    citas_del_dia = obtener_citas_por_fecha(fecha, doctor_id)
    bloqueos_dia  = obtener_bloqueos_fecha(fecha, doctor_id)

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
    cursor_time = datetime.strptime(f"{fecha} {h_inicio:02d}:00", "%Y-%m-%d %H:%M")
    fin_dia     = datetime.strptime(f"{fecha} {h_fin:02d}:00",    "%Y-%m-%d %H:%M")

    while cursor_time + timedelta(minutes=duracion) <= fin_dia:
        fin_slot = cursor_time + timedelta(minutes=duracion)
        if es_hoy and cursor_time <= ahora_mexico:
            cursor_time += timedelta(minutes=30)
            continue
        if es_hoy and minimo and cursor_time < minimo:
            cursor_time += timedelta(minutes=30)
            continue
        disponible = all(fin_slot <= inicio or cursor_time >= fin for inicio, fin in ocupados)
        if disponible:
            slots.append(cursor_time.strftime("%H:%M"))
        cursor_time += timedelta(minutes=30)

    return slots

# ============================================
# PROCESO — Auth con sessions en memoria
# ============================================

def verificar_sesion(request: Request) -> dict | None:
    """Retorna {username, rol, doctor_id} o None."""
    token = request.cookies.get("session_token")
    if not token:
        return None
    return sessions.get(token)

def verificar_sesion_activa(request: Request, roles_permitidos: list = None) -> dict | None:
    """Verifica sesión y opcionalmente que el rol esté permitido.
    roles_permitidos=None significa cualquier rol excepto 'owner'.
    roles_permitidos=['admin','doctor','asistente'] = cualquier rol que no sea owner.
    """
    sesion = verificar_sesion(request)
    if not sesion:
        return None
    if roles_permitidos and sesion["rol"] not in roles_permitidos:
        return None
    return sesion

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

# ============================================
# PROCESO — WhatsApp
# ============================================

def enviar_whatsapp(telefono: str, mensaje: str):
    try:
        token = os.environ.get("WHAPI_TOKEN")
        if not token or not telefono:
            return
        requests.post(
            "https://gate.whapi.cloud/messages/text",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"to": f"521{telefono}@s.whatsapp.net", "body": mensaje},
            timeout=30
        )
    except Exception as e:
        print(f"[WHAPI ERROR] {e}")

def enviar_whatsapp_confirmacion(telefono: str, nombre: str, servicio: str, fecha: str, hora: str, token: str):
    fecha_leg = datetime.strptime(fecha, "%Y-%m-%d").strftime("%d/%m/%Y")
    hora_leg  = datetime.strptime(hora, "%H:%M").strftime("%I:%M %p").lstrip("0")
    base_url  = os.environ.get("BASE_URL", "https://mosadent.up.railway.app")
    enviar_whatsapp(telefono, (
        f"✅ Hola {nombre}, su cita en Dentales ha sido *confirmada*.\n\n"
        f"📅 Fecha: {fecha_leg}\n"
        f"⏰ Hora: {hora_leg}\n"
        f"🦷 Servicio: {servicio}\n\n"
        f"¿Necesita cancelar o reagendar? Entre aquí:\n{base_url}/cancelar/{token}"
    ))

def notificar_doctora(nombre: str, servicio: str, fecha: str, hora: str):
    if not DOCTOR_PHONE:
        return
    fecha_leg = datetime.strptime(fecha, "%Y-%m-%d").strftime("%d/%m/%Y")
    hora_leg  = datetime.strptime(hora, "%H:%M").strftime("%I:%M %p").lstrip("0")
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
        hora_leg  = datetime.strptime(hora, "%H:%M").strftime("%I:%M %p").lstrip("0")
        enviar_whatsapp(telefono, (
            f"⏰ Recordatorio Dentales\n\n"
            f"Hola {nombre}, le recordamos que mañana tiene una cita:\n\n"
            f"📅 Fecha: {fecha_leg}\n"
            f"⏰ Hora: {hora_leg}\n"
            f"🦷 Servicio: {servicio}\n\n"
            f"¿Necesita cancelar o reagendar?\n{base_url}/cancelar/{token}"
        ))

def enviar_recordatorio_irregulares():
    ahora_mexico = datetime.now() - timedelta(hours=6)
    hoy = ahora_mexico.strftime("%Y-%m-%d")
    try:
        conn = get_conn()
        cursor = conn.cursor()
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
            f"⚠️ Recordatorio Dentales\n\n"
            f"Hola {nombre}, le recordamos que *hoy* tiene una cita:\n\n"
            f"⏰ Hora: {hora_leg}\n"
            f"🦷 Servicio: {servicio}\n\n"
            f"Por favor confirme su asistencia. Si no puede asistir, avísenos con anticipación."
        ))

def enviar_backup_semanal():
    if not GMAIL_USER or not GMAIL_APP_PASSWORD or not BACKUP_EMAIL:
        return
    try:
        conn = get_conn()
        cursor = conn.cursor()
        archivos = {}
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
        msg["Subject"] = f"Backup semanal Dentales — {fecha_hoy}"
        msg.attach(MIMEText(
            f"Backup automático semanal del sistema Dentales.\nFecha: {fecha_hoy}\n\n"
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

init_db()

scheduler = AsyncIOScheduler()
scheduler.add_job(enviar_recordatorios,         "cron", hour=14, minute=0)
scheduler.add_job(enviar_recordatorio_irregulares, "cron", hour=13, minute=0)
scheduler.add_job(enviar_backup_semanal,         "cron", day_of_week="mon", hour=14, minute=30)
scheduler.start()

# ============================================
# PROCESO — Modelos
# ============================================

class Mensaje(BaseModel):
    texto: str = Field(..., min_length=1, max_length=1000)
    session_id: str = Field("default", max_length=100)

    def __init__(self, **data):
        if "session_id" in data:
            data["session_id"] = re.sub(r"[^a-zA-Z0-9_-]", "", str(data["session_id"]))[:100] or "default"
        super().__init__(**data)

class CitaRequest(BaseModel):
    nombre: str = Field(..., max_length=100, min_length=2)
    servicio: str = Field(..., max_length=100)
    fecha: str = Field(..., max_length=10)
    hora: str = Field(..., max_length=5)
    telefono: str = Field("", max_length=10)

    def __init__(self, **data):
        if "nombre" in data:
            data["nombre"] = re.sub(r"[\r\n\t]", " ", data["nombre"]).strip()
        if "servicio" in data:
            data["servicio"] = re.sub(r"[\r\n\t]", " ", data["servicio"]).strip()
        if "telefono" in data and data["telefono"]:
            data["telefono"] = re.sub(r"\D", "", str(data["telefono"]))
        super().__init__(**data)
        try:
            fecha_dt = datetime.strptime(self.fecha, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("Formato de fecha inválido (use YYYY-MM-DD)")
        if fecha_dt < date.today():
            raise ValueError("No se pueden agendar citas en fechas pasadas")
        if not re.match(r'^([01][0-9]|2[0-3]):[0-5][0-9]$', self.hora):
            raise ValueError("Formato de hora inválido (use HH:MM)")
        hora_int = int(self.hora.split(":")[0])
        if hora_int < 10 or hora_int >= 19:
            raise ValueError("Hora fuera del horario de atención (10:00-19:00)")
        if self.telefono and not re.match(r'^\d{10}$', self.telefono):
            raise ValueError("Teléfono debe tener exactamente 10 dígitos")
        if self.servicio.lower() not in DURACIONES:
            raise ValueError("Servicio no válido")

class LoginRequest(BaseModel):
    usuario: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1, max_length=100)

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
    session_token = str(uuid.uuid4())

    # Admin
    if datos.usuario == DASHBOARD_USER and datos.password == DASHBOARD_PASSWORD:
        sessions[session_token] = {"username": DASHBOARD_USER, "rol": "admin", "doctor_id": None}
        resp = JSONResponse(content={"status": "ok", "rol": "admin"})
        resp.set_cookie(key="session_token", value=session_token, httponly=True, max_age=43200, samesite="strict", secure=True)
        return resp

    # Owner (solo exportar)
    if OWNER_USER and datos.usuario == OWNER_USER and datos.password == OWNER_PASSWORD:
        sessions[session_token] = {"username": OWNER_USER, "rol": "owner", "doctor_id": None}
        resp = JSONResponse(content={"status": "ok", "rol": "owner"})
        resp.set_cookie(key="session_token", value=session_token, httponly=True, max_age=43200, samesite="strict", secure=True)
        return resp

    # Asistente (env var)
    if ASSISTANT_USER and datos.usuario == ASSISTANT_USER and datos.password == ASSISTANT_PASSWORD:
        sessions[session_token] = {"username": ASSISTANT_USER, "rol": "asistente", "doctor_id": None}
        resp = JSONResponse(content={"status": "ok", "rol": "asistente"})
        resp.set_cookie(key="session_token", value=session_token, httponly=True, max_age=43200, samesite="strict", secure=True)
        return resp

    # Doctor (DB)
    try:
        pw_hash = hash_password(datos.password)
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, nombre, username FROM doctores WHERE username = %s AND password_hash = %s",
            (datos.usuario, pw_hash)
        )
        doctor = cursor.fetchone()
        conn.close()
        if doctor:
            sessions[session_token] = {
                "username": doctor[2], "rol": "doctor",
                "doctor_id": doctor[0], "doctor_nombre": doctor[1]
            }
            resp = JSONResponse(content={"status": "ok", "rol": "doctor"})
            resp.set_cookie(key="session_token", value=session_token, httponly=True, max_age=43200, samesite="strict", secure=True)
            return resp
    except Exception as e:
        print(f"[Login Error] {e}")

    # Registrar intento fallido
    registrar_auditoria(datos.usuario, "desconocido", "login_fallido",
                        f"IP: {request.client.host if request.client else 'desconocida'}")
    return JSONResponse(status_code=401, content={"error": "Credenciales incorrectas"})

@app.get("/api/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("session_token")
    if token and token in sessions:
        del sessions[token]
    response = RedirectResponse(url="/login")
    response.delete_cookie("session_token")
    return response

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
    sesion = verificar_sesion(request)
    if not sesion:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    return JSONResponse(content={
        "rol": sesion["rol"],
        "usuario": sesion["username"],
        "doctor_id": sesion.get("doctor_id"),
        "doctor_nombre": sesion.get("doctor_nombre", "")
    })

@app.get("/api/doctores")
async def api_doctores(request: Request):
    if not verificar_sesion(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    doctores = obtener_doctores()
    return JSONResponse(content={"doctores": doctores})

@app.get("/api/citas")
async def api_citas(request: Request):
    sesion = verificar_sesion(request)
    if not sesion:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    if sesion["rol"] == "owner":
        return JSONResponse(status_code=403, content={"error": "Acceso denegado"})
    # Admin y asistente ven todas; doctor solo las suyas
    doctor_id = sesion.get("doctor_id") if sesion["rol"] == "doctor" else None
    citas = obtener_citas(doctor_id)
    return JSONResponse(content={"total": len(citas), "citas": citas})

@app.get("/api/slots")
async def api_slots(fecha: str, servicio: str, doctor_id: int = None):
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', fecha):
        return JSONResponse(status_code=400, content={"error": "Formato de fecha inválido"})
    try:
        datetime.strptime(fecha, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Fecha inválida"})
    if len(servicio) > 100:
        return JSONResponse(status_code=400, content={"error": "Servicio inválido"})
    duracion = get_duracion(servicio)
    # Si no se especifica doctor, auto-asignar por servicio
    if doctor_id is None:
        doctor_id = asignar_doctor(servicio)
    slots = calcular_slots(fecha, duracion, doctor_id)
    return JSONResponse(content={"fecha": fecha, "servicio": servicio, "duracion": duracion,
                                  "doctor_id": doctor_id, "slots": slots})

@app.post("/api/agendar")
@limiter.limit("10/hour")
async def api_agendar(request: Request, cita: CitaRequest, background_tasks: BackgroundTasks):
    doctor_id = asignar_doctor(cita.servicio)
    duracion  = get_duracion(cita.servicio)
    slots     = calcular_slots(cita.fecha, duracion, doctor_id)
    if cita.hora not in slots:
        return JSONResponse(
            status_code=409,
            content={"error": "Ese horario ya no está disponible", "slots_disponibles": slots}
        )
    auto = get_auto_confirmar()
    token = guardar_cita(cita.nombre, cita.servicio, cita.fecha, cita.hora, duracion,
                         cita.telefono, confirmada=auto, doctor_id=doctor_id)
    background_tasks.add_task(notificar_doctora, cita.nombre, cita.servicio, cita.fecha, cita.hora)
    if auto and cita.telefono:
        background_tasks.add_task(
            enviar_whatsapp_confirmacion,
            cita.telefono, cita.nombre, cita.servicio, cita.fecha, cita.hora, token
        )
    return JSONResponse(content={"status": "ok", "mensaje": "Cita registrada correctamente"})

@app.post("/api/agendar-dashboard")
async def api_agendar_dashboard(cita: CitaRequest, request: Request, background_tasks: BackgroundTasks):
    sesion = verificar_sesion(request)
    if not sesion:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    if sesion["rol"] == "owner":
        return JSONResponse(status_code=403, content={"error": "Acceso denegado"})
    doctor_id = asignar_doctor(cita.servicio)
    duracion  = get_duracion(cita.servicio)
    slots     = calcular_slots(cita.fecha, duracion, doctor_id)
    if cita.hora not in slots:
        return JSONResponse(
            status_code=409,
            content={"error": "Ese horario ya no está disponible", "slots_disponibles": slots}
        )
    token = guardar_cita(cita.nombre, cita.servicio, cita.fecha, cita.hora, duracion,
                         cita.telefono, confirmada=True, doctor_id=doctor_id)
    if cita.telefono:
        background_tasks.add_task(enviar_whatsapp_confirmacion, cita.telefono, cita.nombre,
                                   cita.servicio, cita.fecha, cita.hora, token)
    registrar_auditoria(sesion["username"], sesion["rol"], "crear_cita",
                        f"{cita.nombre} — {cita.servicio} — {cita.fecha} {cita.hora}")
    return JSONResponse(content={"status": "ok", "mensaje": "Cita registrada correctamente"})

@app.patch("/api/citas/{cita_id}/confirmar")
async def confirmar_cita(cita_id: int, request: Request, background_tasks: BackgroundTasks):
    sesion = verificar_sesion(request)
    if not sesion:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    if sesion["rol"] == "owner":
        return JSONResponse(status_code=403, content={"error": "Acceso denegado"})
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE citas SET confirmada = TRUE WHERE id = %s", (cita_id,))
    conn.commit()
    cursor.execute("SELECT nombre, servicio, fecha, hora, telefono, cancelacion_token FROM citas WHERE id = %s", (cita_id,))
    cita = cursor.fetchone()
    conn.close()
    if cita:
        if cita[4]:
            background_tasks.add_task(enviar_whatsapp_confirmacion,
                                       cita[4], cita[0], cita[1], cita[2], cita[3], cita[5] or "")
        background_tasks.add_task(registrar_auditoria, sesion["username"], sesion["rol"],
                                   "confirmar_cita", f"{cita[0]} — {cita[1]} — {cita[2]} {cita[3]}")
    return JSONResponse(content={"status": "ok"})

@app.delete("/api/citas/{cita_id}")
async def eliminar_cita(cita_id: int, request: Request):
    sesion = verificar_sesion(request)
    if not sesion:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    if sesion["rol"] == "owner":
        return JSONResponse(status_code=403, content={"error": "Acceso denegado"})
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT nombre, servicio, fecha, hora FROM citas WHERE id = %s", (cita_id,))
    cita = cursor.fetchone()
    cursor.execute("DELETE FROM citas WHERE id = %s", (cita_id,))
    conn.commit()
    conn.close()
    if cita:
        registrar_auditoria(sesion["username"], sesion["rol"], "eliminar_cita",
                            f"{cita[0]} — {cita[1]} — {cita[2]} {cita[3]}")
    return JSONResponse(content={"status": "ok"})

@app.get("/api/config")
async def get_config(request: Request):
    if not verificar_sesion(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    return JSONResponse(content={"auto_confirmar": get_auto_confirmar()})

@app.post("/api/config")
async def set_config(request: Request):
    sesion = verificar_sesion(request)
    if not sesion:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    if sesion["rol"] == "owner":
        return JSONResponse(status_code=403, content={"error": "Acceso denegado"})
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
async def confirmar_lote(request: Request, background_tasks: BackgroundTasks):
    sesion = verificar_sesion(request)
    if not sesion:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    if sesion["rol"] == "owner":
        return JSONResponse(status_code=403, content={"error": "Acceso denegado"})
    body = await request.json()
    ids = body.get("ids", [])
    if not ids:
        return JSONResponse(content={"status": "ok"})
    if not isinstance(ids, list) or len(ids) > 100:
        return JSONResponse(status_code=400, content={"error": "IDs inválidos"})
    ids = [int(i) for i in ids if isinstance(i, int) and i > 0]
    conn = get_conn()
    cursor = conn.cursor()
    for cita_id in ids:
        cursor.execute("UPDATE citas SET confirmada = TRUE WHERE id = %s", (cita_id,))
        cursor.execute("SELECT nombre, servicio, fecha, hora, telefono, cancelacion_token FROM citas WHERE id = %s", (cita_id,))
        cita = cursor.fetchone()
        if cita:
            if cita[4]:
                background_tasks.add_task(enviar_whatsapp_confirmacion,
                                           cita[4], cita[0], cita[1], cita[2], cita[3], cita[5] or "")
            registrar_auditoria(sesion["username"], sesion["rol"], "confirmar_cita",
                                f"{cita[0]} — {cita[1]} — {cita[2]} {cita[3]}")
    conn.commit()
    conn.close()
    return JSONResponse(content={"status": "ok"})

@app.patch("/api/citas/{cita_id}/no-show")
async def marcar_no_show(cita_id: int, request: Request):
    sesion = verificar_sesion(request)
    if not sesion:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    if sesion["rol"] == "owner":
        return JSONResponse(status_code=403, content={"error": "Acceso denegado"})
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE citas SET no_show = TRUE WHERE id = %s", (cita_id,))
    conn.commit()
    cursor.execute("SELECT nombre, servicio, fecha, hora FROM citas WHERE id = %s", (cita_id,))
    cita = cursor.fetchone()
    conn.close()
    if cita:
        registrar_auditoria(sesion["username"], sesion["rol"], "no_show",
                            f"{cita[0]} — {cita[1]} — {cita[2]} {cita[3]}")
    return JSONResponse(content={"status": "ok"})

@app.get("/api/exportar")
async def exportar_citas(request: Request):
    from fastapi.responses import StreamingResponse
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    sesion = verificar_sesion(request)
    if not sesion or sesion["rol"] not in ("admin", "owner"):
        return JSONResponse(status_code=403, content={"error": "Acceso denegado"})

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c.id, c.nombre, c.telefono, c.servicio, c.fecha, c.hora, c.duracion,
               c.confirmada, c.no_show, c.fecha_registro,
               COALESCE(d.nombre, '—') AS doctor
        FROM citas c
        LEFT JOIN doctores d ON c.doctor_id = d.id
        ORDER BY c.fecha DESC, c.hora DESC
    """)
    citas = cursor.fetchall()
    conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Citas"

    # Estilos
    header_fill = PatternFill("solid", fgColor="1E66B5")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin = Side(style="thin", color="D0D7E2")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center")

    headers = ["ID", "Paciente", "Teléfono", "Servicio", "Fecha", "Hora",
               "Duración (min)", "Doctor", "Estado", "Registrada"]
    ws.append(headers)

    for col_idx, _ in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_align
        cell.border = border

    # Datos
    for cita in citas:
        id_, nombre, tel, servicio, fecha, hora, dur, confirmada, no_show, fecha_reg, doctor = cita
        if no_show:
            estado = "No se presentó"
        elif confirmada:
            estado = "Confirmada"
        else:
            estado = "Pendiente"
        row = [id_, nombre, tel or "—", servicio, fecha, hora, dur, doctor, estado, fecha_reg]
        ws.append(row)
        row_idx = ws.max_row
        # Colorear estado
        estado_cell = ws.cell(row=row_idx, column=9)
        if no_show:
            estado_cell.font = Font(color="6B7280")
        elif confirmada:
            estado_cell.font = Font(color="16A34A", bold=True)
        else:
            estado_cell.font = Font(color="1E66B5")
        for col_idx in range(1, len(headers) + 1):
            ws.cell(row=row_idx, column=col_idx).border = border
            ws.cell(row=row_idx, column=col_idx).alignment = center

    # Anchos de columna
    anchos = [6, 28, 16, 30, 12, 8, 14, 20, 14, 18]
    for i, ancho in enumerate(anchos, 1):
        ws.column_dimensions[get_column_letter(i)].width = ancho
    ws.row_dimensions[1].height = 28

    # Hoja resumen
    ws2 = wb.create_sheet("Resumen")
    total = len(citas)
    confirmadas = sum(1 for c in citas if c[7])
    no_shows = sum(1 for c in citas if c[8])
    pendientes = total - confirmadas - no_shows

    resumen = [
        ("Total de citas", total),
        ("Confirmadas", confirmadas),
        ("Pendientes", pendientes),
        ("No se presentaron", no_shows),
    ]
    ws2.append(["Métrica", "Cantidad"])
    for cell in ws2[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = center
        cell.border = border
    for label, val in resumen:
        ws2.append([label, val])
    ws2.column_dimensions["A"].width = 24
    ws2.column_dimensions["B"].width = 12

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    fecha_hoy = datetime.now().strftime("%Y-%m-%d")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=citas_{fecha_hoy}.xlsx"}
    )

@app.get("/api/auditoria")
async def get_auditoria(request: Request):
    sesion = verificar_sesion(request)
    if not sesion or sesion["rol"] not in ("admin", "doctor"):
        return JSONResponse(status_code=403, content={"error": "Acceso denegado"})
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, usuario, rol, accion, detalle, fecha_hora FROM auditoria ORDER BY id DESC LIMIT 100")
    filas = cursor.fetchall()
    conn.close()
    acciones_label = {
        "crear_cita": "Creó cita", "confirmar_cita": "Confirmó cita",
        "eliminar_cita": "Eliminó cita", "no_show": "No se presentó",
        "crear_bloqueo": "Bloqueó horario", "eliminar_bloqueo": "Quitó bloqueo",
        "login_fallido": "⚠️ Login fallido",
    }
    return JSONResponse(content={"registros": [
        {"id": f[0], "usuario": f[1], "rol": f[2],
         "accion": acciones_label.get(f[3], f[3]),
         "detalle": f[4], "fecha_hora": f[5]}
        for f in filas
    ]})

@app.get("/api/bloqueos")
async def get_bloqueos(request: Request):
    if not verificar_sesion(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, fecha, hora_inicio, hora_fin, motivo, doctor_id FROM bloqueos ORDER BY fecha, hora_inicio")
    filas = cursor.fetchall()
    conn.close()
    return JSONResponse(content={"bloqueos": [
        {"id": f[0], "fecha": f[1], "hora_inicio": f[2], "hora_fin": f[3], "motivo": f[4], "doctor_id": f[5]}
        for f in filas
    ]})

@app.post("/api/bloqueos")
async def crear_bloqueo(request: Request):
    sesion = verificar_sesion(request)
    if not sesion:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    if sesion["rol"] == "owner":
        return JSONResponse(status_code=403, content={"error": "Acceso denegado"})
    body = await request.json()
    fecha = str(body.get("fecha", ""))[:10]
    todo_el_dia = bool(body.get("todo_el_dia", False))
    motivo = re.sub(r"[\r\n\t]", " ", str(body.get("motivo", "") or "")).strip()[:200]
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', fecha):
        return JSONResponse(status_code=400, content={"error": "Formato de fecha inválido"})
    try:
        datetime.strptime(fecha, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Fecha inválida"})
    hora_inicio = hora_fin = None
    if not todo_el_dia:
        hora_inicio = str(body.get("hora_inicio", "") or "")[:5]
        hora_fin    = str(body.get("hora_fin",    "") or "")[:5]
        if not re.match(r'^([01][0-9]|2[0-3]):[0-5][0-9]$', hora_inicio) or \
           not re.match(r'^([01][0-9]|2[0-3]):[0-5][0-9]$', hora_fin):
            return JSONResponse(status_code=400, content={"error": "Formato de hora inválido"})
        if hora_inicio >= hora_fin:
            return JSONResponse(status_code=400, content={"error": "hora_fin debe ser mayor que hora_inicio"})
    # doctor_id: null = todos, número = doctor específico
    bloqueo_doctor_id = body.get("doctor_id") or sesion.get("doctor_id")
    if bloqueo_doctor_id:
        try:
            bloqueo_doctor_id = int(bloqueo_doctor_id)
        except (TypeError, ValueError):
            bloqueo_doctor_id = None
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO bloqueos (fecha, hora_inicio, hora_fin, motivo, doctor_id) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (fecha, hora_inicio, hora_fin, motivo, bloqueo_doctor_id)
    )
    new_id = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    detalle = f"{fecha} — {'Todo el día' if todo_el_dia else f'{hora_inicio}–{hora_fin}'}" + (f" ({motivo})" if motivo else "")
    registrar_auditoria(sesion["username"], sesion["rol"], "crear_bloqueo", detalle)
    return JSONResponse(content={"status": "ok", "id": new_id})

@app.delete("/api/bloqueos/{bloqueo_id}")
async def eliminar_bloqueo(bloqueo_id: int, request: Request):
    sesion = verificar_sesion(request)
    if not sesion:
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    if sesion["rol"] == "owner":
        return JSONResponse(status_code=403, content={"error": "Acceso denegado"})
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT fecha, hora_inicio, hora_fin, motivo FROM bloqueos WHERE id = %s", (bloqueo_id,))
    bloqueo = cursor.fetchone()
    cursor.execute("DELETE FROM bloqueos WHERE id = %s", (bloqueo_id,))
    conn.commit()
    conn.close()
    if bloqueo:
        detalle = f"{bloqueo[0]} — {'Todo el día' if not bloqueo[1] else f'{bloqueo[1]}–{bloqueo[2]}'}" + (f" ({bloqueo[3]})" if bloqueo[3] else "")
        registrar_auditoria(sesion["username"], sesion["rol"], "eliminar_bloqueo", detalle)
    return JSONResponse(content={"status": "ok"})

@app.get("/cancelar/{token}", response_class=HTMLResponse)
async def cancelar_page(token: str):
    if not re.match(r'^[a-zA-Z0-9_-]{10,100}$', token):
        return HTMLResponse("<h2 style='font-family:sans-serif;padding:40px'>Link no válido.</h2>")
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT nombre, servicio, fecha, hora FROM citas WHERE cancelacion_token = %s", (token,))
    cita = cursor.fetchone()
    conn.close()
    if not cita:
        return HTMLResponse("<h2 style='font-family:sans-serif;padding:40px'>Esta cita ya fue cancelada o el link no es válido.</h2>")
    nombre, servicio, fecha, hora = cita
    fecha_leg = datetime.strptime(fecha, "%Y-%m-%d").strftime("%d/%m/%Y")
    hora_leg  = datetime.strptime(hora, "%H:%M").strftime("%I:%M %p").lstrip("0")
    with open("templates/cancelar.html", "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("{{nombre}}", html_escape(nombre)).replace("{{servicio}}", html_escape(servicio))
    html = html.replace("{{fecha}}", html_escape(fecha_leg)).replace("{{hora}}", html_escape(hora_leg))
    html = html.replace("{{token}}", html_escape(token))
    return html

@app.post("/api/cancelar/{token}")
@limiter.limit("5/minute")
async def api_cancelar(token: str, request: Request):
    if not re.match(r'^[a-zA-Z0-9_-]{10,100}$', token):
        return JSONResponse(status_code=400, content={"error": "Token inválido"})
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
    # Límite de 1000 sesiones en memoria para prevenir DoS
    if mensaje.session_id not in conversaciones:
        if len(conversaciones) >= 1000:
            oldest = next(iter(conversaciones))
            del conversaciones[oldest]
        conversaciones[mensaje.session_id] = []
    conversaciones[mensaje.session_id].append({"role": "user", "content": mensaje.texto})
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=SISTEMA_MOSADENT,
        messages=conversaciones[mensaje.session_id]
    )
    respuesta = response.content[0].text
    conversaciones[mensaje.session_id].append({"role": "assistant", "content": respuesta})
    return {"respuesta": respuesta}

@app.post("/limpiar")
@limiter.limit("10/minute")
async def limpiar(request: Request, session: dict = {"session_id": "default"}):
    session_id = session.get("session_id", "default")
    if session_id in conversaciones:
        del conversaciones[session_id]
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
