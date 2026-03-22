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

TWELVE_API_KEY = os.getenv("TWELVE_API_KEY", "")
SHEET_ID = os.getenv("SHEET_ID")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME")

BENZINGA_URL = "https://www.benzinga.com/calendars/stock-splits"

OUTPUT_HEADERS = [
    "Ticker",
    "Company",
    "Announcement Date",
    "Split Date",
    "Ratio",
    "Exchange",
    "Close -14D",
    "Close Pre",
    "Price Now",
    "Source",
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

def parse_any_date(text):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).strftime("%Y-%m-%d")
        except:
            continue
    return None

def normalize_ratio(text):
    m = re.search(r"(\\d+)\\s+for\\s+(\\d+)", text)
    return f"{m.group(1)}-for-{m.group(2)}" if m else text

def parse_splits(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\\n")
    lines = [x.strip() for x in text.splitlines() if x.strip()]

    res = []
    i = 0
    while i < len(lines):
        date = parse_any_date(lines[i])
        if date and i + 5 < len(lines):
            res.append({
                "split_date": date,
                "company": lines[i+1],
                "ticker": lines[i+2],
                "exchange": lines[i+3],
                "ratio": normalize_ratio(lines[i+4]),
                "announcement_date": parse_any_date(lines[i+5]) or lines[i+5]
            })
            i += 8
        else:
            i += 1
    return res

async def get_price_data(client, ticker, date):
    try:
        # Price now
        r = await client.get("https://api.twelvedata.com/price", params={
            "symbol": ticker,
            "apikey": TWELVE_API_KEY
        })
        price_now = r.json().get("price", "N/A")

        # History
        r = await client.get("https://api.twelvedata.com/time_series", params={
            "symbol": ticker,
            "interval": "1day",
            "outputsize": 30,
            "apikey": TWELVE_API_KEY
        })
        data = r.json().get("values", [])

        close_pre = data[1]["close"] if len(data) > 1 else "N/A"
        close_14d = data[14]["close"] if len(data) > 14 else "N/A"

        return close_14d, close_pre, price_now

    except:
        return "N/A", "N/A", "N/A"

async def main():
    sheet = connect_sheet()

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(BENZINGA_URL)
        splits = parse_splits(r.text)

        rows = []

        for i, s in enumerate(splits):
            close_14d, close_pre, price_now = await get_price_data(client, s["ticker"], s["announcement_date"])

            rows.append([
                s["ticker"],
                s["company"],
                s["announcement_date"],
                s["split_date"],
                s["ratio"],
                s["exchange"],
                close_14d,
                close_pre,
                price_now,
                "Benzinga"
            ])

            logging.info(f"{i+1}/{len(splits)} {s['ticker']} {price_now}")
            await asyncio.sleep(1)

        sheet.clear()
        sheet.update("A1", [OUTPUT_HEADERS] + rows)

if __name__ == "__main__":
    asyncio.run(main())
