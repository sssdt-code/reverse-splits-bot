import os
import json
import logging
import asyncio
from datetime import datetime, timedelta

import httpx
import gspread
from bs4 import BeautifulSoup
from oauth2client.service_account import ServiceAccountCredentials

logging.basicConfig(level=logging.INFO)

TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")
FMP_API_KEY = os.getenv("FMP_API_KEY")
SHEET_ID = os.getenv("SHEET_ID")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "splits_feed")

HEADERS = [
    "Ticker","Company","Announcement Date","Split Date",
    "Ratio","Exchange","Close -14D","Close Pre","Price Now","Source"
]

BAD_WORDS = ["ETF", "Defiance", "2X", "3X"]

def connect():
    creds = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
    scope = ["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds, scope)
    return gspread.authorize(creds).open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)

def valid_stock(ticker, company, exchange):
    if exchange == "OTC":
        return False
    if any(w.lower() in company.lower() for w in BAD_WORDS):
        return False
    if len(ticker) > 5:
        return False
    return True

def parse(html):
    soup = BeautifulSoup(html, "html.parser")
    rows = []

    for tr in soup.find_all("tr"):
        tds = [x.get_text(strip=True) for x in tr.find_all("td")]
        if len(tds) < 5:
            continue

        ticker = tds[0].upper()
        company = tds[1]
        ann = tds[2]
        split = tds[3]
        ratio = tds[4]
        exchange = tds[5] if len(tds) > 5 else ""

        if not valid_stock(ticker, company, exchange):
            continue

        rows.append([
            ticker, company, ann, split, ratio, exchange,
            "N/A","N/A","N/A","Benzinga"
        ])

    return rows

async def fetch_splits():
    async with httpx.AsyncClient() as c:
        r = await c.get("https://www.benzinga.com/calendars/stock-splits")
        return parse(r.text)

async def price_now(c, ticker):
    try:
        r = await c.get("https://api.twelvedata.com/price",
            params={"symbol": ticker, "apikey": TWELVE_API_KEY})
        p = r.json().get("price")
        if p:
            return p
    except:
        pass

    try:
        r = await c.get(f"https://financialmodelingprep.com/api/v3/quote/{ticker}",
            params={"apikey": FMP_API_KEY})
        data = r.json()
        if data:
            return data[0].get("price","N/A")
    except:
        pass

    return "N/A"

async def history(c, ticker, ann):
    try:
        ann = datetime.strptime(ann, "%Y-%m-%d")
    except:
        return "N/A","N/A"

    try:
        r = await c.get(
            f"https://financialmodelingprep.com/api/v3/historical-price-full/{ticker}",
            params={"apikey": FMP_API_KEY}
        )
        hist = r.json().get("historical", [])

        if not hist:
            return "N/A","N/A"

        close_pre = hist[0]["close"]
        close_14d = hist[min(14, len(hist)-1)]["close"]

        return close_14d, close_pre

    except:
        return "N/A","N/A"

async def enrich(rows):
    async with httpx.AsyncClient() as c:
        for i, r in enumerate(rows):
            ticker = r[0]

            p = await price_now(c, ticker)
            h14, hp = await history(c, ticker, r[2])

            r[6] = h14
            r[7] = hp
            r[8] = p

            logging.info(f"{i+1}/{len(rows)} {ticker} price={p}")

            await asyncio.sleep(0.5)

    return rows

def update(sheet, rows):
    sheet.clear()
    sheet.update("A1", [HEADERS] + rows)

async def main():
    sheet = connect()

    while True:
        rows = await fetch_splits()
        rows = await enrich(rows)
        update(sheet, rows)

        logging.info("DONE. Sleep 600s")
        await asyncio.sleep(600)

asyncio.run(main())
