import os
import secrets
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
GOOGLE_CLIENT_ID        = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET    = os.getenv("GOOGLE_CLIENT_SECRET", "")
SECRET_KEY              = os.getenv("SECRET_KEY", secrets.token_hex(32))
DATABASE_URL            = os.getenv("DATABASE_URL", "sqlite:///tickets.db")
WEB_HOST                = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT                = int(os.getenv("WEB_PORT", "8000"))
WEB_BASE_URL            = os.getenv("WEB_BASE_URL", "http://localhost:8000")
CHECK_INTERVAL_SECONDS  = int(os.getenv("CHECK_INTERVAL_SECONDS", "120"))

SMTP_HOST     = os.getenv("SMTP_HOST", "")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM     = os.getenv("SMTP_FROM", "")

TM_BASE    = "https://www.ticketmaster.co.il/wbtxapi/api/v1/bxcached/event"
TM_CHANNEL = "INTERNET"
TM_LANG    = "iw"
TM_IMG_BASE = "https://www.ticketmaster.co.il/static/images/live/event/topeventimages/400x400"

TM_HEADERS = {
    "Accept":          "application/json",
    "Accept-Language": "he",
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Referer":         "https://www.ticketmaster.co.il/",
}
