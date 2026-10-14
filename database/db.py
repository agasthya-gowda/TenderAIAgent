import os
import json
import re
import hashlib
import secrets
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

    # Typed, queryable columns alongside the original free-text ones above
    # (value/emd/deadline/published_date/quantity kept as-is for display and
    # backward compatibility) — per the manager's request, critical tender
    # parameters shouldn't only live as loose scraped text that has to be
    # re-parsed by application code on every read. start_date_parsed /
    # end_date_parsed / quantity_count / item_tags are filled in at L1 (the
    # moment a tender is found and saved — see save_tender()); bid/EMD/
    # turnover-requirement amounts genuinely aren't published on either
    # portal's search results page (both show "See bid document"/"Not
    # available" at L1 in practice) — those three are only knowable once
    # the tender's real document is read, so they're filled in at L2
    # (Get Full Analysis — see update_tender_l2_financials(), called from
    # analysis/analyzer.py).
    c.execute("ALTER TABLE tenders ADD COLUMN IF NOT EXISTS start_date_parsed TIMESTAMP")
    c.execute("ALTER TABLE tenders ADD COLUMN IF NOT EXISTS end_date_parsed TIMESTAMP")
    c.execute("ALTER TABLE tenders ADD COLUMN IF NOT EXISTS quantity_count INTEGER")
    c.execute("ALTER TABLE tenders ADD COLUMN IF NOT EXISTS item_tags TEXT[]")
    c.execute("ALTER TABLE tenders ADD COLUMN IF NOT EXISTS bid_value_numeric NUMERIC")
    c.execute("ALTER TABLE tenders ADD COLUMN IF NOT EXISTS emd_numeric NUMERIC")
    c.execute("ALTER TABLE tenders ADD COLUMN IF NOT EXISTS turnover_requirement_numeric NUMERIC")

    # Relevant Tenders — a tender is flagged relevant when its title/tags
    # fuzzy-match one of the CEO's own saved "Relevant Tenders" keywords
    # (see relevant_keywords table below and agent/orchestrator.py's
    # run_relevant_keyword_matching()). Kept as its own flag rather than a
    # status value, since a tender can be relevant AND new/saved/expired at
    # the same time — those two things are independent.
    c.execute("ALTER TABLE tenders ADD COLUMN IF NOT EXISTS is_relevant BOOLEAN DEFAULT FALSE")
    c.execute("ALTER TABLE tenders ADD COLUMN IF NOT EXISTS relevant_keywords TEXT[]")
    c.execute("ALTER TABLE tenders ADD COLUMN IF NOT EXISTS relevant_matched_at TIMESTAMP")

    c.execute("""
        CREATE TABLE IF NOT EXISTS relevant_keywords (
            id SERIAL PRIMARY KEY,
            keyword TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Tables 4-8 — Chipscape's own company profile (turnover, credentials,
    # past-project references/quantities) plus subcontractor/system-integrator
    # partner credentials. Previously this all lived in static Excel files
    # under company_data/ that had to be hand-edited outside the app — moved
    # here per the manager's request so a new certificate or next year's
    # turnover can be added/updated/queried straight from the Admin section
    # of the dashboard instead. See analysis/company_profile.py, which reads
    # these tables to build the eligibility-matching prompt.
    c.execute("""
        CREATE TABLE IF NOT EXISTS company_turnover (
            id SERIAL PRIMARY KEY,
            financial_year TEXT NOT NULL,
            turnover REAL NOT NULL,
            total_with_gst REAL,
            status TEXT DEFAULT 'Audited',
            created_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
            updated_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS company_credentials (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT,
            issuing_authority TEXT,
            certificate_no TEXT,
            issue_date TEXT,
            expiry_date TEXT,
            status TEXT DEFAULT 'Valid',
            created_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
            updated_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
        )
    """)

    # Subcontractor / system-integrator partners Chipscape may bid with —
    # each partner can carry its own set of credentials (e.g. the partner's
    # own OEM authorization or ISO certificate), separate from Chipscape's.
    c.execute("""
        CREATE TABLE IF NOT EXISTS partners (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            role TEXT,
            notes TEXT,
            created_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
            updated_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS partner_credentials (
            id SERIAL PRIMARY KEY,
            partner_id INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            type TEXT,
            issuing_authority TEXT,
            certificate_no TEXT,
            issue_date TEXT,
            expiry_date TEXT,
            status TEXT DEFAULT 'Valid',
            created_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
            updated_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS project_references (
            id SERIAL PRIMARY KEY,
            project_name TEXT NOT NULL,
            customer TEXT,
            department_type TEXT,
            location TEXT,
            solution_category TEXT,
            eligibility_tags TEXT,
            project_scope TEXT,
            project_value REAL,
            government_project TEXT,
            financial_year TEXT,
            project_status TEXT,
            created_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
            updated_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS project_quantities (
            id SERIAL PRIMARY KEY,
            project_name TEXT NOT NULL,
            solution_category TEXT,
            quantity_category TEXT,
            quantity_executed TEXT,
            unit TEXT,
            customer TEXT,
            created_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
            updated_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
        )
    """)

    # Dashboard login accounts. Only 'admin' role sees the Admin section
    # (company credentials, turnover, etc.) and can create new accounts —
    # 'regular' role gets everything else. Replaces the single shared
    # LOGIN_USERNAME/LOGIN_PASSWORD .env credential this table's first row
    # is seeded from below, the same one-time migration-from-static-config
    # pattern as the company profile tables above.
    c.execute("""
        CREATE TABLE IF NOT EXISTS app_users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'regular',
            created_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
            last_active TEXT
        )
    """)
    c.execute("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS last_active TEXT")
    c.execute("SELECT COUNT(*) AS cnt FROM app_users")
    if c.fetchone()["cnt"] == 0:
        seed_user = os.getenv("LOGIN_USERNAME", "").strip()
        seed_pass = os.getenv("LOGIN_PASSWORD", "")
        if seed_user and seed_pass:
            c.execute(
                "INSERT INTO app_users (username, password_hash, role) VALUES (?, ?, 'admin')",
                (seed_user, _hash_password(seed_pass))
            )

    # Chatbot history — one row per conversation, per user, plus its messages.
    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT 'New chat',
            created_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
            updated_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id SERIAL PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_user ON chat_sessions(username, updated_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, id)")

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Chatbot history (see the floating chat panel in dashboard/app.py). Every
# lookup is scoped to the username so one user can never read or change
# another user's conversations.
# ---------------------------------------------------------------------------

def create_chat_session(username: str, title: str = "New chat") -> int:
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO chat_sessions (username, title) VALUES (?, ?) RETURNING id",
        (username, title)
    )
    sid = c.fetchone()["id"]
    conn.commit()
    conn.close()
    return sid


def list_chat_sessions(username: str):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, title, created_at, updated_at FROM chat_sessions "
        "WHERE username = ? ORDER BY updated_at DESC, id DESC",
        (username,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_chat_messages(session_id: int, username: str):
    conn = get_conn()
    rows = conn.execute(
        "SELECT m.role, m.content FROM chat_messages m "
        "JOIN chat_sessions s ON s.id = m.session_id "
        "WHERE m.session_id = ? AND s.username = ? ORDER BY m.id",
        (session_id, username)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_chat_message(session_id: int, username: str, role: str, content: str):
    conn = get_conn()
    owner = conn.execute(
        "SELECT 1 FROM chat_sessions WHERE id = ? AND username = ?",
        (session_id, username)
    ).fetchone()
    if owner:
        conn.execute(
            "INSERT INTO chat_messages (session_id, role, content) VALUES (?, ?, ?)",
            (session_id, role, content)
        )
        conn.execute(
            "UPDATE chat_sessions SET updated_at = to_char(now(), 'YYYY-MM-DD HH24:MI:SS') "
            "WHERE id = ?",
            (session_id,)
        )
        conn.commit()
    conn.close()


def rename_chat_session(session_id: int, username: str, title: str):
    conn = get_conn()
    conn.execute(
        "UPDATE chat_sessions SET title = ? WHERE id = ? AND username = ?",
        (title, session_id, username)
    )
    conn.commit()
    conn.close()


def delete_chat_session(session_id: int, username: str):
    conn = get_conn()
    conn.execute(
        "DELETE FROM chat_sessions WHERE id = ? AND username = ?",
        (session_id, username)
    )
    conn.commit()
    conn.close()


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${digest}"


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, digest = stored_hash.split("$", 1)
    except (ValueError, AttributeError):
        return False
    return hashlib.sha256((salt + password).encode()).hexdigest() == digest


def get_user_by_username(username: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM app_users WHERE LOWER(username)=LOWER(?)", ((username or "").strip(),)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def verify_user_login(username: str, password: str):
    """Returns the matching user's dict (id/username/role) on success, else None."""
    user = get_user_by_username(username)
    if not user or not password:
        return None
    if not _verify_password(password, user["password_hash"]):
        return None
    return user


def create_user(username: str, password: str, role: str = "regular"):
    """Returns the new user's id, or None if that username (case-insensitively)
    already exists."""
    username = (username or "").strip()
    if not username or not password:
        return None
    conn = get_conn()
    c = conn.cursor()
    existing = c.execute(
        "SELECT id FROM app_users WHERE LOWER(username)=LOWER(?)", (username,)
    ).fetchone()
    if existing:
        conn.close()
        return None
    c.execute(
        "INSERT INTO app_users (username, password_hash, role) VALUES (?, ?, ?) RETURNING id",
        (username, _hash_password(password), role)
    )
    user_id = c.fetchone()["id"]
    conn.commit()
    conn.close()
    return user_id


def update_user_password(username: str, new_password: str):
    conn = get_conn()
    conn.execute(
        "UPDATE app_users SET password_hash=? WHERE LOWER(username)=LOWER(?)",
        (_hash_password(new_password), (username or "").strip())
    )
    conn.commit()
    conn.close()


def update_last_active(username: str):
    conn = get_conn()
    conn.execute(
        "UPDATE app_users SET last_active=to_char(now(), 'YYYY-MM-DD HH24:MI:SS') WHERE LOWER(username)=LOWER(?)",
        ((username or "").strip(),)
    )
    conn.commit()
    conn.close()


def get_all_users():
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, username, role, created_at, last_active FROM app_users ORDER BY username"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_admins():
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS cnt FROM app_users WHERE role='admin'").fetchone()
    conn.close()
    return row["cnt"]


def update_user(user_id: int, username: str, role: str, new_password: str = None):
    """Renames/re-roles a user; only touches the password if new_password is
    given (blank means "leave the current password as-is"). Returns True, or
    False if that username (case-insensitively) is already taken by a
    different account."""
    username = (username or "").strip()
    conn = get_conn()
    c = conn.cursor()
    existing = c.execute(
        "SELECT id FROM app_users WHERE LOWER(username)=LOWER(?) AND id<>?", (username, user_id)
    ).fetchone()
    if existing:
        conn.close()
        return False
    if new_password:
        c.execute(
            "UPDATE app_users SET username=?, role=?, password_hash=? WHERE id=?",
            (username, role, _hash_password(new_password), user_id)
        )
    else:
        c.execute("UPDATE app_users SET username=?, role=? WHERE id=?", (username, role, user_id))
    conn.commit()
    conn.close()
    return True


def delete_user(user_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM app_users WHERE id=?", (user_id,))
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

# ── Typed-column parsing helpers (L1: dates/quantity/tags, L2: money) ──
def _parse_tender_datetime(text):
    """Parses either portal's own date/time formats — GeM's 12-hour
    'DD-MM-YYYY H:MM AM/PM' or KPPP's 24-hour 'DD-MM-YYYY HH:MM:SS' (also
    accepts a bare date) — into a real datetime for the typed columns.
    None (never raises) if the text doesn't match, since a scrape can
    legitimately produce an unparseable or missing value."""
    if not text:
        return None
    text = str(text).strip()
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %I:%M %p", "%d-%m-%Y %H:%M", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None

def _parse_tender_int(text):
    """First integer found in a quantity string, or None for placeholders
    like KPPP's 'N/A' (KPPP's own listing page never shows a quantity at
    all — it has Location where GeM has Quantity)."""
    if text is None:
        return None
    text = str(text).strip()
    if not text or text.upper() in ("N/A", "NA", ""):
        return None
    match = re.search(r"\d+", text.replace(",", ""))
    return int(match.group()) if match else None

def _parse_item_tags(title):
    """Splits the scraper's comma-joined item list in the title (e.g.
    'CP Plus bullet camera,12 Channel HD DVR,...') into a real tag list."""
    if not title:
        return None
    tags = [t.strip() for t in str(title).split(",") if t.strip()]
    return tags or None

def _parse_money_amount(text):
    """Best-effort Rs amount out of free text like 'Rs 5,00,000', '₹1.2 Cr'
    or '2 Lakh' — None for placeholders ('See bid document', 'Not
    available', 'Not mentioned in document', etc.), which is the actual
    value on nearly every tender at L1 (neither portal publishes the real
    bid value/EMD on its search results page — only inside the document
    itself, read at L2)."""
    if not text:
        return None
    text = str(text).strip().lower()
    if not text or "not " in text or text in ("n/a", "na", "-"):
        return None
    match = re.search(r"[\d,]+\.?\d*", text)
    if not match:
        return None
    try:
        amount = float(match.group().replace(",", ""))
    except ValueError:
        return None
    if re.search(r"\bcr(ore)?\b", text):
        amount *= 1e7
    elif re.search(r"\bla(kh|c)s?\b", text):
        amount *= 1e5
    return amount

def update_tender_l2_financials(tender_id, estimated_value=None, emd_amount=None, turnover_required=None):
    """Called after L2 (Get Full Analysis) reads the real tender document —
    fills in the typed bid_value/EMD/turnover-requirement columns that L1
    can't, since those amounts aren't on either portal's search page."""
    conn = get_conn()
    conn.execute(
        """UPDATE tenders
           SET bid_value_numeric=?, emd_numeric=?, turnover_requirement_numeric=?
           WHERE id=?""",
        (
            _parse_money_amount(estimated_value),
            _parse_money_amount(emd_amount),
            _parse_money_amount(turnover_required),
            tender_id,
        )
    )
    conn.commit()
    conn.close()

def save_tender(tender: dict):
    conn = get_conn()
    c = conn.cursor()

    # Dedup by the real bid number when we have one — GeM reuses generic
    # titles (e.g. "Body Worn Camera (V2)") across genuinely different bid
    # postings, so title+portal alone falsely treats a new tender as an
    # already-saved one and silently drops it, permanently. Only falls back
    # to title+portal when tender_number wasn't captured for this tender.
    tender_number = tender.get("tender_number") or ""
    if tender_number:
        existing = c.execute(
            "SELECT id FROM tenders WHERE tender_number=? AND portal=?",
            (tender_number, tender.get("portal"))
        ).fetchone()
    else:
        existing = c.execute(
            "SELECT id FROM tenders WHERE title=? AND portal=?",
            (tender.get("title"), tender.get("portal"))
        ).fetchone()

    if not existing:
        # Convert downloaded_files list to JSON string
        downloaded_files = tender.get("downloaded_files", [])
        if isinstance(downloaded_files, list):
            downloaded_files = json.dumps(downloaded_files)

        quantity_raw = tender.get("quantity", "N/A")

        c.execute("""
            INSERT INTO tenders (
                title, portal, tender_number, department,
                location, tender_type, value, emd,
                deadline, published_date, category,
                relevance_score, url, description,
                found_via, downloaded_files, quantity,
                start_date_parsed, end_date_parsed, quantity_count, item_tags
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            quantity_raw,
            _parse_tender_datetime(tender.get("published_date")),
            _parse_tender_datetime(tender.get("deadline")),
            _parse_tender_int(quantity_raw),
            _parse_item_tags(tender.get("title")),
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

def get_unprocessed_new_tenders():
    """Every tender still marked 'new' (not yet reset to 'seen' by the
    daily cycle), with NO restriction to today's calendar date — unlike
    get_all_tenders(status_filter='new'), which intentionally only shows
    *today's* new ones for the dashboard/chatbot's "what's new today" view.
    This one is for incremental Relevant Tenders matching specifically: a
    full search can take long enough to cross midnight (several keywords,
    KPPP retries...), and tenders found just before midnight were being
    silently skipped by the old today-only check once "today" flipped to
    the next calendar day mid-run. Checking by status alone instead closes
    that gap — a tender stays eligible for matching until it's actually
    been processed, regardless of which side of midnight it landed on."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tenders WHERE status='new' ORDER BY found_at DESC"
    ).fetchall()
    conn.close()
    tenders = []
    for r in rows:
        t = dict(r)
        try:
            t["downloaded_files"] = json.loads(t.get("downloaded_files") or "[]")
        except Exception:
            t["downloaded_files"] = []
        tenders.append(t)
    return tenders

def get_new_tenders_needing_analysis(limit=100):
    """The oldest-found `limit` tenders that are still 'new' AND don't
    already have an analysis PDF (so a Relevant Tenders match that already
    got analyzed isn't redone) — used by the nightly 2 AM run to also
    download + AI-analyze every new tender, not just the Relevant Tenders
    matches, but capped so an unusually large batch (hundreds of tenders in
    one run) can't blow out how long the whole nightly run takes. Oldest
    first, i.e. in the order they were actually found, so a capped run
    always covers the earliest ones found that cycle first."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tenders WHERE status='new' "
        "AND (analysis_pdf_path IS NULL OR analysis_pdf_path = '') "
        "ORDER BY found_at ASC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    tenders = []
    for r in rows:
        t = dict(r)
        try:
            t["downloaded_files"] = json.loads(t.get("downloaded_files") or "[]")
        except Exception:
            t["downloaded_files"] = []
        tenders.append(t)
    return tenders

def update_tender_title(tender_id: int, title: str):
    """Refreshes just the title — used to backfill older rows saved before
    the GeM scraper started pulling the real full item list (see the
    data-content fix in portals/gem.py) with the fuller version, without
    re-inserting/duplicating the row."""
    conn = get_conn()
    conn.execute(
        "UPDATE tenders SET title=? WHERE id=?", (title, tender_id)
    )
    conn.commit()
    conn.close()

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
    # Same as `total` above - an expired tender has moved to its own
    # Expired card, so it shouldn't also still count here.
    relevant = conn.execute("SELECT COUNT(*) as c FROM tenders WHERE is_relevant=TRUE AND status != 'expired'").fetchone()["c"]
    searches = conn.execute("SELECT COUNT(*) as c FROM ceo_searches").fetchone()["c"]
    patterns = conn.execute("SELECT COUNT(*) as c FROM learned_patterns").fetchone()["c"]
    conn.close()
    return {
        "total_tenders": total,
        "new": new,
        "saved": saved,
        "expired": expired,
        "relevant": relevant,
        "ceo_searches": searches,
        "patterns_learned": patterns
    }


# ---------------------------------------------------------------------------
# Read-only lookups for the AI chatbot's tool-calling (see agent/chatbot.py)
# — deliberately narrow, scoped queries rather than raw SQL access, so the
# LLM can answer any question about the data without ever being able to
# write/modify/delete anything.
# ---------------------------------------------------------------------------

def get_tenders_by_date(date_str: str, portal: str = None):
    """Tenders the agent found/added on a given day (found_at date), not
    the tender's own start/deadline date — matches how a CEO naturally
    asks "what did I get on this date"."""
    conn = get_conn()
    if portal:
        rows = conn.execute(
            "SELECT * FROM tenders WHERE LEFT(found_at, 10) = ? AND portal = ? "
            "ORDER BY found_at DESC",
            (date_str, portal)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tenders WHERE LEFT(found_at, 10) = ? ORDER BY found_at DESC",
            (date_str,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def search_tenders_by_text(keywords=None, portal: str = None, limit: int = 30,
                           date_from: str = None, date_to: str = None,
                           status: str = None):
    """Chatbot search. `keywords` is one string or a list of alternatives:
    a tender matches if ANY alternative matches, and an alternative matches
    when ALL of its words appear somewhere in the title/department/bid
    number — so "body cam" finds "Body Worn Camera", and the model can pass
    several synonyms in one call. Optional filters: portal, found-on date
    range (found_at, YYYY-MM-DD) and status ('active' = not expired).
    Returns (rows, total_matches) — total is the full count, rows are capped
    at `limit`."""
    if isinstance(keywords, str):
        keywords = [keywords]
    alternatives = []
    for k in (keywords or []):
        words = [w for w in str(k).split() if w]
        if words:
            alternatives.append(words)

    where, params = [], []
    if alternatives:
        or_parts = []
        for words in alternatives:
            and_parts = []
            for w in words:
                and_parts.append("(title ILIKE ? OR department ILIKE ? OR tender_number ILIKE ?)")
                params.extend([f"%{w}%"] * 3)
            or_parts.append("(" + " AND ".join(and_parts) + ")")
        where.append("(" + " OR ".join(or_parts) + ")")
    if portal:
        where.append("portal = ?")
        params.append(portal)
    if date_from:
        where.append("LEFT(found_at, 10) >= ?")
        params.append(date_from)
    if date_to:
        where.append("LEFT(found_at, 10) <= ?")
        params.append(date_to)
    if status == "active":
        where.append("status != 'expired'")
    elif status in ("new", "saved", "expired"):
        where.append("status = ?")
        params.append(status)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) AS c FROM tenders" + clause, tuple(params)).fetchone()["c"]
    rows = conn.execute(
        "SELECT * FROM tenders" + clause + " ORDER BY found_at DESC LIMIT ?",
        tuple(params) + (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows], total

def get_tender_by_number(tender_number: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM tenders WHERE tender_number = ? LIMIT 1",
        (tender_number,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Relevant Tenders — standing keyword list + the tenders they've matched
# ---------------------------------------------------------------------------

def get_relevant_keywords_list():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM relevant_keywords ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_relevant_keyword(keyword: str):
    """Returns the new keyword's id, or None if it (case-insensitively)
    already exists."""
    keyword = (keyword or "").strip()
    if not keyword:
        return None
    conn = get_conn()
    c = conn.cursor()
    existing = c.execute(
        "SELECT id FROM relevant_keywords WHERE LOWER(keyword)=LOWER(?)", (keyword,)
    ).fetchone()
    if existing:
        conn.close()
        return None
    c.execute(
        "INSERT INTO relevant_keywords (keyword) VALUES (?) RETURNING id", (keyword,)
    )
    kw_id = c.fetchone()["id"]
    conn.commit()
    conn.close()
    return kw_id


def update_relevant_keyword(kw_id: int, keyword: str):
    conn = get_conn()
    conn.execute(
        "UPDATE relevant_keywords SET keyword=? WHERE id=?", (keyword.strip(), kw_id)
    )
    conn.commit()
    conn.close()


def delete_relevant_keyword(kw_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM relevant_keywords WHERE id=?", (kw_id,))
    conn.commit()
    conn.close()


def get_relevant_tenders_list():
    conn = get_conn()
    # Excludes expired the same way "All Tenders" already does - a relevant
    # match that's since passed its deadline belongs on the Expired card,
    # not still listed here as if it were a live opportunity. The
    # is_relevant flag/keywords themselves are untouched, so it still shows
    # correctly over on the Expired list.
    rows = conn.execute(
        "SELECT * FROM tenders WHERE is_relevant=TRUE AND status != 'expired' "
        "ORDER BY relevant_matched_at DESC"
    ).fetchall()
    conn.close()
    tenders = []
    for r in rows:
        t = dict(r)
        try:
            t["downloaded_files"] = json.loads(t.get("downloaded_files") or "[]")
        except Exception:
            t["downloaded_files"] = []
        tenders.append(t)
    return tenders


def mark_tender_relevant(tender_id: int, keyword: str):
    """Flags a tender as relevant and records which keyword(s) matched it.
    Safe to call repeatedly for the same tender with different keywords —
    keeps relevant_matched_at as the FIRST time it became relevant."""
    conn = get_conn()
    c = conn.cursor()
    row = c.execute(
        "SELECT relevant_keywords FROM tenders WHERE id=?", (tender_id,)
    ).fetchone()
    existing_kws = list(row["relevant_keywords"]) if row and row["relevant_keywords"] else []
    if keyword not in existing_kws:
        existing_kws.append(keyword)
    c.execute(
        """UPDATE tenders
           SET is_relevant=TRUE,
               relevant_keywords=?,
               relevant_matched_at=COALESCE(relevant_matched_at, CURRENT_TIMESTAMP)
           WHERE id=?""",
        (existing_kws, tender_id)
    )
    conn.commit()
    conn.close()

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


# ── COMPANY PROFILE: TURNOVER ──────────────────────────────
def get_company_turnover_list():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM company_turnover ORDER BY financial_year"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def add_company_turnover(financial_year, turnover, total_with_gst=None, status="Audited"):
    conn = get_conn()
    conn.execute(
        """INSERT INTO company_turnover (financial_year, turnover, total_with_gst, status)
           VALUES (?, ?, ?, ?)""",
        (financial_year, turnover, total_with_gst, status)
    )
    conn.commit()
    conn.close()

def update_company_turnover(turnover_id, financial_year, turnover, total_with_gst=None, status="Audited"):
    conn = get_conn()
    conn.execute(
        """UPDATE company_turnover
           SET financial_year=?, turnover=?, total_with_gst=?, status=?,
               updated_at=to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
           WHERE id=?""",
        (financial_year, turnover, total_with_gst, status, turnover_id)
    )
    conn.commit()
    conn.close()

def delete_company_turnover(turnover_id):
    conn = get_conn()
    conn.execute("DELETE FROM company_turnover WHERE id=?", (turnover_id,))
    conn.commit()
    conn.close()


# ── COMPANY PROFILE: CHIPSCAPE'S OWN CREDENTIALS ───────────
def get_company_credentials_list():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM company_credentials ORDER BY name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def add_company_credential(name, ctype, issuing_authority, certificate_no, issue_date, expiry_date, status="Valid"):
    conn = get_conn()
    conn.execute(
        """INSERT INTO company_credentials
           (name, type, issuing_authority, certificate_no, issue_date, expiry_date, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (name, ctype, issuing_authority, certificate_no, issue_date, expiry_date, status)
    )
    conn.commit()
    conn.close()

def update_company_credential(cred_id, name, ctype, issuing_authority, certificate_no, issue_date, expiry_date, status="Valid"):
    conn = get_conn()
    conn.execute(
        """UPDATE company_credentials
           SET name=?, type=?, issuing_authority=?, certificate_no=?, issue_date=?, expiry_date=?, status=?,
               updated_at=to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
           WHERE id=?""",
        (name, ctype, issuing_authority, certificate_no, issue_date, expiry_date, status, cred_id)
    )
    conn.commit()
    conn.close()

def delete_company_credential(cred_id):
    conn = get_conn()
    conn.execute("DELETE FROM company_credentials WHERE id=?", (cred_id,))
    conn.commit()
    conn.close()


# ── SUBCONTRACTOR / SYSTEM-INTEGRATOR PARTNERS ─────────────
def get_partners_list():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM partners ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def add_partner(name, role="", notes=""):
    conn = get_conn()
    row = conn.execute(
        "INSERT INTO partners (name, role, notes) VALUES (?, ?, ?) RETURNING id",
        (name, role, notes)
    ).fetchone()
    conn.commit()
    conn.close()
    return row["id"] if row else None

def update_partner(partner_id, name, role="", notes=""):
    conn = get_conn()
    conn.execute(
        """UPDATE partners SET name=?, role=?, notes=?,
               updated_at=to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
           WHERE id=?""",
        (name, role, notes, partner_id)
    )
    conn.commit()
    conn.close()

def delete_partner(partner_id):
    """Also drops the partner's own credentials (ON DELETE CASCADE on
    partner_credentials.partner_id)."""
    conn = get_conn()
    conn.execute("DELETE FROM partners WHERE id=?", (partner_id,))
    conn.commit()
    conn.close()

def get_partner_credentials_list(partner_id=None):
    """All partner credentials, or just one partner's if partner_id is given."""
    conn = get_conn()
    if partner_id is not None:
        rows = conn.execute(
            "SELECT * FROM partner_credentials WHERE partner_id=? ORDER BY name",
            (partner_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM partner_credentials ORDER BY partner_id, name"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def add_partner_credential(partner_id, name, ctype, issuing_authority, certificate_no, issue_date, expiry_date, status="Valid"):
    conn = get_conn()
    conn.execute(
        """INSERT INTO partner_credentials
           (partner_id, name, type, issuing_authority, certificate_no, issue_date, expiry_date, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (partner_id, name, ctype, issuing_authority, certificate_no, issue_date, expiry_date, status)
    )
    conn.commit()
    conn.close()

def update_partner_credential(cred_id, name, ctype, issuing_authority, certificate_no, issue_date, expiry_date, status="Valid"):
    conn = get_conn()
    conn.execute(
        """UPDATE partner_credentials
           SET name=?, type=?, issuing_authority=?, certificate_no=?, issue_date=?, expiry_date=?, status=?,
               updated_at=to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
           WHERE id=?""",
        (name, ctype, issuing_authority, certificate_no, issue_date, expiry_date, status, cred_id)
    )
    conn.commit()
    conn.close()

def delete_partner_credential(cred_id):
    conn = get_conn()
    conn.execute("DELETE FROM partner_credentials WHERE id=?", (cred_id,))
    conn.commit()
    conn.close()


# ── COMPANY PROFILE: PAST PROJECT REFERENCES ───────────────
def get_project_references_list():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM project_references ORDER BY financial_year DESC, project_name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def add_project_reference(data: dict):
    conn = get_conn()
    conn.execute(
        """INSERT INTO project_references
           (project_name, customer, department_type, location, solution_category,
            eligibility_tags, project_scope, project_value, government_project,
            financial_year, project_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data.get("project_name"), data.get("customer"), data.get("department_type"),
            data.get("location"), data.get("solution_category"), data.get("eligibility_tags"),
            data.get("project_scope"), data.get("project_value"), data.get("government_project"),
            data.get("financial_year"), data.get("project_status"),
        )
    )
    conn.commit()
    conn.close()

def update_project_reference(ref_id, data: dict):
    conn = get_conn()
    conn.execute(
        """UPDATE project_references
           SET project_name=?, customer=?, department_type=?, location=?, solution_category=?,
               eligibility_tags=?, project_scope=?, project_value=?, government_project=?,
               financial_year=?, project_status=?,
               updated_at=to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
           WHERE id=?""",
        (
            data.get("project_name"), data.get("customer"), data.get("department_type"),
            data.get("location"), data.get("solution_category"), data.get("eligibility_tags"),
            data.get("project_scope"), data.get("project_value"), data.get("government_project"),
            data.get("financial_year"), data.get("project_status"), ref_id,
        )
    )
    conn.commit()
    conn.close()

def delete_project_reference(ref_id):
    conn = get_conn()
    conn.execute("DELETE FROM project_references WHERE id=?", (ref_id,))
    conn.commit()
    conn.close()


# ── COMPANY PROFILE: PROJECT QUANTITIES SUPPLIED ───────────
def get_project_quantities_list():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM project_quantities ORDER BY project_name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def add_project_quantity(data: dict):
    conn = get_conn()
    conn.execute(
        """INSERT INTO project_quantities
           (project_name, solution_category, quantity_category, quantity_executed, unit, customer)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            data.get("project_name"), data.get("solution_category"), data.get("quantity_category"),
            data.get("quantity_executed"), data.get("unit"), data.get("customer"),
        )
    )
    conn.commit()
    conn.close()

def update_project_quantity(qty_id, data: dict):
    conn = get_conn()
    conn.execute(
        """UPDATE project_quantities
           SET project_name=?, solution_category=?, quantity_category=?, quantity_executed=?, unit=?, customer=?,
               updated_at=to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
           WHERE id=?""",
        (
            data.get("project_name"), data.get("solution_category"), data.get("quantity_category"),
            data.get("quantity_executed"), data.get("unit"), data.get("customer"), qty_id,
        )
    )
    conn.commit()
    conn.close()

def delete_project_quantity(qty_id):
    conn = get_conn()
    conn.execute("DELETE FROM project_quantities WHERE id=?", (qty_id,))
    conn.commit()
    conn.close()


init_db()
