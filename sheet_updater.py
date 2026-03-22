import os
import json
import logging
import asyncio
import re
from datetime import datetime, timedelta

import httpx
import gspread
from bs4 import BeautifulSoup
from oauth2client.service_account import ServiceAccountCredentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

TWELVE_API_KEY = os.getenv("TWELVE_API_KEY", "").strip()
FMP_API_KEY = os.getenv("FMP_API_KEY", "").strip()
SHEET_ID = os.getenv("SHEET_ID", "").strip()
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "splits_feed").strip()
POLL_INTERVAL = int(os.getenv("SHEET_POLL_INTERVAL_SECONDS", "600"))

BENZINGA_URL = "https://www.benzinga.com/calendars/stock-splits"

HEADERS = [
    "Ticker","Company","Announcement Date","Split Date","Ratio",
    "Exchange","Close -14D","Close Pre","Price Now","Source"
]

def connect_sheet():
    creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)

def normalize_date(v):
    for fmt in ("%m/%d/%Y","%Y-%m-%d","%b %d, %Y","%B %d, %Y"):
        try:
            return datetime.strptime(v.strip(), fmt).strftime("%Y-%m-%d")
        except:
            pass
    return v

def is_date(v):
    for fmt in ("%m/%d/%Y","%Y-%m-%d","%b %d, %Y","%B %d, %Y"):
        try:
            datetime.strptime(v.strip(), fmt)
            return True
        except:
            pass
    return False

def is_ratio(v):
    v=v.lower()
    return ("for" in v or ":" in v)

def clean_ratio(v):
    return v.replace(":", "-for-").replace(" for ", "-for-").replace(" ", "")

# 🔥 ФИЛЬТР (главное изменение)
def is_good_stock(ticker, company):
    name = company.lower()

    if "etf" in name:
        return False
    if "defiance" in name:
        return False
    if ticker.endswith("x"):  # мусорные тикеры
        return False

    return True

def parse(html):
    soup = BeautifulSoup(html, "html.parser")
    out = []

    for tr in soup.find_all("tr"):
        cells=[c.get_text(" ",strip=True) for c in tr.find_all(["td","th"])]
        if len(cells)<6:
            continue

        dates=[normalize_date(c) for c in cells if is_date(c)]
        if not dates:
            continue

        split_date=dates[0]
        ann_date=dates[1] if len(dates)>1 else ""

        ratio=[clean_ratio(c) for c in cells if is_ratio(c)]
        if not ratio:
            continue
        ratio=ratio[0]

        upper=[c.upper() for c in cells]

        ticker=""
        exchange=""

        for c in upper:
            if c in ["NASDAQ","NYSE","AMEX","ARCA","BATS"]:
                exchange=c

        for c in upper:
            if c.isalpha() and len(c)<=6 and c not in ["NASDAQ","NYSE","AMEX","ARCA","BATS"]:
                ticker=c
                break

        company=""
        for c in cells:
            if c.upper()!=ticker and not is_date(c) and not is_ratio(c):
                if len(c)>3:
                    company=c
                    break

        if not ticker or not company:
            continue

        # 🔥 фильтр мусора
        if not is_good_stock(ticker, company):
            continue

        out.append([
            ticker,company,ann_date,split_date,ratio,exchange,
            "N/A","N/A","N/A","Benzinga"
        ])

    # убираем дубли
    uniq=[]
    seen=set()
    for r in out:
        key=(r[0],r[3])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)

    return uniq

async def get_price(client, ticker):
    # Twelve
    if TWELVE_API_KEY:
        try:
            r=await client.get("https://api.twelvedata.com/price",
                params={"symbol":ticker,"apikey":TWELVE_API_KEY})
            p=r.json().get("price")
            if p:
                return p
        except:
            pass

    # FMP fallback
    if FMP_API_KEY:
        try:
            r=await client.get(f"https://financialmodelingprep.com/api/v3/quote/{ticker}",
                params={"apikey":FMP_API_KEY})
            d=r.json()
            if isinstance(d,list) and d:
                return d[0].get("price","N/A")
        except:
            pass

    return "N/A"

async def enrich(rows):
    async with httpx.AsyncClient() as client:
        for i,row in enumerate(rows,1):
            row[8]=await get_price(client,row[0])
            logging.info(f"{i}/{len(rows)} {row[0]} price={row[8]}")
            await asyncio.sleep(0.7)
    return rows

def write(sheet,rows):
    sheet.clear()
    sheet.update("A1",[HEADERS]+rows)

async def main():
    sheet=connect_sheet()

    while True:
        try:
            async with httpx.AsyncClient() as c:
                r=await c.get(BENZINGA_URL)
                rows=parse(r.text)

            rows=await enrich(rows)
            write(sheet,rows)

        except Exception as e:
            logging.exception(e)

        await asyncio.sleep(POLL_INTERVAL)

if __name__=="__main__":
    asyncio.run(main())
