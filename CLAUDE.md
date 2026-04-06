# CLAUDE.md — consultorio-chatbot

## Contexto del proyecto

Sistema de agendamiento de citas para consultorios dentales. Tiene dos interfaces:
- **Chat del paciente** (`/`) — chatbot estilo WhatsApp con wizard de agendado de 4 pasos
- **Dashboard de la doctora** (`/dashboard`) — panel con agenda del día y calendario mensual, protegido por login

El primer cliente objetivo es **MOSADENT** — consultorio dental en Guadalupe, N.L. El sistema prompt y la UI están configurados con información real de ese consultorio.

---

## Stack

- **Backend:** Python 3.12 + FastAPI + Uvicorn
- **Base de datos:** PostgreSQL (Railway) — migrado de SQLite
- **AI:** Claude API (Anthropic) — modelo `claude-sonnet-4-20250514`
- **Frontend:** HTML + CSS + JavaScript vanilla (sin frameworks)
- **Deploy:** Railway
- **Tipografías:** Cormorant Garamond (serif, títulos) + DM Sans (sans-serif, cuerpo)
- **WhatsApp:** Twilio
- **Monitoreo:** UptimeRobot (ping a `HEAD /`)

---

## Estructura de archivos

```
consultorio-chatbot/
├── app.py                  ← backend principal FastAPI
├── requirements.txt
├── Procfile                ← Railway: uvicorn app:app --host 0.0.0.0 --port $PORT
├── railway.json
├── .gitignore
└── templates/
    ├── index.html          ← chat del paciente
    ├── dashboard.html      ← panel de la doctora
    ├── login.html          ← login para acceder al dashboard
    └── cancelar.html       ← página pública para cancelar cita por token
```

---

## Cómo correr el proyecto localmente

```bash
# Activar venv compartido
source ~/Documents/ai-projects/venv/bin/activate

# Correr servidor
uvicorn app:app --reload

# Puerto: http://localhost:8000
```

Variables de entorno en `~/Documents/ai-projects/.env`:
```
ANTHROPIC_API_KEY=sk-ant-...
DASHBOARD_USER=...
DASHBOARD_PASSWORD=...
SESSION_SECRET=...
DATABASE_URL=postgresql://...
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
TWILIO_WHATSAPP_DOCTORA=whatsapp:+52...
EMAIL_USER=...
EMAIL_PASSWORD=...
EMAIL_DEST=...
```

---

## Endpoints

### Públicos
| Método | Ruta | Descripción |
|--------|------|-------------|
| HEAD | `/` | Health check para UptimeRobot |
| GET | `/` | Chat del paciente (index.html) |
| GET | `/login` | Página de login (login.html) |
| POST | `/api/login` | Autenticación (rate limit: 10/min) |
| GET | `/api/logout` | Cerrar sesión |
| POST | `/chat` | Mensaje al chatbot (Claude API, rate limit: 30/min) |
| POST | `/limpiar` | Limpiar historial de conversación |
| GET | `/api/slots` | Horarios disponibles por fecha y servicio |
| POST | `/api/agendar` | Guardar cita desde chat (rate limit: 10/hour) |
| GET | `/cancelar/{token}` | Página de cancelación (cancelar.html) |
| POST | `/api/cancelar/{token}` | Cancelar cita por token único |

