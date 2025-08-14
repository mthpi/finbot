# api/webhook.py
import os, re, uuid
from datetime import datetime
import pytz
from fastapi import FastAPI, Request, Response
from google.oauth2.service_account import Credentials
import gspread

app = FastAPI()

# ---- настройки времени и допустимых валют ----
TZ = pytz.timezone("Asia/Almaty")
CURRENCIES = {"RUB", "KZT", "USD", "EUR"}

# ---- утилиты ----
def today_iso() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")

def now_local_iso() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

def parse_msg(text: str, base_currency: str):
    m = re.match(r"^\s*([+\-])\s*([\d.,]+)\s*([a-zA-Z]{3})?\s*(.*)$", text or "")
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    amount = sign * float(m.group(2).replace(",", "."))
    cur = (m.group(3) or base_currency).upper()
    if cur not in CURRENCIES:
        return None

    rest = m.group(4) or ""
    tags = re.findall(r"#([^\s#]+)", rest)
    category, subcategory = "", ""
    if tags:
        first = tags[0]
        if "/" in first:
            category, subcategory = first.split("/", 1)
        else:
            category = first

    # Убираем все #теги, лишние пробелы — это и будет description
    description = re.sub(r"#([^\s#]+)", "", rest).strip()

    return {
        "amount": amount,
        "currency": cur,
        "category": category,
        "subcategory": subcategory,
        "description": description
    }


def get_sheets():
    """Ленивая инициализация Google Sheets, берём секреты из ENV."""
    sheet_id = os.environ["SHEET_ID"]
    sa_email = os.environ["GCP_SA_EMAIL"]
    sa_pk = os.environ["GCP_SA_PRIVATE_KEY"].replace("\\n", "\n")

    creds = Credentials.from_service_account_info(
        {
            "type": "service_account",
            "client_email": sa_email,
            "private_key": sa_pk,
            "token_uri": "https://oauth2.googleapis.com/token"
        },
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    ws_tx = sh.worksheet("Transactions")  # должен существовать
    return ws_tx

# ---- health (GET) ----
@app.get("/")
@app.get("/api/webhook")
def health():
    return {"ok": True}

# ---- webhook (POST) ----
@app.post("/")
@app.post("/api/webhook")
async def webhook(req: Request):
    # 1) читаем апдейт и не падаем по пустякам
    try:
        upd = await req.json()
    except Exception:
        return Response("bad json", status_code=400)

    msg = (upd.get("message") or upd.get("edited_message")) or {}
    text = msg.get("text") or ""

    # (опционально) белый список пользователя
    allowed_env = os.environ.get("ALLOWED_USER_ID")
    if allowed_env:
        try:
            allowed_id = int(allowed_env)
            from_id = (msg.get("from") or {}).get("id")
            if from_id != allowed_id:
                return Response("ok")
        except Exception:
            # если ALLOWED_USER_ID некорректный, просто продолжаем
            pass

    # 2) парсим сумму/валюту/категорию
    base = os.environ.get("BASE_CURRENCY", "RUB").upper()
    p = parse_msg(text, base)
    if not p:
        return Response("ok")

    # 3) быстро записываем «сырую» строку (amount_base оставляем пустым)
    try:
        ws_tx = get_sheets()
        row = [
            str(uuid.uuid4()),   # id
            now_local_iso(),     # timestamp_local
            today_iso(),         # date
            round(p["amount"], 2),   # amount
            p["currency"],           # currency
            "",                      # amount_base
            base,                    # base_currency
            p["category"],           # category
            p["subcategory"],        # subcategory
            p["description"],        # description  ← вот сюда
        ]

        ws_tx.append_row(row)
    except Exception as e:
        # В логах Vercel будет видно, что именно пошло не так (права/ключ/имя листа)
        print("SHEETS_WRITE_ERROR:", e)
        return Response("SHEETS_WRITE_ERROR", status_code=500)

    # 4) мгновенно отдаём 200 OK, чтобы Telegram не ловил таймауты
    return Response("ok")
