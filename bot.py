import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta, date

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

HEADERS = {
    "User-Agent": os.getenv("SEC_USER_AGENT", "Sergey Zinin your_email@example.com")
}

# Основной календарный источник
BASE_CALENDAR_URLS = [
    "https://stockanalysis.com/actions/splits/",
    "https://stockanalysis.com/actions/splits/?p={page}",
    "https://stockanalysis.com/actions/splits/?page={page}",
]

MAIN_MARKET_SYMBOLS_URLS = [
    "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt",
]

DATE_FORMATS = (
    "%b %d, %Y",
    "%B %d, %Y",
    "%Y-%m-%d",
    "%d.%m.%Y",
)

def clean_text(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    return html.unescape(soup.get_text(" ", strip=True))

def parse_any_date(text: str) -> str | None:
    text = " ".join((text or "").strip().split())
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None

def normalize_ratio(text: str) -> str:
    t = " ".join((text or "").strip().split()).lower()
    m = re.search(r"(\\d+)\\s*[:\\-]?\\s*for\\s*(\\d+)", t)
    if m:
        return f"{m.group(1)}-for-{m.group(2)}"
    m2 = re.search(r"(\\d+)\\s*:\\s*(\\d+)", t)
    if m2:
        return f"{m2.group(1)}-for-{m2.group(2)}"
    return (text or "").strip()

def looks_like_symbol(text: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{1,5}", (text or "").strip()))

async def load_allowed_symbols(client: httpx.AsyncClient) -> set[str]:
    symbols = set()
    for url in MAIN_MARKET_SYMBOLS_URLS:
        try:
            r = await client.get(url, timeout=20)
            r.raise_for_status()
            for line in r.text.splitlines()[1:]:
                if not line.strip() or line.startswith("File Creation Time"):
                    continue
                parts = line.split("|")
                if not parts:
                    continue
                sym = parts[0].strip().upper()
                if sym:
                    symbols.add(sym)
        except Exception as e:
            logging.warning("Failed to load symbols from %s: %s", url, e)
    logging.info("Loaded %s main-market symbols", len(symbols))
    return symbols

def extract_rows_from_html(html_text: str, allowed_symbols: set[str]) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    results = []

    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            texts = [clean_text(c.get_text(" ", strip=True)) for c in cells]
            if len(texts) < 5:
                continue

            # Ожидаем примерно:
            # [Date, Symbol, Company, Type, Ratio, ...]
            date_text = texts[0]
            symbol = texts[1].upper().strip()
            company = texts[2].strip()
            split_type = texts[3].lower().strip()
            ratio_text = texts[4].strip()

            iso_date = parse_any_date(date_text)
            if not iso_date:
                continue
            if not looks_like_symbol(symbol):
                continue
            if symbol not in allowed_symbols:
                continue
            if "reverse" not in split_type:
                continue

            results.append(
                {
                    "ticker": symbol,
                    "company": company,
                    "ratio": normalize_ratio(ratio_text),
                    "effective_date": iso_date,
                    "source": "StockAnalysis calendar",
                }
            )
    return results

async def fetch_calendar_all(allowed_symbols: set[str]) -> list[dict]:
    results = []
    seen_pages = set()

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        # Базовая страница
        for url in [
            "https://stockanalysis.com/actions/splits/",
        ]:
            try:
                r = await client.get(url, timeout=20)
                r.raise_for_status()
                rows = extract_rows_from_html(r.text, allowed_symbols)
                results.extend(rows)
                seen_pages.add(url)
                logging.info("Fetched base calendar page: %s rows", len(rows))
            except Exception as e:
                logging.warning("Failed to fetch base calendar page %s: %s", url, e)

        # Пробуем несколько страниц/вариантов URL, чтобы захватить более дальние upcoming
        for page in range(1, 8):
            for template in BASE_CALENDAR_URLS[1:]:
                url = template.format(page=page)
                if url in seen_pages:
                    continue
                try:
                    r = await client.get(url, timeout=20)
                    if r.status_code >= 400:
                        continue
                    rows = extract_rows_from_html(r.text, allowed_symbols)
                    if rows:
                        logging.info("Fetched paged calendar %s: %s rows", url, len(rows))
                        results.extend(rows)
                    seen_pages.add(url)
                except Exception as e:
                    logging.warning("Failed to fetch paged calendar %s: %s", url, e)

    deduped = []
    seen = set()
    for item in results:
        key = (item["ticker"], item["ratio"], item["effective_date"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    deduped.sort(key=lambda x: (x["effective_date"], x["ticker"]))
    logging.info("Calendar deduped items total: %s", len(deduped))
    return deduped

def filter_by_date(items: list[dict], target_date: str) -> list[dict]:
    return [x for x in items if x["effective_date"] == target_date]

def filter_range(items: list[dict], start_date: str, end_date: str) -> list[dict]:
    return [x for x in items if start_date <= x["effective_date"] <= end_date]

def current_month_range() -> tuple[str, str]:
    today = date.today()
    first = today.replace(day=1)
    if today.month == 12:
        next_month = date(today.year + 1, 1, 1)
    else:
        next_month = date(today.year, today.month + 1, 1)
    last = next_month - timedelta(days=1)
    return first.isoformat(), last.isoformat()

def next_month_range() -> tuple[str, str]:
    today = date.today()
    if today.month == 12:
        first = date(today.year + 1, 1, 1)
        next_after = date(today.year + 1, 2, 1)
    elif today.month == 11:
        first = date(today.year, 12, 1)
        next_after = date(today.year + 1, 1, 1)
    else:
        first = date(today.year, today.month + 1, 1)
        next_after = date(today.year, today.month + 2, 1)
    last = next_after - timedelta(days=1)
    return first.isoformat(), last.isoformat()

def format_grouped(title: str, items: list[dict]) -> str:
    if not items:
        return f"{title}: ничего не найдено."

    lines = [title]
    current = None
    for item in items:
        if item["effective_date"] != current:
            current = item["effective_date"]
            lines.append("")
            lines.append(f"📅 {current}")
        lines.append(f"{item['ticker']} | {item['ratio']} | {item['company']}")
    return "\n".join(lines)

def format_date_list(title: str, target_date: str, items: list[dict]) -> str:
    if not items:
        return f"На {target_date} upcoming reverse splits не найдены."
    lines = [f"{title} {target_date}:"]
    for item in items:
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

async def send_text(bot, text: str) -> None:
    # Telegram limits, режем длинные сообщения
    chunk_size = 3500
    if len(text) <= chunk_size:
        await bot.send_message(chat_id=CHAT_ID, text=text, disable_web_page_preview=True)
        return

    parts = []
    current = ""
    for line in text.splitlines():
        if len(current) + len(line) + 1 > chunk_size:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}".strip()
    if current:
        parts.append(current)

    for part in parts:
        await bot.send_message(chat_id=CHAT_ID, text=part, disable_web_page_preview=True)

async def ensure_allowed_symbols(app: Application) -> set[str]:
    allowed_symbols = app.bot_data.get("allowed_symbols", set())
    if allowed_symbols:
        return allowed_symbols

    async with httpx.AsyncClient(headers=HEADERS) as client:
        allowed_symbols = await load_allowed_symbols(client)
    app.bot_data["allowed_symbols"] = allowed_symbols
    return allowed_symbols

async def scanner_loop(app: Application) -> None:
    sent_calendar = set()
    allowed_symbols = await ensure_allowed_symbols(app)

    try:
        initial = await fetch_calendar_all(allowed_symbols)
        for item in initial:
            key = f'{item["ticker"]}|{item["ratio"]}|{item["effective_date"]}'
            sent_calendar.add(key)
        logging.info("Seeded calendar cache with %s items", len(sent_calendar))
    except Exception as e:
        logging.warning("Failed to seed calendar cache: %s", e)

    while True:
        try:
            items = await fetch_calendar_all(allowed_symbols)
            logging.info("Calendar items fetched: %s", len(items))

            today = datetime.now().strftime("%Y-%m-%d")
            future_items = [x for x in items if x["effective_date"] >= today]

            new_items = []
            for item in future_items:
                key = f'{item["ticker"]}|{item["ratio"]}|{item["effective_date"]}'
                if key in sent_calendar:
                    continue
                sent_calendar.add(key)
                new_items.append(item)

            logging.info("New calendar items this cycle: %s", len(new_items))

            for item in new_items:
                logging.info("Sending calendar push: %s %s %s", item["ticker"], item["ratio"], item["effective_date"])
                await send_text(app.bot, format_calendar_push(item))

        except Exception as e:
            logging.exception("Calendar scanner loop error: %s", e)

        logging.info("Sleeping %s seconds...", POLL_SECONDS)
        await asyncio.sleep(POLL_SECONDS)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Bot running")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Status: running\n"
        "Mode: full calendar monitor\n"
        f"Poll interval: {POLL_SECONDS} sec"
    )

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Test OK")

async def cmd_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_symbols = await ensure_allowed_symbols(context.application)
    items = await fetch_calendar_all(allowed_symbols)
    today = datetime.now().strftime("%Y-%m-%d")
    items = [x for x in items if x["effective_date"] >= today]
    await send_text(context.application.bot, format_grouped("📋 Все upcoming reverse splits", items))

async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_symbols = await ensure_allowed_symbols(context.application)
    items = await fetch_calendar_all(allowed_symbols)
    start_date, end_date = current_month_range()
    items = filter_range(items, start_date, end_date)
    await send_text(context.application.bot, format_grouped("🗓 Reverse splits в этом месяце", items))

async def cmd_nextmonth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_symbols = await ensure_allowed_symbols(context.application)
    items = await fetch_calendar_all(allowed_symbols)
    start_date, end_date = next_month_range()
    items = filter_range(items, start_date, end_date)
    await send_text(context.application.bot, format_grouped("🗓 Reverse splits в следующем месяце", items))

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_symbols = await ensure_allowed_symbols(context.application)
    items = await fetch_calendar_all(allowed_symbols)
    start_date = datetime.now().strftime("%Y-%m-%d")
    end_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    items = filter_range(items, start_date, end_date)
    await send_text(context.application.bot, format_grouped("📆 Reverse splits на 7 дней", items))

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
    await send_text(context.application.bot, format_date_list("📅 Upcoming reverse splits на", target_date, items))

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target_date = datetime.now().strftime("%Y-%m-%d")
    allowed_symbols = await ensure_allowed_symbols(context.application)
    items = await fetch_calendar_all(allowed_symbols)
    items = filter_by_date(items, target_date)
    await send_text(context.application.bot, format_date_list("📅 Reverse splits на сегодня", target_date, items))

async def cmd_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    allowed_symbols = await ensure_allowed_symbols(context.application)
    items = await fetch_calendar_all(allowed_symbols)
    items = filter_by_date(items, target_date)
    await send_text(context.application.bot, format_date_list("📅 Reverse splits на завтра", target_date, items))

async def cmd_t1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    allowed_symbols = await ensure_allowed_symbols(context.application)
    items = await fetch_calendar_all(allowed_symbols)
    items = filter_by_date(items, target_date)
    await send_text(context.application.bot, format_date_list("🔥 T-1 reverse splits на", target_date, items))

async def post_init(app: Application) -> None:
    asyncio.create_task(scanner_loop(app))

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing in .env")
    if not CHAT_ID:
        raise RuntimeError("CHAT_ID is missing in .env")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("all", cmd_all))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("nextmonth", cmd_nextmonth))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("date", cmd_date))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("t1", cmd_t1))

    logging.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
