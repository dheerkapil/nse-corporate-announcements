"""
web_ui.py — Flask application entry point.

Step 9 scope:
  • GitHub cache-branch persistence (push + restore)
  • Startup restore thread
  • Three backed-up files:
      - announcements_backup.json      (from config.GITHUB_ANN_FILE)
      - notify_settings_backup.json    (from config.GITHUB_SETTINGS_FILE)
      - topic_views_backup.json        (constant, defined below)
"""

import os
import json
import time
import base64
import random
import hmac
import hashlib
import logging
import threading
from datetime import datetime

import requests
from flask import (
    Flask, request, jsonify, redirect,
    render_template, make_response,
)

import config
import announcements
import telegram_notify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── GitHub file names on the cache branch ────────────────────────
GH_ANN_FILE = config.GITHUB_ANN_FILE
GH_SETTINGS_FILE = config.GITHUB_SETTINGS_FILE
GH_VIEWS_FILE = "topic_views_backup.json"

GH_API = "https://api.github.com"

# Updated by the fetcher thread each cycle. Used by /api/health.
_last_tick = time.time()
_last_tick_lock = threading.Lock()


def _stamp_tick():
    global _last_tick
    with _last_tick_lock:
        _last_tick = time.time()


def _get_last_tick():
    with _last_tick_lock:
        return _last_tick


# ═════════════════════════════════════════════════════════════════
#  OTP STATE  (per-IP, persisted to a JSON file)
# ═════════════════════════════════════════════════════════════════

_otp_lock = threading.Lock()


def _read_otp_state():
    try:
        if not os.path.exists(config.OTP_STATE_FILE):
            return {}
        with _otp_lock:
            with open(config.OTP_STATE_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"OTP state read failed: {e}")
        return {}


def _write_otp_state(data):
    try:
        with _otp_lock:
            tmp = config.OTP_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, config.OTP_STATE_FILE)
    except Exception as e:
        logger.error(f"OTP state write failed: {e}")


def _client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    return xff.split(",")[0].strip() if xff else (request.remote_addr or "unknown")


# ═════════════════════════════════════════════════════════════════
#  SESSION COOKIE
# ═════════════════════════════════════════════════════════════════

def _make_session_token():
    expiry = int(time.time()) + config.SESSION_DURATION_SECONDS
    sig = hmac.new(
        config.SESSION_SECRET_KEY.encode(),
        str(expiry).encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{expiry}.{sig}"


def _valid_session_token(tok):
    try:
        exp_str, sig = tok.rsplit(".", 1)
        expected = hmac.new(
            config.SESSION_SECRET_KEY.encode(),
            exp_str.encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(sig, expected) and int(exp_str) > time.time()
    except Exception:
        return False


def _is_authenticated():
    sid = request.cookies.get("session_id")
    return bool(sid and _valid_session_token(sid))


# ═════════════════════════════════════════════════════════════════
#  AUTH GATE
# ═════════════════════════════════════════════════════════════════

_PUBLIC_PATHS = {
    "/login",
    "/api/send_otp",
    "/api/verify_otp",
    "/api/health",
    "/favicon.ico",
}


@app.before_request
def _gate():
    p = request.path
    if p in _PUBLIC_PATHS:
        return None
    if not _is_authenticated():
        if p.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 401
        return redirect("/login")
    return None


# ═════════════════════════════════════════════════════════════════
#  GITHUB  (cache-branch persistence)
# ═════════════════════════════════════════════════════════════════

def _gh_headers():
    return {
        "Authorization": f"Bearer {config.GITHUB_BACKUP_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "nse-corporate-announcements",
    }


def _gh_raw_headers():
    return {
        "Authorization": f"Bearer {config.GITHUB_BACKUP_TOKEN}",
        "Accept": "application/vnd.github.raw",
        "User-Agent": "nse-corporate-announcements",
    }


def gh_get(filename):
    """
    Fetch a file from the cache branch. Returns the raw text, or None.
    Uses the raw media type so files >1 MB work without base64 bloat.
    """
    if not config.GITHUB_BACKUP_TOKEN:
        return None
    url = f"{GH_API}/repos/{config.GITHUB_REPO}/contents/{filename}"
    url += f"?ref={config.GITHUB_CACHE_BRANCH}"
    try:
        r = requests.get(url, headers=_gh_raw_headers(), timeout=60)
        if r.status_code != 200:
            logger.warning(f"gh_get({filename}): HTTP {r.status_code}")
            return None
        return r.text
    except Exception as e:
        logger.warning(f"gh_get({filename}): {e}")
        return None


def gh_put(filename, content, message):
    """
    Create or update a file on the cache branch.
    Returns True on success, False otherwise.
    """
    if not config.GITHUB_BACKUP_TOKEN:
        return False

    url = f"{GH_API}/repos/{config.GITHUB_REPO}/contents/{filename}"
    url += f"?ref={config.GITHUB_CACHE_BRANCH}"

    sha = None
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=15)
        if r.status_code == 200:
            sha = r.json().get("sha")
        elif r.status_code != 404:
            logger.warning(f"gh_put({filename}) lookup: HTTP {r.status_code}")
            return False
    except Exception as e:
        logger.warning(f"gh_put({filename}) lookup: {e}")
        return False

    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": config.GITHUB_CACHE_BRANCH,
    }
    if sha:
        body["sha"] = sha

    try:
        r = requests.put(url, headers=_gh_headers(), json=body, timeout=60)
        if r.status_code in (200, 201):
            return True
        logger.warning(f"gh_put({filename}): HTTP {r.status_code} {r.text[:120]}")
        return False
    except Exception as e:
        logger.warning(f"gh_put({filename}): {e}")
        return False


