"""
Agent Orchestrator
The brain of the system.
Coordinates watcher + learner + searcher.
Runs in background 24/7.
Searches every day at 2 AM automatically.
"""

import threading
import time
import json
from datetime import datetime
from database.db import (
    save_tender, get_all_ceo_searches, update_downloaded_files,
    save_kppp_criteria, get_kppp_criteria_list, delete_expired_tenders
)
from agent.learner import expand_keyword_instantly, analyze_patterns, get_search_queries
from agent.watcher import start_watching, register_callback

def check_relevance_ai(original_keyword: str, titles: list) -> list:
    """
    Use AI to check which tender titles are actually relevant
    to what the CEO originally searched for.
    Processes in batches of 20 to avoid response truncation.
    Returns list of True/False matching titles list.
    """
    if not titles:
        return []

    BATCH_SIZE = 20
    all_flags = []

    for i in range(0, len(titles), BATCH_SIZE):
        batch = titles[i:i + BATCH_SIZE]
        batch_flags = _check_relevance_batch(original_keyword, batch)
        all_flags.extend(batch_flags)

    return all_flags


def _check_relevance_batch(original_keyword: str, titles: list) -> list:
    """Check relevance for a single batch of titles (max ~20)."""
    if not titles:
        return []

    try:
        import requests
        import os
        import json as json_lib

        DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

        titles_numbered = "\n".join([f"{i+1}. {t}" for i, t in enumerate(titles)])

        prompt = f"""The user is looking for tenders related to: "{original_keyword}"
Here is a list of tender titles found:
{titles_numbered}
For EACH numbered title, decide if it is genuinely relevant to "{original_keyword}".
A tender is relevant if it's about the same product/service category, even if worded differently.
A tender is NOT relevant if it's about a completely different category (e.g. food, furniture, cleaning supplies) when searching for electronics/security equipment.
Respond ONLY with a JSON array of true/false values, one per title, in order.
Example: [true, false, true, true, false]"""

        response = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1000
            },
            timeout=60
        )

        result = response.json()["choices"][0]["message"]["content"].strip()

        if "```json" in result:
            result = result.split("```json")[1].split("```")[0]
        elif "```" in result:
            result = result.split("```")[1].split("```")[0]

        flags = json_lib.loads(result)

        normalized_flags = []
        for f in flags:
            if isinstance(f, bool):
                normalized_flags.append(f)
            elif isinstance(f, str):
                normalized_flags.append(f.strip().lower() == "true")
            else:
                normalized_flags.append(bool(f))
        flags = normalized_flags

        while len(flags) < len(titles):
            flags.append(True)

        return flags[:len(titles)]

    except Exception as e:
        print(f"[Relevance Check] Batch failed: {e}, defaulting to keep all in this batch")
        return [True] * len(titles)
_state = {
    "status": "idle",
    "last_search": None,
    "last_learn": None,
    "last_full_search_date": None,
    "tenders_found_today": 0,
    "log": [],
    "running": False,
    "ceo_searches_count": 0,
    "current_keyword": "",
    "current_portal": ""
}

_lock = threading.Lock()
_new_keyword_buffer = []

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    with _lock:
        _state["log"].append(entry)
        if len(_state["log"]) > 200:
            _state["log"] = _state["log"][-200:]
    print(entry)

def get_state():
    with _lock:
        return dict(_state)

def get_log():
    with _lock:
        return list(_state["log"])

def on_ceo_search(keyword: str, portal: str):
    global _new_keyword_buffer
    log(f"👀 CEO searched: '{keyword}' on {portal}")

    from database.db import save_ceo_search
    save_ceo_search(keyword, portal)

    with _lock:
        _state["ceo_searches_count"] += 1
        _state["current_keyword"] = keyword
        _state["current_portal"] = portal
        _new_keyword_buffer.append(keyword)
    threading.Thread(
        target=_instant_expand_and_search,
        args=(keyword, portal),
        daemon=True
    ).start()

