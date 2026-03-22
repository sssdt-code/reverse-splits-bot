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
SHEET_ID = os.getenv("SHEET_ID", "").strip()
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "splits_feed").strip()
POLL_INTERVAL = int(os.getenv("SHEET_POLL_INTERVAL_SECONDS", "600"))

BENZINGA_URL = "https://www.benzinga.com/calendars/stock-splits"

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

EXCLUDED_EXCHANGES = {"OTC"}

# Явные правки для случаев, где Benzinga местами путает компанию и тикер
TICKER_OVERRIDES = {
    "JIADE": "JDZG",
    "TUNIU": "TOUR",
    "SANRIO": "SNROF",
}

PREFERRED_US_EXCHANGES = {
    "NASDAQ", "NYSE", "AMEX", "ARCA", "BATS"
}

DATE_FORMATS = (
    "%m/%d/%Y",
    "%Y-%m-%d",
    "%b %d, %Y",
    "%B %d, %Y",
)


def connect_sheet():
    creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)


def normalize_date(value: str) -> str:
    value = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return value


def is_date_like(value: str) -> bool:
    value = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(value, fmt)
            return True
        except Exception:
            pass
    return False


def looks_like_ratio(value: str) -> bool:
    v = (value or "").strip().lower().replace(" ", "")
    return ("for" in v or ":" in v) and any(ch.isdigit() for ch in v)


def normalize_ratio(value: str) -> str:
    v = (value or "").strip()
    v = v.replace(":", "-for-")
    v = re.sub(r"\s+[Ff]or\s+", "-for-", v)
    v = re.sub(r"\s+", "", v)
    return v


def looks_like_ticker(value: str) -> bool:
    v = (value or "").strip().upper()
    return 1 <= len(v) <= 6 and v.isalpha()


def parse_benzinga_table(html_text: str):
    soup = BeautifulSoup(html_text, "html.parser")

    rows = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) >= 6:
            rows.append(cells)

    parsed = []
    for cells in rows:
        joined = " | ".join(cells).lower()
        if "split date" in joined and "ticker" in joined:
            continue

        date_cells = [normalize_date(c) for c in cells if is_date_like(c)]
        if not date_cells:
            continue

        split_date = date_cells[0]
        announcement_date = date_cells[1] if len(date_cells) > 1 else ""

        ratio_candidates = [c for c in cells if looks_like_ratio(c)]
        if not ratio_candidates:
            continue
        ratio = normalize_ratio(ratio_candidates[0])

        upper_cells = [c.strip().upper() for c in cells]

        exchange = ""
        for c in upper_cells:
            if c in {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "OTC"}:
                exchange = c
                break

        ticker = ""
        for c in upper_cells:
            if c in {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "OTC"}:
                continue
            if looks_like_ticker(c):
                ticker = c
                break

        company = ""
        company_candidates = []
        for c in cells:
            cu = c.strip().upper()
            if cu == ticker or cu == exchange:
                continue
            if is_date_like(c) or looks_like_ratio(c):
                continue
            if len(c.strip()) > 2:
                company_candidates.append(c.strip())

        if company_candidates:
            company = company_candidates[0]

        if not ticker or not company or not split_date or not ratio:
            continue

        # Исправляем известные кривые строки
        if ticker in TICKER_OVERRIDES:
            ticker = TICKER_OVERRIDES[ticker]

        if exchange in EXCLUDED_EXCHANGES:
            continue

        parsed.append([
            ticker,
            company,
            announcement_date,
            split_date,
            ratio,
            exchange,
            "N/A",
            "N/A",
            "N/A",
            "Benzinga",
        ])

    dedup = []
    seen = set()
    for row in parsed:
        key = (row[0], row[3], row[4], row[5])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(row)

    dedup.sort(key=lambda x: (x[3], x[0]))
    return dedup


