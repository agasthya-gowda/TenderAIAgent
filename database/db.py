import os
import json
from datetime import datetime

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

PG_HOST = os.getenv("PG_HOST", "127.0.0.1")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_DATABASE = os.getenv("PG_DATABASE", "tender_agent")
PG_USER = os.getenv("PG_USER", "tender_app")
PG_PASSWORD = os.getenv("PG_PASSWORD", "ChipscapeApp2026")


class _PGCursor:
    """Wraps a real psycopg2 cursor so existing call sites (here and in
    orchestrator.py/gem.py/kppp.py/clear_all.py) keep working exactly as
    they did against sqlite3 — same `?` placeholder style, same
    dict-like row access, same execute()->fetchone()/fetchall() chaining
    — without needing to touch any of those call sites."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, params=()):
        self._cur.execute(sql.replace("?", "%s"), params)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class _PGConnection:
    """Wraps a real psycopg2 connection the same way — lets the rest of
    the app call conn.execute(...)/conn.cursor()/conn.executemany(...)
    exactly like it did with sqlite3.Connection."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql.replace("?", "%s"), params)
        return _PGCursor(cur)

    def executemany(self, sql, seq_of_params):
        cur = self._conn.cursor()
        cur.executemany(sql.replace("?", "%s"), seq_of_params)
        return _PGCursor(cur)

    def cursor(self):
        return _PGCursor(self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor))

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_conn():
    # PostgreSQL is a real multi-user database (unlike the SQLite file
    # this replaced) — concurrent readers and writers, including several
    # team members using the dashboard at once alongside the background
    # agent's own writes, are handled natively without any WAL/busy-timeout
    # workaround needed.
    raw_conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DATABASE,
        user=PG_USER, password=PG_PASSWORD
    )
    return _PGConnection(raw_conn)

def init_db():
    conn = get_conn()
    c = conn.cursor()

    # Table 1 - stores every search CEO makes
    c.execute("""
        CREATE TABLE IF NOT EXISTS ceo_searches (
            id SERIAL PRIMARY KEY,
            keyword TEXT NOT NULL,
            portal TEXT,
            searched_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
            frequency INTEGER DEFAULT 1
        )
    """)

    # Table 2 - stores what agent learned from CEO
    c.execute("""
        CREATE TABLE IF NOT EXISTS learned_patterns (
            id SERIAL PRIMARY KEY,
            category TEXT,
            keywords TEXT,
            weight REAL DEFAULT 1.0,
            portal TEXT DEFAULT 'GeM',
            last_updated TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
        )
    """)
    # Safety net for an older install's table that predates the portal
    # column — a no-op on a freshly created table.
    c.execute("ALTER TABLE learned_patterns ADD COLUMN IF NOT EXISTS portal TEXT DEFAULT 'GeM'")

    # Table 2c - tracks which GeM keywords have already had their one-time
    # full/unlimited-pagination pass (run by Start Agent / the 2 AM job).
    # A keyword gets the full pass exactly once, ever — every later Start
    # Agent/2 AM run for that same keyword instead pages only until it
    # reaches tenders whose Start Date is no newer than last_searched_at
    # (see search_gem's stop_before_date), instead of re-walking every page
    # or an arbitrary fixed page count again.
    c.execute("""
        CREATE TABLE IF NOT EXISTS gem_keyword_full_search_log (
            keyword TEXT PRIMARY KEY,
            first_full_search_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
            last_searched_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
        )
    """)
    # Safety net for an older install's table that predates last_searched_at.
    c.execute("ALTER TABLE gem_keyword_full_search_log ADD COLUMN IF NOT EXISTS last_searched_at TEXT")

    # Table 2b - stores KPPP-specific search filters set by the CEO. KPPP has
    # no keyword search, so instead of a keyword this stores the actual
    # fields its own search form takes (category, procuring entity, tender
    # number, closure date range, tender type). Each saved row is re-applied
    # automatically on every future agent search cycle, same as GeM keywords.
    c.execute("""
        CREATE TABLE IF NOT EXISTS kppp_search_criteria (
            id SERIAL PRIMARY KEY,
            category TEXT,
            procuring_entity TEXT,
            tender_number TEXT,
            from_date TEXT,
            to_date TEXT,
            tender_type TEXT,
            tender_name TEXT,
            created_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
            last_searched_at TEXT
        )
    """)
    # Safety net for an older install's table that predates tender_name /
    # last_searched_at.
    c.execute("ALTER TABLE kppp_search_criteria ADD COLUMN IF NOT EXISTS tender_name TEXT")
    c.execute("ALTER TABLE kppp_search_criteria ADD COLUMN IF NOT EXISTS last_searched_at TEXT")

    # Table 3 - stores all tenders found
    c.execute("""
        CREATE TABLE IF NOT EXISTS tenders (
            id SERIAL PRIMARY KEY,
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
            found_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
            quantity TEXT
        )
    """)

    conn.commit()
    conn.close()