def _instant_expand_and_search(keyword: str, portal: str):

    if _state.get("status") in ("searching", "expanding"):
        log(f"⏳ Search already in progress, skipping duplicate trigger for '{keyword}'")
        return
    with _lock:
        _state["status"] = "expanding"
    log(f"🧠 Expanding '{keyword}' with AI...")
    try:
        result = expand_keyword_instantly(keyword)
        expanded = result.get("expanded_keywords", [keyword])
        category = result.get("category", keyword.title())
        log(f"✅ Expanded to {len(expanded)} keywords: {', '.join(expanded[:5])}...")
        with _lock:
            _state["status"] = "searching"
            _state["last_learn"] = datetime.now().isoformat()

        # KPPP is not searched here — it has no keyword search of its own,
        # so this GeM keyword box never actually drove a useful KPPP search
        # (the previous KPPP thread just re-ran the same fixed listing).
        # KPPP is searched via its own filter form (see on_kppp_search)
        # instead, using KPPP's real search fields.
        _search_portal("GeM", expanded, category, 15)

        with _lock:
            _state["status"] = "watching"
            _state["last_search"] = datetime.now().isoformat()
        log(f"✅ Search complete for '{keyword}'")

    except Exception as e:
        log(f"❌ Error expanding '{keyword}': {e}")
        with _lock:
            _state["status"] = "watching"

def _search_portal(portal: str, keywords: list, category: str, max_pages: int = 5, respect_stop_flag: bool = False):
    # GeM-only: KPPP has no keyword search, so it's handled separately by
    # on_kppp_search / _run_kppp_criteria_search using KPPP's real filter
    # fields instead of a keyword list.
    if portal != "GeM":
        log(f"⚠️ _search_portal only supports GeM, got '{portal}'")
        return
    try:
        from portals.gem import search_gem_sync
        search_fn = search_gem_sync

        # Not gated on _state["running"] by default — a manual search from
        # the search box must work whether or not Start Agent has been
        # clicked this session. respect_stop_flag=True is only passed by the
        # agent-driven callers (full catalog run, 2 AM scheduler) so that
        # clicking Stop Agent actually cuts a multi-keyword run short
        # instead of letting it keep going through every remaining keyword.
        should_stop = (lambda: not _state["running"]) if respect_stop_flag else None

        found_count = 0
        for keyword in keywords[:8]:
            if should_stop and should_stop():
                log(f"🛑 [{portal}] Stop requested — ending search early")
                break
            try:
                log(f"🔎 [{portal}] Searching: '{keyword}'")
                tenders = search_fn(keyword, category, max_pages=max_pages, should_stop=should_stop)

                # Batch relevance check using AI
                if tenders:
                    titles = [t.get("title", "") for t in tenders]
                    relevant_flags = check_relevance_ai(keyword, titles)
                    print(f"[Relevance] Keyword '{keyword}': {relevant_flags}")
                else:
                    relevant_flags = []
                
                for i, tender in enumerate(tenders):
                    is_relevant = relevant_flags[i] if i < len(relevant_flags) else True
                    if not is_relevant:
                        continue  # skip irrelevant tender

                    tender_id = save_tender(tender)
                    if tender_id:
                        found_count += 1
                        files = tender.get("downloaded_files", [])
                        if files:
                            update_downloaded_files(tender_id, files)
                        log(f"📄 [{portal}] Found: {tender['title'][:60]}...")
            except Exception as e:
                log(f"⚠️ [{portal}] Error searching '{keyword}': {e}")

        with _lock:
            _state["tenders_found_today"] += found_count
        log(f"✅ [{portal}] Done. Found {found_count} new tenders")

    except Exception as e:
        log(f"❌ [{portal}] Fatal error: {e}")

