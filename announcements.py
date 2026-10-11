"""
announcements.py — NSE corporate announcements data layer.

Responsibilities:
  • NSE session cookie management (refresh on TTL)
  • Fetch the corporate-announcements feed
  • Classify each announcement into a topic and an impact direction
  • SQLite persistence with deduplication
  • Read helpers for the summary page and the topic detail page
  • Unread tracking per topic
  • Retention pruning
  • Backup / restore helpers for the GitHub cache branch

This module has no Flask dependencies. It is imported by web_ui.py and by
the background fetcher thread. Nothing in here talks to Telegram — that
lives in telegram_notify.py.
"""

import re
import time
import sqlite3
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

import requests

import config

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════
#  TIME HELPERS
# ═════════════════════════════════════════════════════════════════

def _now():
    return datetime.now(config.TIMEZONE)


def _now_str():
    return _now().strftime("%Y-%m-%d %H:%M:%S")


def _range_bounds(days):
    """Return (from_date, to_date) as 'YYYY-MM-DD' strings in IST."""
    now = _now()
    start = now - timedelta(days=int(days))
    return start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")


_DATE_FORMATS = [
    "%d-%b-%Y %H:%M:%S",
    "%d-%b-%Y %H:%M",
    "%d-%b-%Y",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d-%m-%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
]


def _normalize_date(raw):
    """Normalize any NSE date string to 'YYYY-MM-DD HH:MM:SS'. Returns '' on failure."""
    if not raw:
        return ""
    raw = str(raw).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return ""


# ═════════════════════════════════════════════════════════════════
#  NSE SESSION
# ═════════════════════════════════════════════════════════════════

_session = None
_session_ts = 0.0
_session_lock = threading.Lock()


def _get_session():
    """Return a requests.Session with fresh NSE cookies, or None on failure."""
    global _session, _session_ts
    now = time.time()
    with _session_lock:
        if _session is not None and (now - _session_ts) < config.NSE_SESSION_TTL_SECONDS:
            return _session

        s = requests.Session()
        s.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": config.NSE_REFERER,
        })
        try:
            s.get(config.NSE_BASE, timeout=config.NSE_COOKIE_WARMUP_TIMEOUT)
            s.get(config.NSE_REFERER, timeout=config.NSE_COOKIE_WARMUP_TIMEOUT)
        except Exception as e:
            logger.warning(f"NSE cookie warm-up failed: {e}")
            _session = None
            return None

        _session = s
        _session_ts = now
        logger.info("NSE session established")
        return _session


# ═════════════════════════════════════════════════════════════════
#  CLASSIFIER
# ═════════════════════════════════════════════════════════════════

# Rules are ordered most-specific → most-generic. First match wins.
# Example: "Board meeting to consider dividend" hits Dividend first,
# which is the more useful signal than Board Meeting.
_TOPIC_RULES = [
    ("Dividend",       r"\bdividend\b"),
    ("Bonus Issue",    r"\bbonus\b"),
    ("Rights Issue",   r"\brights?\s+issue\b"
                       r"|\bpreferential\s+(?:issue|allotment)\b"
                       r"|\bqip\b"),
    ("Buyback",        r"\bbuy[\s-]?back\b"),
    ("Demerger",       r"\bdemerger\b|\bde-merger\b|\bspin[\s-]?off\b"
                       r"|\bscheme\s+of\s+arrangement\b"),
    ("Acquisition",    r"\bacquisition\b|\bamalgamation\b|\bmerger\b"
                       r"|\btakeover\b|\bsubsidiary\b"),
    ("Order Win",      r"\b(?:wins?|winning|awarded|secures?|bags?|receives?|received)\b"
                       r".{0,60}\b(?:order|contract|project|tender|loa|loi|work)\b"
                       r"|\b(?:order|contract|project|tender)\b.{0,60}"
                       r"\b(?:win|award|receiv|secur|bag)\w*\b"),
    ("Capex",          r"\bcapex\b|\bcapital\s+expenditure\b|\bexpansion\b"
                       r"|\bgreenfield\b|\bbrownfield\b|\bnew\s+plant\b"
                       r"|\bfund\s+raising\b"),
    ("Tax/Regulatory", r"\bpenalt(?:y|ies)\b|\bsebi\b|\brbi\b|\bgst\b"
                       r"|\bshow[\s-]?cause\b|\bcompliance\b|\bdefault\b"
                       r"|\bdowngrade\b|\bresignation\b"),
    ("Earnings",       r"\bfinancial\s+results?\b|\bq[1-4]\b|\bquarterly\b"
                       r"|\bannual\s+results?\b|\bresults?\b|\bprofit\b"
                       r"|\brevenue\b|\bebitda\b"),
    ("AGM",            r"\bannual\s+general\s+meeting\b|\bagm\b|\begm\b"
                       r"|\bextraordinary\s+general\s+meeting\b"),
    ("Board Meeting",  r"\bboard\s+meeting\b"),
]


