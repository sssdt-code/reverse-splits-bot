import os
import asyncio
import logging
import httpx
from telegram import Bot

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("NEWS_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
POLL_INTERVAL = int(os.getenv("NEWS_POLL_INTERVAL_SECONDS", "600"))

if not BOT_TOKEN:
    raise RuntimeError("NEWS_BOT_TOKEN is missing")

bot = Bot(token=BOT_TOKEN)

async def fetch_splits():
    url = "https://api.benzinga.com/api/v2/calendar/splits"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
            if r.status_code != 200:
                logging.error(f"Bad response: {r.status_code}")
                return []

            data = r.json()
            if not isinstance(data, dict):
                return []

            return data.get("splits", [])
    except Exception as e:
        logging.error(f"Fetch error: {e}")
        return []

def format_split(item):
    ticker = item.get("ticker", "")
    ratio = item.get("split_ratio", "")
    name = item.get("company_name", "")
    date = item.get("execution_date", "")
    return f"{date}\n{ticker} | {ratio}\n{name}"

seen = set()

async def loop():
    while True:
        splits = await fetch_splits()
        new_items = []

        for s in splits:
            key = f"{s.get('ticker')}_{s.get('execution_date')}"
            if key not in seen:
                seen.add(key)
                new_items.append(s)

        if new_items:
            for s in new_items[:5]:
                msg = "📰 NEW SPLIT\n\n" + format_split(s)
                await bot.send_message(chat_id=CHAT_ID, text=msg)

        logging.info(f"Checked. New: {len(new_items)}")
        await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(loop())
