# CLAUDE.md — consultorio-chatbot

## Contexto del proyecto

Sistema de agendamiento de citas para consultorios dentales. Tiene dos interfaces:
- **Chat del paciente** (`/`) — chatbot estilo WhatsApp con wizard de agendado de 4 pasos
- **Dashboard de la doctora** (`/dashboard`) — panel con agenda del día y calendario mensual, protegido por login

El primer cliente objetivo es **MOSADENT** — consultorio dental en Guadalupe, N.L. El sistema prompt y la UI están configurados con información real de ese consultorio.

---

## Stack

- **Backend:** Python 3.12 + FastAPI + Uvicorn
- **Base de datos:** SQLite (`citas.db`)
- **AI:** Claude API (Anthropic) — modelo `claude-sonnet-4-20250514`
- **Frontend:** HTML + CSS + JavaScript vanilla (sin frameworks)
- **Deploy:** Railway
- **Tipografías:** Cormorant Garamond (serif, títulos) + DM Sans (sans-serif, cuerpo)

---

## Estructura de archivos

```
consultorio-chatbot/
├── app.py                  ← backend principal FastAPI
├── citas.db                ← base de datos SQLite (se crea automático)
├── requirements.txt
├── Procfile                ← Railway: uvicorn app:app --host 0.0.0.0 --port $PORT
├── railway.json
├── .gitignore              ← incluye citas.db
└── templates/
    ├── index.html          ← chat del paciente
    ├── dashboard.html      ← panel de la doctora
    └── login.html          ← login para acceder al dashboard
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
```

---

## Endpoints

### Públicos
| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/` | Chat del paciente (index.html) |
| GET | `/login` | Página de login (login.html) |
| POST | `/api/login` | Autenticación — devuelve token |
| GET | `/api/logout` | Cerrar sesión |
| POST | `/chat` | Mensaje al chatbot (Claude API) |
| GET | `/api/slots` | Horarios disponibles por fecha y servicio |
| POST | `/api/agendar` | Guardar cita en SQLite |

### Protegidos (requieren sesión)
| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/dashboard` | Panel de la doctora |
| GET | `/citas` | Alias de /dashboard |
| GET | `/api/citas` | JSON con todas las citas |

---

## Base de datos SQLite

### Tabla `citas`
```sql
CREATE TABLE citas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL,
    servicio TEXT NOT NULL,
    fecha TEXT NOT NULL,        -- formato: YYYY-MM-DD
    hora TEXT NOT NULL,         -- formato: HH:MM
    duracion INTEGER NOT NULL,  -- en minutos
    fecha_registro TEXT NOT NULL
)
```

### Notas importantes
- `citas.db` se crea automáticamente al arrancar el servidor con `init_db()`
- En Railway el filesystem es efímero — las citas se pierden al reiniciar (pendiente migrar a PostgreSQL)
- No hay tabla de sesiones — la autenticación es determinística (ver Auth)

---

## Auth

El sistema usa un **token determinístico** — se calcula con SHA256 a partir de `DASHBOARD_USER:DASHBOARD_PASSWORD:SESSION_SECRET`. No se guarda en ningún lado. Cualquier réplica puede verificarlo.

```python
def get_token_valido():
    data = f"{DASHBOARD_USER}:{DASHBOARD_PASSWORD}:{SESSION_SECRET}"
    return hashlib.sha256(data.encode()).hexdigest()
```

La verificación lee el header `X-Auth-Token` en cada request protegida.

---

## Lógica de slots

`calcular_slots(fecha, duracion)` — genera horarios disponibles para una fecha y servicio:

- Horario del consultorio: **10:00 AM a 7:00 PM** (HORA_INICIO=10, HORA_FIN=19)
- Slots cada 30 minutos
- Si la fecha es **hoy**, filtra slots pasados y aplica regla de **2 horas de anticipación mínima**
- El timezone de México es UTC-6 — se usa `datetime.now() - timedelta(hours=6)` para calcular hora local correctamente en Railway (que corre en UTC)
- Slots ocupados se calculan comparando rangos de inicio/fin de citas existentes

### Duraciones por servicio
```python
DURACIONES = {
    "limpieza dental": 30,
    "revisión general": 30,
    "blanqueamiento": 60,
    "resinas": 60,
    "extracción": 45,
    "ortodoncia": 60,
    "implantes": 90,
}
```