# ── Push helpers (fire-and-forget threads) ───────────────────────

def push_announcements():
    rows = announcements.get_all_for_backup()
    if not rows:
        return
    payload = json.dumps(rows, default=str)
    if gh_put(GH_ANN_FILE, payload, f"Announcements: {len(rows)} rows"):
        logger.info(f"Pushed {len(rows)} announcements to GitHub")


def push_views():
    rows = announcements.get_views_for_backup()
    payload = json.dumps(rows, default=str)
    if gh_put(GH_VIEWS_FILE, payload, f"Topic views: {len(rows)}"):
        logger.info(f"Pushed {len(rows)} topic views to GitHub")


def push_settings():
    settings = telegram_notify.get_settings_for_backup()
    payload = json.dumps(settings, default=str)
    if gh_put(GH_SETTINGS_FILE, payload, "Notify settings"):
        logger.info("Pushed notify settings to GitHub")


def _bg(fn):
    """Run a push function in a daemon thread — never block a request."""
    threading.Thread(target=fn, daemon=True).start()


# ── Restore helpers (called once at startup) ─────────────────────

def restore_announcements():
    content = gh_get(GH_ANN_FILE)
    if not content:
        logger.info("Announcements restore: no backup found")
        return 0
    try:
        data = json.loads(content)
        if not isinstance(data, list):
            logger.warning("Announcements restore: invalid payload")
            return 0
        n = announcements.restore_from_backup(data)
        logger.info(f"Announcements restore: {n} rows inserted")
        return n
    except Exception as e:
        logger.error(f"Announcements restore failed: {e}")
        return 0


def restore_views():
    content = gh_get(GH_VIEWS_FILE)
    if not content:
        logger.info("Topic views restore: no backup found")
        return 0
    try:
        data = json.loads(content)
        if not isinstance(data, list):
            logger.warning("Topic views restore: invalid payload")
            return 0
        n = announcements.restore_views_from_backup(data)
        logger.info(f"Topic views restore: {n} rows restored")
        return n
    except Exception as e:
        logger.error(f"Topic views restore failed: {e}")
        return 0


def restore_settings():
    content = gh_get(GH_SETTINGS_FILE)
    if not content:
        logger.info("Settings restore: no backup found")
        return False
    try:
        data = json.loads(content)
        ok = telegram_notify.restore_settings_from_backup(data)
        logger.info(f"Settings restore: {'ok' if ok else 'skipped'}")
        return ok
    except Exception as e:
        logger.error(f"Settings restore failed: {e}")
        return False


# ═════════════════════════════════════════════════════════════════
#  ROUTES — AUTH
# ═════════════════════════════════════════════════════════════════

@app.route("/login")
def login_page():
    if _is_authenticated():
        return redirect("/announcements")
    return render_template("login.html")


@app.route("/api/send_otp", methods=["POST"])
def send_otp():
    ip = _client_ip()
    now = time.time()

    state = _read_otp_state()
    existing = state.get(ip)
    if existing and now - existing.get("sent_at", 0) < config.OTP_THROTTLE_SECONDS:
        wait = int(config.OTP_THROTTLE_SECONDS - (now - existing["sent_at"]))
        return jsonify({"status": "error", "message": f"Wait {wait}s"}), 429

    code = f"{random.randint(0, 10 ** config.OTP_DIGITS - 1):0{config.OTP_DIGITS}d}"
    state[ip] = {
        "code": code,
        "expiry": now + config.OTP_VALIDITY_SECONDS,
        "sent_at": now,
        "attempts": 0,
    }
    _write_otp_state(state)

    telegram_notify.send_message(
        f"<b>Login OTP</b>\n"
        f"Code: <b>{code}</b>\n"
        f"Valid {config.OTP_VALIDITY_SECONDS // 60} min."
    )
    return jsonify({"status": "ok"})