_IMPACT_RULES = [
    ("positive", r"\b(?:win|award|secure|receiv|approv|commission|expand"
                 r"|acquisition|invest|buyback|bonus|dividend|order|contract"
                 r"|record|highest|milestone|partnership|agreement|launch"
                 r"|success|growth|profit|surge|jump|rise|recommend)\w*\b"),
    ("negative", r"\b(?:penalt|show[\s-]?cause|default|downgrad|resign"
                 r"|terminat|cancel|withdraw|lawsuit|litigation|fraud|scam"
                 r"|lost|loss|decline|fall|drop|slump|delay|postpone)\w*\b"),
]


def classify_topic(subject):
    s = (subject or "").lower()
    for topic, pat in _TOPIC_RULES:
        if re.search(pat, s, re.IGNORECASE):
            return topic
    return "Other"


def classify_impact(subject):
    s = (subject or "").lower()
    for direction, pat in _IMPACT_RULES:
        if re.search(pat, s, re.IGNORECASE):
            return direction
    return "neutral"


# ═════════════════════════════════════════════════════════════════
#  DATABASE
# ═════════════════════════════════════════════════════════════════

@contextmanager
def db():
    c = sqlite3.connect(config.DB_FILE, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS announcements (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol        TEXT NOT NULL,
                company       TEXT,
                subject       TEXT NOT NULL,
                topic         TEXT NOT NULL,
                impact        TEXT NOT NULL DEFAULT 'neutral',
                announced_at  TEXT NOT NULL,
                attachment    TEXT,
                seen_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(symbol, subject, announced_at)
            );
            CREATE INDEX IF NOT EXISTS idx_ann_topic  ON announcements(topic);
            CREATE INDEX IF NOT EXISTS idx_ann_symbol ON announcements(symbol);
            CREATE INDEX IF NOT EXISTS idx_ann_date   ON announcements(announced_at);
            CREATE INDEX IF NOT EXISTS idx_ann_seen   ON announcements(seen_at);

            CREATE TABLE IF NOT EXISTS topic_views (
                topic         TEXT PRIMARY KEY,
                viewed_at     TIMESTAMP NOT NULL
            );
        """)
    logger.info("Announcements DB ready")


# ═════════════════════════════════════════════════════════════════
#  FETCHER
# ═════════════════════════════════════════════════════════════════

_fetch_lock = threading.Lock()


def _parse_payload(payload):
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []

    out = []
    for item in rows:
        try:
            symbol = (item.get("symbol") or item.get("sm_symbol") or "").strip().upper()
            subject = (item.get("subject") or item.get("desc") or "").strip()
            if not symbol or not subject:
                continue

            raw_date = item.get("an_dt") or item.get("exchdisstime") or ""
            announced_at = _normalize_date(raw_date) or _now_str()

            out.append({
                "symbol":       symbol,
                "company":      (item.get("sm_name") or "").strip(),
                "subject":      subject,
                "topic":        classify_topic(subject),
                "impact":       classify_impact(subject),
                "announced_at": announced_at,
                "attachment":   (item.get("attchmntFile") or "").strip(),
            })
        except Exception:
            continue
    return out


def fetch_announcements():
    """Fetch raw announcements from NSE. Returns list of dicts (may be empty)."""
    s = _get_session()
    if s is None:
        return []
    try:
        r = s.get(config.NSE_ANNOUNCEMENTS_URL, timeout=config.NSE_REQUEST_TIMEOUT)
        if r.status_code != 200:
            logger.warning(f"NSE announcements HTTP {r.status_code}")
            return []
        payload = r.json()
    except Exception as e:
        logger.warning(f"NSE announcements fetch failed: {e}")
        return []
    return _parse_payload(payload)


# ═════════════════════════════════════════════════════════════════
#  SAVE
# ═════════════════════════════════════════════════════════════════

def save_announcements(items):
    """
    Insert new rows, ignore duplicates.
    Returns the subset of items that were genuinely new (i.e. inserted now).
    """
    if not items:
        return []
    new_items = []
    with db() as c:
        for it in items:
            cur = c.execute("""
                INSERT OR IGNORE INTO announcements
                    (symbol, company, subject, topic, impact, announced_at, attachment)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                it["symbol"],
                it.get("company", ""),
                it["subject"],
                it["topic"],
                it["impact"],
                it["announced_at"],
                it.get("attachment", ""),
            ))
            if cur.rowcount == 1:
                new_items.append(it)
    return new_items


