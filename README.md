# Ticket Monitor

Generic ticket availability monitor for Ticketmaster Israel, with Telegram bot alerts and a Google SSO web dashboard.

## Features

- Browse live events, performances, and ticket types via Telegram inline menus
- Subscribe to specific ticket types with a max-price ceiling
- Get notified the moment matching tickets appear (checked every 2 minutes)
- Web dashboard (FastAPI + Google SSO) to view and manage subscriptions
- Link Telegram ↔ Google account for unified subscription management
- Pinned summary message in your Telegram chat keeps your active subscriptions visible

## Stack

- **Bot**: python-telegram-bot v21 (async)
- **Web**: FastAPI + Jinja2 + Starlette sessions
- **Auth**: Google OAuth2 via authlib
- **DB**: SQLAlchemy 2.0 + SQLite
- **Scheduler**: APScheduler (AsyncIOScheduler, 2-minute interval)
- **API**: Ticketmaster Israel REST endpoints

## Setup

1. Copy `.env.example` to `.env` and fill in all values:

```
TELEGRAM_BOT_TOKEN=...
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
SECRET_KEY=...          # any random hex string (openssl rand -hex 32)
WEB_BASE_URL=http://localhost:8000
DATABASE_URL=sqlite:///tickets.db
```

2. Add `http://localhost:8000/auth/callback` as an authorized redirect URI in your Google Cloud Console OAuth client.

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Run:

```bash
python run.py
```

The Telegram bot and web server start together. Open `http://localhost:8000` to access the dashboard.