---

## Flujo del chat (index.html)

El agendado es un **wizard de 4 pasos** dentro del chat:

1. **Fecha** — calendario mensual, días pasados deshabilitados
2. **Servicio** — grid 2 columnas con nombre y duración
3. **Hora** — grid 3 columnas con slots disponibles (cargados desde `/api/slots`)
4. **Confirmación** — resumen + input de nombre + botón confirmar

Al confirmar llama a `/api/agendar`. Si el slot ya está ocupado devuelve error 409 y regresa al paso 1.

El chat también responde preguntas vía Claude API. Después de cada respuesta del bot aparece automáticamente un botón "Agendar cita" para empujar el agendado.

---

## Dashboard (dashboard.html)

Layout de dos columnas en desktop, tabs en móvil (≤640px):

- **Izquierda / Tab Agenda:** Timeline del día por hora (10:00 a 19:00), tarjetas de cita con nombre, servicio y duración
- **Derecha / Tab Calendario:** Mini calendario mensual + vista horaria tipo Google Calendar con bloques proporcionales a la duración

`ALTURA_HORA = 72px` — cada hora ocupa 72px en el grid visual. Los bloques se posicionan absolutamente.

Auto-refresh cada 30 segundos con `setInterval(cargarCitas, 30000)`.

---

## Deploy en Railway

- URL producción: `mosadent.up.railway.app`
- Deploy automático al hacer push a `main`
- Variables de entorno en Railway (sin comillas en los valores):
  - `ANTHROPIC_API_KEY`
  - `DASHBOARD_USER`
  - `DASHBOARD_PASSWORD`
  - `SESSION_SECRET`
  - `DATABASE_URL` → referencia: `${{Postgres.DATABASE_URL}}`
- Railway puede tener múltiples réplicas — por eso el auth es determinístico y no guarda estado en memoria

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
- Número de WhatsApp (para Twilio, Fase 3)
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
   - Renombrar la variable si quieres (ej. `SISTEMA_CLIENTE`)

3. **Actualizar el frontend**
   - Cambiar "MOSADENT" por el nombre del cliente en `index.html`, `dashboard.html`, `login.html`
   - Cambiar colores si el cliente quiere su identidad visual (variables CSS en `:root`)
   - Cambiar logo si aplica

4. **Crear proyecto en Railway**
   - New Project → Deploy from GitHub repo
   - Agregar plugin PostgreSQL
   - Configurar variables de entorno:
     - `ANTHROPIC_API_KEY` (tu key o la del cliente)
     - `DASHBOARD_USER` / `DASHBOARD_PASSWORD` / `SESSION_SECRET`
     - `DATABASE_URL` → `${{Postgres.DATABASE_URL}}`
   - Generar dominio o conectar dominio propio del cliente

5. **Verificar**
   - Hacer una cita de prueba desde el chat
   - Verificar que aparece en el dashboard
   - Confirmar y eliminar la cita de prueba
   - Entregar URL y credenciales al cliente

---

## Decisiones técnicas importantes

- **No usar `python`, siempre `python3`**
- **El cliente `anthropic` se inicializa dentro del endpoint `/chat`**, no globalmente, para que tome la API key de las variables de entorno correctamente en Railway
- **`height: 100dvh`** en lugar de `100vh` para compatibilidad con Safari móvil
- **`-webkit-overflow-scrolling: touch`** en el área de chat para scroll suave en iOS
- **`-webkit-tap-highlight-color: transparent`** en botones para evitar flash azul al tocar en móvil
- El wizard no llama `renderCalendario()` completo al seleccionar hora/servicio — solo actualiza clases CSS para evitar el bug de scroll en móvil donde el re-render movía el layout y el dedo tocaba elementos equivocados

---

## Pendientes conocidos

1. **Twilio WhatsApp** — al confirmar cita en dashboard, enviar WhatsApp al paciente. También recordatorio 24h antes
2. **Multi-tenant** — migrar a un solo deploy con tabla `consultorios` y `consultorio_id` en citas. Hacer cuando haya 3+ clientes
3. **Bloquear horarios** — que la doctora pueda marcar días/horas como no disponibles
4. **Historial del paciente** — ver citas anteriores por número de teléfono