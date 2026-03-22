import os
import time
import httpx
import logging
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)

TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")
SHEET_ID = os.getenv("SHEET_ID")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME")


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
    creds = Credentials.from_service_account_file(
        "google.json",
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)


def fetch_splits():
    url = "https://www.benzinga.com/calendars/stock-splits"
    r = httpx.get(url, timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")

    rows = []
    table = soup.find("table")

    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue

        ticker = tds[0].text.strip()
        company = tds[1].text.strip()
        ann = tds[2].text.strip()
        split = tds[3].text.strip()
        ratio = tds[4].text.strip()
        exchange = tds[5].text.strip()

        # ❌ убираем OTC
        if "OTC" in exchange:
            continue

        rows.append({
            "ticker": ticker,
            "company": company,
            "ann": ann,
            "split": split,
            "ratio": ratio,
            "exchange": exchange,
        })

    return rows


def get_price(symbol):
    try:
        url = f"https://api.twelvedata.com/price?symbol={symbol}&apikey={TWELVE_API_KEY}"
        r = httpx.get(url, timeout=10)
        data = r.json()
        return float(data.get("price")) if "price" in data else None
    except:
        return None


def get_history(symbol, days_back):
    try:
        end = datetime.utcnow()
        start = end - timedelta(days=days_back + 5)

        url = (
            "https://api.twelvedata.com/time_series"
            f"?symbol={symbol}&interval=1day"
            f"&start_date={start.date()}&end_date={end.date()}"
            f"&apikey={TWELVE_API_KEY}"
        )

        r = httpx.get(url, timeout=15)
        data = r.json()

        values = data.get("values")
        if not values:
            return None

        # берем нужный день назад
        if len(values) > days_back:
            return float(values[days_back]["close"])

        return None

    except:
        return None


def build_rows(data):
    final = []

    for i, row in enumerate(data):
        ticker = row["ticker"]

        price_now = get_price(ticker)
        close_pre = get_history(ticker, 1)
        close_14d = get_history(ticker, 14)

        logging.info(f"{i+1}/{len(data)} {ticker} price={price_now}")

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

        time.sleep(1)  # чтобы не словить лимиты

    return final


def main():
    sheet = get_sheet()
    splits = fetch_splits()
    rows = build_rows(splits)

    sheet.update("A1", [HEADERS] + rows)
    logging.info("DONE. Sleep 600s")

    time.sleep(600)


if __name__ == "__main__":
    while True:
        main()
