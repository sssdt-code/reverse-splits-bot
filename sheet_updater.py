import os
import json
import logging
import asyncio
import re
from datetime import datetime, timedelta, timezone

import httpx
import gspread
from bs4 import BeautifulSoup
from oauth2client.service_account import ServiceAccountCredentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

BENZINGA_URL = "https://www.benzinga.com/calendars/stock-splits"
HEADERS = {
    "User-Agent": os.getenv("SEC_USER_AGENT", "Sergey Zinin sssdt14@gmail.com")
}
POLL_INTERVAL = int(os.getenv("SHEET_POLL_INTERVAL_SECONDS", "1800"))
SHEET_ID = os.getenv("SHEET_ID", "1NQ5VTbZ310X5y53Fxa1LtS_k_mI8cd_5FBhIal0dtHk")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "splits_feed")

MAIN_MARKET_SYMBOLS_URLS = [
    "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt",
]

DATE_FORMATS = (
    "%m/%d/%Y",
    "%Y-%m-%d",
    "%b %d, %Y",
    "%B %d, %Y",
)

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


def parse_any_date(text: str) -> str | None:
    text = " ".join((text or "").strip().split())
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def normalize_ratio(text: str) -> str:
    t = " ".join((text or "").strip().split())
    t = t.replace(":", " for ")
    m = re.search(r"(\d+(?:\.\d+)?)\s+for\s+(\d+(?:\.\d+)?)", t, re.IGNORECASE)
    if m:
        return f"{m.group(1)}-for-{m.group(2)}"
    return (text or "").strip()


def connect_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]

    creds_env = os.getenv("GOOGLE_CREDENTIALS")
    if creds_env:
        creds_dict = json.loads(creds_env)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name("google.json", scope)

    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)


async def load_allowed_symbols() -> set[str]:
    symbols = set()
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
        for url in MAIN_MARKET_SYMBOLS_URLS:
            try:
                r = await client.get(url)
                r.raise_for_status()
                for line in r.text.splitlines()[1:]:
                    if not line.strip() or line.startswith("File Creation Time"):
                        continue
                    parts = line.split("|")
                    if parts:
                        sym = parts[0].strip().upper()
                        if sym:
                            symbols.add(sym)
            except Exception as e:
                logging.warning("Failed loading symbols from %s: %s", url, e)
    logging.info("Loaded %s allowed symbols", len(symbols))
    return symbols


