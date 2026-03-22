import os
import httpx
import asyncio
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials

GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")
SHEET_ID = os.getenv("SHEET_ID")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Split Feed")
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")
FMP_API_KEY = os.getenv("FMP_API_KEY")

HEADERS = ["Ticker","Company","Announcement Date","Split Date","Ratio","Exchange","Close -14D","Close Pre","Price Now","Source"]

def connect_sheet():
    creds_dict = eval(GOOGLE_CREDENTIALS)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)

async def fetch_splits():
    url = "https://api.benzinga.com/api/v2/calendar/splits"
    params = {
        "token": "demo",
        "parameters[date_from]": (datetime.utcnow() - timedelta(days=10)).strftime("%Y-%m-%d"),
        "parameters[date_to]": (datetime.utcnow() + timedelta(days=40)).strftime("%Y-%m-%d")
    }

    async with httpx.AsyncClient() as client:
        r = await client.get(url, params=params, timeout=20)
        data = r.json()

    rows = []

    for item in data:
        ticker = item.get("ticker", "").strip()
        exchange = item.get("exchange", "")

        # ❌ убираем OTC
        if exchange == "OTC":
            continue

        rows.append({
            "ticker": ticker,
            "company": item.get("name", ""),
            "ann": item.get("announced_date", ""),
            "split": item.get("execution_date", ""),
            "ratio": item.get("split_ratio", ""),
            "exchange": exchange,
            "source": "Benzinga"
        })

    # ❌ убираем дубли
    unique = {}
    for r in rows:
        unique[r["ticker"]] = r

    return list(unique.values())

async def get_price(client, ticker):
    # Twelve
    if TWELVE_API_KEY:
        try:
            r = await client.get(
                "https://api.twelvedata.com/price",
                params={"symbol": ticker, "apikey": TWELVE_API_KEY},
                timeout=10
            )
            data = r.json()
            price = data.get("price")
            if price and price != "null":
                return price
        except:
            pass

    # FMP fallback
    if FMP_API_KEY:
        try:
            r = await client.get(
                f"https://financialmodelingprep.com/api/v3/historical-price-full/{ticker}",
                params={"apikey": FMP_API_KEY},
                timeout=10
            )
            data = r.json()

            if "historical" in data and data["historical"]:
                return data["historical"][0]["close"]
        except:
            pass

    return "N/A"

async def get_history(client, ticker):
    if not FMP_API_KEY:
        return "N/A", "N/A"

    try:
        r = await client.get(
            f"https://financialmodelingprep.com/api/v3/historical-price-full/{ticker}",
            params={"apikey": FMP_API_KEY},
            timeout=10
        )
        data = r.json()

        if "historical" not in data or not data["historical"]:
            return "N/A", "N/A"

        hist = data["historical"]

        close_pre = hist[0]["close"] if len(hist) > 0 else "N/A"
        close_14d = hist[13]["close"] if len(hist) > 13 else "N/A"

        return close_14d, close_pre

    except:
        return "N/A", "N/A"

async def main():
    sheet = connect_sheet()
    splits = await fetch_splits()

    async with httpx.AsyncClient() as client:
        rows = [HEADERS]

        for s in splits:
            ticker = s["ticker"]

            price = await get_price(client, ticker)
            close_14d, close_pre = await get_history(client, ticker)

            rows.append([
                ticker,
                s["company"],
                s["ann"],
                s["split"],
                s["ratio"],
                s["exchange"],
                close_14d,
                close_pre,
                price,
                s["source"]
            ])

    sheet.clear()
    sheet.update("A1", rows)

if __name__ == "__main__":
    asyncio.run(main())
