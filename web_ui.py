"""
web_ui.py — Flask application entry point.
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
from datetime import datetime, timedelta

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

GH_ANN_FILE = config.GITHUB_ANN_FILE          # legacy single-file backup
ANN_DIR = "data/ann"                          # one small file per day
ANN_INDEX = f"{ANN_DIR}/_index.json"          # {date: content-hash}
GH_SETTINGS_FILE = config.GITHUB_SETTINGS_FILE
GH_VIEWS_FILE = "topic_views_backup.json"

GH_API = "https://api.github.com"

_last_tick = time.time()
_last_tick_lock = threading.Lock()

# Set by startup_thread once restore is complete. fetcher_thread waits
# on this event before its first cycle, so it never competes with the
# restore for the SQLite write lock.
_restore_complete = threading.Event()


def _stamp_tick():
    global _last_tick
    with _last_tick_lock:
        _last_tick = time.time()


def _get_last_tick():
    with _last_tick_lock:
        return _last_tick


# ═════════════════════════════════════════════════════════════════
#  OTP STATE
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
#  GITHUB
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
    if not config.GITHUB_BACKUP_TOKEN:
        return None
    url = f"{GH_API}/repos/{config.GITHUB_REPO}/contents/{filename}"
    url += f"?ref={config.GITHUB_CACHE_BRANCH}"
    try:
        r = requests.get(url, headers=_gh_raw_headers(), timeout=90)
        if r.status_code != 200:
            logger.warning(f"gh_get({filename}): HTTP {r.status_code}")
            return None
        return r.text
    except Exception as e:
        logger.warning(f"gh_get({filename}): {e}")
        return None


_gh_put_lock = threading.Lock()


def gh_put(filename, content, message):
    # Pushes can fire from several threads at once (the fetcher loop, plus the
    # _bg() calls in the /api/... routes). Serialise them, or two force-updates
    # can race and silently drop one file.
    with _gh_put_lock:
        return _gh_commit({filename: content}, [], message)


def _gh_commit(files, deletes, message):
    """One commit carrying `files` (added/updated) and `deletes` (removed),
    with NO parent, force-updated onto the cache branch.

    Keeping it to a single commit is what stops the branch growing history; the
    caller passes only the files that actually changed, so pushes stay small.
    """
    if not config.GITHUB_BACKUP_TOKEN:
        return False

    repo = config.GITHUB_REPO
    branch = config.GITHUB_CACHE_BRANCH

    # current tree, so everything we're not touching is preserved
    base_tree = None
    r = requests.get(
        f"{GH_API}/repos/{repo}/git/ref/heads/{branch}",
        headers=_gh_headers(), timeout=15,
    )
    if r.status_code == 200:
        commit_sha = r.json()["object"]["sha"]
        r2 = requests.get(
            f"{GH_API}/repos/{repo}/git/commits/{commit_sha}",
            headers=_gh_headers(), timeout=15,
        )
        if r2.status_code == 200:
            base_tree = r2.json()["tree"]["sha"]

    entries = []
    for path, content in files.items():
        r = requests.post(
            f"{GH_API}/repos/{repo}/git/blobs",
            headers=_gh_headers(),
            json={"content": content, "encoding": "utf-8"},
            timeout=120,
        )
        if r.status_code != 201:
            logger.warning(f"gh_commit blob {path}: HTTP {r.status_code}")
            return False
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": r.json()["sha"]})

    for path in deletes:
        # sha=None removes the path from the tree
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})

    tree_body = {"tree": entries}
    if base_tree:
        tree_body["base_tree"] = base_tree
    r = requests.post(
        f"{GH_API}/repos/{repo}/git/trees",
        headers=_gh_headers(), json=tree_body, timeout=60,
    )
    if r.status_code != 201:
        logger.warning(f"gh_commit tree: HTTP {r.status_code}")
        return False
    tree_sha = r.json()["sha"]

    r = requests.post(
        f"{GH_API}/repos/{repo}/git/commits",
        headers=_gh_headers(),
        json={"message": message, "tree": tree_sha},
        timeout=60,
    )
    if r.status_code != 201:
        logger.warning(f"gh_commit commit: HTTP {r.status_code}")
        return False
    new_commit = r.json()["sha"]

    r = requests.patch(
        f"{GH_API}/repos/{repo}/git/refs/heads/{branch}",
        headers=_gh_headers(),
        json={"sha": new_commit, "force": True},
        timeout=30,
    )
    return r.status_code in (200, 201)


def _ann_payload(rows):
    return json.dumps(rows, default=str, separators=(",", ":"))


def push_announcements():
    """Store announcements as one small file per day and push only the day
    files that changed — a few hundred KB, never the whole table."""
    rows = announcements.get_all_for_backup()
    if not rows:
        return

    by_date = {}
    for r in rows:
        d = str(r.get("announced_at", ""))[:10]
        if d:
            by_date.setdefault(d, []).append(r)
    if not by_date:
        return

    idx_raw = gh_get(ANN_INDEX)
    stored = {}
    if idx_raw:
        try:
            stored = (json.loads(idx_raw) or {}).get("dates", {}) or {}
        except Exception:
            stored = {}

    files, new_index, changed = {}, {}, 0
    for d, day_rows in by_date.items():
        payload = _ann_payload(day_rows)
        h = hashlib.sha1(payload.encode("utf-8")).hexdigest()
        new_index[d] = h
        if stored.get(d) != h:
            files[f"{ANN_DIR}/{d}.json"] = payload
            changed += 1

    # prune day-files that have aged out of the retention window
    cutoff = (datetime.now(config.TIMEZONE) - timedelta(days=config.RETENTION_DAYS)).strftime("%Y-%m-%d")
    deletes = []
    for d in list(stored.keys()):
        if d < cutoff:
            deletes.append(f"{ANN_DIR}/{d}.json")
            new_index.pop(d, None)

    if not files and not deletes:
        return

    files[ANN_INDEX] = json.dumps({"dates": new_index}, separators=(",", ":"))
    with _gh_put_lock:
        ok = _gh_commit(files, deletes, f"Announcements: {changed} day-file(s)")
    if ok:
        logger.info(f"Pushed {changed} day-file(s), pruned {len(deletes)}")


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
    threading.Thread(target=fn, daemon=True).start()


def restore_announcements():
    idx_raw = gh_get(ANN_INDEX)
    dates = []
    if idx_raw:
        try:
            dates = sorted((json.loads(idx_raw) or {}).get("dates", {}).keys())
        except Exception:
            dates = []

    if not dates:
        # legacy single-file backup (before per-day files)
        content = gh_get(GH_ANN_FILE)
        if not content:
            logger.info("Announcements restore: no backup found")
            return 0
        try:
            data = json.loads(content)
        except Exception as e:
            logger.error(f"Announcements restore failed: {e}")
            return 0
        if not isinstance(data, list):
            logger.warning("Announcements restore: invalid payload")
            return 0
        n = announcements.restore_from_backup(data)
        logger.info(f"Announcements restore (legacy file): {n} rows")
        return n

    total = 0
    for d in dates:
        content = gh_get(f"{ANN_DIR}/{d}.json")
        if not content:
            continue
        try:
            data = json.loads(content)
        except Exception:
            continue
        if isinstance(data, list):
            total += announcements.restore_from_backup(data)
    logger.info(f"Announcements restore: {total} rows from {len(dates)} day-files")
    return total


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


@app.route("/api/nse_probe")
def api_nse_probe():
    """Temporary diagnostic: which HTTP client profile can reach NSE from
    this host. Sits behind the login gate like everything else."""
    return jsonify({
        "nse": announcements.probe_nse(),
        "bse": announcements.probe_bse(),
        "relay": announcements.probe_relay(),
    })


@app.route("/")
def root():
    return redirect("/announcements")


# ═════════════════════════════════════════════════════════════════
#  ROUTES — PAGES
# ═════════════════════════════════════════════════════════════════

@app.route("/announcements")
def announcements_page():
    return render_template("announcements.html")


@app.route("/announcements/topic/<path:topic>")
def topic_page(topic):
    if topic not in config.TOPICS:
        return redirect("/announcements")
    return render_template("topic.html")


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


def _date_args():
    return (
        (request.args.get("from_date") or "").strip(),
        (request.args.get("to_date") or "").strip(),
    )


@app.route("/api/announcements/summary")
def api_summary():
    days = _int_arg("days", config.DEFAULT_RANGE_DAYS)
    symbol = (request.args.get("symbol") or "").strip()
    impact = (request.args.get("impact") or "All").strip()
    from_date, to_date = _date_args()

    rows = announcements.query_summary(
        days=days,
        symbol=symbol or None,
        impact=impact if impact != "All" else None,
        from_date=from_date or None,
        to_date=to_date or None,
    )
    return jsonify({
        "days": days,
        "from_date": from_date,
        "to_date": to_date,
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
    from_date, to_date = _date_args()

    rows = announcements.query_topic(
        topic=topic,
        days=days,
        symbol=symbol or None,
        impact=impact if impact != "All" else None,
        limit=limit,
        offset=offset,
        from_date=from_date or None,
        to_date=to_date or None,
    )
    total = announcements.count_topic(
        topic=topic,
        days=days,
        symbol=symbol or None,
        impact=impact if impact != "All" else None,
        from_date=from_date or None,
        to_date=to_date or None,
    )
    return jsonify({
        "topic": topic,
        "days": days,
        "from_date": from_date,
        "to_date": to_date,
        "total": total,
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
    mode = (request.args.get("mode") or "").strip().lower()
    force = (mode == "backfill")

    # Wait up to 60s for restore to complete before triggering fetch
    _restore_complete.wait(timeout=60)

    result = announcements.run_fetch_cycle(force_backfill=force)
    _stamp_tick()

    notify_result = {"sent": 0, "skipped": 0, "failed": 0}
    if result["new_items"]:
        notify_result = telegram_notify.notify_items(result["new_items"])

    if result["new"]:
        _bg(push_announcements)

    return jsonify({
        "status": "ok",
        "mode": result.get("mode", "default"),
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
    Waits for the restore to finish before its first cycle.
    Without this, the first fetch competes with restore for the
    SQLite write lock and fails with 'database is locked'.
    """
    _restore_complete.wait()

    # Small grace period so the WAL has a moment to settle
    time.sleep(5)

    logger.info(f"Fetcher thread started — interval {config.POLL_INTERVAL_SECONDS}s")

    while True:
        try:
            result = announcements.run_fetch_cycle()
            _stamp_tick()

            if result["new_items"]:
                notify_result = telegram_notify.notify_items(result["new_items"])
                logger.info(
                    f"Cycle ({result.get('mode','default')}): "
                    f"fetched={result['fetched']} "
                    f"new={result['new']} "
                    f"notify={notify_result}"
                )
            else:
                logger.info(
                    f"Cycle ({result.get('mode','default')}): "
                    f"fetched={result['fetched']} new=0"
                )

            if result["new"]:
                push_announcements()

        except Exception as e:
            logger.exception(f"fetcher_thread error: {e}")

        time.sleep(config.POLL_INTERVAL_SECONDS)


def startup_thread():
    time.sleep(3)
    logger.info("Startup restore beginning")
    try:
        restore_settings()
        restore_views()
        restore_announcements()
        announcements.reclassify_all()
    except Exception as e:
        logger.exception(f"startup restore error: {e}")
    finally:
        # Always release the gate — even if restore errored — so the
        # fetcher isn't blocked forever.
        _restore_complete.set()
    logger.info("Startup restore complete")


# ═════════════════════════════════════════════════════════════════
#  INIT
# ═════════════════════════════════════════════════════════════════

announcements.init_db()

threading.Thread(target=startup_thread, daemon=True).start()
threading.Thread(target=fetcher_thread, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)