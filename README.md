# AI-Powered Dental Appointment System

A full-stack web application that lets dental clinics manage appointments through a conversational AI chat and a protected admin dashboard. Built with FastAPI, PostgreSQL, and the Anthropic Claude API — deployed on Railway.

## Features

### Patient-facing chat (`/`)
- Natural language conversation powered by **Claude AI** (claude-sonnet)
- 4-step appointment wizard: service → date → available slot → confirmation
- Real-time slot availability calculated against existing appointments and blocked times
- Rate-limited (slowapi) to prevent abuse

### Admin dashboard (`/dashboard`)
- **Multi-role authentication**: admin · doctor · assistant · owner
- List and calendar views of all appointments
- Confirm, delete, and mark no-show per appointment — or confirm in bulk
- Block time slots (by day or hour range) per doctor
- Toggle auto-confirm mode for incoming appointments
- Export all appointments to a formatted `.xlsx` file (with a summary sheet)
- Real-time push updates via **Server-Sent Events (SSE)** — new appointments appear instantly without refreshing
- Full audit log of every action (creates, confirms, deletions, failed logins)

### Automated workflows
- WhatsApp confirmation sent to the patient on booking (via **Whapi.cloud**)
- Daily reminder at 9 AM (Mexico time) for all confirmed appointments that day
- Extra reminder for patients with a prior no-show history
- Weekly backup of all data (appointments, blocks, audit) delivered as CSV attachments by **Gmail SMTP**
- All jobs scheduled with **APScheduler**

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12 · FastAPI · Uvicorn |
| Database | PostgreSQL |
| AI | Anthropic Claude API (`claude-sonnet`) |
| Frontend | Vanilla HTML · CSS · JavaScript |
| Notifications | Whapi.cloud (WhatsApp) · Gmail SMTP |
| Scheduler | APScheduler |
| Deploy | Railway |

## Project structure

```
Dentales/
├── app.py              ← all backend logic (endpoints, DB, scheduler, system prompt)
├── templates/
│   ├── index.html      ← patient chat interface
│   ├── dashboard.html  ← admin panel
│   ├── login.html
│   └── cancelar.html   ← public cancellation page (token-based)
├── .env.example        ← required environment variables
├── Procfile
└── requirements.txt
```

## Getting started

### 1. Clone and install

```bash
git clone https://github.com/Erick-CamposA01247257/Chatbot-Dashboards.git
cd Dentales
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
# Edit .env with your values
```

See [Configuration](#configuration) for a description of each variable.

### 3. Run

```bash
uvicorn app:app --reload
# http://localhost:8000
```

## Configuration

All clinic-specific settings and secrets are read from environment variables. Copy `.env.example` to `.env` and fill in your values.

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `CLINIC_NAME` | Clinic display name (used in the chat and WhatsApp messages) |
| `CLINIC_CITY` | City shown in the chat interface |
| `CLINIC_ADDRESS` | Full address shown to patients |
| `CLINIC_PHONE` | Phone number for emergency instructions |
| `BASE_URL` | Public URL of your deploy (used in cancellation links) |
| `SESSION_SECRET` | Random secret for password hashing |
| `DASHBOARD_USER` / `DASHBOARD_PASSWORD` | Admin credentials |
| `ASSISTANT_USER` / `ASSISTANT_PASSWORD` | Assistant role credentials |
| `OWNER_USER` / `OWNER_PASSWORD` | Owner role (export-only access) |
| `DOCTOR_NOMBRE` / `DOCTOR_USERNAME` / `DOCTOR_PASSWORD` | Seeds a doctor record in the DB on startup |
| `DOCTOR_PHONE` | WhatsApp number to notify the doctor on new appointments |
| `GMAIL_USER` / `GMAIL_APP_PASSWORD` / `BACKUP_EMAIL` | Gmail SMTP for weekly backups |
| `WHAPI_TOKEN` / `WHAPI_CHANNEL_URL` | Whapi.cloud credentials (optional) |

## Deploying to Railway

1. Create a new Railway project and add a **PostgreSQL** plugin.
2. Set all environment variables from `.env.example` in the Railway dashboard.
3. Railway will auto-deploy on every push to `main` using the `Procfile`.

## Security

- Security headers on every response: `Content-Security-Policy`, `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`
- Session tokens stored in **HttpOnly + SameSite=Strict cookies**
- Passwords hashed with **SHA-256 + secret salt** (no plaintext storage)
- All user input validated and sanitized with **Pydantic** before hitting the database
- Rate limiting on `/api/login` (10/min) and `/api/agendar` (10/hour)
- Failed login attempts are recorded in the audit log

## License

© 2025 Erick Campos. All Rights Reserved.
