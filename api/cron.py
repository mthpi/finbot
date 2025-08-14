# api/cron.py
import os, requests
from datetime import datetime
import pytz
from fastapi import FastAPI, Response
from google.oauth2.service_account import Credentials
import gspread

app = FastAPI()
TZ = pytz.timezone("Asia/Almaty")

def gclient():
    sheet_id = os.environ["SHEET_ID"]
    email = os.environ["GCP_SA_EMAIL"]
    pk = os.environ["GCP_SA_PRIVATE_KEY"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(
        {"type":"service_account","client_email":email,"private_key":pk,"token_uri":"https://oauth2.googleapis.com/token"},
        scopes=["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    return sh.worksheet("Transactions"), sh.worksheet("Rates")

def fetch_rate(date_iso, base, quote):
    if base == quote: return 1.0
    url = f"https://api.exchangerate.host/{date_iso}?base={base}&symbols={quote}"
    j = requests.get(url, timeout=10).json()
    return float(j["rates"][quote])

@app.get("/")
@app.get("/api/cron")
def health():
    return {"ok": True}

@app.post("/")
@app.post("/api/cron")
def run():
    BASE = os.environ.get("BASE_CURRENCY","RUB").upper()
    ws_tx, ws_rates = gclient()

    # читаем все строки (немного на старте)
    data = ws_tx.get_all_values()  # header + rows
    header = data[0]
    idx = {name:i for i,name in enumerate(header)}
    changed = False

    # пройдёмся и заполним пустые amount_base
    for r in range(1, len(data)):
        row = data[r]
        if idx.get("amount_base") is None: return Response("NO amount_base col", 500)
        if row[idx["amount_base"]]: 
            continue  # уже посчитано

        date_iso = row[idx["date"]]
        cur = row[idx["currency"]]
        amount = float(row[idx["amount"]])
        base = row[idx["base_currency"]]

        # курс: base->quote, нам нужен quote->base = 1/rate
        rate = fetch_rate(date_iso, base, cur) if cur != base else 1.0
        amount_base = round(amount * (1.0 / rate), 2)

        # пишем обратно только одну ячейку amount_base
        ws_tx.update_cell(r+1, idx["amount_base"]+1, amount_base)
        changed = True

    return {"updated": changed}
