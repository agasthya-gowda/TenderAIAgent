import sqlite3
import os
import json
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "tenders.db")

def get_conn():
    # WAL lets the dashboard's reads run concurrently with the background
    # agent's writes instead of blocking behind its lock (the default
    # rollback-journal mode serializes them, which is what caused the
    # dashboard to randomly stall for seconds while a search was running).
    # busy_timeout is a fallback for the brief moments even WAL still contends.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()

    # Table 1 - stores every search CEO makes
    c.execute("""
        CREATE TABLE IF NOT EXISTS ceo_searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL,
            portal TEXT,
            searched_at TEXT DEFAULT (datetime('now')),
            frequency INTEGER DEFAULT 1
        )
    """)

    # Table 2 - stores what agent learned from CEO
    c.execute("""
        CREATE TABLE IF NOT EXISTS learned_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT,
            keywords TEXT,
            weight REAL DEFAULT 1.0,
            last_updated TEXT DEFAULT (datetime('now'))
        )
    """)

    # Table 2b - stores KPPP-specific search filters set by the CEO. KPPP has
    # no keyword search, so instead of a keyword this stores the actual
    # fields its own search form takes (category, procuring entity, tender
    # number, closure date range, tender type). Each saved row is re-applied
    # automatically on every future agent search cycle, same as GeM keywords.
    c.execute("""
        CREATE TABLE IF NOT EXISTS kppp_search_criteria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT,
            procuring_entity TEXT,
            tender_number TEXT,
            from_date TEXT,
            to_date TEXT,
            tender_type TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Table 3 - stores all tenders found
    c.execute("""
        CREATE TABLE IF NOT EXISTS tenders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            portal TEXT,
            tender_number TEXT,
            department TEXT,
            location TEXT,
            tender_type TEXT,
            value TEXT,
            emd TEXT,
            deadline TEXT,
            published_date TEXT,
            category TEXT,
            relevance_score REAL DEFAULT 0.0,
            url TEXT,
            description TEXT,
            found_via TEXT,
            downloaded_files TEXT,
            original_pdf_path TEXT,
            analysis_pdf_path TEXT,
            status TEXT DEFAULT 'new',
            found_at TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.commit()
    conn.close()

def save_ceo_search(keyword: str, portal: str):
    conn = get_conn()
    c = conn.cursor()
    existing = c.execute(
        "SELECT id, frequency FROM ceo_searches WHERE keyword=? AND portal=?",
        (keyword.lower().strip(), portal)
    ).fetchone()
    if existing:
        c.execute(
            "UPDATE ceo_searches SET frequency=?, searched_at=? WHERE id=?",
            (existing["frequency"] + 1, datetime.now().isoformat(), existing["id"])
        )
    else:
        c.execute(
            "INSERT INTO ceo_searches (keyword, portal) VALUES (?, ?)",
            (keyword.lower().strip(), portal)
        )
    conn.commit()
    conn.close()

def get_all_ceo_searches():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM ceo_searches ORDER BY frequency DESC, searched_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def save_kppp_criteria(criteria: dict):
    """Save a CEO-defined KPPP search filter (category/entity/dates/type).
    Skips saving an exact duplicate of an already-saved criteria set.
    Refuses to save a blank procuring_entity — that turns into an unscoped,
    every-department search that then runs forever on every future cycle
    (the UI is expected to require an entity before calling this, but this
    guard exists so a bad call can never quietly poison the criteria list)."""
    if not (criteria.get("procuring_entity") or "").strip():
        return
    conn = get_conn()
    c = conn.cursor()
    fields = ("category", "procuring_entity", "tender_number", "from_date", "to_date", "tender_type")
    values = tuple(criteria.get(f, "") or "" for f in fields)
    existing = c.execute(
        """SELECT id FROM kppp_search_criteria
           WHERE category=? AND procuring_entity=? AND tender_number=?
             AND from_date=? AND to_date=? AND tender_type=?""",
        values
    ).fetchone()
    if not existing:
        c.execute(
            """INSERT INTO kppp_search_criteria
               (category, procuring_entity, tender_number, from_date, to_date, tender_type)
               VALUES (?,?,?,?,?,?)""",
            values
        )
    conn.commit()
    conn.close()

def get_kppp_criteria_list():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM kppp_search_criteria ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def delete_kppp_criteria(criteria_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM kppp_search_criteria WHERE id=?", (criteria_id,))
    conn.commit()
    conn.close()

def save_learned_patterns(category: str, keywords: str, weight: float = 1.0):
    conn = get_conn()
    c = conn.cursor()
    existing = c.execute(
        "SELECT id FROM learned_patterns WHERE category=?", (category,)
    ).fetchone()
    if existing:
        c.execute(
            "UPDATE learned_patterns SET keywords=?, weight=?, last_updated=? WHERE id=?",
            (keywords, weight, datetime.now().isoformat(), existing["id"])
        )
    else:
        c.execute(
            "INSERT INTO learned_patterns (category, keywords, weight) VALUES (?,?,?)",
            (category, keywords, weight)
        )
    conn.commit()
    conn.close()

def get_learned_patterns():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM learned_patterns ORDER BY weight DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def save_tender(tender: dict):
    conn = get_conn()
    c = conn.cursor()

    # Check if tender already exists
    existing = c.execute(
        "SELECT id FROM tenders WHERE title=? AND portal=?",
        (tender.get("title"), tender.get("portal"))
    ).fetchone()

    if not existing:
        # Convert downloaded_files list to JSON string
        downloaded_files = tender.get("downloaded_files", [])
        if isinstance(downloaded_files, list):
            downloaded_files = json.dumps(downloaded_files)

        c.execute("""
            INSERT INTO tenders (
                title, portal, tender_number, department,
                location, tender_type, value, emd,
                deadline, published_date, category,
                relevance_score, url, description,
                found_via, downloaded_files, quantity
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            tender.get("title"),
            tender.get("portal"),
            tender.get("tender_number", ""),
            tender.get("department", ""),
            tender.get("location", ""),
            tender.get("tender_type", ""),
            tender.get("value", ""),
            tender.get("emd", ""),
            tender.get("deadline", ""),
            tender.get("published_date", ""),
            tender.get("category", ""),
            tender.get("relevance_score", 0),
            tender.get("url", ""),
            tender.get("description", ""),
            tender.get("found_via", ""),
            downloaded_files,
            tender.get("quantity", "N/A")
        ))
        conn.commit()
        row_id = c.lastrowid
        conn.close()
        return row_id

    conn.close()
    return None

def get_all_tenders(status_filter=None, today_only=False):
    conn = get_conn()
    from datetime import datetime, date
    today = date.today().isoformat()
    
    if status_filter == "new" or today_only:
        rows = conn.execute(
            "SELECT * FROM tenders WHERE status='new' AND date(found_at) = ? ORDER BY found_at DESC",
            (today,)
        ).fetchall()
    elif status_filter:
        rows = conn.execute(
            "SELECT * FROM tenders WHERE status=? ORDER BY relevance_score DESC, found_at DESC",
            (status_filter,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tenders ORDER BY relevance_score DESC, found_at DESC"
        ).fetchall()
    conn.close()

    tenders = []
    for r in rows:
        t = dict(r)
        # Convert downloaded_files JSON string back to list
        try:
            t["downloaded_files"] = json.loads(t.get("downloaded_files") or "[]")
        except Exception:
            t["downloaded_files"] = []
        tenders.append(t)
    return tenders

def update_tender_status(tender_id: int, status: str):
    conn = get_conn()
    conn.execute(
        "UPDATE tenders SET status=? WHERE id=?", (status, tender_id)
    )
    conn.commit()
    conn.close()

def update_tender_pdfs(tender_id: int, original_path: str = None, analysis_path: str = None):
    conn = get_conn()
    if original_path:
        conn.execute(
            "UPDATE tenders SET original_pdf_path=? WHERE id=?",
            (original_path, tender_id)
        )
    if analysis_path:
        conn.execute(
            "UPDATE tenders SET analysis_pdf_path=? WHERE id=?",
            (analysis_path, tender_id)
        )
    conn.commit()
    conn.close()

def update_downloaded_files(tender_id: int, files: list):
    conn = get_conn()
    conn.execute(
        "UPDATE tenders SET downloaded_files=? WHERE id=?",
        (json.dumps(files), tender_id)
    )
    conn.commit()
    conn.close()

def get_stats():
    from datetime import date
    today = date.today().isoformat()
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) as c FROM tenders").fetchone()["c"]
    new = conn.execute(
        "SELECT COUNT(*) as c FROM tenders WHERE status='new' AND date(found_at) = ?",
        (today,)
    ).fetchone()["c"]
    saved = conn.execute("SELECT COUNT(*) as c FROM tenders WHERE status='saved'").fetchone()["c"]
    searches = conn.execute("SELECT COUNT(*) as c FROM ceo_searches").fetchone()["c"]
    patterns = conn.execute("SELECT COUNT(*) as c FROM learned_patterns").fetchone()["c"]
    conn.close()
    return {
        "total_tenders": total,
        "new": new,
        "saved": saved,
        "ceo_searches": searches,
        "patterns_learned": patterns
    }

def get_department_counts(portal: str, top_n: int = 5):
    """Real per-department tender counts for one portal, top N plus an
    'Others' bucket for the rest — read-only, used by the dashboard charts."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT department, COUNT(*) as c FROM tenders
           WHERE portal=? AND department IS NOT NULL AND TRIM(department) != ''
           GROUP BY department ORDER BY c DESC""",
        (portal,)
    ).fetchall()
    conn.close()
    data = [(r["department"], r["c"]) for r in rows]
    if len(data) <= top_n:
        return data
    top = data[:top_n]
    others_count = sum(c for _, c in data[top_n:])
    if others_count:
        top.append(("Others", others_count))
    return top

_CAMERA_TYPE_KEYWORDS = [
    ("ANPR Camera", ("anpr", "number plate", "license plate", "automatic number plate")),
    ("Body Worn Camera", ("body worn", "bodycam", "body cam")),
    ("PTZ Camera", ("ptz",)),
    ("Thermal Camera", ("thermal",)),
    ("CCTV Camera", ("cctv", "surveillance camera", "surveillance system", "dome camera",
                      "bullet camera", "ip camera", "nvr", "dvr", "surveillance")),
]

def get_camera_type_counts(portal: str):
    """Classify each tender's title into a camera-type bucket by keyword
    match — real counts over the actual scraped titles, not invented data.
    A title matching none of the known keyword groups falls into 'Others'."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT title FROM tenders WHERE portal=?", (portal,)
    ).fetchall()
    conn.close()
    buckets = {label: 0 for label, _ in _CAMERA_TYPE_KEYWORDS}
    buckets["Others"] = 0
    for r in rows:
        title = (r["title"] or "").lower()
        matched = False
        for label, keywords in _CAMERA_TYPE_KEYWORDS:
            if any(kw in title for kw in keywords):
                buckets[label] += 1
                matched = True
                break
        if not matched:
            buckets["Others"] += 1
    return [(label, count) for label, count in buckets.items() if count > 0]

def get_daily_trend(portal: str, days: int = 7):
    """Real count of tenders found (by found_at, the date we actually saved
    each row) per day for the last `days` days, for one portal. Days with
    zero tenders are included as 0, not omitted."""
    from datetime import date, timedelta
    conn = get_conn()
    rows = conn.execute(
        """SELECT date(found_at) as d, COUNT(*) as c FROM tenders
           WHERE portal=? GROUP BY d""",
        (portal,)
    ).fetchall()
    conn.close()
    counts = {r["d"]: r["c"] for r in rows}
    today = date.today()
    out = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        out.append((d.isoformat(), counts.get(d.isoformat(), 0)))
    return out

def delete_tender(tender_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM tenders WHERE id=?", (tender_id,))
    conn.commit()
    conn.close()

def delete_expired_tenders() -> int:
    """Permanently removes every tender whose deadline (End Date) has
    already passed — a closed tender can't be bid on or usefully analyzed,
    so there's no reason to keep it around. Only rows with a clearly
    parseable DD-MM-YYYY date are touched; a missing/unparseable deadline
    (e.g. "Not available") is left alone rather than risking a wrong delete.
    Returns how many rows were removed."""
    import re
    from datetime import date
    conn = get_conn()
    rows = conn.execute("SELECT id, deadline FROM tenders").fetchall()
    today = date.today()
    expired_ids = []
    for r in rows:
        match = re.match(r"(\d{2})-(\d{2})-(\d{4})", (r["deadline"] or "").strip())
        if not match:
            continue
        day, month, year = (int(g) for g in match.groups())
        try:
            deadline_date = date(year, month, day)
        except ValueError:
            continue
        if deadline_date < today:
            expired_ids.append(r["id"])
    if expired_ids:
        conn.executemany("DELETE FROM tenders WHERE id=?", [(i,) for i in expired_ids])
        conn.commit()
    conn.close()
    return len(expired_ids)

def delete_pattern(pattern_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM learned_patterns WHERE id=?", (pattern_id,))
    conn.commit()
    conn.close()
init_db()