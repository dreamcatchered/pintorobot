# pintorobot

Pinterest scraper Telegram bot with SQLite caching.

Search Pinterest images directly from Telegram — no official API needed. Results are cached by Telegram `file_id` in SQLite for instant repeated searches.

## Features

- Pinterest image search (no API — pure scraping)
- Telegram bot interface
- SQLite cache for Telegram file_ids (instant repeat searches)
- Flask API for external queries

## Stack

![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-000000?style=flat&logo=flask&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?style=flat&logo=sqlite&logoColor=white)

## Setup

```bash
pip install pyTelegramBotAPI flask requests beautifulsoup4
```

Set environment variables:
```env
BOT_TOKEN=your_bot_token
```

Add your Pinterest cookies to `USER_COOKIES` in `start.py` for better results (avoids rate limits).

```bash
python start.py
```

## Contact

Telegram: [@dreamcatch_r](https://t.me/dreamcatch_r)