# ═════════════════════════════════════════════════════════════════
#  QUERIES
# ═════════════════════════════════════════════════════════════════

def query_summary(days=None, symbol=None, impact=None):
    """
    Return one dict per topic in config.TOPICS:
        {'topic': str, 'total': int, 'new': int, 'latest': 'YYYY-MM-DD HH:MM:SS'}
    Topics with zero announcements in the range are included with total=0.
    """
    if days is None:
        days = config.DEFAULT_RANGE_DAYS
    from_d, to_d = _range_bounds(days)

    where = ["announced_at >= ?", "announced_at <= ?"]
    params = [from_d, to_d + " 23:59:59"]
    if symbol:
        where.append("UPPER(symbol) LIKE ?")
        params.append(f"%{symbol.upper()}%")
    if impact and impact != "All":
        where.append("impact = ?")
        params.append(impact)

    sql_totals = f"""
        SELECT topic,
               COUNT(*) AS total,
               MAX(announced_at) AS latest
        FROM announcements
        WHERE {' AND '.join(where)}
        GROUP BY topic
    """
    with db() as c:
        total_rows = c.execute(sql_totals, params).fetchall()

    by_topic = {r["topic"]: {"total": r["total"], "latest": r["latest"] or ""} for r in total_rows}

    views = get_topic_views()

    sql_unread = f"""
        SELECT topic, seen_at
        FROM announcements
        WHERE {' AND '.join(where)}
    """
    with db() as c:
        unread_rows = c.execute(sql_unread, params).fetchall()

    unread = {t: 0 for t in config.TOPICS}
    for r in unread_rows:
        t = r["topic"]
        seen_at = r["seen_at"] or ""
        last_viewed = views.get(t, "")
        if not last_viewed or seen_at > last_viewed:
            unread[t] = unread.get(t, 0) + 1

    return [
        {
            "topic":  topic,
            "total":  by_topic.get(topic, {}).get("total", 0),
            "new":    unread.get(topic, 0),
            "latest": by_topic.get(topic, {}).get("latest", ""),
        }
        for topic in config.TOPICS
    ]


def query_topic(topic, days=None, symbol=None, impact=None, limit=None, offset=0):
    """Return announcements for one topic in the range, newest first."""
    if days is None:
        days = config.DEFAULT_RANGE_DAYS
    if limit is None:
        limit = config.MAX_ROWS_PER_PAGE
    from_d, to_d = _range_bounds(days)

    where = ["topic = ?", "announced_at >= ?", "announced_at <= ?"]
    params = [topic, from_d, to_d + " 23:59:59"]
    if symbol:
        where.append("UPPER(symbol) LIKE ?")
        params.append(f"%{symbol.upper()}%")
    if impact and impact != "All":
        where.append("impact = ?")
        params.append(impact)

    sql = f"""
        SELECT id, symbol, company, subject, topic, impact, announced_at, attachment
        FROM announcements
        WHERE {' AND '.join(where)}
        ORDER BY announced_at DESC
        LIMIT ? OFFSET ?
    """
    params.extend([int(limit), int(offset)])

    with db() as c:
        rows = c.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ═════════════════════════════════════════════════════════════════
#  UNREAD TRACKING
# ═════════════════════════════════════════════════════════════════

def mark_topic_viewed(topic):
    """Stamp the given topic as viewed now. Called when the topic page opens."""
    now_str = _now_str()
    with db() as c:
        c.execute("""
            INSERT INTO topic_views (topic, viewed_at)
            VALUES (?, ?)
            ON CONFLICT(topic) DO UPDATE SET viewed_at = excluded.viewed_at
        """, (topic, now_str))
    logger.info(f"Marked topic viewed: {topic}")


def get_topic_views():
    """Return {topic: 'YYYY-MM-DD HH:MM:SS'} for every topic that has been viewed."""
    with db() as c:
        rows = c.execute("SELECT topic, viewed_at FROM topic_views").fetchall()
    return {r["topic"]: r["viewed_at"] for r in rows}