def has_had_full_gem_search(keyword: str) -> bool:
    """True once this exact keyword has already had its one-time
    full/unlimited-pagination pass (case-insensitive)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM gem_keyword_full_search_log WHERE keyword=?",
        (keyword.lower().strip(),)
    ).fetchone()
    conn.close()
    return row is not None

def mark_gem_full_search_done(keyword: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO gem_keyword_full_search_log (keyword) VALUES (?) ON CONFLICT (keyword) DO NOTHING",
        (keyword.lower().strip(),)
    )
    conn.commit()
    conn.close()

def get_gem_last_searched_at(keyword: str):
    """The Start Date cutoff for this keyword's last completed search run —
    None if it's never been searched before (i.e. its upcoming run should be
    the one-time full/unlimited pass, not a date-limited one)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT last_searched_at FROM gem_keyword_full_search_log WHERE keyword=?",
        (keyword.lower().strip(),)
    ).fetchone()
    conn.close()
    return row["last_searched_at"] if row else None

def update_gem_last_searched(keyword: str):
    """Stamps 'now' as this keyword's last search time, run after every
    batch search (first-time or date-limited) — this is what the NEXT run
    uses as its Start-Date cutoff, so results already covered by this run
    are never re-walked. Inserts the row if this was actually the
    first-ever search for the keyword (mark_gem_full_search_done runs
    alongside this in that case, so ON CONFLICT here just keeps the
    first_full_search_at that call already set)."""
    conn = get_conn()
    conn.execute(
        """INSERT INTO gem_keyword_full_search_log (keyword, last_searched_at)
           VALUES (?, to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
           ON CONFLICT (keyword) DO UPDATE SET last_searched_at = to_char(now(), 'YYYY-MM-DD HH24:MI:SS')""",
        (keyword.lower().strip(),)
    )
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