async def get_price_now(client: httpx.AsyncClient, ticker: str) -> str:
    if not TWELVE_API_KEY:
        return "N/A"

    try:
        r = await client.get(
            "https://api.twelvedata.com/price",
            params={"symbol": ticker, "apikey": TWELVE_API_KEY},
            timeout=20,
        )
        data = r.json()
        price = data.get("price")
        if price is None:
            return "N/A"
        return str(price)
    except Exception as e:
        logging.warning("Price fetch failed for %s: %s", ticker, e)
        return "N/A"


async def get_history_prices(client: httpx.AsyncClient, ticker: str, announcement_date: str):
    if not TWELVE_API_KEY or not announcement_date:
        return "N/A", "N/A"

    try:
        ann_dt = datetime.strptime(announcement_date, "%Y-%m-%d")
    except Exception:
        return "N/A", "N/A"

    # Берем запас по дням, чтобы хватило на 14D даже с выходными
    start_date = (ann_dt - timedelta(days=45)).strftime("%Y-%m-%d")
    end_date = ann_dt.strftime("%Y-%m-%d")

    try:
        r = await client.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": ticker,
                "interval": "1day",
                "start_date": start_date,
                "end_date": end_date,
                "outputsize": 60,
                "apikey": TWELVE_API_KEY,
            },
            timeout=25,
        )
        data = r.json()
        values = data.get("values", [])
        if not values:
            return "N/A", "N/A"

        parsed = []
        for row in values:
            dt_raw = row.get("datetime") or row.get("date")
            close = row.get("close")
            if not dt_raw or close is None:
                continue
            try:
                dt = datetime.strptime(dt_raw[:10], "%Y-%m-%d")
                parsed.append((dt, float(close)))
            except Exception:
                continue

        if not parsed:
            return "N/A", "N/A"

        # Twelve обычно отдает от новых к старым, сортируем по возрастанию
        parsed.sort(key=lambda x: x[0])

        close_pre = "N/A"
        pre_candidates = [price for dt, price in parsed if dt < ann_dt]
        if pre_candidates:
            close_pre = f"{pre_candidates[-1]:.4f}"

        target_14d = ann_dt - timedelta(days=14)
        close_14d = "N/A"
        hist_candidates = [price for dt, price in parsed if dt <= target_14d]
        if hist_candidates:
            close_14d = f"{hist_candidates[-1]:.4f}"
        elif pre_candidates:
            close_14d = f"{pre_candidates[0]:.4f}"

        return close_14d, close_pre
    except Exception as e:
        logging.warning("History fetch failed for %s: %s", ticker, e)
        return "N/A", "N/A"


async def enrich_rows(rows):
    async with httpx.AsyncClient() as client:
        for i, row in enumerate(rows, start=1):
            ticker = row[0]
            announcement_date = row[2]

            close_14d, close_pre = await get_history_prices(client, ticker, announcement_date)
            await asyncio.sleep(0.5)

            price_now = await get_price_now(client, ticker)
            await asyncio.sleep(0.75)

            row[6] = close_14d
            row[7] = close_pre
            row[8] = price_now

            logging.info(
                "Prepared row %s/%s for %s | close_14d=%s close_pre=%s price_now=%s",
                i, len(rows), ticker, close_14d, close_pre, price_now
            )

    return rows


async def fetch_splits():
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        r = await client.get(
            BENZINGA_URL,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        r.raise_for_status()
        return parse_benzinga_table(r.text)


def rewrite_sheet(sheet, rows):
    values = [HEADERS] + rows
    sheet.clear()
    sheet.update("A1", values)
    logging.info("Sheet updated with %s rows", len(rows))


async def main_loop():
    sheet = connect_sheet()

    while True:
        try:
            rows = await fetch_splits()
            logging.info("Parsed splits: %s", len(rows))

            if rows:
                rows = await enrich_rows(rows)
                rewrite_sheet(sheet, rows)
            else:
                logging.warning("No splits parsed; keeping previous sheet untouched")

        except Exception as e:
            logging.exception("Updater error: %s", e)

        logging.info("Sleeping %s seconds...", POLL_INTERVAL)
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main_loop())
