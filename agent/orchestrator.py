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
from datetime import datetime, timedelta
from database.db import (
    save_tender, get_all_ceo_searches, update_downloaded_files,
    save_kppp_criteria, get_kppp_criteria_list, mark_expired_tenders,
    save_ceo_search, save_learned_patterns,
    has_had_full_gem_search, mark_gem_full_search_done,
    get_gem_last_searched_at, update_gem_last_searched,
    update_kppp_last_searched
)
from agent.watcher import start_watching, register_callback


def _parse_kppp_published_date(value):
    """Parses whatever format KPPP's own results table prints its
    Published/Start Date column in — tried loosest-to-strictest since the
    exact format isn't guaranteed. Returns None (never raises) when it
    can't be parsed, so a date-limited repeat search can fall back to
    keeping the tender rather than risk silently dropping a real one."""
    if not value or not value.strip():
        return None
    text = value.strip()
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_db_timestamp(value):
    """Parses a 'YYYY-MM-DD HH24:MI:SS' timestamp string (the format every
    *_at/*_searched_at column in the DB stores) into a datetime — None in,
    None out, so callers can pass a possibly-missing last-searched value
    straight through without a separate None check.

    Shifted one day earlier than the stored value before being used as a
    search cutoff (per the CEO's manager: treat the last-searched moment as
    one day earlier than it actually was) — a deliberate overlap so a
    repeat search can never miss a tender due to clock/timezone drift or a
    portal listing a tender a little later than its real Start Date.
    Tenders already saved from the overlapped window are skipped anyway via
    the normal "already in database" dedup, so the overlap costs nothing."""
    if not value:
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed - timedelta(days=1)


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


def _check_relevance_batch(original_keyword: str, titles: list, max_attempts: int = 3) -> list:
    """Check relevance for a single batch of titles (max ~20). Retries the
    DeepSeek call a few times before giving up — a transient truncated/
    malformed response (rare, but does happen) shouldn't immediately
    trigger the fail-safe "exclude everything" fallback below when a
    second try would likely have succeeded cleanly."""
    if not titles:
        return []

    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return _check_relevance_batch_once(original_keyword, titles)
        except Exception as e:
            last_error = e
            print(f"[Relevance Check] Attempt {attempt}/{max_attempts} failed: {e}")

    print(f"[Relevance Check] All {max_attempts} attempts failed ({last_error}), excluding all in this batch (fail safe, not fail open)")
    return [False] * len(titles)


