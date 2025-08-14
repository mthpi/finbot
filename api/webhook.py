# api/webhook.py
from fastapi import FastAPI, Request, Response

app = FastAPI()

# --- Health (GET) на двух путях ---
@app.get("/")
@app.get("/api/webhook")
def health():
    return {"ok": True}

# --- Webhook (POST) на двух путях ---
@app.post("/")
@app.post("/api/webhook")
async def webhook(req: Request):
    _ = await req.body()   # читаем тело, чтобы не падать
    return Response("ok")  # Telegram ждёт 200 OK