def count_unread_by_topic(days=None, symbol=None, impact=None):
    """
    Return {topic: unread_count} where unread means:
      seen_at > last_viewed_at[topic]
    A topic never viewed shows all its rows in the range as unread.
    """
    if days is None:
        days = config.DEFAULT_RANGE_DAYS
    from_d, to_d = _range_bounds(days)

    views = get_topic_views()

    where = ["announced_at >= ?", "announced_at <= ?"]
    params = [from_d, to_d + " 23:59:59"]
    if symbol:
        where.append("UPPER(symbol) LIKE ?")
        params.append(f"%{symbol.upper()}%")
    if impact and impact != "All":
        where.append("impact = ?")
        params.append(impact)

    sql = f"""
        SELECT topic, seen_at
        FROM announcements
        WHERE {' AND '.join(where)}
    """
    with db() as c:
        rows = c.execute(sql, params).fetchall()

    counts = {t: 0 for t in config.TOPICS}
    for r in rows:
        topic = r["topic"]
        seen_at = r["seen_at"] or ""
        last_viewed = views.get(topic, "")
        if not last_viewed or seen_at > last_viewed:
            counts[topic] = counts.get(topic, 0) + 1
    return counts


# ═════════════════════════════════════════════════════════════════
#  RETENTION
# ═════════════════════════════════════════════════════════════════

def prune_old():
    """Delete rows older than RETENTION_DAYS. Returns deleted count."""
    cutoff = (_now() - timedelta(days=config.RETENTION_DAYS)).strftime("%Y-%m-%d")
    with db() as c:
        cur = c.execute("DELETE FROM announcements WHERE announced_at < ?", (cutoff,))
        n = cur.rowcount
    if n:
        logger.info(f"Pruned {n} announcements older than {cutoff}")
    return n


# ═════════════════════════════════════════════════════════════════
#  PUBLIC ENTRY POINT
# ═════════════════════════════════════════════════════════════════

def run_fetch_cycle():
    """
    Orchestrate a full fetch → save → prune cycle.
    Returns dict:
        {
          'fetched': int,        # rows returned by NSE
          'new': int,            # rows genuinely inserted
          'new_items': [dict],   # the new items, for Telegram notification
        }
    """
    with _fetch_lock:
        items = fetch_announcements()
        if not items:
            return {"fetched": 0, "new": 0, "new_items": []}

        new_items = save_announcements(items)
        prune_old()

        logger.info(f"Fetch cycle: {len(items)} fetched, {len(new_items)} new")
        return {
            "fetched":   len(items),
            "new":       len(new_items),
            "new_items": new_items,
        }


# ═════════════════════════════════════════════════════════════════
#  BACKUP / RESTORE  (called by web_ui.py)
# ═════════════════════════════════════════════════════════════════

def get_all_for_backup():
    """Return all rows in the retention window, ordered by date."""
    cutoff = (_now() - timedelta(days=config.RETENTION_DAYS)).strftime("%Y-%m-%d")
    with db() as c:
        rows = c.execute("""
            SELECT symbol, company, subject, topic, impact,
                   announced_at, attachment, seen_at
            FROM announcements
            WHERE announced_at >= ?
            ORDER BY announced_at
        """, (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def restore_from_backup(items):
    """Insert rows restored from GitHub backup. Returns inserted count."""
    if not items:
        return 0
    inserted = 0
    with db() as c:
        for it in items:
            try:
                cur = c.execute("""
                    INSERT OR IGNORE INTO announcements
                        (symbol, company, subject, topic, impact, announced_at, attachment)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    it.get("symbol", ""),
                    it.get("company", ""),
                    it.get("subject", ""),
                    it.get("topic", "Other"),
                    it.get("impact", "neutral"),
                    it.get("announced_at", ""),
                    it.get("attachment", ""),
                ))
                inserted += cur.rowcount
            except Exception:
                continue
    logger.info(f"Restored {inserted} announcements from backup")
    return inserted


def get_views_for_backup():
    """Return the topic_views table for GitHub backup."""
    with db() as c:
        rows = c.execute("SELECT topic, viewed_at FROM topic_views").fetchall()
    return [dict(r) for r in rows]


def restore_views_from_backup(items):
    """Restore topic_views rows from a GitHub backup. Returns restored count."""
    if not items:
        return 0
    restored = 0
    with db() as c:
        for it in items:
            try:
                c.execute("""
                    INSERT INTO topic_views (topic, viewed_at)
                    VALUES (?, ?)
                    ON CONFLICT(topic) DO UPDATE SET viewed_at = excluded.viewed_at
                """, (it.get("topic", ""), it.get("viewed_at", "")))
                restored += 1
            except Exception:
                continue
    logger.info(f"Restored {restored} topic view records")
    return restored