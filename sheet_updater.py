import os
import json
import logging
import asyncio
from datetime import datetime

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
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return value


def is_date_like(value: str) -> bool:
    value = (value or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
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
    v = (value or "").strip().replace(":", "-for-").replace(" for ", "-for-").replace(" For ", "-for-")
    v = v.replace(" ", "")
    return v


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

        split_date = None
        announcement_date = None
        ticker = None
        company = None
        exchange = None
        ratio = None

        for cell in cells:
            if not split_date and is_date_like(cell):
                split_date = normalize_date(cell)
                continue

        date_cells = [normalize_date(c) for c in cells if is_date_like(c)]
        if len(date_cells) >= 2:
            split_date = date_cells[0]
            announcement_date = date_cells[1]
        elif len(date_cells) == 1:
            split_date = date_cells[0]
            announcement_date = ""

        ratio_candidates = [c for c in cells if looks_like_ratio(c)]
        if ratio_candidates:
            ratio = normalize_ratio(ratio_candidates[0])

        upper_cells = [c.strip().upper() for c in cells]
        for c in upper_cells:
            if 1 <= len(c) <= 6 and c.isupper() and c.isalpha():
                if c not in {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "OTC"}:
                    ticker = c
                    break

        for c in upper_cells:
            if c in {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "OTC"}:
                exchange = c
                break

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

        if ticker and company and split_date and ratio:
            parsed.append([
                ticker,
                company,
                announcement_date or "",
                split_date,
                ratio,
                exchange or "",
                "N/A",
                "N/A",
                "N/A",
                "Benzinga",
            ])

    dedup = []
    seen = set()
    for row in parsed:
        key = (row[0], row[3], row[4])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(row)

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


async def enrich_prices(rows):
    async with httpx.AsyncClient() as client:
        for i, row in enumerate(rows, start=1):
            ticker = row[0]
            row[8] = await get_price_now(client, ticker)
            logging.info("Prepared row %s/%s for %s | price_now=%s", i, len(rows), ticker, row[8])
            await asyncio.sleep(1.0)
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
                rows = await enrich_prices(rows)
                rewrite_sheet(sheet, rows)
            else:
                logging.warning("No splits parsed; keeping previous sheet untouched")

        except Exception as e:
            logging.exception("Updater error: %s", e)

        logging.info("Sleeping %s seconds...", POLL_INTERVAL)
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main_loop())
