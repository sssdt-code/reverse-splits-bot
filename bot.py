import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta

import feedparser
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
POLL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "600"))

FEEDS = [
    ("GlobeNewswire", "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies"),
    ("PR Newswire", "https://www.prnewswire.com/rss/news-releases-list.rss"),
    ("BusinessWire", "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFJXkJeEFJ5WnA="),
    ("Nasdaq Trader", "https://www.nasdaqtrader.com/rss.aspx?feed=currentheadlines&categorylist=1"),
]

SPLITS_CALENDAR_URL = "https://stockanalysis.com/actions/splits/"

HEADERS = {
    "User-Agent": os.getenv("SEC_USER_AGENT", "Sergey Zinin your_email@example.com")
}

MAIN_MARKET_SYMBOLS_URLS = [
    "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt",
]

STRICT_RS_PHRASES = [
    "reverse stock split",
    "reverse split",
    "share consolidation",
    "stock consolidation",
]

RATIO_TEXT_RE = re.compile(r"\b1\s*[-:]?\s*for\s*[-:]?\s*(\d+)\b", re.IGNORECASE)
TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")


def clean_text(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    return html.unescape(soup.get_text(" ", strip=True))


def has_strict_rs_phrase(text: str) -> bool:
    t = (text or "").lower()
    return any(p in t for p in STRICT_RS_PHRASES)


def extract_ratio_if_strict(text: str) -> str | None:
    if not has_strict_rs_phrase(text):
        return None
    m = RATIO_TEXT_RE.search(text or "")
    if not m:
        return None
    return f"1-for-{m.group(1)}"


async def fetch_page_text(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url, timeout=20, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return html.unescape(soup.get_text(" ", strip=True))
    except Exception as e:
        logging.warning("Failed to fetch page text for %s: %s", url, e)
        return ""


async def load_allowed_symbols(client: httpx.AsyncClient) -> set[str]:
    symbols = set()
    for url in MAIN_MARKET_SYMBOLS_URLS:
        try:
            r = await client.get(url, timeout=20)
            r.raise_for_status()
            lines = r.text.splitlines()
            for line in lines[1:]:
                if not line.strip() or line.startswith("File Creation Time"):
                    continue
                parts = line.split("|")
                if not parts:
                    continue
                symbol = parts[0].strip().upper()
                if symbol:
                    symbols.add(symbol)
        except Exception as e:
            logging.warning("Failed to load symbols from %s: %s", url, e)
    logging.info("Loaded %s main-market symbols", len(symbols))
    return symbols


def extract_best_ticker(text: str, allowed_symbols: set[str]) -> str | None:
    for c in TICKER_RE.findall(text or ""):
        sym = c.upper()
        if sym in allowed_symbols:
            return sym
    return None


async def scan_feed(client: httpx.AsyncClient, source_name: str, feed_url: str, allowed_symbols: set[str]) -> list[dict]:
    logging.info("Scanning %s", source_name)
    feed = feedparser.parse(feed_url)
    alerts = []

    for entry in feed.entries:
        title = clean_text(entry.get("title", ""))
        summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
        link = entry.get("link", "")

        text = f"{title}\n{summary}"
        ratio = extract_ratio_if_strict(text)

        if ratio is None:
            body = await fetch_page_text(client, link)
            text = f"{text}\n{body}"
        else:
            body = ""

        ratio = extract_ratio_if_strict(text)
        if ratio is None:
            continue

        ticker = extract_best_ticker(text, allowed_symbols)
        if not ticker:
            continue

        alerts.append(
            {
                "ticker": ticker,
                "ratio": ratio,
                "title": title,
                "link": link,
                "source": source_name,
                "excerpt": (summary or body)[:300],
            }
        )

    logging.info("%s -> %s matching alerts", source_name, len(alerts))
    return alerts


async def scan_all_sources(allowed_symbols: set[str]) -> list[dict]:
    async with httpx.AsyncClient(headers=HEADERS) as client:
        tasks = [scan_feed(client, source_name, feed_url, allowed_symbols) for source_name, feed_url in FEEDS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    alerts = []
    for result in results:
        if isinstance(result, Exception):
            logging.warning("Source scan failed: %s", result)
            continue
        alerts.extend(result)

    deduped = []
    seen = set()
    for item in alerts:
        key = (item["ticker"], item["ratio"], item["link"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def parse_calendar_date(text: str) -> str | None:
    text = text.strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def normalize_calendar_ratio(text: str) -> str:
    t = " ".join(text.strip().split()).lower()
    m = re.search(r"(\d+)\s+for\s+(\d+)", t)
    if not m:
        return text.strip()
    left = m.group(1)
    right = m.group(2)
    return f"{left}-for-{right}"


async def fetch_calendar_all(allowed_symbols: set[str]) -> list[dict]:
    async with httpx.AsyncClient(headers=HEADERS) as client:
        r = await client.get(SPLITS_CALENDAR_URL, timeout=20, follow_redirects=True)
        r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    results = []

    rows = soup.find_all("tr")
    for row in rows:
        cells = row.find_all(["td", "th"])
        texts = [c.get_text(" ", strip=True) for c in cells]
        if len(texts) < 5:
            continue

        date_text = texts[0]
        symbol = texts[1].upper()
        company = texts[2]
        split_type = texts[3].lower()
        ratio_text = texts[4]

        iso_date = parse_calendar_date(date_text)
        if not iso_date:
            continue

        if "reverse" not in split_type:
            continue

        if symbol not in allowed_symbols:
            continue

        results.append(
            {
                "ticker": symbol,
                "company": company,
                "ratio": normalize_calendar_ratio(ratio_text),
                "effective_date": iso_date,
                "source": "StockAnalysis calendar",
            }
        )

    deduped = []
    seen = set()
    for item in results:
        key = (item["ticker"], item["ratio"], item["effective_date"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    deduped.sort(key=lambda x: (x["effective_date"], x["ticker"]))
    return deduped


def filter_by_date(items: list[dict], target_date: str) -> list[dict]:
    return [x for x in items if x["effective_date"] == target_date]


def filter_range(items: list[dict], start_date: str, end_date: str) -> list[dict]:
    return [x for x in items if start_date <= x["effective_date"] <= end_date]


def format_date_list(title: str, target_date: str, items: list[dict]) -> str:
    if not items:
        return f"На {target_date} upcoming reverse splits не найдены."

    lines = [f"{title} {target_date}:"]
    for item in items:
        lines.append(f"{item['ticker']} | {item['ratio']} | {item['company']}")
    return "\n".join(lines)


def format_grouped(title: str, items: list[dict]) -> str:
    if not items:
        return f"{title}: ничего не найдено."

    lines = [title]
    current_date = None
    for item in items:
        if item["effective_date"] != current_date:
            current_date = item["effective_date"]
            lines.append("")
            lines.append(f"📅 {current_date}")
        lines.append(f"{item['ticker']} | {item['ratio']} | {item['company']}")
    return "\n".join(lines)


def format_calendar_push(item: dict) -> str:
    return (
        "📅 NEW UPCOMING REVERSE SPLIT\n\n"
        f"Ticker: {item['ticker']}\n"
        f"Ratio: {item['ratio']}\n"
        f"Effective date: {item['effective_date']}\n"
        f"Company: {item['company']}\n"
        f"Source: {item['source']}"
    )


async def send(bot, text: str) -> None:
    await bot.send_message(chat_id=CHAT_ID, text=text, disable_web_page_preview=False)


async def scanner_loop(app: Application) -> None:
    sent_news = set()
    sent_calendar = set()

    async with httpx.AsyncClient(headers=HEADERS) as client:
        allowed_symbols = await load_allowed_symbols(client)

    app.bot_data["allowed_symbols"] = allowed_symbols

    try:
        initial_calendar = await fetch_calendar_all(allowed_symbols)
        for item in initial_calendar:
            key = f'{item["ticker"]}|{item["ratio"]}|{item["effective_date"]}'
            sent_calendar.add(key)
        logging.info("Seeded calendar cache with %s items", len(sent_calendar))
    except Exception as e:
        logging.warning("Failed to seed calendar cache: %s", e)

    while True:
        try:
            alerts = await scan_all_sources(allowed_symbols)
            logging.info("Total matching news alerts this cycle: %s", len(alerts))

            for item in alerts:
                key = f'{item["ticker"]}|{item["ratio"]}|{item["link"]}'
                if key in sent_news:
                    continue
                sent_news.add(key)

                msg = (
                    f"🚨 RS ALERT\n\n"
                    f"Ticker: {item['ticker']}\n"
                    f"Ratio: {item['ratio']}\n"
                    f"Source: {item['source']}\n"
                    f"Title: {item['title']}\n"
                    f"Link: {item['link']}"
                )
                logging.info("Sending news alert: %s %s from %s", item["ticker"], item["ratio"], item["source"])
                await send(app.bot, msg)

            calendar_items = await fetch_calendar_all(allowed_symbols)
            logging.info("Calendar items fetched: %s", len(calendar_items))

            today = datetime.now().strftime("%Y-%m-%d")
            future_calendar_items = [x for x in calendar_items if x["effective_date"] >= today]

            new_calendar_items = []
            for item in future_calendar_items:
                key = f'{item["ticker"]}|{item["ratio"]}|{item["effective_date"]}'
                if key in sent_calendar:
                    continue
                sent_calendar.add(key)
                new_calendar_items.append(item)

            logging.info("New calendar items this cycle: %s", len(new_calendar_items))

            for item in new_calendar_items:
                logging.info("Sending calendar push: %s %s %s", item["ticker"], item["ratio"], item["effective_date"])
                await send(app.bot, format_calendar_push(item))

        except Exception as e:
            logging.exception("Scanner loop error: %s", e)

        logging.info("Sleeping %s seconds...", POLL_SECONDS)
        await asyncio.sleep(POLL_SECONDS)


async def ensure_allowed_symbols(app: Application) -> set[str]:
    allowed_symbols = app.bot_data.get("allowed_symbols", set())
    if allowed_symbols:
        return allowed_symbols

    async with httpx.AsyncClient(headers=HEADERS) as client:
        allowed_symbols = await load_allowed_symbols(client)
    app.bot_data["allowed_symbols"] = allowed_symbols
    return allowed_symbols


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Bot running")


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Test OK")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Status: running\n"
        f"Sources: {len(FEEDS)} news feeds + calendar\n"
        f"Poll interval: {POLL_SECONDS} sec"
    )


async def cmd_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Используй так: /date 2026-03-31")
        return

    target_date = context.args[0].strip()
    try:
        datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        await update.message.reply_text("Формат даты должен быть YYYY-MM-DD")
        return

    allowed_symbols = await ensure_allowed_symbols(context.application)
    items = await fetch_calendar_all(allowed_symbols)
    items = filter_by_date(items, target_date)
    await update.message.reply_text(format_date_list("📅 Upcoming reverse splits на", target_date, items))


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target_date = datetime.now().strftime("%Y-%m-%d")
    allowed_symbols = await ensure_allowed_symbols(context.application)
    items = await fetch_calendar_all(allowed_symbols)
    items = filter_by_date(items, target_date)
    await update.message.reply_text(format_date_list("📅 Reverse splits на сегодня", target_date, items))


async def cmd_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    allowed_symbols = await ensure_allowed_symbols(context.application)
    items = await fetch_calendar_all(allowed_symbols)
    items = filter_by_date(items, target_date)
    await update.message.reply_text(format_date_list("📅 Reverse splits на завтра", target_date, items))


async def cmd_t1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    allowed_symbols = await ensure_allowed_symbols(context.application)
    items = await fetch_calendar_all(allowed_symbols)
    items = filter_by_date(items, target_date)
    await update.message.reply_text(format_date_list("🔥 T-1 reverse splits на", target_date, items))


async def cmd_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_symbols = await ensure_allowed_symbols(context.application)
    items = await fetch_calendar_all(allowed_symbols)
    today = datetime.now().strftime("%Y-%m-%d")
    items = [x for x in items if x["effective_date"] >= today]
    await update.message.reply_text(format_grouped("📋 Все upcoming reverse splits", items[:100]))


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    start_date = datetime.now().strftime("%Y-%m-%d")
    end_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    allowed_symbols = await ensure_allowed_symbols(context.application)
    items = await fetch_calendar_all(allowed_symbols)
    items = filter_range(items, start_date, end_date)
    await update.message.reply_text(format_grouped("📆 Reverse splits на 7 дней", items))


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    start_date = datetime.now().strftime("%Y-%m-%d")
    end_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    allowed_symbols = await ensure_allowed_symbols(context.application)
    items = await fetch_calendar_all(allowed_symbols)
    items = filter_range(items, start_date, end_date)
    await update.message.reply_text(format_grouped("🗓 Reverse splits на 30 дней", items[:150]))


async def post_init(app: Application) -> None:
    asyncio.create_task(scanner_loop(app))


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing in .env")
    if not CHAT_ID:
        raise RuntimeError("CHAT_ID is missing in .env")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("date", cmd_date))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("t1", cmd_t1))
    app.add_handler(CommandHandler("all", cmd_all))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("month", cmd_month))

    logging.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