### Protegidos (requieren sesión)
| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/dashboard` | Panel de la doctora |
| GET | `/citas` | Alias de /dashboard |
| GET | `/api/citas` | JSON con todas las citas (con estado irregular y no_show) |
| POST | `/api/agendar-dashboard` | Crear cita desde dashboard (siempre confirmada) |
| PATCH | `/api/citas/{id}/confirmar` | Confirmar cita + enviar WhatsApp al paciente |
| DELETE | `/api/citas/{id}` | Eliminar cita |
| PATCH | `/api/citas/{id}/no-show` | Marcar paciente como no presentado |
| POST | `/api/citas/confirmar-lote` | Confirmar múltiples citas de una vez |
| GET | `/api/bloqueos` | Listar bloqueos de horario |
| POST | `/api/bloqueos` | Crear bloqueo (día completo o rango de horas) |
| DELETE | `/api/bloqueos/{id}` | Eliminar bloqueo |
| GET | `/api/config` | Leer configuración (auto_confirmar) |
| POST | `/api/config` | Modificar configuración |
| GET | `/api/auditoria` | Últimas 100 acciones (solo rol doctor) |
| GET | `/api/me` | Retorna rol y usuario actual |

---

## Base de datos PostgreSQL

### Tabla `citas`
```sql
CREATE TABLE citas (
    id SERIAL PRIMARY KEY,
    nombre TEXT NOT NULL,
    servicio TEXT NOT NULL,
    fecha TEXT NOT NULL,              -- formato: YYYY-MM-DD
    hora TEXT NOT NULL,               -- formato: HH:MM
    duracion INTEGER NOT NULL,        -- en minutos
    fecha_registro TEXT NOT NULL,
    telefono TEXT DEFAULT '',
    confirmada BOOLEAN DEFAULT FALSE,
    cancelacion_token TEXT UNIQUE,    -- token para cancelación pública
    no_show BOOLEAN DEFAULT FALSE     -- paciente no se presentó
)
```

### Tabla `config`
```sql
CREATE TABLE config (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
)
-- Almacena: auto_confirmar (true/false)
```

### Tabla `bloqueos`
```sql
CREATE TABLE bloqueos (
    id SERIAL PRIMARY KEY,
    fecha TEXT NOT NULL,
    hora_inicio TEXT,   -- NULL = día completo bloqueado
    hora_fin TEXT,
    motivo TEXT DEFAULT ''
)
```

### Tabla `auditoria`
```sql
CREATE TABLE auditoria (
    id SERIAL PRIMARY KEY,
    usuario TEXT NOT NULL,
    rol TEXT NOT NULL,
    accion TEXT NOT NULL,   -- crear_cita, confirmar_cita, eliminar_cita, no_show, crear_bloqueo, eliminar_bloqueo
    detalle TEXT DEFAULT '',
    fecha_hora TEXT NOT NULL
)
```

### Tabla `usuarios`
```sql
CREATE TABLE usuarios (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    rol TEXT NOT NULL   -- 'doctor' o 'asistente'
)
```

---

## Auth

Multi-usuario con roles. Sesión basada en cookie HttpOnly.

- **doctor** — acceso completo, incluyendo auditoría
- **asistente** — acceso al dashboard, sin auditoría

El token de sesión es determinístico (SHA256) para no requerir tabla de sesiones:
```python
def get_token_valido():
    data = f"{DASHBOARD_USER}:{DASHBOARD_PASSWORD}:{SESSION_SECRET}"
    return hashlib.sha256(data.encode()).hexdigest()