def on_kppp_search(criteria: dict):
    """
    CEO submitted the KPPP-specific search form (category, procuring entity,
    tender number, closure dates, tender type). Unlike GeM, KPPP has no
    keyword search, so this saves the real filter values instead of a
    keyword and immediately runs a search with them. Every future agent
    cycle (Start Agent, 2 AM scheduled run) re-applies every saved KPPP
    criteria set automatically, the same way GeM's learned keywords do.
    """
    save_kppp_criteria(criteria)
    log(f"👀 CEO set KPPP search filter: {criteria}")
    threading.Thread(
        target=_run_kppp_criteria_search,
        args=([criteria],),
        daemon=True
    ).start()

def _run_kppp_criteria_search(criteria_list: list, respect_stop_flag: bool = False):
    if not criteria_list:
        return
    if _state.get("status") in ("searching", "expanding"):
        log("⏳ Search already in progress, skipping duplicate KPPP trigger")
        return
    with _lock:
        _state["status"] = "searching"
    try:
        from portals.kppp import search_kppp_sync

        # Not gated on _state["running"] by default — see _search_portal for
        # why. respect_stop_flag=True (agent-driven callers only) stops the
        # loop before starting the next saved filter once Stop Agent is hit;
        # it can't abort a single KPPP search already mid-CAPTCHA-retry.
        found_count = 0
        for criteria in criteria_list:
            if respect_stop_flag and not _state["running"]:
                log("🛑 [KPPP] Stop requested — ending search early")
                break
            if not (criteria.get("procuring_entity") or "").strip() and not (criteria.get("tender_name") or "").strip():
                # A criteria row with neither an entity nor a tender name
                # searches every KPPP department at once — refuse to run it
                # rather than silently pulling in tenders outside what was
                # actually asked for.
                log(f"⚠️ [KPPP] Skipping saved filter with no procuring entity or tender name: {criteria}")
                continue
            try:
                log(f"🔎 [KPPP] Searching with saved filter: {criteria}")
                tenders = search_kppp_sync(criteria)
                for tender in tenders:
                    tender_id = save_tender(tender)
                    if tender_id:
                        found_count += 1
                        files = tender.get("downloaded_files", [])
                        if files:
                            update_downloaded_files(tender_id, files)
                        log(f"📄 [KPPP] Found: {tender['title'][:60]}...")
            except Exception as e:
                log(f"⚠️ [KPPP] Error searching with filter {criteria}: {e}")

        with _lock:
            _state["tenders_found_today"] += found_count
        log(f"✅ [KPPP] Done. Found {found_count} new tenders")
    except Exception as e:
        log(f"❌ [KPPP] Fatal error: {e}")
    finally:
        with _lock:
            _state["status"] = "watching"
            _state["last_search"] = datetime.now().isoformat()

def _scheduled_search():
    """Runs every day at 2 AM automatically."""
    import datetime as dt
    while _state["running"]:
        now = dt.datetime.now()
        next_2am = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if now >= next_2am:
            next_2am += dt.timedelta(days=1)

        seconds_until_2am = (next_2am - now).total_seconds()
        log(f"⏰ Next search scheduled at 2:00 AM ({int(seconds_until_2am/3600)} hours away)")
        time.sleep(seconds_until_2am)

        if not _state["running"]:
            break

        searches = get_all_ceo_searches()
        kppp_criteria_list = get_kppp_criteria_list()
        if not searches and not kppp_criteria_list:
            log("⏰ Scheduled search skipped — no CEO searches yet")
            continue
        # Reset old new tenders to "seen" status
        from database.db import get_conn
        conn = get_conn()
        conn.execute("UPDATE tenders SET status='seen' WHERE status='new'")
        conn.commit()
        conn.close()
        log("🔄 Reset old tenders — fresh start for today")

        removed = delete_expired_tenders()
        if removed:
            log(f"🧹 Removed {removed} expired tender(s) (past their deadline)")

        log("⏰ Daily 2 AM scheduled search starting...")

        if searches:
            with _lock:
                _state["status"] = "learning"

            result = analyze_patterns()
            summary = result.get("summary", "")
            if summary:
                log(f"📋 Pattern summary: {summary}")

            queries = get_search_queries()
            gem_keywords = [q["keyword"] for q in queries if q["portal"] == "GeM"][:10]
            category = queries[0].get("category", "") if queries else ""

            log(f"🚀 Running {len(gem_keywords)} scheduled GeM searches...")
            with _lock:
                _state["status"] = "searching"
            _search_portal("GeM", gem_keywords, category, 15, respect_stop_flag=True)

        if kppp_criteria_list:
            log(f"🚀 Running {len(kppp_criteria_list)} scheduled KPPP searches...")
            _run_kppp_criteria_search(kppp_criteria_list, respect_stop_flag=True)

        with _lock:
            _state["status"] = "watching"
            _state["last_search"] = datetime.now().isoformat()
            _state["last_full_search_date"] = datetime.now().date().isoformat()
            _state["tenders_found_today"] = 0

        log("✅ Daily scheduled search complete")

