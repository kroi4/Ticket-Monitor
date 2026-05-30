"""Entry point — starts the web server and Telegram bot together."""
import asyncio
import logging
import threading
import sys

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import db
from bot import build_application
from monitor import run_monitor
from web import app as web_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.FileHandler("ticket_monitor.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def _start_web():
    """Run FastAPI in a background thread."""
    uvicorn.run(
        web_app,
        host=config.WEB_HOST,
        port=config.WEB_PORT,
        log_level="warning",
    )


async def main():
    if not config.TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN.startswith("1234"):
        log.error("TELEGRAM_BOT_TOKEN חסר — ערוך את קובץ .env")
        return

    # Init DB
    db.init_db()
    log.info("Database initialized")

    # Web server in daemon thread
    web_thread = threading.Thread(target=_start_web, daemon=True)
    web_thread.start()
    log.info("Web server started at http://%s:%d", config.WEB_HOST, config.WEB_PORT)

    # Build Telegram bot
    application = build_application()

    # Monitoring scheduler (runs inside bot's asyncio loop)
    scheduler = AsyncIOScheduler(timezone="Asia/Jerusalem")
    scheduler.add_job(
        lambda: asyncio.create_task(run_monitor(application.bot)),
        "interval",
        seconds=60,
        id="monitor",
    )
    scheduler.start()
    log.info("Monitor scheduler started (every 60s)")

    log.info("=" * 55)
    log.info("🎟️  Ticket Monitor is running!")
    log.info("   Bot:  https://t.me/ticket_monitorBOT")
    log.info("   Web:  %s", config.WEB_BASE_URL)
    log.info("=" * 55)

    # Run bot (blocks until Ctrl+C)
    async with application:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        # Keep running until interrupted
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            scheduler.shutdown(wait=False)
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
            log.info("Stopped.")


if __name__ == "__main__":
    asyncio.run(main())