def parse_benzinga_html(html_text: str, allowed_symbols: set[str]) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = [x.strip() for x in text.splitlines() if x.strip()]

    results = []
    i = 0
    while i < len(lines):
        split_date = parse_any_date(lines[i])
        if not split_date:
            i += 1
            continue

        if i + 7 >= len(lines):
            i += 1
            continue

        company = lines[i + 1]
        ticker = lines[i + 2].upper()
        exchange = lines[i + 3].upper()
        ratio = normalize_ratio(lines[i + 4])
        announcement_date = parse_any_date(lines[i + 5]) or lines[i + 5]

        if ticker not in allowed_symbols:
            i += 1
            continue

        if "OTC" in exchange:
            i += 1
            continue

        results.append(
            {
                "ticker": ticker,
                "company": company,
                "announcement_date": announcement_date,
                "split_date": split_date,
                "ratio": ratio,
                "exchange": exchange,
                "source": "Benzinga",
            }
        )

        i += 8

    deduped = []
    seen = set()
    for item in results:
        key = (item["ticker"], item["split_date"], item["ratio"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    deduped.sort(key=lambda x: (x["split_date"], x["ticker"]))
    return deduped


async def fetch_upcoming_splits(allowed_symbols: set[str]) -> list[dict]:
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25) as client:
        r = await client.get(BENZINGA_URL)
        r.raise_for_status()
        items = parse_benzinga_html(r.text, allowed_symbols)

    today = datetime.now().strftime("%Y-%m-%d")
    return [x for x in items if x["split_date"] >= today]


async def get_price_now(ticker: str) -> str:
    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    params = {"symbols": ticker}

    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()

        result = data.get("quoteResponse", {}).get("result", [])
        if not result:
            return "N/A"

        price = result[0].get("regularMarketPrice")
        if price is None:
            return "N/A"

        return f"{price:.2f}"
    except Exception as e:
        logging.warning("Price now fetch failed for %s: %s", ticker, e)
        return "N/A"


async def get_history_prices(ticker: str, ref_date_str: str) -> tuple[str, str]:
    if not ref_date_str or ref_date_str == "N/A":
        return "N/A", "N/A"

    try:
        ref_dt = datetime.strptime(ref_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return "N/A", "N/A"

    start_dt = ref_dt - timedelta(days=30)
    end_dt = ref_dt + timedelta(days=2)

    period1 = int(start_dt.timestamp())
    period2 = int(end_dt.timestamp())

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {
        "period1": str(period1),
        "period2": str(period2),
        "interval": "1d",
        "includePrePost": "false",
        "events": "div,splits",
    }

    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()

        result = data.get("chart", {}).get("result", [])
        if not result:
            return "N/A", "N/A"

        timestamps = result[0].get("timestamp", [])
        quote = result[0].get("indicators", {}).get("quote", [{}])[0]
        closes = quote.get("close", [])

        rows = []
        for ts, close in zip(timestamps, closes):
            if close is None:
                continue
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            rows.append((dt, float(close)))

        if not rows:
            return "N/A", "N/A"

        target_14d = ref_dt - timedelta(days=14)

        close_pre = "N/A"
        pre_candidates = [price for dt, price in rows if dt < ref_dt]
        if pre_candidates:
            close_pre = f"{pre_candidates[-1]:.2f}"

        close_14d = "N/A"
        hist_candidates = [(dt, price) for dt, price in rows if dt <= target_14d]
        if hist_candidates:
            close_14d = f"{hist_candidates[-1][1]:.2f}"
        else:
            earlier = [(dt, price) for dt, price in rows if dt < ref_dt]
            if earlier:
                close_14d = f"{earlier[0][1]:.2f}"

        return close_14d, close_pre

    except Exception as e:
        logging.warning("History fetch failed for %s: %s", ticker, e)
        return "N/A", "N/A"


async def build_rows(items: list[dict]) -> list[list[str]]:
    rows = []

    for index, item in enumerate(items, start=1):
        ticker = item["ticker"]

        close_14d, close_pre = await get_history_prices(ticker, item["announcement_date"])
        await asyncio.sleep(1.5)

        price_now = await get_price_now(ticker)
        await asyncio.sleep(1.5)

        rows.append([
            item["ticker"],
            item["company"],
            item["announcement_date"],
            item["split_date"],
            item["ratio"],
            item["exchange"],
            close_14d,
            close_pre,
            price_now,
            item["source"],
        ])

        logging.info(
            "Prepared row %s/%s for %s | close_14d=%s close_pre=%s price_now=%s",
            index, len(items), ticker, close_14d, close_pre, price_now
        )

    return rows


def rewrite_sheet(sheet, rows: list[list[str]]) -> None:
    values = [OUTPUT_HEADERS] + rows if rows else [OUTPUT_HEADERS]
    sheet.clear()
    sheet.update("A1", values)
    logging.info("Sheet updated with %s rows", len(rows))


async def main():
    sheet = connect_sheet()
    allowed_symbols = await load_allowed_symbols()

    while True:
        try:
            items = await fetch_upcoming_splits(allowed_symbols)
            logging.info("Upcoming splits fetched: %s", len(items))
            rows = await build_rows(items)
            rewrite_sheet(sheet, rows)
        except Exception as e:
            logging.exception("Updater error: %s", e)

        logging.info("Sleeping %s seconds...", POLL_INTERVAL)
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
