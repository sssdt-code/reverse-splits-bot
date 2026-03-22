import os
import json
import logging
import asyncio
import re
from datetime import datetime

import httpx
import gspread
from bs4 import BeautifulSoup
from oauth2client.service_account import ServiceAccountCredentials

logging.basicConfig(level=logging.INFO)

TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")
SHEET_ID = os.getenv("SHEET_ID")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME")

URL = "https://www.benzinga.com/calendars/stock-splits"

HEADERS = [
    "Ticker","Company","Announcement Date","Split Date","Ratio","Exchange",
    "Close -14D","Close Pre","Price Now","Source"
]

def connect():
    creds = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    c = ServiceAccountCredentials.from_json_keyfile_dict(creds, scope)
    return gspread.authorize(c).open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)

def parse(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    
    out = []
    i = 0
    while i < len(lines):
        try:
            date = datetime.strptime(lines[i], "%m/%d/%Y").strftime("%Y-%m-%d")
            out.append([
                lines[i+2],
                lines[i+1],
                lines[i+5],
                date,
                lines[i+4],
                lines[i+3]
            ])
            i += 8
        except:
            i += 1
    return out

async def price(client, ticker):
    try:
        r = await client.get("https://api.twelvedata.com/price", params={
            "symbol": ticker,
            "apikey": TWELVE_API_KEY
        })
        return r.json().get("price","N/A")
    except:
        return "N/A"

async def loop():
    sheet = connect()
    async with httpx.AsyncClient() as client:
        while True:
            try:
                r = await client.get(URL)
                data = parse(r.text)

                rows = []
                for i,x in enumerate(data):
                    p = await price(client, x[0])
                    rows.append(x + ["N/A","N/A",p,"Benzinga"])
                    print(i, x[0], p)
                    await asyncio.sleep(1)

                sheet.clear()
                sheet.update("A1", [HEADERS] + rows)

            except Exception as e:
                print("ERROR:", e)

            print("sleeping...")
            await asyncio.sleep(600)  # каждые 10 минут

asyncio.run(loop())
