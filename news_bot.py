cat > news_bot.py <<'EOF'
import os
import csv
import asyncio
import html
import logging
import re
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import feedparser
import httpx
from bs4 import BeautifulSoup
from telegram import Bot

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("NEWS_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
POLL_INTERVAL = int(os.getenv("NEWS_POLL_INTERVAL_SECONDS", "300"))
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "Sergey Zinin sssdt14@gmail.com")

if not BOT_TOKEN:
    raise RuntimeError("NEWS_BOT_TOKEN is missing")
if not CHAT_ID:
    raise RuntimeError("CHAT_ID is missing")

bot = Bot(token=BOT_TOKEN)

HEADERS = {"User-Agent": SEC_USER_AGENT}

FEEDS = [
    ("GlobeNewswire", "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies"),
    ("PR Newswire", "https://www.prnewswire.com/rss/news-releases-list.rss"),
    ("SEC Current", "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&output=atom"),
]

KEYWORDS = [
    "reverse stock split",
    "reverse split",
    "share consolidation",
    "stock consolidation",
    "ads ratio change",
    "ratio change",
]

RATIO_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:for|:|-for-)\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
WORD_RATIO_RE = re.compile(r"\bone\s+for\s+([a-z\-]+|\d+)\b", re.IGNORECASE)

NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20", "thirty": "30", "forty": "40",
    "fifty": "50", "sixty": "60", "seventy": "70", "eighty": "80",
    "ninety": "90", "hundred": "100",
}

CSV_FILE = "splits.csv"


def clean_text(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    return html.unescape(soup.get_text(" ", strip=True))


def has_signal(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in KEYWORDS)


def parse_number_word(token: str) -> str | None:
    token = token.lower().strip()
    if token.isdigit():
        return token
    if "-" in token:
        parts = token.split("-")
        vals = [NUMBER_WORDS.get(p) for p in parts]
        if all(vals) and len(vals) == 2 and vals[0] in {"20", "30", "40", "50", "60", "70", "80", "90"}:
            return str(int(vals[0]) + int(vals[1]))
    return NUMBER_WORDS.get(token)


def normalize_ratio(text: str) -> str:
    t = " ".join((text or "").strip().split()).lower()

    m = RATIO_RE.search(t)
    if m:
        return f"{m.group(1)}-for-{m.group(2)}"

    m2 = WORD_RATIO_RE.search(t)
    if m2:
        right = parse_number_word(m2.group(1))
        if right:
            return f"1-for-{right}"

    return ""


def get_split_type(ratio: str) -> str:
    try:
        left, right = ratio.split("-for-")
        left = float(left)
        right = float(right)
        if left < right:
            return "RS"
        if left > right:
            return "FS"
    except Exception:
        pass
    return ""


def extract_ticker(text: str) -> str:
    m = re.search(r"\(([A-Z]{1,8})\)", text)
    if m:
        return m.group(1)

    m = re.search(r"\b([A-Z]{1,8})\b", text)
    if m:
        return m.group(1)

    return "N/A"


async def fetch_article_text(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url, timeout=20, follow_redirects=True)
        r.raise_for_status()
        return clean_text(r.text)
    except Exception as e:
        logging.warning("Article fetch failed %s: %s", url, e)
        return ""


async def get_price(ticker: str) -> str:
    if not ticker or ticker == "N/A":
        return "N/A"

    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    params = {"symbols": ticker}

    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()

        result = data.get("quoteResponse", {}).get("result", [])
        if not result:
            return "N/A"

        price = result[0].get("regularMarketPrice")
        if price is None:
            return "N/A"

        return f"${price:.2f}"
    except Exception as e:
        logging.warning("Price fetch failed for %s: %s", ticker, e)
        return "N/A"


def ensure_csv_exists() -> None:
    if os.path.isfile(CSV_FILE):
        return

    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "Ticker", "Ratio", "Type", "Company", "Price"])


def write_to_csv(item: dict) -> None:
    ensure_csv_exists()

    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            item.get("ticker", ""),
            item.get("ratio", ""),
            get_split_type(item.get("ratio", "")),
            item.get("title", ""),
            item.get("price", "N/A"),
        ])


async def scan_feed(client: httpx.AsyncClient, source_name: str, url: str) -> list[dict]:
    parsed = feedparser.parse(url)
    results = []
    cutoff = datetime.utcnow() - timedelta(days=14)

    for entry in parsed.entries:
        title = clean_text(entry.get("title", ""))
        summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
        link = entry.get("link", "")

        published_raw = entry.get("published") or entry.get("updated") or ""
        if published_raw:
            try:
                pub = parsedate_to_datetime(published_raw)
                pub_naive = pub.replace(tzinfo=None) if pub.tzinfo else pub
                if pub_naive < cutoff:
                    continue
            except Exception:
                pass

        text = f"{title}\n{summary}"

        if not has_signal(text):
            body = await fetch_article_text(client, link)
            text = f"{text}\n{body}"

        if not has_signal(text):
            continue

        ratio = normalize_ratio(text)
        if not ratio:
            continue

        ticker = extract_ticker(text)
        price = await get_price(ticker)

        results.append(
            {
                "ticker": ticker,
                "price": price,
                "ratio": ratio,
                "title": title,
                "link": link,
                "source": source_name,
            }
        )

    return results


async def fetch_news() -> list[dict]:
    async with httpx.AsyncClient(headers=HEADERS) as client:
        tasks = [scan_feed(client, name, url) for name, url in FEEDS]
        chunks = await asyncio.gather(*tasks, return_exceptions=True)

    result = []
    for chunk in chunks:
        if isinstance(chunk, Exception):
            logging.warning("Feed scan failed: %s", chunk)
            continue
        result.extend(chunk)

    deduped = []
    seen_local = set()
    for item in result:
        key = (item["ticker"], item["ratio"], item["link"])
        if key in seen_local:
            continue
        seen_local.add(key)
        deduped.append(item)

    return deduped


def format_alert(item: dict) -> str:
    return (
        "🚨 REVERSE SPLIT NEWS\n\n"
        f"Ticker: {item['ticker']}\n"
        f"Price: {item['price']}\n"
        f"Ratio: {item['ratio']}\n"
        f"Source: {item['source']}\n\n"
        f"{item['title']}\n"
        f"{item['link']}"
    )


seen = set()


async def loop() -> None:
    ensure_csv_exists()

    while True:
        try:
            items = await fetch_news()
            new_items = []

            for item in items:
                key = f"{item['ticker']}_{item['ratio']}_{item['link']}"
                if key not in seen:
                    seen.add(key)
                    new_items.append(item)

            logging.info("Checked. New: %s", len(new_items))

            for item in new_items[:10]:
                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=format_alert(item),
                    disable_web_page_preview=False,
                )
                write_to_csv(item)

        except Exception as e:
            logging.exception("Loop error: %s", e)

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(loop())
EOF