def _check_relevance_batch_once(original_keyword: str, titles: list) -> list:
    import requests
    import os
    import json as json_lib

    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

    titles_numbered = "\n".join([f"{i+1}. {t}" for i, t in enumerate(titles)])

    prompt = f"""The user is looking for tenders related to: "{original_keyword}"
Here is a list of tender titles found:
{titles_numbered}
For EACH numbered title, decide if it is genuinely relevant to "{original_keyword}" specifically —
not just a broader category it happens to belong to. A tender is relevant only if a person who typed
"{original_keyword}" would actually want to see it (the same specific product/item, even if worded
differently or abbreviated). A tender is NOT relevant if it's a different product that merely shares
a word with "{original_keyword}" — e.g. if searching for "binoculars", an "Optical Table" or "Optical
Fiber Cable" is NOT relevant even though both contain "optical"; if searching for "camera", a "Camera
Trap for wildlife study" used as a research instrument may still count, but unrelated lab equipment
that merely mentions "camera" in a spec sheet does not. When in doubt, be strict and mark it false.
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

    # A short/truncated AI response means those trailing titles were never
    # actually judged — treat them as unverified and exclude them rather
    # than assuming they're relevant. This matters most for the AI-expanded
    # "bonus" keywords (never the CEO's literal search term, which skips
    # this check entirely) — an unverified match there has no business
    # polluting the tenders table.
    while len(flags) < len(titles):
        flags.append(False)

    return flags[:len(titles)]
_state = {
    "status": "idle",
    "last_search": None,
    "last_learn": None,
    "last_full_search_date": None,
    "last_search_completed_at": None,
    # True for the entire span of a full catalog search (every keyword/KPPP
    # filter), unlike "status" above which flickers between "searching" and
    # "watching" from one keyword to the next — the dashboard uses this one
    # (not "status") to decide whether to keep auto-refreshing, since
    # gating on the flickering value would let the page land on a
    # "watching" render mid-search and then never refresh again.
    "search_in_progress": False,
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
        _state["status"] = "searching"
    log(f"🔎 Searching GeM for '{keyword}' — exactly what was typed, no AI keyword expansion...")
    try:
        # No AI keyword expansion — the CEO wants a search for "X" to return
        # exactly what GeM's own "Contains" search returns for "X", nothing
        # more (previously an AI-guessed synonym list like "wear"/"weather"
        # for "worn" pulled in unrelated tenders GeM itself never showed).
        # Still recorded as a (single-keyword) learned pattern — this isn't
        # read back for the daily/Start Agent re-search list any more (that
        # now comes straight from ceo_searches, see _scheduled_search /
        # _run_full_catalog_search), but other things still read it (the
        # Patterns Learned page, stats).
        from database.db import save_learned_patterns
        save_learned_patterns(
            category=keyword.title(),
            keywords=json.dumps([keyword]),
            weight=1.0
        )
        with _lock:
            _state["last_learn"] = datetime.now().isoformat()

        # KPPP is not searched here — it has no keyword search of its own,
        # so this GeM keyword box never actually drove a useful KPPP search
        # (the previous KPPP thread just re-ran the same fixed listing).
        # KPPP is searched via its own filter form (see on_kppp_search)
        # instead, using KPPP's real search fields.
        _search_portal("GeM", [keyword], keyword.title(), 15, primary_keyword=keyword)

        # A manual search in the search bar is its own batch of "new
        # tenders" just like a Start Agent run's — check them against the
        # saved Relevant Tenders keywords right now too, instead of making
        # the CEO wait for the next Start Agent click to find out this
        # search's results include a relevant match.
        run_relevant_keyword_matching(scope="incremental")

        with _lock:
            _state["status"] = "watching"
            _state["last_search"] = datetime.now().isoformat()
        log(f"✅ Search complete for '{keyword}'")

    except Exception as e:
        log(f"❌ Error searching '{keyword}': {e}")
        with _lock:
            _state["status"] = "watching"

def _search_portal(portal: str, keywords: list, category: str, max_pages: int = 5, respect_stop_flag: bool = False, primary_keyword: str = None, primary_keywords: set = None):
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
                if primary_keywords is not None:
                    # Start Agent / 2 AM batch runs: primary_keywords is the
                    # set of literal keywords the CEO actually typed (index 0
                    # of each learned pattern — see get_search_queries). Those
                    # skip the AI relevance filter below just like a manual
                    # search does, so a real GeM hit for the CEO's own
                    # keyword can never be silently dropped by our own AI's
                    # disagreement with GeM's match. AI-suggested synonyms
                    # further down the same pattern still get filtered.
                    is_primary = keyword.strip().lower() in primary_keywords
                else:
                    is_primary = primary_keyword is not None and keyword.strip().lower() == primary_keyword.strip().lower()
                # Start Agent / 2 AM batch runs give each of their keywords
                # one full/unlimited pass too — but only the very first time
                # that keyword is ever run through a batch cycle. Every batch
                # run after that for the same keyword instead searches only
                # for tenders whose Start Date is newer than the last time it
                # was searched (see search_gem's stop_before_date) — not a
                # re-walk of every page, and not an arbitrary fixed page
                # count either.
                is_first_batch_pass = primary_keyword is None and not has_had_full_gem_search(keyword)
                # Full pagination: a manual single-keyword search always
                # walks every page; a batch run (Start Agent / 2 AM) only
                # does that on a keyword's very first pass, then relies on
                # stop_before_date for incremental catch-up afterwards —
                # this is unchanged by whether the keyword is the CEO's
                # literal term or an AI synonym (that's a separate decision,
                # just below, about the AI relevance filter).
                do_full_pagination = (is_primary and primary_keywords is None) or is_first_batch_pass
                # The literal keyword the CEO typed gets every page GeM has
                # (None = unlimited, see search_gem) so the count matches
                # GeM's own site exactly; AI-expanded synonyms stay capped —
                # they're bonus discovery, not the thing being compared 1:1.
                stop_before_date = None
                if do_full_pagination:
                    pages_for_keyword = None
                    log(f"🔎 [{portal}] Searching: '{keyword}' (full pagination)")
                else:
                    stop_before_date = _parse_db_timestamp(get_gem_last_searched_at(keyword))
                    # Unlimited pages here too — search_gem's own
                    # stop_before_date check decides when to stop, not a
                    # page count, since "how many pages of new tenders
                    # exist since last time" varies run to run.
                    pages_for_keyword = None
                    log(
                        f"🔎 [{portal}] Searching: '{keyword}' "
                        f"(tenders newer than {stop_before_date or 'unknown — falling back to one capped pass'})"
                    )
                    if stop_before_date is None:
                        # No recorded last-search time (shouldn't normally
                        # happen once a keyword has had its full pass, but
                        # fall back safely rather than accidentally paging
                        # forever) — a capped pass instead of unlimited.
                        pages_for_keyword = max_pages
                tenders = search_fn(
                    keyword, category, max_pages=pages_for_keyword,
                    should_stop=should_stop, stop_before_date=stop_before_date
                )
                if is_first_batch_pass:
                    mark_gem_full_search_done(keyword)
                update_gem_last_searched(keyword)

                if is_primary:
                    # This is GeM's own "Contains" search result for the
                    # exact term the CEO typed — GeM already decided
                    # relevance. Re-filtering it with our own AI guess would
                    # risk silently dropping tenders GeM itself shows, which
                    # breaks the "matches GeM exactly" guarantee. Only the
                    # AI-guessed synonym keywords below get the AI filter.
                    relevant_flags = [True] * len(tenders)
                elif tenders:
                    # Judge relevance against the CEO's actual original word
                    # (primary_keyword), not the AI-suggested synonym itself
                    # — a broad DeepSeek suggestion like "optical instrument"
                    # genuinely contains things like "Optical Table" or
                    # "Optical Fiber Cable", so checking relevance against
                    # the suggestion just rubber-stamps its own noise. What
                    # actually matters is whether the CEO who searched
                    # "binoculars" would consider a result relevant.
                    relevance_target = primary_keyword or keyword
                    titles = [t.get("title", "") for t in tenders]
                    relevant_flags = check_relevance_ai(relevance_target, titles)
                    print(f"[Relevance] Keyword '{keyword}' (vs '{relevance_target}'): {relevant_flags}")
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
    # Attach the saved row's own id to criteria before searching with it —
    # without this, the search below ran with a criteria dict that had no
    # id at all, so it could never record its own "last searched" time
    # (that stamp only gets written when an id is present). Every later
    # cycle (Start Agent, 2 AM) already fetches criteria from the database,
    # id included, so this only ever mattered for a filter's very first,
    # immediate search.
    saved_id = save_kppp_criteria(criteria)
    if saved_id is not None:
        criteria = {**criteria, "id": saved_id}
    log(f"👀 CEO set KPPP search filter: {criteria}")

    # KPPP has no free-text keyword box and no AI expansion step of its own,
    # so it never went through on_ceo_search/expand_keyword_instantly — the
    # only two places that feed the "Search History" and "Patterns Learned"
    # cards. Log it here directly (using whichever of tender_name/entity/
    # category is most specific) so those cards reflect KPPP searches too,
    # same as they already do for GeM's keyword box.
    search_label = (
        criteria.get("tender_name")
        or criteria.get("procuring_entity")
        or criteria.get("category")
        or "KPPP search"
    )
    save_ceo_search(search_label, "KPPP")
    save_learned_patterns(
        category=search_label,
        keywords=json.dumps([search_label]),
        weight=1.0,
        portal="KPPP"
    )

    threading.Thread(
        target=_run_kppp_search_then_check_relevant,
        args=(criteria,),
        daemon=True
    ).start()

def _run_kppp_search_then_check_relevant(criteria: dict):
    """Wraps _run_kppp_criteria_search for a manual KPPP search (on_kppp_search
    only — Start Agent/2AM already call run_relevant_keyword_matching
    themselves after their own KPPP pass, untouched here) so the CEO doesn't
    have to click Start Agent afterward just to see this search's own
    results checked against the saved Relevant Tenders keywords."""
    _run_kppp_criteria_search([criteria])
    run_relevant_keyword_matching(scope="incremental")

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
                # A repeat search of an already-saved filter (has a DB id
                # and has been searched before) pages only until it finds
                # tenders already covered by that last search (KPPP lists
                # newest-published first — see search_kppp's
                # stop_before_date) instead of walking every one of its
                # (often dozens of) pages again. A brand-new filter (no id
                # yet, e.g. straight from on_kppp_search) or one never
                # successfully searched before pages through everything,
                # same as today.
                criteria_id = criteria.get("id")
                last_searched = _parse_db_timestamp(criteria.get("last_searched_at"))
                is_repeat_search = criteria_id is not None and last_searched is not None
                log(
                    f"🔎 [KPPP] Searching with saved filter: {criteria}"
                    + (f" (tenders newer than {last_searched})" if is_repeat_search else " (full search)")
                )
                tenders = search_kppp_sync(criteria, stop_before_date=last_searched if is_repeat_search else None)
                if is_repeat_search:
                    # search_kppp's own pagination already stops once it
                    # reaches nothing newer, but the boundary page itself
                    # can still mix a few already-seen rows in with new
                    # ones — this client-side pass drops those (dedup in
                    # save_tender() would also catch them, but this keeps
                    # the "Skipped N" log accurate for troubleshooting).
                    before = len(tenders)
                    tenders = [
                        t for t in tenders
                        if (parsed := _parse_kppp_published_date(t.get("published_date")))
                        is None or parsed > last_searched
                    ]
                    skipped = before - len(tenders)
                    if skipped:
                        log(f"⏭️ [KPPP] Skipped {skipped} tender(s) already covered by the last search")
                for tender in tenders:
                    tender_id = save_tender(tender)
                    if tender_id:
                        found_count += 1
                        files = tender.get("downloaded_files", [])
                        if files:
                            update_downloaded_files(tender_id, files)
                        log(f"📄 [KPPP] Found: {tender['title'][:60]}...")
                if criteria_id is not None:
                    update_kppp_last_searched(criteria_id)
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

        moved = mark_expired_tenders()
        if moved:
            log(f"🕒 Moved {moved} tender(s) to Expired (past their deadline)")

        log("⏰ Daily 2 AM scheduled search starting...")
        with _lock:
            _state["search_in_progress"] = True

        try:
            if searches:
                with _lock:
                    _state["status"] = "searching"

                # Exactly the CEO's own raw typed keywords, one GeM search
                # per keyword, nothing AI-expanded added on top — a search
                # for "X" here must return exactly what GeM's own "Contains"
                # search returns for "X" (see _instant_expand_and_search,
                # the search-bar equivalent of this, which works the same
                # way). No analyze_patterns()/get_search_queries() call here
                # any more — those generated extra AI-guessed synonym
                # keywords that aren't on GeM's own site for this term, and
                # slowed the run down searching them.
                gem_keywords = sorted({
                    s["keyword"] for s in searches if s["portal"] == "GeM"
                })
                log(f"🚀 Running {len(gem_keywords)} scheduled GeM searches...")
                for kw in gem_keywords:
                    if not _state["running"]:
                        log("🛑 [GeM] Stop requested — ending search early")
                        break
                    # primary_keywords (not the singular primary_keyword) —
                    # that's what keeps this on the existing "full pagination
                    # only on this keyword's very first-ever batch pass, then
                    # incremental (stop_before_date) afterwards" behavior;
                    # the singular form here would instead make every run,
                    # every night, re-walk every GeM page for every keyword.
                    _search_portal("GeM", [kw], kw.title(), 15, respect_stop_flag=True, primary_keywords={kw})
                # Reset before the KPPP step below — _run_kppp_criteria_search
                # refuses to run while status is "searching"/"expanding" (that
                # guard exists to stop a manual KPPP search from colliding with
                # one already running), but left at "searching" from the GeM
                # step just above, it was tripping over its own leftover
                # status and skipping the scheduled KPPP search entirely every
                # night.
                with _lock:
                    _state["status"] = "watching"

            if kppp_criteria_list:
                log(f"🚀 Running {len(kppp_criteria_list)} scheduled KPPP searches...")
                _run_kppp_criteria_search(kppp_criteria_list, respect_stop_flag=True)

                from portals.kppp import backfill_kppp_nit_ids_sync
                n_backfilled = backfill_kppp_nit_ids_sync()
                if n_backfilled:
                    log(f"🔧 [KPPP] Backfilled nit_id for {n_backfilled} older tender(s)")

            # Nightly-only: download + analyze every new tender (not just
            # Relevant Tenders matches), capped at 100 — see
            # analyze_new_tenders_batch's docstring. Runs BEFORE the
            # Relevant Tenders check below on purpose: that check still
            # independently guarantees any relevant match gets analyzed
            # even if it fell outside this cap, so the cap only affects
            # the "everything else" tenders, never a relevant one.
            analyze_new_tenders_batch(cap=100)

            log("🎯 Checking Relevant Tenders keywords against today's new tenders...")
            run_relevant_keyword_matching(scope="incremental")
        finally:
            # In a `finally` for the same reason as _run_full_catalog_search
            # — this flag must never get stuck True, or the dashboard would
            # auto-refresh forever.
            with _lock:
                _state["status"] = "watching"
                _state["last_search"] = datetime.now().isoformat()
                _state["last_full_search_date"] = datetime.now().date().isoformat()
                _state["last_search_completed_at"] = datetime.now().isoformat()
                _state["tenders_found_today"] = 0
                _state["search_in_progress"] = False

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
        with _lock:
            _state["search_in_progress"] = False
        return

    log("🚀 Running full catalog search...")
    with _lock:
        _state["search_in_progress"] = True

    try:
        if searches:
            # Same as _scheduled_search — exactly the CEO's own raw typed
            # keywords, one GeM search per keyword, no AI-expanded synonyms.
            gem_keywords = sorted({
                s["keyword"] for s in searches if s["portal"] == "GeM"
            })
            for kw in gem_keywords:
                if not _state["running"]:
                    log("🛑 [GeM] Stop requested — ending search early")
                    break
                _search_portal("GeM", [kw], kw.title(), 15, respect_stop_flag=True, primary_keywords={kw})

        if kppp_criteria_list:
            _run_kppp_criteria_search(kppp_criteria_list, respect_stop_flag=True)

            # One-off (per tender) backfill for older KPPP tenders saved
            # before nit_id existed, so their documents can be downloaded
            # too — see backfill_kppp_nit_ids_sync's docstring. Cheap once
            # the backlog's cleared: it's just a quick "any missing?" check,
            # no browser launched at all when there's nothing left to do.
            from portals.kppp import backfill_kppp_nit_ids_sync
            n_backfilled = backfill_kppp_nit_ids_sync()
            if n_backfilled:
                log(f"🔧 [KPPP] Backfilled nit_id for {n_backfilled} older tender(s)")

        log("🎯 Checking Relevant Tenders keywords against today's new tenders...")
        run_relevant_keyword_matching(scope="incremental")
    finally:
        # In a `finally` so a crash anywhere above can't leave this stuck
        # True forever — the dashboard auto-refreshes the page for as long
        # as this stays True, so a stuck flag would mean it never stops.
        with _lock:
            _state["last_full_search_date"] = datetime.now().date().isoformat()
            _state["last_search_completed_at"] = datetime.now().isoformat()
            _state["search_in_progress"] = False
    log("✅ Full catalog search complete — check New Tenders!")

def analyze_new_tenders_batch(cap: int = 100):
    """Nightly-2-AM-only step: downloads the document and runs the full AI
    analysis for every new tender found that cycle, not just the ones that
    turn out to match a Relevant Tenders keyword (which already get this
    via run_relevant_keyword_matching, independently of this cap) — capped
    at `cap` tenders so one unusually large batch can't make the whole
    nightly run take hours. GeM/KPPP search logic and Relevant Tenders
    matching are both completely unaffected by this; it only adds the
    PDF+analysis step on top, for tenders that wouldn't otherwise have
    gotten one. Returns how many were actually analyzed."""
    from database.db import get_new_tenders_needing_analysis, update_tender_pdfs
    from analysis.analyzer import analyze_tender

    tenders = get_new_tenders_needing_analysis(limit=cap)
    if not tenders:
        return 0

    log(f"📄 Downloading + analyzing {len(tenders)} new tender(s) (capped at {cap})...")
    analyzed = 0
    for t in tenders:
        try:
            path = analyze_tender(t)
            if path:
                update_tender_pdfs(t["id"], analysis_path=path)
                analyzed += 1
        except Exception as e:
            log(f"⚠️ [New Tenders] Analysis failed for tender {t['id']}: {e}")
    log(f"📄 Finished analyzing {analyzed} new tender(s)")
    return analyzed

def run_relevant_keyword_matching(scope: str = "incremental", keyword: str = None):
    """
    Step 3 of every agent run (after expired-check + new-tender-fetch):
    fuzzy-matches saved "Relevant Tenders" keywords against tenders already
    sitting in the database (L1 data), flags matches, and auto-runs full
    L2 analysis (download + AI extraction + analysis PDF) for any match
    that doesn't already have one.

    scope="full" (keyword required): one-time full scan of EVERY tender in
    the Total Tenders table against a single keyword — used right when
    that keyword is added or edited, so it also catches older tenders that
    were already in the database before the keyword existed.

    scope="incremental" (default): scans only today's New Tenders (the
    batch this run just fetched) against ALL saved relevant keywords —
    used on every Start Agent click / 2 AM run after that. Fast, since
    older tenders were already checked once when each keyword was added.
    """
    from database.db import (
        get_relevant_keywords_list, get_all_tenders,
        get_unprocessed_new_tenders, mark_tender_relevant,
        update_tender_pdfs,
    )
    from rapidfuzz import fuzz
    from analysis.analyzer import analyze_tender

    if scope == "full":
        if not keyword:
            return 0
        keywords = [keyword]
        tenders = get_all_tenders()
    else:
        keywords = [k["keyword"] for k in get_relevant_keywords_list()]
        if not keywords:
            return 0
        # NOT get_all_tenders(status_filter="new", today_only=True) — that
        # only counts tenders found on today's calendar date, so a search
        # that crosses midnight silently skips anything found just before
        # it (see get_unprocessed_new_tenders's docstring). Checking by
        # status alone instead catches every still-unprocessed tender,
        # regardless of which side of midnight it landed on.
        tenders = get_unprocessed_new_tenders()

    if not tenders:
        return 0

    # token_set_ratio splits on whitespace only, so a hyphenated word like
    # "Body-Worn Camera" becomes one token ("body-worn") that shares no
    # words at all with the saved keyword "body worn camera" — tanking the
    # score to ~20 even though it's obviously the same thing. Normalizing
    # punctuation (hyphens, slashes, commas...) to spaces first fixes that
    # without changing anything else about the matching.
    import re
    def _normalize(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    matched_count = 0
    for t in tenders:
        title = (t.get("title") or "").lower()
        tags = " ".join(t.get("item_tags") or []) if t.get("item_tags") else ""
        text = _normalize(f"{title} {tags}".strip())
        if not text:
            continue
        for kw in keywords:
            # token_set_ratio compares whole words (ignoring order/repeats),
            # not raw characters — avoids partial_ratio's false positives
            # (e.g. "PTZ Camera with Monitor" character-matching unrelated
            # "Camera Monitoring System" just because the letters overlap).
            score = fuzz.token_set_ratio(_normalize(kw), text)
            if score < 70:
                continue
            mark_tender_relevant(t["id"], kw)
            matched_count += 1
            if not t.get("analysis_pdf_path"):
                try:
                    log(f"🎯 [Relevant] Running full analysis for: {t.get('title', '')[:60]}...")
                    path = analyze_tender(t)
                    if path:
                        update_tender_pdfs(t["id"], analysis_path=path)
                except Exception as e:
                    log(f"⚠️ [Relevant] Analysis failed for tender {t['id']}: {e}")
            break  # already flagged relevant, no need to check remaining keywords

    if matched_count:
        log(f"🎯 [Relevant] {matched_count} tender(s) matched relevant-keyword filters")
    return matched_count


def add_relevant_keyword_and_scan(keyword: str):
    """Adds a new Relevant Tenders keyword and kicks off its one-time full
    scan of the Total Tenders table in the background (so the Admin form
    doesn't hang waiting for L2 analysis on every match)."""
    from database.db import add_relevant_keyword
    kw_id = add_relevant_keyword(keyword)
    if kw_id:
        threading.Thread(
            target=run_relevant_keyword_matching,
            kwargs={"scope": "full", "keyword": keyword},
            daemon=True
        ).start()
    return kw_id


def update_relevant_keyword_and_rescan(kw_id: int, keyword: str):
    """Updates a Relevant Tenders keyword's text and re-runs its full scan
    in the background, same as adding a new one."""
    from database.db import update_relevant_keyword
    update_relevant_keyword(kw_id, keyword)
    threading.Thread(
        target=run_relevant_keyword_matching,
        kwargs={"scope": "full", "keyword": keyword},
        daemon=True
    ).start()


_scheduler_thread = None

def start_agent():
    global _scheduler_thread

    with _lock:
        _state["running"] = True
        _state["status"] = "watching"
        # Set synchronously here (not just inside _run_full_catalog_search,
        # which only flips it after its own 3s startup delay) so the very
        # first rerun right after the Start Agent click already carries
        # search_in_progress=True — otherwise the dashboard's auto-refresh
        # meta tag wouldn't be in that first render, and with nothing else
        # to trigger a later rerun, the page would never refresh itself.
        _state["search_in_progress"] = True

    log("🤖 Tender Agent started")

    moved = mark_expired_tenders()
    if moved:
        log(f"🕒 Moved {moved} tender(s) to Expired (past their deadline)")

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