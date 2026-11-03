"""
telegram_notify.py — Telegram notification layer.

Responsibilities:
  • Persist the user's topic-selection (which topics to push)
  • Format announcement messages for Telegram HTML mode
  • Send messages via the bot with retries
  • Provide the single entry point the fetcher calls after each cycle

This module has no Flask dependencies. It reads its bot token and chat id
from config.py, which sources them from environment variables.

Nothing here fetches from NSE. It receives already-classified items from
announcements.py and decides whether to send each one.
"""

import os
import json
import time
import html
import logging
import threading
from datetime import datetime

import requests

import config

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════
#  NOTIFY SETTINGS  (which topics to push)
# ═════════════════════════════════════════════════════════════════

_settings_lock = threading.Lock()
_settings_memo = {"mtime": 0.0, "data": None}


def _settings_path():
    return config.NOTIFY_SETTINGS_FILE


def _default_settings():
    return {
        "ticked": list(config.DEFAULT_TICKED_TOPICS),
        "updated_at": "",
    }


def load_notify_settings():
    """
    Return the notify settings dict: {'ticked': [...], 'updated_at': '...'}.
    Cached in memory with mtime check to avoid file reads on hot paths.
    """
    path = _settings_path()
    try:
        mtime = os.path.getmtime(path) if os.path.exists(path) else 0.0
    except Exception:
        mtime = 0.0

    with _settings_lock:
        if _settings_memo["data"] is not None and _settings_memo["mtime"] == mtime:
            return _settings_memo["data"]

        if mtime == 0.0:
            data = _default_settings()
        else:
            try:
                with open(path) as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = _default_settings()
                if "ticked" not in data or not isinstance(data["ticked"], list):
                    data["ticked"] = []
                # Keep only valid topics, drop anything stale
                data["ticked"] = [t for t in data["ticked"] if t in config.TOPICS]
            except Exception as e:
                logger.warning(f"notify_settings read failed: {e}")
                data = _default_settings()

        _settings_memo["mtime"] = mtime
        _settings_memo["data"] = data
        return data


