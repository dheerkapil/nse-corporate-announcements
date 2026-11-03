import os
import pytz

# ── Time ─────────────────────────────────────────────────
TIMEZONE = pytz.timezone("Asia/Kolkata")

# ── Fetching ─────────────────────────────────────────────
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "900"))
NSE_SESSION_TTL_SECONDS = 360
NSE_REQUEST_TIMEOUT = 15
NSE_COOKIE_WARMUP_TIMEOUT = 10

NSE_BACKFILL_CHUNK_DAYS = 7
NSE_BACKFILL_CHUNK_DELAY = 1.5

# ── Retention ────────────────────────────────────────────
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "90"))
DEFAULT_RANGE_DAYS = int(os.environ.get("DEFAULT_RANGE_DAYS", "90"))
MAX_ROWS_PER_PAGE = 1000

# ── Database ─────────────────────────────────────────────
DB_FILE = os.environ.get("DB_FILE", "announcements.db")
NOTIFY_SETTINGS_FILE = os.environ.get("NOTIFY_SETTINGS_FILE", "notify_settings.json")
OTP_STATE_FILE = os.environ.get("OTP_STATE_FILE", "otp_state.json")

# ── Telegram ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_RETRIES = 3
TELEGRAM_RETRY_DELAY = 2

# ── Auth ─────────────────────────────────────────────────
SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY", "change-me-please")
SESSION_DURATION_SECONDS = 30 * 24 * 3600
OTP_VALIDITY_SECONDS = 300
OTP_THROTTLE_SECONDS = 60
OTP_MAX_ATTEMPTS = 5
OTP_DIGITS = 4

# ── GitHub backup ────────────────────────────────────────
GITHUB_BACKUP_TOKEN = os.environ.get("GITHUB_BACKUP_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "yourname/nse-corporate-announcements")
GITHUB_CACHE_BRANCH = os.environ.get("GITHUB_CACHE_BRANCH", "cache")
GITHUB_ANN_FILE = "announcements_backup.json"
GITHUB_SETTINGS_FILE = "notify_settings_backup.json"

# ── NSE endpoints ────────────────────────────────────────
# Browser profile curl_cffi should imitate when talking to NSE. NSE blocks
# clients whose TLS fingerprint does not look like a real browser.
NSE_IMPERSONATE = os.environ.get("NSE_IMPERSONATE", "chrome")

# Try the free relay before hitting NSE directly. NSE blocks this host's
# address, so the relay is the route that actually works.
NSE_USE_RELAY = os.environ.get("NSE_USE_RELAY", "1").lower() not in ("0", "false", "no")

# How often to repeat a Telegram alert while fetching is broken.
ALERT_COOLDOWN_SECONDS = int(os.environ.get("ALERT_COOLDOWN_SECONDS", "3600"))

NSE_BASE = "https://www.nseindia.com"
NSE_ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements?index=equities"
NSE_REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ── Topic taxonomy (21 topics, alphabetical) ─────────────
TOPICS = [
    "Acquisition",
    "AGM",
    "Appointment",
    "Board Meeting",
    "Bonus Issue",
    "Buyback",
    "Capacity Addition",
    "Capex",
    "Credit Rating",
    "Demerger",
    "Dividend",
    "Earnings",
    "General Updates",
    "Investor Communication",
    "Newspaper Publication",
    "Order Win",
    "Other",
    "Press Release",
    "Rights Issue",
    "Shareholding",
    "Tax/Regulatory",
]

DEFAULT_TICKED_TOPICS = []