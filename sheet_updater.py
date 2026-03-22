import os
import json
import time
import httpx
import logging
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
from bs4 import BeautifulSoup

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


def fetch_splits():
    url = "https://www.benzinga.com/calendars/stock-splits"
    r = httpx.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    rows = []

    if not table:
        return rows

    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue

        ticker = tds[0].get_text(strip=True).upper()
        company = tds[1].get_text(strip=True)
        ann = tds[2].get_text(strip=True)
        split = tds[3].get_text(strip=True)
        ratio = tds[4].get_text(strip=True)
        exchange = tds[5].get_text(strip=True).upper()

        if "OTC" in exchange:
            continue

        bad_words = ["ETF", "DEFIANCE", "2X", "3X"]
        if any(word in company.upper() for word in bad_words):
            continue

        if len(ticker) > 5:
            continue

        rows.append({
            "ticker": ticker,
            "company": company,
            "ann": ann,
            "split": split,
            "ratio": ratio,
            "exchange": exchange,
        })

    unique = {}
    for row in rows:
        unique[(row["ticker"], row["split"])] = row

    return list(unique.values())


def get_price(symbol):
    try:
        url = f"https://api.twelvedata.com/price?symbol={symbol}&apikey={TWELVE_API_KEY}"
        r = httpx.get(url, timeout=10)
        data = r.json()
        if "price" in data and data["price"] not in (None, "null", ""):
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
            "%s/%s %s price=%s close_pre=%s close_14d=%s",
            i, len(data), ticker, price_now, close_pre, close_14d
        )

        final.append([
            ticker,
            row["company"],
            row["ann"],
            row["split"],
            row["ratio"],
            row["exchange"],
            close_14d if close_14d is not None else "N/A",
            close_pre if close_pre is not None else "N/A",
            price_now if price_now is not None else "N/A",
            "Benzinga",
        ])

        time.sleep(1)

    return final


def main():
    sheet = get_sheet()

    while True:
        splits = fetch_splits()
        rows = build_rows(splits)

        if rows:
            sheet.clear()
            sheet.update("A1", [HEADERS] + rows)
            logging.info("Sheet updated with %s rows", len(rows))
        else:
            logging.warning("No rows parsed; sheet left unchanged")

        logging.info("Sleeping 600 seconds...")
        time.sleep(600)


if __name__ == "__main__":
    main()
