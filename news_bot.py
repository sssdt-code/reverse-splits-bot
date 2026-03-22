import os
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
]

RATIO_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:for|:|-for-)\s*(\d+(?:\.\d+)?)", re.IGNORECASE)

def clean_text(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    return html.unescape(soup.get_text(" ", strip=True))

def has_signal(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in KEYWORDS)

def normalize_ratio(text: str) -> str:
    m = RATIO_RE.search(text)
    if m:
        return f"{m.group(1)}-for-{m.group(2)}"
    return ""

def extract_ticker(text: str) -> str:
    m = re.search(r"\(([A-Z]{1,5})\)", text)
    if m:
        return m.group(1)
    return "N/A"

async def fetch_article_text(client, url):
    try:
        r = await client.get(url, timeout=15)
        return clean_text(r.text)
    except:
        return ""

async def scan_feed(client, source_name, url):
    parsed = feedparser.parse(url)
    results = []

    for entry in parsed.entries[:20]:
        title = clean_text(entry.get("title", ""))
        summary = clean_text(entry.get("summary", ""))
        link = entry.get("link", "")

        text = f"{title} {summary}"

        if not has_signal(text):
            text += await fetch_article_text(client, link)

        if not has_signal(text):
            continue

        ratio = normalize_ratio(text)
        if not ratio:
            continue

        results.append({
            "ticker": extract_ticker(text),
            "ratio": ratio,
            "title": title,
            "link": link,
            "source": source_name
        })

    return results

async def fetch_news():
    async with httpx.AsyncClient(headers=HEADERS) as client:
        tasks = [scan_feed(client, name, url) for name, url in FEEDS]
        chunks = await asyncio.gather(*tasks)

    result = []
    for c in chunks:
        result.extend(c)

    return result

seen = set()

async def loop():
    # 🔥 ТЕСТ — ОДИН РАЗ ПРИ СТАРТЕ
    await bot.send_message(chat_id=CHAT_ID, text="✅ NEWS BOT ЗАПУЩЕН")

    while True:
        try:
            items = await fetch_news()
            new = []

            for i in items:
                key = f"{i['ticker']}_{i['ratio']}_{i['link']}"
                if key not in seen:
                    seen.add(key)
                    new.append(i)

            logging.info(f"Checked. New: {len(new)}")

            for i in new[:5]:
                msg = (
                    "🚨 REVERSE SPLIT NEWS\n\n"
                    f"{i['ticker']}\n"
                    f"{i['ratio']}\n"
                    f"{i['source']}\n\n"
                    f"{i['title']}\n"
                    f"{i['link']}"
                )
                await bot.send_message(chat_id=CHAT_ID, text=msg)

        except Exception as e:
            logging.error(e)

        await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(loop())
