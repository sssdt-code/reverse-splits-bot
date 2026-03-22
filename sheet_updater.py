import os
import json
import time
import httpx
import logging
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO)

TWELVE_API_KEY = os.getenv("TWELVE_API_KEY", "").strip()
SHEET_ID = os.getenv("SHEET_ID", "").strip()
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "splits_feed").strip()
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS", "")

HEADERS = [
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


def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)


# 🚀 Новый способ — API вместо HTML
def fetch_splits():
    url = "https://api.benzinga.com/api/v2.1/calendar/splits"

    params = {
        "token": "demo",  # работает без ключа
        "parameters[date_from]": "2026-01-01",
    }

    try:
        r = httpx.get(url, params=params, timeout=20)
        data = r.json()

        rows = []

        for item in data:
            ticker = item.get("ticker", "").upper()
            company = item.get("company_name", "")
            split_date = item.get("execution_date", "")
            ann = item.get("announcement_date", "")
            ratio = item.get("ratio", "")
            exchange = item.get("exchange", "").upper()

            if not ticker or not split_date:
                continue

            if "OTC" in exchange:
                continue

            if len(ticker) > 5:
                continue

            rows.append({
                "ticker": ticker,
                "company": company,
                "ann": ann,
                "split": split_date,
                "ratio": ratio,
                "exchange": exchange,
            })

        # убираем дубли
        unique = {}
        for r in rows:
            unique[(r["ticker"], r["split"])] = r

        return list(unique.values())

    except Exception as e:
        logging.error(f"fetch_splits error: {e}")
        return []


def get_price(symbol):
    try:
        url = f"https://api.twelvedata.com/price?symbol={symbol}&apikey={TWELVE_API_KEY}"
        r = httpx.get(url, timeout=10)
        data = r.json()
        if "price" in data:
            return float(data["price"])
    except:
        pass
    return None


def get_history(symbol, days_back):
    try:
        end = datetime.utcnow()
        start = end - timedelta(days=days_back + 10)

        url = (
            "https://api.twelvedata.com/time_series"
            f"?symbol={symbol}"
            "&interval=1day"
            f"&start_date={start.date()}"
            f"&end_date={end.date()}"
            f"&apikey={TWELVE_API_KEY}"
        )

        r = httpx.get(url, timeout=15)
        data = r.json()
        values = data.get("values", [])

        if not values:
            return None

        if len(values) > days_back:
            return float(values[days_back]["close"])

        return float(values[-1]["close"])

    except:
        return None


def build_rows(data):
    final = []

    for i, row in enumerate(data, start=1):
        ticker = row["ticker"]

        price_now = get_price(ticker)
        close_pre = get_history(ticker, 1)
        close_14d = get_history(ticker, 14)

        logging.info(
            "%s/%s %s price=%s",
            i, len(data), ticker, price_now
        )

        final.append([
            ticker,
            row["company"],
            row["ann"],
            row["split"],
            row["ratio"],
            row["exchange"],
            close_14d if close_14d else "N/A",
            close_pre if close_pre else "N/A",
            price_now if price_now else "N/A",
            "Benzinga",
        ])

        time.sleep(1)

    return final


def main():
    sheet = get_sheet()

    while True:
        splits = fetch_splits()
        logging.info(f"Fetched {len(splits)} splits")

        rows = build_rows(splits)

        if rows:
            sheet.clear()
            sheet.update("A1", [HEADERS] + rows)
            logging.info(f"Updated sheet with {len(rows)} rows")
        else:
            logging.warning("No rows parsed")

        logging.info("Sleeping 600 seconds...")
        time.sleep(600)


if __name__ == "__main__":
    main()
