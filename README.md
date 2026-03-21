# Reverse Split Telegram Bot

Telegram-бот для новостных алертов по reverse stock split только по основным биржам США, без OTC.

Что он делает:
- мониторит **SEC current filings** и ловит ранние сигналы по формам `PRE 14A`, `DEF 14A`, `DEFA14A`, `8-K`, `6-K`, `SC 14A`
- мониторит **Nasdaq Trader** и ловит подтверждённые notices по effective date reverse split
- фильтрует тикеры через **официальные файлы Nasdaq Symbol Directory** (`nasdaqlisted.txt` и `otherlisted.txt`), поэтому OTC отсекается
- отправляет алерты в Telegram-чат или канал
- хранит дедупликацию в SQLite, чтобы не спамить повторно

## Почему эта версия лучше

Проблема большинства самодельных RS-ботов в том, что они либо:
- ловят слишком поздно,
- не фильтруют OTC,
- или шлют дубли.

Здесь логика разделена на 2 слоя:
1. **SEC = ранние сигналы**
   - `PRE 14A` → EARLY
   - `DEF 14A` / `DEFA14A` → PROXY
   - `8-K` → CONFIRMED
2. **Nasdaq Trader = биржевое подтверждение**
   - обычно там уже есть ratio и effective date

## Что считается "основными рынками"

По умолчанию бот пропускает только:
- NASDAQ
- NYSE
- NYSE American
- NYSE Arca

Настраивается через `ALLOWED_EXCHANGES`.

## Быстрый запуск локально

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

## Как получить BOT_TOKEN

1. Открой `@BotFather` в Telegram.
2. Создай бота через `/newbot`.
3. Забери токен и вставь в `.env`.

## Как получить CHAT_ID канала

Для канала:
1. Добавь бота админом в канал.
2. Отправь любой пост в канал.
3. Временно запусти бота без `CHAT_ID`.
4. Напиши боту в личку `/testalert` и `/status`.
5. Для канала удобнее использовать `@RawDataBot` или `@userinfobot`, либо получить id через update webhook/polling. Обычно канал начинается с `-100...`.

## Важная настройка для SEC

В `.env` обязательно укажи нормальный `SEC_USER_AGENT`, потому что SEC просит идентифицировать скрипты и соблюдать fair access. У SEC также есть ограничение по скорости запросов — не более 10 req/sec. Официальные developer resources и fair access policy описаны на SEC. citeturn531225search7turn531225search1turn531225search17

## Официальные источники, на которые опирается бот

- **SEC current filings / EDGAR** — для ранних прокси и 8-K. SEC публикует current filings и предоставляет JSON submissions API через `data.sec.gov`. citeturn531225search7turn531225search17
- **Nasdaq Trader news alerts** — у Nasdaq Trader есть RSS/news system и конкретные corporate action notices по reverse split. citeturn436574search1turn436574search2turn436574search0
- **Nasdaq Symbol Directory** — официальные `nasdaqlisted.txt` и `otherlisted.txt` позволяют отделить основные биржи от OTC и понять exchange code. citeturn174365search1turn174365search0turn174365search2

## Формат алерта

Пример:

```text
🚨 REVERSE SPLIT ALERT
Ticker: YYGH
Exchange: NASDAQ
Company: YY Group Holding Limited
Stage: CONFIRMED
Source: NASDAQ
Form: Nasdaq Trader notice
Ratio: 1-for-50
Effective date: Monday, March 23, 2026
Headline: Information Regarding the Reverse Stock Split and CUSIP Number Change for YYGH
Excerpt: The reverse stock split will become effective on Monday, March 23, 2026.
Link: ...
```

## Команды

- `/start` — запуск
- `/status` — статус бота
- `/recent` — последние отправленные алерты
- `/testalert` — тестовое сообщение

## Что я бы рекомендовал улучшить следующим этапом

1. Добавить watchlist только для тикеров ниже $1.
2. Подтягивать price/market cap из брокерского API или стороннего market data API.
3. Разделить канал на:
   - `EARLY / PROXY / CONFIRMED`
   - отдельный high-probability feed
4. Добавить постинг сразу в Telegram-канал и отдельный лог в CSV/Google Sheet.

## Ограничения этой версии

- NYSE/NYSE American ранние сигналы идут в основном через SEC, а не через единый биржевой notice feed как у Nasdaq.
- Если компания подаёт filing без явной формулировки reverse split в доступном тексте, бот может пропустить событие.
- Для очень частого polling лучше деплоить на VPS/Render/Railway, а не держать локально.

## Deploy на Render/Railway

Можно использовать `Dockerfile` или обычный Python service.

Команда запуска:

```bash
python bot.py
```