def _run_full_catalog_search():
    """
    Run every saved GeM pattern + every saved KPPP filter right now.
    Triggered on every "Start Agent" click — always fetches fresh results
    on demand, regardless of whether a full search already ran today. This
    is the manual/on-demand counterpart to the automatic daily 2 AM sweep
    in _scheduled_search(); dedup in save_tender() (by title+portal) keeps
    repeated runs from creating duplicate rows.
    """
    time.sleep(3)

    searches = get_all_ceo_searches()
    kppp_criteria_list = get_kppp_criteria_list()
    if not searches and not kppp_criteria_list:
        log("💡 No previous searches. Use search box to find tenders.")
        return

    log("🚀 Running full catalog search...")

    if searches:
        queries = get_search_queries()
        gem_keywords = [q["keyword"] for q in queries if q["portal"] == "GeM"][:8]
        category = queries[0].get("category", "") if queries else ""
        _search_portal("GeM", gem_keywords, category, 15, respect_stop_flag=True)

    if kppp_criteria_list:
        _run_kppp_criteria_search(kppp_criteria_list, respect_stop_flag=True)

    with _lock:
        _state["last_full_search_date"] = datetime.now().date().isoformat()
    log("✅ Full catalog search complete — check New Tenders!")

_scheduler_thread = None

def start_agent():
    global _scheduler_thread

    with _lock:
        _state["running"] = True
        _state["status"] = "watching"

    log("🤖 Tender Agent started")

    removed = delete_expired_tenders()
    if removed:
        log(f"🧹 Removed {removed} expired tender(s) (past their deadline)")

    log("👀 Watching CEO browser activity on GeM & KPPP...")

    register_callback(on_ceo_search)
    start_watching()

    # Always fetch fresh tenders on every Start Agent click, whether it's
    # the first start today or a restart later.
    threading.Thread(
        target=_run_full_catalog_search,
        daemon=True
    ).start()

    # Only one scheduler loop should ever be alive — clicking Start Agent
    # again (e.g. after Stop) must not stack duplicate 2 AM loops.
    if _scheduler_thread is None or not _scheduler_thread.is_alive():
        _scheduler_thread = threading.Thread(
            target=_scheduled_search,
            daemon=True
        )
        _scheduler_thread.start()

    log("✅ Agent is live and monitoring")

def stop_agent():
    with _lock:
        _state["running"] = False
        _state["status"] = "idle"
    log("🛑 Agent stopped")

def trigger_manual_search():
    searches = get_all_ceo_searches()
    if searches:
        latest = searches[0]["keyword"]
        portal = searches[0]["portal"]
        log(f"🔄 Manual search triggered for: '{latest}'")
        threading.Thread(
            target=_instant_expand_and_search,
            args=(latest, portal),
            daemon=True
        ).start()
    else:
        log("⚠️ No CEO searches yet — please search on GeM or KPPP first")