@app.route("/api/verify_otp", methods=["POST"])
def verify_otp():
    code = str((request.json or {}).get("code", "")).strip()
    ip = _client_ip()
    now = time.time()

    state = _read_otp_state()
    entry = state.get(ip)

    if not entry or entry.get("expiry", 0) < now:
        return jsonify({"status": "error", "message": "No OTP or expired"}), 401

    entry["attempts"] = entry.get("attempts", 0) + 1
    if entry["attempts"] > config.OTP_MAX_ATTEMPTS:
        state.pop(ip, None)
        _write_otp_state(state)
        return jsonify({"status": "error", "message": "Too many attempts"}), 401

    if entry["code"] != code:
        state[ip] = entry
        _write_otp_state(state)
        return jsonify({"status": "error", "message": "Invalid code"}), 401

    state.pop(ip, None)
    _write_otp_state(state)

    resp = make_response(jsonify({"status": "ok"}))
    https = (
        request.headers.get("X-Forwarded-Proto") == "https"
        or request.is_secure
    )
    resp.set_cookie(
        "session_id",
        _make_session_token(),
        max_age=config.SESSION_DURATION_SECONDS,
        httponly=True,
        samesite="Lax",
        secure=https,
        path="/",
    )
    telegram_notify.send_message(f"Login from <code>{ip}</code>")
    return resp


@app.route("/api/logout", methods=["POST"])
def logout():
    resp = make_response(jsonify({"status": "ok"}))
    resp.set_cookie("session_id", "", max_age=0, path="/")
    return resp


# ═════════════════════════════════════════════════════════════════
#  ROUTES — HEALTH & ROOT
# ═════════════════════════════════════════════════════════════════

@app.route("/api/health")
def health():
    age = int(time.time() - _get_last_tick())
    if age > 900:
        return jsonify({"status": "error", "age": age}), 503
    return jsonify({"status": "ok", "age": age}), 200


@app.route("/")
def root():
    return redirect("/announcements")


# ═════════════════════════════════════════════════════════════════
#  ROUTES — PAGES  (placeholders until Steps 10–12)
# ═════════════════════════════════════════════════════════════════

_PLACEHOLDER_STYLE = """
<style>
    body { font-family: -apple-system, Segoe UI, Roboto, sans-serif;
           padding: 40px; background: #f8f9fa; color: #202124; }
    h2   { margin: 0 0 8px 0; }
    p    { color: #5f6368; margin: 0 0 16px 0; }
    a    { color: #1a73e8; text-decoration: none; margin-right: 16px; }
    a:hover { text-decoration: underline; }
    code { background: #f1f3f4; padding: 2px 6px; border-radius: 4px; }
</style>
"""


def _placeholder(body_html):
    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>nse-corporate-announcements</title>{_PLACEHOLDER_STYLE}"
        f"</head><body>{body_html}</body></html>"
    )


@app.route("/announcements")
def announcements_page():
    body = (
        "<h2>Corporate Announcements</h2>"
        "<p>Summary page placeholder. The real template arrives in Step 11.</p>"
        "<p>"
        "<a href='/announcements/topic/Order%20Win'>Sample topic: Order Win</a>"
        "<a href='/announcements/topic/Dividend'>Sample topic: Dividend</a>"
        "</p>"
        "<p>JSON APIs are live:"
        " <code>/api/announcements/summary</code>"
        " <code>/api/announcements/topic/&lt;topic&gt;</code>"
        " <code>/api/notify_settings</code>"
        "</p>"
        "<p><a href='#' onclick=\"event.preventDefault();"
        "fetch('/api/logout',{method:'POST'})"
        ".finally(()=>location.href='/login');\">Log out</a></p>"
    )
    return _placeholder(body)


@app.route("/announcements/topic/<path:topic>")
def topic_page(topic):
    if topic not in config.TOPICS:
        return _placeholder(
            f"<h2>Unknown topic</h2>"
            f"<p><code>{topic}</code> is not one of the configured topics.</p>"
            f"<p><a href='/announcements'>Back to summary</a></p>"
        ), 404
    body = (
        f"<h2>{topic}</h2>"
        f"<p>Topic detail placeholder. The real template arrives in Step 12.</p>"
        f"<p><a href='/announcements'>Back to summary</a></p>"
        f"<p>JSON API: <code>/api/announcements/topic/{topic}</code></p>"
    )
    return _placeholder(body)


# ═════════════════════════════════════════════════════════════════
#  ROUTES — JSON APIS
# ═════════════════════════════════════════════════════════════════

