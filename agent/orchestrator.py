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
from database.db import save_tender, get_all_ceo_searches, update_downloaded_files
from agent.learner import expand_keyword_instantly, analyze_patterns, get_search_queries
from agent.watcher import start_watching, register_callback

def check_relevance_ai(original_keyword: str, titles: list) -> list:
    """
    Use AI to check which tender titles are actually relevant
    to what the CEO originally searched for.
    Returns list of True/False matching titles list.
    """
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
                "max_tokens": 500
            },
            timeout=60
        )
        
        result = response.json()["choices"][0]["message"]["content"].strip()
        
        if "```json" in result:
            result = result.split("```json")[1].split("```")[0]
        elif "```" in result:
            result = result.split("```")[1].split("```")[0]
        
        flags = json_lib.loads(result)
        
        # Normalize all values to proper booleans (handles string 'true'/'false' too)
        normalized_flags = []
        for f in flags:
            if isinstance(f, bool):
                normalized_flags.append(f)
            elif isinstance(f, str):
                normalized_flags.append(f.strip().lower() == "true")
            else:
                normalized_flags.append(bool(f))
        flags = normalized_flags
        
        # Pad or trim to match titles length
        while len(flags) < len(titles):
            flags.append(True)
        
        return flags[:len(titles)]
        
    except Exception as e:
        import traceback
        print(f"[Relevance Check] Failed: {e}")
        traceback.print_exc()
        return [True] * len(titles)
_state = {
    "status": "idle",
    "last_search": None,
    "last_learn": None,
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

        gem_thread = threading.Thread(
            target=_search_portal,
            args=("GeM", expanded, category),
            daemon=True
        )
        kppp_thread = threading.Thread(
            target=_search_portal,
            args=("KPPP", expanded, category),
            daemon=True
        )
        gem_thread.start()
        kppp_thread.start()
        gem_thread.join()
        kppp_thread.join()

        with _lock:
            _state["status"] = "watching"
            _state["last_search"] = datetime.now().isoformat()
        log(f"✅ Search complete for '{keyword}'")

    except Exception as e:
        log(f"❌ Error expanding '{keyword}': {e}")
        with _lock:
            _state["status"] = "watching"

def _search_portal(portal: str, keywords: list, category: str):
    try:
        if portal == "GeM":
            from portals.gem import search_gem_sync
            search_fn = search_gem_sync
        else:
            from portals.kppp import search_kppp_sync
            search_fn = search_kppp_sync

        found_count = 0
        for keyword in keywords[:8]:
            if not _state["running"]:
                log(f"🛑 [{portal}] Search stopped by user")
                break
            try:
                log(f"🔎 [{portal}] Searching: '{keyword}'")
                tenders = search_fn(keyword, category)
                
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
        if not searches:
            log("⏰ Scheduled search skipped — no CEO searches yet")
            continue
        # Reset old new tenders to "seen" status
        from database.db import get_conn
        conn = get_conn()
        conn.execute("UPDATE tenders SET status='seen' WHERE status='new'")
        conn.commit()
        conn.close()
        log("🔄 Reset old tenders — fresh start for today")

        log("⏰ Daily 2 AM scheduled search starting...")

        with _lock:
            _state["status"] = "learning"

        result = analyze_patterns()
        summary = result.get("summary", "")
        if summary:
            log(f"📋 Pattern summary: {summary}")

        queries = get_search_queries()
        if not queries:
            log("⚠️ No search queries available")
            with _lock:
                _state["status"] = "watching"
            continue

        log(f"🚀 Running {len(queries)} scheduled searches...")

        with _lock:
            _state["status"] = "searching"

        gem_queries = [q for q in queries if q["portal"] == "GeM"]
        kppp_queries = [q for q in queries if q["portal"] == "KPPP"]
        gem_keywords = [q["keyword"] for q in gem_queries[:10]]
        kppp_keywords = [q["keyword"] for q in kppp_queries[:10]]
        category = queries[0].get("category", "") if queries else ""

        gem_thread = threading.Thread(
            target=_search_portal,
            args=("GeM", gem_keywords, category),
            daemon=True
        )
        kppp_thread = threading.Thread(
            target=_search_portal,
            args=("KPPP", kppp_keywords, category),
            daemon=True
        )
        gem_thread.start()
        kppp_thread.start()
        gem_thread.join()
        kppp_thread.join()

        with _lock:
            _state["status"] = "watching"
            _state["last_search"] = datetime.now().isoformat()
            _state["tenders_found_today"] = 0

        log("✅ Daily scheduled search complete")

def _run_startup_search():
    """Run immediate search when agent starts."""
    time.sleep(3)
    
    searches = get_all_ceo_searches()
    if not searches:
        log("💡 No previous searches. Use search box to find tenders.")
        return
    
    log("🚀 Running startup search for today's tenders...")
    
    queries = get_search_queries()
    if not queries:
        return
    
    gem_keywords = [q["keyword"] for q in queries if q["portal"] == "GeM"][:8]
    kppp_keywords = [q["keyword"] for q in queries if q["portal"] == "KPPP"][:8]
    category = queries[0].get("category", "") if queries else ""
    
    gem_thread = threading.Thread(
        target=_search_portal,
        args=("GeM", gem_keywords, category),
        daemon=True
    )
    kppp_thread = threading.Thread(
        target=_search_portal,
        args=("KPPP", kppp_keywords, category),
        daemon=True
    )
    gem_thread.start()
    kppp_thread.start()
    gem_thread.join()
    kppp_thread.join()
    
    log("✅ Startup search complete — check New Tenders!")

def start_agent():
    with _lock:
        _state["running"] = True
        _state["status"] = "watching"

    log("🤖 Tender Agent started")
    log("👀 Watching CEO browser activity on GeM & KPPP...")

    register_callback(on_ceo_search)
    start_watching()

    # Immediately search on startup
    threading.Thread(
        target=_run_startup_search,
        daemon=True
    ).start()

    scheduler = threading.Thread(
        target=_scheduled_search,
        daemon=True
    )
    scheduler.start()

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