"""
announcements.py — NSE corporate announcements data layer.
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
    now = _now()
    start = now - timedelta(days=int(days))
    return start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")


def _nse_date_fmt(dt):
    return dt.strftime("%d-%m-%Y")


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
#
#  NSE publishes announcements under fixed, structured category labels
#  (e.g., "Bagging/Receiving of orders/contracts"). We map those
#  directly. Anything not in the map falls through to regex rules and
#  finally to "Other".
# ═════════════════════════════════════════════════════════════════

# Exact-match lookup of NSE's fixed category labels → our topic.
# Keys are lowercase for case-insensitive matching.
_NSE_CATEGORY_MAP = {
    # Order wins
    "awarding of order(s)/contract(s)":                          "Order Win",
    "bagging/receiving of orders/contracts":                     "Order Win",
    "letter of award":                                            "Order Win",

    # Shareholding and related filings
    "issue of securities":                                        "Shareholding",
    "related party transactions":                                 "Shareholding",
    "trading plan under pit":                                     "Shareholding",
    "trading window":                                             "Shareholding",
    "monitoring agency report":                                   "Shareholding",
    "conversion":                                                 "Shareholding",
    "shareholding pattern":                                       "Shareholding",
    "disclosure under regulation 29":                             "Shareholding",
    "disclosure under regulation 30":                             "Shareholding",
    "disclosure under regulation 31":                             "Shareholding",
    "qualified institutional placement":                          "Rights Issue",
    "preferential issue":                                         "Rights Issue",

    # Appointments and governance changes
    "change in company secretary/compliance officer":             "Appointment",
    "change in auditors":                                         "Appointment",
    "change in director":                                         "Appointment",
    "change in management":                                       "Appointment",
    "demise":                                                     "Appointment",
    "appointment":                                                "Appointment",
    "resignation":                                                "Appointment",
    "cessation":                                                  "Appointment",

    # Regulatory / compliance / legal
    "statement of deviation(s) or variation(s) under reg. 32":    "Tax/Regulatory",
    "statement of deviation or variation under regulation 32":    "Tax/Regulatory",
    "pendency of litigation(s)/dispute(s) or the outcome impacting the company": "Tax/Regulatory",
    "action(s) taken or orders passed":                           "Tax/Regulatory",
    "action(s) initiated or orders passed":                       "Tax/Regulatory",
    "corporate insolvency resolution process":                    "Tax/Regulatory",
    "suspension of trading":                                      "Tax/Regulatory",
    "giving guarantees/indemnity/ becoming a surety for third party": "Tax/Regulatory",
    "disruption of operations":                                   "Tax/Regulatory",
    "defaults on payment of interest/principal":                  "Tax/Regulatory",
    "rescission/termination(s)":                                  "Tax/Regulatory",
    "granting/withdrawal/surrender/cancellation/suspension of key licenses/ regulatory approvals": "Tax/Regulatory",
    "penalty":                                                    "Tax/Regulatory",
    "show cause notice":                                          "Tax/Regulatory",
    "regulatory action":                                          "Tax/Regulatory",

    # Capex / business expansion
    "adoption of new line(s) of business":                        "Capex",
    "diversification/disinvestment":                              "Capex",
    "product launch":                                             "Capex",
    "utilisation of funds":                                       "Capex",
    "capacity addition":                                          "Capacity Addition",
    "commencement of commercial production":                      "Capacity Addition",
    "commercial production":                                      "Capacity Addition",
    "commissioning":                                              "Capacity Addition",

    # Acquisition / agreements / M&A
    "agreements":                                                 "Acquisition",
    "memorandum of understanding/agreements":                     "Acquisition",
    "sale or disposal":                                           "Acquisition",
    "scheme of arrangement":                                      "Demerger",

    # Delisting / demerger
    "delisting":                                                  "Demerger",
    "voluntary delisting":                                        "Demerger",

    # Press / disclosure
    "news verification":                                          "Press Release",
    "disclosure of material issue":                               "Press Release",
    "integrated filing- governance":                              "Press Release",
    "press release":                                              "Press Release",
    "media release":                                              "Press Release",

    # Meetings
    "shareholders meeting":                                       "AGM",
    "annual general meeting":                                     "AGM",
    "extraordinary general meeting":                              "AGM",
    "postal ballot":                                              "AGM",
    "committee meeting updates":                                  "Board Meeting",
    "board meeting":                                              "Board Meeting",

    # Corporate actions
    "record date":                                                "Dividend",
    "dividend":                                                   "Dividend",
    "bonus issue":                                                "Bonus Issue",
    "rights issue":                                               "Rights Issue",
    "buy back":                                                   "Buyback",
    "buyback":                                                    "Buyback",

    # Investor / newspaper / earnings
    "earnings call":                                              "Investor Communication",
    "analyst meet":                                               "Investor Communication",
    "investor presentation":                                      "Investor Communication",
    "copy of newspaper publication":                              "Newspaper Publication",
    "newspaper publication":                                      "Newspaper Publication",
    "newspaper advertisement":                                    "Newspaper Publication",
    "financial results":                                          "Earnings",
    "quarterly results":                                          "Earnings",
    "credit rating":                                              "Credit Rating",

    # Explicit "General Updates" bucket
    "general updates":                                            "General Updates",
    "general update":                                             "General Updates",
    "corrigendum":                                                "General Updates",
    "monthly business updates":                                   "General Updates",
    "monthly business update":                                    "General Updates",
    "business update":                                            "General Updates",
    "updates":                                                    "General Updates",
    "update":                                                     "General Updates",
    "others":                                                     "Other",
    "other":                                                      "Other",
}


# Fallback regex rules for anything not covered by the direct map.
# Ordered most-specific → most-generic.
_TOPIC_RULES = [
    ("Dividend",              r"\bdividend\b"),
    ("Bonus Issue",           r"\bbonus\b"),
    ("Rights Issue",          r"\brights?\s+issue\b"
                              r"|\bpreferential\s+(?:issue|allotment)\b"
                              r"|\bqip\b"),
    ("Buyback",               r"\bbuy[\s-]?back\b"),
    ("Demerger",              r"\bdemerger\b|\bde-merger\b|\bspin[\s-]?off\b"
                              r"|\bscheme\s+of\s+arrangement\b"),
    ("Acquisition",           r"\bacquisition\b|\bamalgamation\b|\bmerger\b"
                              r"|\btakeover\b|\bsubsidiary\b|\bjoint\s+venture\b"
                              r"|\bmoU\b|\bmemorandum\s+of\s+understanding\b"),
    ("Shareholding",          r"\bshareholding\b"
                              r"|\ballotment\s+of\s+(?:securities|shares|equity)\b"
                              r"|\binsider\s+trading\b"
                              r"|\bregulation\s+(?:7|29|30|31)\b"
                              r"|\bpromoter\s+(?:holding|stake|pledge)\b"
                              r"|\besop\b|\bsweat\s+equity\b"
                              r"|\bconversion\s+of\s+(?:warrants|debentures)\b"
                              r"|\bdisclosure\s+under\b"
                              r"|\boptions?\s+to\s+purchase\b"),
    ("Appointment",           r"\bappoint(?:ment|ed|ing)?\b"
                              r"|\bresignation\b"
                              r"|\bcessation\b"
                              r"|\bchange\s+in\s+(?:director|management|kmp|ceo|cfo|md)\b"
                              r"|\bindependent\s+director\b"
                              r"|\bmanaging\s+director\b"
                              r"|\bchief\s+(?:executive|financial)\b"),
    ("Order Win",             r"\b(?:wins?|winning|awarded|awarding|secures?|securing|bags?|bagging|receives?|receiving|received)\b"
                              r".{0,60}\b(?:order|contract|project|tender|loa|loi|work)\b"
                              r"|\b(?:order|contract|project|tender)\b.{0,60}"
                              r"\b(?:win|award|receiv|secur|bag)\w*\b"),
    ("Capacity Addition",     r"\b(?:capacity|production\s+capacity)\s+"
                              r"(?:expansion|addition|increase|enhancement)\b"
                              r"|\bcapacity\s+addition\b"
                              r"|\bcommission(?:ing|ed)?\b"
                              r"|\bnew\s+(?:plant|facility|unit|manufacturing)\b"
                              r"|\bgreenfield\b|\bbrownfield\b"
                              r"|\bcommencement\s+of\s+(?:commercial\s+)?production\b"),
    ("Capex",                 r"\bcapex\b|\bcapital\s+expenditure\b"
                              r"|\bfund\s+raising\b|\bcapital\s+raising\b"),
    ("Tax/Regulatory",        r"\bpenalt(?:y|ies)\b|\bsebi\b|\brbi\b|\bgst\b"
                              r"|\bshow[\s-]?cause\b|\bdefault\b"
                              r"|\bdowngrade\b|\bnotice\s+under\b"
                              r"|\bprosecution\b|\binvestigation\b"
                              r"|\bsearch\s+and\s+seizure\b"),
    ("Credit Rating",         r"\bcredit\s+rating\b|\brating\s+action\b"
                              r"|\bicra\b|\bcrisil\b|\bcare\s+ratings?\b"
                              r"|\bind[- ]?ra\b|\bacuite\b"
                              r"|\brating\s+(?:upgrade|downgrade|reaffirm)"),
    ("Investor Communication",r"\bearnings?\s+(?:call|conference)\b"
                              r"|\banalyst\s+(?:meet|call|conference)\b"
                              r"|\binvestor\s+(?:call|meet|presentation|deck|conference)\b"
                              r"|\btranscript\s+of\b"
                              r"|\binstitutional\s+investor\s+meet\b"),
    ("Newspaper Publication", r"\bnewspaper\s+(?:publication|advertisement|advert)\b"
                              r"|\bcopy\s+of\s+newspaper\b"
                              r"|\bpublication\s+in\s+newspaper\b"),
    ("Press Release",         r"\bpress\s+release\b"
                              r"|\bmedia\s+release\b"
                              r"|\bpress\s+note\b"),
    ("Earnings",              r"\bfinancial\s+results?\b|\bq[1-4]\b"
                              r"|\bquarterly\b|\bannual\s+results?\b"
                              r"|\bresults?\b|\bprofit\b"
                              r"|\brevenue\b|\bebitda\b"),
    ("AGM",                   r"\bannual\s+general\s+meeting\b|\bagm\b|\begm\b"
                              r"|\bextraordinary\s+general\s+meeting\b"
                              r"|\bpostal\s+ballot\b"
                              r"|\bshareholders\s+meeting\b"),
    ("Board Meeting",         r"\bboard\s+meeting\b"
                              r"|\bcommittee\s+meeting\b"),
]


_IMPACT_RULES = [
    ("positive", r"\b(?:win|award|secure|receiv|approv|commission|expand"
                 r"|acquisition|invest|buyback|bonus|dividend|order|contract"
                 r"|record|highest|milestone|partnership|agreement|launch"
                 r"|success|growth|profit|surge|jump|rise|recommend"
                 r"|upgrade|reaffirm)\w*\b"),
    ("negative", r"\b(?:penalt|show[\s-]?cause|default|downgrad|resign"
                 r"|terminat|cancel|withdraw|lawsuit|litigation|fraud|scam"
                 r"|lost|loss|decline|fall|drop|slump|delay|postpone"
                 r"|investigation|seizure)\w*\b"),
]


def classify_topic(subject):
    if not subject:
        return "Other"
    key = subject.strip().lower()
    mapped = _NSE_CATEGORY_MAP.get(key)
    if mapped:
        return mapped
    for topic, pat in _TOPIC_RULES:
        if re.search(pat, key, re.IGNORECASE):
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
    c = sqlite3.connect(config.DB_FILE, timeout=60)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
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


def oldest_row_date():
    with db() as c:
        row = c.execute("SELECT MIN(announced_at) AS d FROM announcements").fetchone()
    if row and row["d"]:
        return row["d"][:10]
    return None


def row_count():
    with db() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM announcements").fetchone()
    return row["n"] if row else 0


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


def fetch_announcements(from_date=None, to_date=None):
    s = _get_session()
    if s is None:
        return []

    url = config.NSE_ANNOUNCEMENTS_URL
    params = None
    if from_date and to_date:
        params = {
            "from_date": _nse_date_fmt(from_date),
            "to_date":   _nse_date_fmt(to_date),
        }

    try:
        if params:
            r = s.get(url, params=params, timeout=config.NSE_REQUEST_TIMEOUT)
        else:
            r = s.get(url, timeout=config.NSE_REQUEST_TIMEOUT)

        if r.status_code == 429:
            logger.warning("NSE rate-limited (429) on announcements fetch")
            return []
        if r.status_code == 403:
            logger.warning("NSE 403 — refreshing session and retrying once")
            global _session, _session_ts
            with _session_lock:
                _session = None
                _session_ts = 0.0
            s = _get_session()
            if s is None:
                return []
            try:
                if params:
                    r = s.get(url, params=params, timeout=config.NSE_REQUEST_TIMEOUT)
                else:
                    r = s.get(url, timeout=config.NSE_REQUEST_TIMEOUT)
            except Exception as e:
                logger.warning(f"NSE retry failed: {e}")
                return []

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
#  BACKFILL
# ═════════════════════════════════════════════════════════════════

def _chunk_windows(start_date, end_date, chunk_days):
    cursor = start_date
    while cursor <= end_date:
        chunk_end = cursor + timedelta(days=chunk_days - 1)
        if chunk_end > end_date:
            chunk_end = end_date
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def needs_backfill():
    n = row_count()
    if n == 0:
        return True
    oldest = oldest_row_date()
    if not oldest:
        return True
    try:
        oldest_dt = datetime.strptime(oldest, "%Y-%m-%d").date()
    except Exception:
        return True
    target_start = (_now() - timedelta(days=config.BACKFILL_DAYS)).date()
    return oldest_dt > target_start


def run_backfill():
    now_dt = _now()
    end_d = now_dt.date()
    start_d = (now_dt - timedelta(days=config.BACKFILL_DAYS)).date()

    total_fetched = 0
    total_new = 0
    chunks_done = 0

    logger.info(f"Backfill starting: {start_d} → {end_d} in "
                f"{config.NSE_BACKFILL_CHUNK_DAYS}-day chunks")

    for chunk_start, chunk_end in _chunk_windows(
        start_d, end_d, config.NSE_BACKFILL_CHUNK_DAYS
    ):
        items = fetch_announcements(from_date=chunk_start, to_date=chunk_end)
        if not items:
            logger.info(f"  chunk {chunk_start}..{chunk_end}: 0 rows")
        else:
            new = save_announcements(items)
            total_fetched += len(items)
            total_new += len(new)
            logger.info(f"  chunk {chunk_start}..{chunk_end}: "
                        f"{len(items)} fetched, {len(new)} new")
        chunks_done += 1
        time.sleep(config.NSE_BACKFILL_CHUNK_DELAY)

    logger.info(f"Backfill complete: {total_fetched} fetched, "
                f"{total_new} new, {chunks_done} chunks")
    return {"fetched": total_fetched, "new": total_new, "chunks": chunks_done}


# ═════════════════════════════════════════════════════════════════
#  QUERIES
# ═════════════════════════════════════════════════════════════════

def _parse_date_arg(raw):
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw).strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def _resolve_range(days=None, from_date=None, to_date=None):
    fd = _parse_date_arg(from_date)
    td = _parse_date_arg(to_date)
    if fd and td:
        if fd > td:
            fd, td = td, fd
        return fd.strftime("%Y-%m-%d"), td.strftime("%Y-%m-%d")
    if days is None:
        days = config.DEFAULT_RANGE_DAYS
    return _range_bounds(days)


def query_summary(days=None, symbol=None, impact=None,
                  from_date=None, to_date=None):
    from_d, to_d = _resolve_range(days=days, from_date=from_date, to_date=to_date)

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
    by_topic = {r["topic"]: {"total": r["total"], "latest": r["latest"] or ""}
                for r in total_rows}

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


def query_topic(topic, days=None, symbol=None, impact=None,
                limit=None, offset=0, from_date=None, to_date=None):
    if limit is None:
        limit = config.MAX_ROWS_PER_PAGE
    from_d, to_d = _resolve_range(days=days, from_date=from_date, to_date=to_date)

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


def count_topic(topic, days=None, symbol=None, impact=None,
                from_date=None, to_date=None):
    from_d, to_d = _resolve_range(days=days, from_date=from_date, to_date=to_date)
    where = ["topic = ?", "announced_at >= ?", "announced_at <= ?"]
    params = [topic, from_d, to_d + " 23:59:59"]
    if symbol:
        where.append("UPPER(symbol) LIKE ?")
        params.append(f"%{symbol.upper()}%")
    if impact and impact != "All":
        where.append("impact = ?")
        params.append(impact)
    sql = f"SELECT COUNT(*) AS n FROM announcements WHERE {' AND '.join(where)}"
    with db() as c:
        row = c.execute(sql, params).fetchone()
    return row["n"] if row else 0


# ═════════════════════════════════════════════════════════════════
#  UNREAD TRACKING
# ═════════════════════════════════════════════════════════════════

def mark_topic_viewed(topic):
    now_str = _now_str()
    with db() as c:
        c.execute("""
            INSERT INTO topic_views (topic, viewed_at)
            VALUES (?, ?)
            ON CONFLICT(topic) DO UPDATE SET viewed_at = excluded.viewed_at
        """, (topic, now_str))
    logger.info(f"Marked topic viewed: {topic}")


def get_topic_views():
    with db() as c:
        rows = c.execute("SELECT topic, viewed_at FROM topic_views").fetchall()
    return {r["topic"]: r["viewed_at"] for r in rows}


def count_unread_by_topic(days=None, symbol=None, impact=None):
    from_d, to_d = _resolve_range(days=days)
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

def run_fetch_cycle(force_backfill=False):
    with _fetch_lock:
        mode = "default"

        if force_backfill or needs_backfill():
            mode = "backfill"
            result = run_backfill()
            prune_old()
            today_str = _now().strftime("%Y-%m-%d")
            with db() as c:
                rows = c.execute("""
                    SELECT symbol, company, subject, topic, impact,
                           announced_at, attachment
                    FROM announcements
                    WHERE announced_at >= ? AND seen_at >= datetime('now', '-1 minute')
                """, (today_str,)).fetchall()
            new_items = [dict(r) for r in rows]
            return {
                "fetched":   result["fetched"],
                "new":       result["new"],
                "new_items": new_items,
                "mode":      mode,
            }

        items = fetch_announcements()
        if not items:
            return {"fetched": 0, "new": 0, "new_items": [], "mode": mode}

        new_items = save_announcements(items)
        prune_old()
        logger.info(f"Fetch cycle: {len(items)} fetched, {len(new_items)} new")
        return {
            "fetched":   len(items),
            "new":       len(new_items),
            "new_items": new_items,
            "mode":      mode,
        }


# ═════════════════════════════════════════════════════════════════
#  BACKUP / RESTORE
# ═════════════════════════════════════════════════════════════════

def get_all_for_backup():
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
    if not items:
        return 0

    prepared = []
    for it in items:
        subject = it.get("subject", "")
        prepared.append((
            it.get("symbol", ""),
            it.get("company", ""),
            subject,
            classify_topic(subject),
            classify_impact(subject),
            it.get("announced_at", ""),
            it.get("attachment", ""),
        ))

    inserted = 0
    with db() as c:
        before = c.total_changes
        c.executemany("""
            INSERT OR IGNORE INTO announcements
                (symbol, company, subject, topic, impact, announced_at, attachment)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, prepared)
        inserted = c.total_changes - before

    logger.info(f"Restore: {inserted} new rows inserted from {len(items)} records")
    return inserted


def get_views_for_backup():
    with db() as c:
        rows = c.execute("SELECT topic, viewed_at FROM topic_views").fetchall()
    return [dict(r) for r in rows]


def restore_views_from_backup(items):
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


def reclassify_all():
    updated = 0
    with db() as c:
        rows = c.execute("SELECT id, subject, topic, impact FROM announcements").fetchall()
        updates = []
        for r in rows:
            new_topic = classify_topic(r["subject"])
            new_impact = classify_impact(r["subject"])
            if new_topic != r["topic"] or new_impact != r["impact"]:
                updates.append((new_topic, new_impact, r["id"]))
        if updates:
            c.executemany(
                "UPDATE announcements SET topic = ?, impact = ? WHERE id = ?",
                updates
            )
            updated = len(updates)
    if updated:
        logger.info(f"Reclassified {updated} rows with current rules")
    else:
        logger.info("Reclassify: no rows needed updating")
    return updated