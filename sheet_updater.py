import os
import json
import logging
import asyncio
import re
from datetime import datetime, timedelta

import httpx
import gspread
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

TWELVE_API_KEY = os.getenv("TWELVE_API_KEY", "").strip()
SHEET_ID = os.getenv("SHEET_ID", "").strip()
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "splits_feed").strip()
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS", "").strip()
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
    "% vs Close -14D",
    "% vs Close Pre",
    "Source",
]

DATE_FORMATS = (
    "%m/%d/%Y",
    "%Y-%m-%d",
    "%b %d, %Y",
    "%B %d, %Y",
)

VALID_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "OTC"}
EXCLUDED_EXCHANGES = {"OTC"}
BAD_COMPANY_WORDS = {"ETF", "DEFIANCE"}

TICKER_OVERRIDES = {
    "JIADE": "JDZG",
    "TUNIU": "TOUR",
    "SANRIO": "SNROF",
}


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


def is_good_stock(ticker: str, company: str, exchange: str) -> bool:
    if exchange in EXCLUDED_EXCHANGES:
        return False
    upper_name = (company or "").upper()
    if any(word in upper_name for word in BAD_COMPANY_WORDS):
        return False
    return True


def parse_benzinga_html(html_text: str):
    soup = BeautifulSoup(html_text, "html.parser")

    table_rows = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) >= 6:
            table_rows.append(cells)

    parsed = []
    for cells in table_rows:
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
            if c in VALID_EXCHANGES:
                exchange = c
                break

        ticker = ""
        for c in upper_cells:
            if c in VALID_EXCHANGES:
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

        if ticker in TICKER_OVERRIDES:
            ticker = TICKER_OVERRIDES[ticker]

        if not is_good_stock(ticker, company, exchange):
            continue

        parsed.append([
            ticker,            # A
            company,           # B
            announcement_date, # C
            split_date,        # D
            ratio,             # E
            exchange,          # F
            "",                # G Close -14D
            "",                # H Close Pre
            "",                # I Price Now
            "",                # J % vs Close -14D
            "",                # K % vs Close Pre
            "Benzinga",        # L Source
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


async def fetch_splits():
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        r = await client.get(
            BENZINGA_URL,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        r.raise_for_status()
        rows = parse_benzinga_html(r.text)
        logging.info("Parsed splits: %s", len(rows))
        return rows


async def twelve_series(client: httpx.AsyncClient, ticker: str, announcement_date: str):
    try:
        ann_dt = datetime.strptime(announcement_date, "%Y-%m-%d") if announcement_date else datetime.utcnow()

        start = (ann_dt - timedelta(days=40)).date()
        end = datetime.utcnow().date()

        r = await client.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": ticker,
                "interval": "1day",
                "start_date": str(start),
                "end_date": str(end),
                "outputsize": 100,
                "apikey": TWELVE_API_KEY,
            },
            timeout=20,
        )

        data = r.json()

        if "values" not in data:
            logging.warning("%s: no values in response -> %s", ticker, data)
            return None, None, None

        values = data.get("values", [])
        if not values:
            return None, None, None

        parsed = []
        for row in values:
            dt_raw = row.get("datetime") or row.get("date")
            close = row.get("close")
            if not dt_raw or close in (None, "", "null"):
                continue
            try:
                parsed.append((datetime.strptime(dt_raw[:10], "%Y-%m-%d"), float(close)))
            except Exception:
                continue

        if not parsed:
            return None, None, None

        parsed.sort(key=lambda x: x[0])

        price_now = parsed[-1][1]

        close_pre = None
        before = [p for d, p in parsed if d < ann_dt]
        if before:
            close_pre = before[-1]

        target = ann_dt - timedelta(days=14)
        hist = [p for d, p in parsed if d <= target]

        close_14d = None
        if hist:
            close_14d = hist[-1]
        elif before:
            close_14d = before[0]

        return price_now, close_pre, close_14d

    except Exception as e:
        logging.warning("%s: series error -> %s", ticker, e)
        return None, None, None


async def enrich_rows(rows):
    async with httpx.AsyncClient() as client:
        for i, row in enumerate(rows, start=1):
            ticker = row[0]
            announcement_date = row[2]

            price_now, close_pre, close_14d = await twelve_series(client, ticker, announcement_date)

            row[6] = round(close_14d, 4) if close_14d is not None else ""
            row[7] = round(close_pre, 4) if close_pre is not None else ""
            row[8] = round(price_now, 4) if price_now is not None else ""

            sheet_row = i + 1

            row[9] = f'=IF(OR(G{sheet_row}="";I{sheet_row}="";G{sheet_row}=0);"";I{sheet_row}/G{sheet_row}-1)'
            row[10] = f'=IF(OR(H{sheet_row}="";I{sheet_row}="";H{sheet_row}=0);"";I{sheet_row}/H{sheet_row}-1)'

            logging.info(
                "Prepared row %s/%s for %s | close_14d=%s close_pre=%s price_now=%s",
                i, len(rows), ticker, row[6], row[7], row[8]
            )

            await asyncio.sleep(0.8)

    return rows


def apply_formatting(sheet, row_count: int):
    last_row = max(row_count, 2)

    try:
        sheet.format(
            "A1:L1",
            {
                "textFormat": {"bold": True},
                "horizontalAlignment": "CENTER"
            }
        )

        sheet.format(
            f"G2:I{last_row}",
            {
                "numberFormat": {
                    "type": "NUMBER",
                    "pattern": "0.0000"
                }
            }
        )

        sheet.format(
            f"J2:K{last_row}",
            {
                "numberFormat": {
                    "type": "PERCENT",
                    "pattern": "0.00%"
                }
            }
        )
    except Exception as e:
        logging.warning("Formatting skipped: %s", e)


def rewrite_sheet(sheet, rows):
    values = [HEADERS] + rows
    sheet.clear()
    sheet.update(
        range_name=f"A1:L{len(values)}",
        values=values,
        value_input_option="USER_ENTERED"
    )
    apply_formatting(sheet, len(values))
    logging.info("Sheet updated with %s rows", len(rows))


async def main_loop():
    sheet = get_sheet()

    while True:
        try:
            rows = await fetch_splits()

            if rows:
                rows = await enrich_rows(rows)
                rewrite_sheet(sheet, rows)
            else:
                logging.warning("No rows parsed; keeping previous sheet untouched")

        except Exception as e:
            logging.exception("Updater error: %s", e)

        logging.info("Sleeping %s seconds...", POLL_INTERVAL)
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main_loop())