```

---

## Lógica de slots

`calcular_slots(fecha, duracion)` — genera horarios disponibles:

- Horario del consultorio: **10:00 AM a 7:00 PM** (HORA_INICIO=10, HORA_FIN=19)
- Slots cada 30 minutos
- Si la fecha es **hoy**, filtra slots pasados y aplica regla de **2 horas de anticipación mínima**
- Timezone México UTC-6: se usa `datetime.now() - timedelta(hours=6)` en Railway (corre en UTC)
- Slots ocupados: comparando rangos inicio/fin de citas existentes
- Respeta bloqueos: si el día está completamente bloqueado o el slot cae en un rango bloqueado, se excluye

### Duraciones por servicio
```python
DURACIONES = {
    "limpieza dental": 30,
    "revisión general / diagnóstico": 30,
    "blanqueamiento dental": 60,
    "resinas y restauraciones": 60,
    "extracciones": 45,
    "ortodoncia y brackets": 60,
    "implantes dentales": 90,
}
```

---

## Flujo del chat (index.html)

El agendado es un **wizard de 4 pasos** dentro del chat:

1. **Fecha** — calendario mensual, días pasados deshabilitados
2. **Servicio** — grid 2 columnas con nombre y duración
3. **Hora** — grid 3 columnas con slots disponibles (cargados desde `/api/slots`)
4. **Confirmación** — resumen + inputs de nombre y teléfono + botón confirmar

Al confirmar llama a `/api/agendar`. Si el slot ya está ocupado devuelve error 409 y regresa al paso 1.

El chat también responde preguntas vía Claude API. Después de cada respuesta del bot aparece automáticamente un botón "Agendar cita".

---

## Dashboard (dashboard.html)

Layout de dos columnas en desktop, tabs en móvil (≤640px).

### Panel izquierdo — Agenda (Timeline)
- Timeline 10:00–19:00 con tarjetas de cita por hora
- Estados de cita: **pendiente** (azul), **confirmada** (verde), **irregular** (naranja), **no-show** (gris)
- Bloqueos en rojo con botón "Quitar"
- Clic en tarjeta → abre modal de detalle

### Panel derecho — Calendario
- Mini calendario mensual con navegación ‹ ›
- Días con citas: punto azul. Días con bloqueos: punto rojo
- Grid visual tipo Google Calendar: bloques proporcionales a duración (`ALTURA_HORA = 72px`)

### Modal de detalle de cita
- Datos: nombre, teléfono, servicio, fecha, hora, duración, estado
- Alerta si el paciente es irregular (historial de no-shows)
- Historial de citas anteriores del paciente por teléfono
- Botones contextuales: **Confirmar** / **No se presentó** / **Eliminar**

### Otras funcionalidades del header
- **+ Nueva cita** — formulario para crear cita desde el dashboard (siempre confirmada)
- **Bloquear** — modal para bloquear día completo o rango de horas con motivo
- **Auto** — toggle auto-confirmar (afecta citas futuras desde el chat)
- **Auditoría** — tabla de últimas 100 acciones (solo doctor)
- **Confirmar lote** — barra flotante al seleccionar múltiples citas con checkboxes

Auto-refresh cada 30 segundos con `setInterval(cargarCitas, 30000)`.

---

## Integraciones

### WhatsApp (Twilio)
- **Al agendar** (desde chat): notificación a la doctora con datos de la nueva cita
- **Al confirmar** (desde dashboard): WhatsApp al paciente con link de cancelación
- **Recordatorio 24h antes** (CRON 8 AM México = 14:00 UTC): a citas confirmadas del día siguiente
- **Recordatorio extra** (CRON 7 AM México = 13:00 UTC): a pacientes con historial de no-shows

### Email (Gmail SMTP)
- **Backup semanal** (CRON Lunes 8:30 AM México = 14:30 UTC): exporta CSV de citas, bloqueos y auditoría

### UptimeRobot
- Monitor HTTP apuntando a `mosadent.up.railway.app`
- Usa `HEAD /` para el ping (devuelve 200 vacío)
- Check cada 5 minutos
- Alertas por correo al detectar caída o recuperación

---

## Deploy en Railway

- URL producción: `mosadent.up.railway.app`
- Deploy automático al hacer push a `main`
- Variables de entorno en Railway (sin comillas en los valores):
  - `ANTHROPIC_API_KEY`
  - `DASHBOARD_USER` / `DASHBOARD_PASSWORD` / `SESSION_SECRET`
  - `DATABASE_URL` → referencia: `${{Postgres.DATABASE_URL}}`
  - `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_WHATSAPP_FROM` / `TWILIO_WHATSAPP_DOCTORA`
  - `EMAIL_USER` / `EMAIL_PASSWORD` / `EMAIL_DEST`

---

## Onboarding de nuevo cliente

### Información que necesitas del cliente
- Nombre oficial del consultorio
- Dirección completa
- Teléfono de contacto
- Horario de atención (días y horas)
- Servicios que ofrecen con sus nombres exactos
- Logo (PNG o SVG, fondo transparente)
- Usuario y contraseña para el dashboard
- Número de WhatsApp (doctora + pacientes)
- ¿Tienen dominio propio?
- Preguntas frecuentes o info adicional para el chatbot

### Pasos para desplegar un cliente nuevo

1. **Copiar el proyecto**
   ```bash
   cp -r consultorio-chatbot nuevo-cliente
   cd nuevo-cliente
   git init && git remote add origin <nuevo-repo>
   ```

2. **Actualizar la información del consultorio en `app.py`**
   - Editar `SISTEMA_MOSADENT` con nombre, dirección, teléfono, horario y servicios del cliente
   - Actualizar `DURACIONES` si el cliente tiene servicios con distintas duraciones
   - Renombrar la variable si quieres (ej. `SISTEMA_CLIENTE`)

3. **Actualizar el frontend**
   - Cambiar "MOSADENT" por el nombre del cliente en `index.html`, `dashboard.html`, `login.html`
   - Cambiar colores si el cliente quiere su identidad visual (variables CSS en `:root`)
   - Cambiar logo si aplica

4. **Crear proyecto en Railway**
   - New Project → Deploy from GitHub repo
   - Agregar plugin PostgreSQL
   - Configurar variables de entorno
   - Generar dominio o conectar dominio propio del cliente

5. **Verificar**
   - Hacer una cita de prueba desde el chat
   - Verificar que aparece en el dashboard
   - Confirmar y eliminar la cita de prueba
   - Confirmar que llegan WhatsApps
   - Configurar UptimeRobot
   - Entregar URL y credenciales al cliente

---

## Decisiones técnicas importantes

- **No usar `python`, siempre `python3`**
- **El cliente `anthropic` se inicializa dentro del endpoint `/chat`**, no globalmente, para que tome la API key de las variables de entorno correctamente en Railway
- **`height: 100dvh`** en lugar de `100vh` para compatibilidad con Safari móvil
- **`-webkit-overflow-scrolling: touch`** en el área de chat para scroll suave en iOS
- **`-webkit-tap-highlight-color: transparent`** en botones para evitar flash azul al tocar en móvil
- El wizard no llama `renderCalendario()` completo al seleccionar hora/servicio — solo actualiza clases CSS para evitar el bug de scroll en móvil donde el re-render movía el layout y el dedo tocaba elementos equivocados
- `HEAD /` devuelve `Response()` vacío (200) — no renderiza HTML, solo confirma que el servidor está vivo

---

## Pendientes conocidos

1. **Multi-tenant** — migrar a un solo deploy con tabla `consultorios` y `consultorio_id` en citas. Hacer cuando haya 3+ clientes
2. **Reagendar desde dashboard** — mover una cita a otra fecha/hora sin eliminarla
3. **Estadísticas / reportes** — resumen de citas por mes, servicios más solicitados, tasa de no-shows
