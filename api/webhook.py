# api/webhook.py
import os, re, uuid, logging
from datetime import datetime
import pytz
from fastapi import FastAPI, Request, Response
from google.oauth2.service_account import Credentials
import gspread

app = FastAPI()

# ---------- логирование ----------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("webhook")
logger.setLevel(LOG_LEVEL)

# ---------- настройки времени и валют ----------
TZ = pytz.timezone("Asia/Almaty")
CURRENCIES = {"RUB", "KZT", "USD", "EUR"}

def today_iso() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")

def now_local_iso() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

def parse_msg(text: str, base_currency: str):
    """
    Ожидаемый формат:
      - расход:  -1200 kzt кофе #еда/кофе
      - доход:   +3000 rub репетитор #доход/репетитор
    Валюта опциональна (по умолчанию base_currency).
    Категория через #тег: #еда или #еда/кофе.
    Возвращает (dict|None, reason:str)
    """
    txt = text or ""
    m = re.match(r"^\s*([+\-])\s*([\d.,]+)\s*([a-zA-Z]{3})?\s*(.*)$", txt)
    if not m:
        return None, "regex_no_match"

    try:
        sign = -1 if m.group(1) == "-" else 1
        amount = sign * float(m.group(2).replace(",", "."))
    except Exception:
        return None, "amount_parse_error"

    cur = (m.group(3) or base_currency).upper()
    if cur not in CURRENCIES:
        return None, f"unsupported_currency:{cur}"

    rest = (m.group(4) or "").strip()

    # теги
    tags = re.findall(r"#([^\s#]+)", rest)
    category, subcategory = "", ""
    if tags:
        first = tags[0]
        if "/" in first:
            category, subcategory = first.split("/", 1)
        else:
            category = first

    # описание: всё без #тегов, нормализуем пробелы
    description = re.sub(r"#([^\s#]+)", "", rest)
    description = re.sub(r"\s+", " ", description).strip()

    return ({
        "amount": round(amount, 2),
        "currency": cur,
        "category": category,
        "subcategory": subcategory,
        "description": description
    }, "ok")

def get_sheets():
    """Ленивая инициализация Google Sheets, берём секреты из ENV."""
    sheet_id = os.environ["SHEET_ID"]
    sa_email = os.environ["GCP_SA_EMAIL"]
    # Частые проблемы: ключ скопирован с литералами \n или \r\n
    sa_pk = os.environ["GCP_SA_PRIVATE_KEY"]
    sa_pk = sa_pk.replace("\\n", "\n").replace("\\r\\n", "\n").replace("\r\n", "\n")

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
    ws_tx = sh.worksheet("Transactions")  # лист должен существовать
    return ws_tx

# ---- health (GET) ----
@app.get("/")
@app.get("/api/webhook")
def health():
    return {"ok": True, "ts": now_local_iso()}

# ---- webhook (POST) ----
@app.post("/")
@app.post("/api/webhook")
async def webhook(req: Request):
    # 1) читаем апдейт
    try:
        upd = await req.json()
    except Exception as e:
        logger.warning("bad_json: %s", e)
        return Response("bad json", status_code=400)

    msg = (upd.get("message") or upd.get("edited_message")) or {}
    text = msg.get("text") or ""
    from_obj = msg.get("from") or {}
    from_id = from_obj.get("id")
    logger.info("incoming: from_id=%s text=%r", from_id, text)

    # 2) белый список (если задан)
    allowed_env = os.environ.get("ALLOWED_USER_ID")
    if allowed_env:
        try:
            allowed_id = int(allowed_env)
        except Exception:
            logger.error("ALLOWED_USER_ID not an int: %r", allowed_env)
            allowed_id = None
        if allowed_id is not None and from_id != allowed_id:
            logger.info("ignored_by_allowlist: got=%s expected=%s", from_id, allowed_id)
            return Response("ok")

    # 3) парсим сумму/валюту/категорию/описание
    base = os.environ.get("BASE_CURRENCY", "RUB").upper()
    p, reason = parse_msg(text, base)
    if not p:
        logger.info("ignored_by_parser: reason=%s text=%r", reason, text)
        return Response("ok")

    # 4) пишем строку в Google Sheets
    try:
        ws_tx = get_sheets()
        row = [
            str(uuid.uuid4()),       # id
            now_local_iso(),         # timestamp_local
            today_iso(),             # date
            p["amount"],             # amount
            p["currency"],           # currency
            "",                      # amount_base (посчитает формула/таблица)
            base,                    # base_currency
            p["category"],           # category
            p["subcategory"],        # subcategory
            p["description"],        # description
        ]
        ws_tx.append_row(row, value_input_option="RAW")
        logger.info("sheet_append_ok: amount=%s cur=%s cat=%s sub=%s desc=%r",
                    p['amount'], p['currency'], p['category'], p['subcategory'], p['description'])
    except Exception as e:
        logger.error("SHEETS_WRITE_ERROR: %s", e)
        return Response("SHEETS_WRITE_ERROR", status_code=500)

    # 5) 200 OK
    return Response("ok")