def _int_arg(name, default):
    raw = request.args.get(name, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@app.route("/api/announcements/summary")
def api_summary():
    days = _int_arg("days", config.DEFAULT_RANGE_DAYS)
    symbol = (request.args.get("symbol") or "").strip()
    impact = (request.args.get("impact") or "All").strip()
    rows = announcements.query_summary(
        days=days,
        symbol=symbol or None,
        impact=impact if impact != "All" else None,
    )
    return jsonify({
        "days": days,
        "symbol": symbol,
        "impact": impact,
        "rows": rows,
    })


@app.route("/api/announcements/topic/<path:topic>")
def api_topic(topic):
    if topic not in config.TOPICS:
        return jsonify({"error": "unknown topic", "topic": topic}), 404

    days = _int_arg("days", config.DEFAULT_RANGE_DAYS)
    symbol = (request.args.get("symbol") or "").strip()
    impact = (request.args.get("impact") or "All").strip()
    limit = _int_arg("limit", config.MAX_ROWS_PER_PAGE)
    offset = _int_arg("offset", 0)

    rows = announcements.query_topic(
        topic=topic,
        days=days,
        symbol=symbol or None,
        impact=impact if impact != "All" else None,
        limit=limit,
        offset=offset,
    )
    return jsonify({
        "topic": topic,
        "days": days,
        "count": len(rows),
        "offset": offset,
        "limit": limit,
        "rows": rows,
    })


@app.route("/api/announcements/mark_viewed/<path:topic>", methods=["POST"])
def api_mark_viewed(topic):
    if topic not in config.TOPICS:
        return jsonify({"error": "unknown topic", "topic": topic}), 404
    announcements.mark_topic_viewed(topic)
    _bg(push_views)
    return jsonify({"status": "ok", "topic": topic})


@app.route("/api/announcements/refresh", methods=["POST"])
def api_refresh():
    result = announcements.run_fetch_cycle()
    _stamp_tick()

    notify_result = {"sent": 0, "skipped": 0, "failed": 0}
    if result["new_items"]:
        notify_result = telegram_notify.notify_items(result["new_items"])

    if result["new"]:
        _bg(push_announcements)

    return jsonify({
        "status": "ok",
        "fetched": result["fetched"],
        "new": result["new"],
        "notify": notify_result,
    })


@app.route("/api/notify_settings", methods=["GET"])
def api_notify_settings_get():
    settings = telegram_notify.load_notify_settings()
    return jsonify({
        "ticked": settings.get("ticked", []),
        "updated_at": settings.get("updated_at", ""),
        "all_topics": config.TOPICS,
    })


@app.route("/api/notify_settings", methods=["POST"])
def api_notify_settings_set():
    body = request.json or {}
    ticked = body.get("ticked", [])
    if not isinstance(ticked, list):
        return jsonify({"status": "error", "message": "ticked must be a list"}), 400
    saved = telegram_notify.save_notify_settings(ticked)
    _bg(push_settings)
    return jsonify({
        "status": "ok",
        "ticked": saved.get("ticked", []),
        "updated_at": saved.get("updated_at", ""),
    })


# ═════════════════════════════════════════════════════════════════
#  BACKGROUND THREADS
# ═════════════════════════════════════════════════════════════════

def fetcher_thread():
    """
    Every POLL_INTERVAL_SECONDS:
      1. Fetch / save / prune
      2. Stamp _last_tick for health
      3. Send Telegram notifications for new items with ticked topics
      4. Push the announcements table to GitHub if anything was new
    """
    time.sleep(10)
    logger.info(f"Fetcher thread started — interval {config.POLL_INTERVAL_SECONDS}s")

    while True:
        try:
            result = announcements.run_fetch_cycle()
            _stamp_tick()

            if result["new_items"]:
                notify_result = telegram_notify.notify_items(result["new_items"])
                logger.info(
                    f"Cycle: fetched={result['fetched']} "
                    f"new={result['new']} "
                    f"notify={notify_result}"
                )
            else:
                logger.info(f"Cycle: fetched={result['fetched']} new=0")

            if result["new"]:
                push_announcements()

        except Exception as e:
            logger.exception(f"fetcher_thread error: {e}")

        time.sleep(config.POLL_INTERVAL_SECONDS)


def startup_thread():
    """
    Runs once, briefly after boot. Restores all three backups from
    the GitHub cache branch before the fetcher starts doing real work.
    """
    time.sleep(3)
    logger.info("Startup restore beginning")
    try:
        restore_settings()
        restore_views()
        restore_announcements()
    except Exception as e:
        logger.exception(f"startup restore error: {e}")
    logger.info("Startup restore complete")


# ═════════════════════════════════════════════════════════════════
#  INIT
# ═════════════════════════════════════════════════════════════════

announcements.init_db()

threading.Thread(target=startup_thread, daemon=True).start()
threading.Thread(target=fetcher_thread, daemon=True).start()


# ═════════════════════════════════════════════════════════════════
#  LOCAL DEV ENTRY
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)