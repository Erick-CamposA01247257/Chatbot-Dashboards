from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import anthropic
import os

load_dotenv()

app = FastAPI()
client = anthropic.Anthropic()

# ============================================
# INICIO — información real de MOSADENT
# ============================================

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
- Pregunta al paciente: nombre completo, servicio que necesita y día/hora preferida
- Horarios disponibles: Lunes a Domingo de 10:00 AM a 7:00 PM
- Confirma que la cita quedó registrada y que la doctora les confirmará

INSTRUCCIONES DE COMPORTAMIENTO:
- Responde siempre en español, de forma amable y profesional
- Si preguntan por precios exactos, di que varían según el caso y ofrece agendar una cita de diagnóstico gratuita
- Si hay una emergencia dental, indica que llamen directamente al 81 1679 8832
- Nunca inventes información que no esté en este documento
- Cuando alguien quiera agendar, recopila: nombre, servicio y horario preferido
- Mantén respuestas cortas y claras — esto es WhatsApp, no un ensayo"""

conversaciones = {}

class Mensaje(BaseModel):
    texto: str
    session_id: str = "default"

# ============================================
# PROCESO — endpoints
# ============================================

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/chat")
async def chat(mensaje: Mensaje):
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

@app.get("/citas")
async def ver_citas():
    return {"mensaje": "Próximamente — integración con calendario"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)