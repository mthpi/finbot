import os, re, uuid, requests
from datetime import datetime
import pytz
from fastapi import FastAPI, Request, Response
from google.oauth2.service_account import Credentials
import gspread

app = FastAPI()
TZ = pytz.timezone("Asia/Almaty")
CURRENCIES = {"RUB","KZT","USD","EUR"}

# ---------- health ----------
@app.get("/")
@app.get("/api/webhook")
def health():
    return {"ok": True}

# ---------- utils ----------
def today_iso():
    return datetime.now(TZ).strftime("%Y-%m-%d")

def now_local_iso():
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
    return {"amount": amount, "currency": cur, "category": category, "subcategory": subcategory}

def need_env(name, cast=str):
    v = os.environ.get(name)
    if v is None or v == "":
        raise RuntimeError(f"MISSING_ENV:{name}")
    return cast(v) if cast is not str else v

def get_sheets():
    """Ленивая инициализация клиентов Google; отдаём понятные ошибки вместо немого падения."""
    SHEET_ID = need_env("SHEET_ID")
    GCP_SA_EMAIL = need_env("GCP_SA_EMAIL")
    GCP_SA_PRIVATE_KEY = need_env("GCP_SA_PRIVATE_KEY").replace("\\n", "\n")

    creds = Credentials.from_service_account_info(
        {
            "type": "service_account",
            "client_email": GCP_SA_EMAIL,
            "private_key": GCP_SA_PRIVATE_KEY,
            "token_uri": "https://oauth2.googleapis.com/token"
        },
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws_tx = sh.worksheet("Transactions")
    except gspread.WorksheetNotFound:
        raise RuntimeError("SHEET_ERROR: лист 'Transactions' не найден")
    try:
        ws_rates = sh.worksheet("Rates")
    except gspread.WorksheetNotFound:
        raise RuntimeError("SHEET_ERROR: лист 'Rates' не найден")
    return ws_tx, ws_rates

def get_rate_from_sheet(ws_rates, date_iso: str, base: str, quote: str):
    if base == quote:
        return 1.0
    values = ws_rates.get_all_values()  # немного данных, ок
    for row in values[1:]:
        if len(row) < 4:
            continue
        d, b, q, r = row[0], row[1], row[2], row[3]
        if d == date_iso and b.upper() == base and q.upper() == quote:
            try:
                return float(r)
            except:
                pass
    return None

def fetch_rate(date_iso: str, base: str, quote: str) -> float:
    if base == quote:
        return 1.0
    url = f"https://api.exchangerate.host/{date_iso}?base={base}&symbols={quote}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    j = r.json()
    rate = j.get("rates", {}).get(quote)
    if not rate:
        raise RuntimeError("RATE_API_ERROR")
    return float(rate)

def ensure_rate(ws_rates, date_iso: str, base: str, quote: str) -> float:
    r = get_rate_from_sheet(ws_rates, date_iso, base, quote)
    if r is not None:
        return r
    r = fetch_rate(date_iso, base, quote)
    ws_rates.append_row([date_iso, base, quote, f"{r:.6f}"])
    return r

# ---------- webhook ----------
@app.post("/")
@app.get("/api/webhook")
async def webhook(req: Request):
    # Инициализация env и Google Sheets
    try:
        BASE_CURRENCY = os.environ.get("BASE_CURRENCY", "RUB").upper()
        allowed_id_env = os.environ.get("ALLOWED_USER_ID")
        ALLOWED_ID = int(allowed_id_env) if allowed_id_env else None
        ws_tx, ws_rates = get_sheets()
    except Exception as e:
        return Response(f"INIT_ERROR:{e}", status_code=500)

    # Читаем апдейт Telegram
    try:
        upd = await req.json()
    except Exception:
        return Response("bad json", status_code=400)

    msg = (upd.get("message") or upd.get("edited_message")) or {}
    text = msg.get("text")
    from_id = (msg.get("from") or {}).get("id")

    # Белый список: только твой ID (если задан)
    if ALLOWED_ID and from_id != ALLOWED_ID:
        return Response("ok")

    parsed = parse_msg(text, BASE_CURRENCY)
    if not parsed:
        return Response("ok")

    date_iso = today_iso()
    amount = parsed["amount"]
    cur = parsed["currency"]

    # Конвертация в базовую валюту
    try:
        if cur == BASE_CURRENCY:
            amount_base = amount
        else:
            r = ensure_rate(ws_rates, date_iso, BASE_CURRENCY, cur)   # r = base->quote
            amount_base = amount * (1.0 / r)                          # quote->base
    except Exception as e:
        return Response(f"RATE_ERROR:{e}", status_code=500)

    # Записываем строку (строго по твоим 9 колонкам)
    row = [
        str(uuid.uuid4()),     # id
        now_local_iso(),       # timestamp_local
        date_iso,              # date
        round(amount, 2),      # amount (в исходной валюте)
        cur,                   # currency
        round(amount_base, 2), # amount_base (в базовой)
        BASE_CURRENCY,         # base_currency
        parsed["category"],    # category
        parsed["subcategory"]  # subcategory
    ]
    try:
        ws_tx.append_row(row)
    except Exception as e:
        return Response(f"SHEETS_WRITE_ERROR:{e}", status_code=500)

    return Response("ok")