def delete_ceo_search(search_id: int):
    """Delete a Search History entry, and unwire it from whatever actually
    drives future re-searching so the agent stops fetching tenders for it —
    just removing the history row wouldn't be enough on its own:
    - GeM: the keyword is stripped out of every learned pattern's keyword
      list (a pattern can bundle several original search terms together,
      so the whole pattern is only dropped if that leaves it empty), and
      its one-time-full-search tracking is cleared so a future re-add
      starts fresh instead of silently staying "already done".
    - KPPP: the matching saved filter row (by tender_name or procuring
      entity) is removed from kppp_search_criteria, which is what the
      agent actually re-runs on every cycle — not ceo_searches.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT keyword, portal FROM ceo_searches WHERE id=?", (search_id,)
    ).fetchone()
    if not row:
        conn.close()
        return
    keyword, portal = row["keyword"], row["portal"]
    conn.execute("DELETE FROM ceo_searches WHERE id=?", (search_id,))

    if portal == "KPPP":
        conn.execute(
            "DELETE FROM kppp_search_criteria WHERE lower(tender_name)=? OR lower(procuring_entity)=?",
            (keyword.lower(), keyword.lower())
        )
    else:
        patterns = conn.execute(
            "SELECT id, keywords FROM learned_patterns WHERE portal='GeM' OR portal IS NULL"
        ).fetchall()
        for p in patterns:
            try:
                kws = json.loads(p["keywords"]) if p["keywords"] else []
            except Exception:
                kws = []
            new_kws = [k for k in kws if k.strip().lower() != keyword.strip().lower()]
            if len(new_kws) != len(kws):
                if new_kws:
                    conn.execute(
                        "UPDATE learned_patterns SET keywords=? WHERE id=?",
                        (json.dumps(new_kws), p["id"])
                    )
                else:
                    conn.execute("DELETE FROM learned_patterns WHERE id=?", (p["id"],))
        conn.execute("DELETE FROM gem_keyword_full_search_log WHERE keyword=?", (keyword.lower().strip(),))

    conn.commit()
    conn.close()

def save_kppp_criteria(criteria: dict):
    """Save a CEO-defined KPPP search filter (category/entity/dates/type/tender name).
    Skips saving an exact duplicate of an already-saved criteria set.
    Refuses to save a filter with neither a procuring_entity nor a
    tender_name — that turns into an unscoped, every-department search that
    then runs forever on every future cycle (the UI is expected to require
    at least one of the two before calling this, but this guard exists so a
    bad call can never quietly poison the criteria list)."""
    if not (criteria.get("procuring_entity") or "").strip() and not (criteria.get("tender_name") or "").strip():
        return
    conn = get_conn()
    c = conn.cursor()
    fields = ("category", "procuring_entity", "tender_number", "from_date", "to_date", "tender_type", "tender_name")
    values = tuple(criteria.get(f, "") or "" for f in fields)
    existing = c.execute(
        """SELECT id FROM kppp_search_criteria
           WHERE category=? AND procuring_entity=? AND tender_number=?
             AND from_date=? AND to_date=? AND tender_type=? AND tender_name=?""",
        values
    ).fetchone()
    if not existing:
        c.execute(
            """INSERT INTO kppp_search_criteria
               (category, procuring_entity, tender_number, from_date, to_date, tender_type, tender_name)
               VALUES (?,?,?,?,?,?,?)""",
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

def update_kppp_last_searched(criteria_id: int):
    """Stamps 'now' as this saved KPPP filter's last search time — run
    after every search of it (first-time or date-limited). KPPP's own form
    has no Start-Date filter field (its date-range inputs filter by tender
    closure date, a different field), so unlike GeM this is used to filter
    results client-side after fetching rather than to shape the request
    itself — see _run_kppp_criteria_search in orchestrator.py."""
    conn = get_conn()
    conn.execute(
        "UPDATE kppp_search_criteria SET last_searched_at = to_char(now(), 'YYYY-MM-DD HH24:MI:SS') WHERE id=?",
        (criteria_id,)
    )
    conn.commit()
    conn.close()

def delete_kppp_criteria(criteria_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM kppp_search_criteria WHERE id=?", (criteria_id,))
    conn.commit()
    conn.close()

def save_learned_patterns(category: str, keywords: str, weight: float = 1.0, portal: str = "GeM"):
    conn = get_conn()
    c = conn.cursor()
    existing = c.execute(
        "SELECT id FROM learned_patterns WHERE category=?", (category,)
    ).fetchone()
    if existing:
        c.execute(
            "UPDATE learned_patterns SET keywords=?, weight=?, portal=?, last_updated=? WHERE id=?",
            (keywords, weight, portal, datetime.now().isoformat(), existing["id"])
        )
    else:
        c.execute(
            "INSERT INTO learned_patterns (category, keywords, weight, portal) VALUES (?,?,?,?)",
            (category, keywords, weight, portal)
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
            RETURNING id
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
        row_id = c.fetchone()["id"]
        conn.commit()
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
            "SELECT * FROM tenders WHERE status='new' AND LEFT(found_at, 10) = ? ORDER BY found_at DESC",
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

def clear_tender_analysis(tender_id: int):
    """Wipes a tender's saved analysis PDF and downloaded documents so
    'Get Full Analysis' runs completely fresh next time — used by the
    Re-analyze button for cases where the first attempt produced a broken
    or incomplete report (e.g. a KPPP CAPTCHA failure left no real
    documents to read, so the AI had nothing to analyze)."""
    conn = get_conn()
    conn.execute(
        "UPDATE tenders SET analysis_pdf_path=NULL, downloaded_files='[]' WHERE id=?",
        (tender_id,)
    )
    conn.commit()
    conn.close()

def get_stats():
    from datetime import date
    today = date.today().isoformat()
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) as c FROM tenders WHERE status != 'expired'").fetchone()["c"]
    new = conn.execute(
        "SELECT COUNT(*) as c FROM tenders WHERE status='new' AND LEFT(found_at, 10) = ?",
        (today,)
    ).fetchone()["c"]
    saved = conn.execute("SELECT COUNT(*) as c FROM tenders WHERE status='saved'").fetchone()["c"]
    expired = conn.execute("SELECT COUNT(*) as c FROM tenders WHERE status='expired'").fetchone()["c"]
    searches = conn.execute("SELECT COUNT(*) as c FROM ceo_searches").fetchone()["c"]
    patterns = conn.execute("SELECT COUNT(*) as c FROM learned_patterns").fetchone()["c"]
    conn.close()
    return {
        "total_tenders": total,
        "new": new,
        "saved": saved,
        "expired": expired,
        "ceo_searches": searches,
        "patterns_learned": patterns
    }

def get_department_counts(portal: str, top_n: int = 5):
    """Real per-department tender counts for one portal, top N plus an
    'Others' bucket for the rest — read-only, used by the dashboard charts."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT department, COUNT(*) as c FROM tenders
           WHERE portal=? AND status != 'expired'
             AND department IS NOT NULL AND TRIM(department) != ''
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
        """SELECT LEFT(found_at, 10) as d, COUNT(*) as c FROM tenders
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

def mark_expired_tenders() -> int:
    """Moves every tender whose deadline (End Date) has already passed into
    its own 'expired' status instead of deleting it — a closed tender can't
    be bid on anymore, but the manager wants it kept around for later
    analysis (e.g. reviewing why a bid was missed), so it's surfaced under
    the "Expired Tenders" card rather than removed. Only rows with a clearly
    parseable DD-MM-YYYY date are touched, and a tender already marked
    expired is skipped; a missing/unparseable deadline (e.g. "Not
    available") is left alone rather than risking a wrong reclassification.
    Returns how many rows were newly marked."""
    import re
    from datetime import date
    conn = get_conn()
    rows = conn.execute("SELECT id, deadline FROM tenders WHERE status != 'expired'").fetchall()
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
        conn.executemany("UPDATE tenders SET status='expired' WHERE id=?", [(i,) for i in expired_ids])
        conn.commit()
    conn.close()
    return len(expired_ids)

def delete_pattern(pattern_id: int):
    """Delete a Patterns Learned entry and unwire it from future re-searching,
    same guarantee as delete_ceo_search:
    - GeM: the pattern row itself is what Start Agent/2AM read keywords from,
      so removing it already stops future searches for its keywords; also
      clear their one-time-full-search tracking so a future re-add starts
      fresh instead of silently staying "already done".
    - KPPP: the pattern's keywords are the tender_name/procuring_entity
      labels used when the filter was saved, but the real thing the agent
      re-runs every cycle is kppp_search_criteria, not learned_patterns — so
      each matching saved filter row must be deleted too."""
    conn = get_conn()
    row = conn.execute(
        "SELECT keywords, portal FROM learned_patterns WHERE id=?", (pattern_id,)
    ).fetchone()
    if not row:
        conn.close()
        return
    try:
        kws = json.loads(row["keywords"]) if row["keywords"] else []
    except Exception:
        kws = []

    conn.execute("DELETE FROM learned_patterns WHERE id=?", (pattern_id,))

    if row["portal"] == "KPPP":
        for kw in kws:
            conn.execute(
                "DELETE FROM kppp_search_criteria WHERE lower(tender_name)=? OR lower(procuring_entity)=?",
                (kw.strip().lower(), kw.strip().lower())
            )
    else:
        for kw in kws:
            conn.execute("DELETE FROM gem_keyword_full_search_log WHERE keyword=?", (kw.strip().lower(),))

    conn.commit()
    conn.close()
init_db()