def save_notify_settings(ticked_topics):
    """
    Persist the ticked-topics list. Returns the saved settings dict.
    Accepts any iterable; filters to valid topics and dedupes.
    """
    ticked = sorted(set(t for t in (ticked_topics or []) if t in config.TOPICS))
    data = {
        "ticked": ticked,
        "updated_at": datetime.now(config.TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
    }
    path = _settings_path()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        with _settings_lock:
            _settings_memo["mtime"] = os.path.getmtime(path)
            _settings_memo["data"] = data
        logger.info(f"notify_settings saved: {len(ticked)} topics ticked")
    except Exception as e:
        logger.error(f"notify_settings write failed: {e}")
    return data


def get_ticked_topics():
    """Return a set of currently ticked topics."""
    return set(load_notify_settings().get("ticked", []))


# ═════════════════════════════════════════════════════════════════
#  MESSAGE FORMATTING
# ═════════════════════════════════════════════════════════════════

def _fmt_announced(dt_str):
    """'2026-10-11 14:30:00' → '11 Oct 2026 14:30 IST'. Best-effort."""
    if not dt_str:
        return "-"
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d %b %Y %H:%M") + " IST"
    except Exception:
        return dt_str


def format_announcement(item):
    """
    Build the Telegram HTML message for one announcement.

    Layout:
        📢 SYMBOL                       (linked to NSE PDF, if available)
        [Topic]

        Subject: ...
        Impact: ...
        Announced: 11 Oct 2026 14:30 IST
    """
    symbol = (item.get("symbol") or "").upper()
    topic = item.get("topic") or "Other"
    subject = html.escape(item.get("subject") or "")
    impact = item.get("impact") or "neutral"
    announced = _fmt_announced(item.get("announced_at") or "")
    attachment = (item.get("attachment") or "").strip()

    if attachment:
        header = f'📢 <a href="{html.escape(attachment)}">{html.escape(symbol)}</a>'
    else:
        header = f"📢 <b>{html.escape(symbol)}</b>"
        subject = subject + " <i>(no attachment on NSE)</i>"

    lines = [
        header,
        f"<b>[{html.escape(topic)}]</b>",
        "",
        f"<b>Subject:</b> {subject}",
        f"<b>Impact:</b> {impact}",
        f"<b>Announced:</b> {announced}",
    ]
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════
#  SENDER
# ═════════════════════════════════════════════════════════════════

def send_message(text):
    """
    Send one HTML message to the configured chat.
    Returns True on success, False on failure after retries.
    """
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        logger.warning("Telegram not configured (token or chat_id missing)")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    for attempt in range(1, config.TELEGRAM_RETRIES + 1):
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                return True
            logger.warning(f"Telegram HTTP {r.status_code} on attempt {attempt}: {r.text[:120]}")
        except Exception as e:
            logger.warning(f"Telegram send attempt {attempt} failed: {e}")
        if attempt < config.TELEGRAM_RETRIES:
            time.sleep(config.TELEGRAM_RETRY_DELAY)

    logger.error(f"Telegram send gave up after {config.TELEGRAM_RETRIES} attempts")
    return False


# ═════════════════════════════════════════════════════════════════
#  PUBLIC ENTRY POINTS  (called by the fetcher thread in web_ui.py)
# ═════════════════════════════════════════════════════════════════

def notify_items(items):
    """
    Given a list of newly-inserted announcement dicts, send a Telegram
    message for each one whose topic is currently ticked.

    Returns {'sent': int, 'skipped': int, 'failed': int}.
    """
    if not items:
        return {"sent": 0, "skipped": 0, "failed": 0}

    ticked = get_ticked_topics()
    if not ticked:
        return {"sent": 0, "skipped": len(items), "failed": 0}

    sent = 0
    skipped = 0
    failed = 0

    for item in items:
        if item.get("topic") not in ticked:
            skipped += 1
            continue
        msg = format_announcement(item)
        if send_message(msg):
            sent += 1
        else:
            failed += 1
        time.sleep(0.5)   # polite pacing — Telegram allows ~30 msg/s

    logger.info(f"Notify: {sent} sent, {skipped} skipped, {failed} failed")
    return {"sent": sent, "skipped": skipped, "failed": failed}


# ═════════════════════════════════════════════════════════════════
#  BACKUP / RESTORE HELPERS  (called by web_ui.py)
# ═════════════════════════════════════════════════════════════════

# =================================================================
#  FAILURE ALERTS
#
#  The fetcher runs every 15 minutes. When it cannot get data, say so once
#  and then stay quiet for the cooldown window instead of shouting every cycle.
# =================================================================

_alert_lock = threading.Lock()
_alert_state = {"last_sent": 0.0, "failing": False}


def alert_fetch_failure(reason, cooldown_seconds=None):
    """Report a broken fetch, at most once per cooldown window.

    Returns True if a message was sent, False if it was suppressed.
    """
    if cooldown_seconds is None:
        cooldown_seconds = getattr(config, "ALERT_COOLDOWN_SECONDS", 3600)

    with _alert_lock:
        now = time.time()
        _alert_state["failing"] = True
        if now - _alert_state["last_sent"] < cooldown_seconds:
            logger.info("Fetch-failure alert suppressed (within cooldown)")
            return False
        _alert_state["last_sent"] = now

    minutes = max(1, int(cooldown_seconds // 60))
    text = (
        "<b>NSE fetch failing</b>\n"
        f"{html.escape(str(reason or 'no route returned data'))}\n\n"
        "No new announcements are being collected. "
        f"This alert repeats at most every {minutes} minutes."
    )
    return send_message(text)


def alert_fetch_recovered():
    """Send one message when fetching works again after a failure."""
    with _alert_lock:
        if not _alert_state["failing"]:
            return False
        _alert_state["failing"] = False
        _alert_state["last_sent"] = 0.0

    return send_message(
        "<b>NSE fetch recovered</b>\nAnnouncements are arriving again."
    )


def get_settings_for_backup():
    """Return the notify settings dict for GitHub backup."""
    return load_notify_settings()


def restore_settings_from_backup(data):
    """Restore notify settings from a GitHub backup dict."""
    if not isinstance(data, dict):
        return False
    ticked = data.get("ticked", [])
    if not isinstance(ticked, list):
        return False
    save_notify_settings(ticked)
    logger.info(f"Restored notify settings: {len(ticked)} topics ticked")
    return True