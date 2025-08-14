import os, re, uuid, requests
from datetime import datetime
import pytz
from fastapi import FastAPI, Request, Response
from google.oauth2.service_account import Credentials
import gspread

app = FastAPI()

# ====== ENV (заполни на Vercel в переменных окружения) ======
BOT_TOKEN = os.environ["BOT_TOKEN"]                 # НОВЫЙ токен бота (старый – отозвать в @BotFather)
SHEET_ID = os.environ["SHEET_ID"]                   # ID таблицы (кусок между /d/ и /edit в URL)
BASE_CURRENCY = os.environ.get("BASE_CURRENCY", "RUB").upper()
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])  # твой Telegram user_id (узнаешь у @userinfobot)

GCP_SA_EMAIL = os.environ["GCP_SA_EMAIL"]           # из JSON серв.аккаунта
GCP_SA_PRIVATE_KEY = os.environ["GCP_SA_PRIVATE_KEY"].replace("\\n", "\n")  # из JSON (с переносами строк)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_info(
    {"type":"service_account","client_email":GCP_SA_EMAIL,"private_key":GCP_SA_PRIVATE_KEY},
    scopes=SCOPES
)
gc = gspread.authorize(creds)
sh = gc.open_by_key(SHEET_ID)
ws_tx = sh.worksheet("Transactions")
ws_rates = sh.worksheet("Rates")

TZ = pytz.timezone("Asia/Almaty")
CURRENCIES = {"RUB","KZT","USD","EUR"}

def now_local_iso():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

def today_iso():
    return datetime.now(TZ).strftime("%Y-%m-%d")

def parse_msg(text: str, base_currency: str):
    """
    Ожидаемый формат сообщений боту:
      - расход:  -1200 kzt кофе #еда/кофе
      - доход:   +3000 rub репетитор #доход/репетитор
    Валюта опциональна (если не указана — берём базовую RUB).
    Категория задаётся хэштегом (#еда или #еда/кофе).
    """
    m = re.match(r"^\s*([+\-])\s*([\d.,]+)\s*([a-zA-Z]{3})?\s*(.*)$", text)
    if not m: return None
    sign = -1 if m.group(1) == "-" else 1
    amount = sign * float(m.group(2).replace(",", "."))
    cur = (m.group(3) or base_currency).upper()
    if cur not in CURRENCIES: return None

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

def get_rate_from_sheet(date_iso: str, base: str, quote: str):
    if base == quote: return 1.0
    # header: date | base | quote | rate
    values = ws_rates.get_all_values()
    for row in values[1:]:
        if len(row) < 4: continue
        d, b, q, r = row[0], row[1], row[2], row[3]
        if d == date_iso and b.upper() == base and q.upper() == quote:
            try: return float(r)
            except: pass
    return None

def save_rate(date_iso: str, base: str, quote: str, rate: float):
    ws_rates.append_row([date_iso, base, quote, f"{rate:.6f}"])

def fetch_rate(date_iso: str, base: str, quote: str) -> float:
    if base == quote: return 1.0
    url = f"https://api.exchangerate.host/{date_iso}?base={base}&symbols={quote}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    j = r.json()
    rate = j.get("rates", {}).get(quote)
    if not rate: raise RuntimeError("rate not found")
    return float(rate)

def ensure_rate(date_iso: str, base: str, quote: str) -> float:
    """В листе Rates храним курс base->quote на дату.
       Для пересчёта quote->base используем 1/rate."""
    r = get_rate_from_sheet(date_iso, base, quote)
    if r is not None: return r
    r = fetch_rate(date_iso, base, quote)
    save_rate(date_iso, base, quote, r)
    return r

@app.get("/")          # health-check для проверки в браузере
def health():
    return {"ok": True}

@app.post("/")
async def webhook(req: Request):
    upd = await req.json()
    msg = upd.get("message") or upd.get("edited_message")
    if not msg or "text" not in msg: return Response("ok")

    # Разрешаем писать только твоему ID
    if msg.get("from", {}).get("id") != ALLOWED_USER_ID:
        return Response("ok")

    text = msg["text"].strip()
    parsed = parse_msg(text, BASE_CURRENCY)
    if not parsed:  # неверный формат — молча игнорируем
        return Response("ok")

    date_iso = today_iso()
    amount = parsed["amount"]
    cur = parsed["currency"]
    # получаем rate base->quote; для amount в quote нам нужно quote->base = 1/rate
    if cur == BASE_CURRENCY:
        amount_base = amount
    else:
        r = ensure_rate(date_iso, BASE_CURRENCY, cur)
        amount_base = amount * (1.0 / r)

    row = [
        str(uuid.uuid4()),   # id
        now_local_iso(),     # timestamp_local
        date_iso,            # date
        round(amount, 2),    # amount (в исходной валюте)
        cur,                 # currency
        round(amount_base, 2), # amount_base (в RUB)
        BASE_CURRENCY,       # base_currency
        parsed["category"],  # category
        parsed["subcategory"]# subcategory
    ]
    ws_tx.append_row(row)

    # бот ничего не пишет в ответ
    return Response("ok")
