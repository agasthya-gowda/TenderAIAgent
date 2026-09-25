"""
KPPP Portal Searcher
Searches Karnataka Public Procurement Portal
kppp.karnataka.gov.in for tenders.
Handles CAPTCHA automatically using Claude vision.
Extracts all real details from results table.
Downloads all tender documents as zip.
"""

import asyncio
import os
import json
import zipfile
from dotenv import load_dotenv
from captcha.solver import solve_captcha

load_dotenv()

DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "downloads")


def _parse_kppp_datetime(text: str):
    """Parses KPPP's results-table 'NIT Published' column (confirmed live
    as DD-MM-YYYY HH:MM:SS, e.g. '18-09-2026 19:10:33') into a datetime —
    None (never raises) if it doesn't match, so a date-limited repeat
    search can safely fall back to not skipping a row it can't parse."""
    from datetime import datetime
    if not text or not text.strip():
        return None
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y"):
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    return None


async def _get_visible_results_table(page):
    """KPPP keeps Goods/Works/Services as 3 separate <table> elements in
    the DOM at once (only the active tab's is actually on screen) — a
    plain 'table tbody tr' selector matches rows from all 3, so every
    caller needs the one that's actually visible, not just the first
    match. Returns None if none are visible yet."""
    tables = await page.query_selector_all("table")
    for t in tables:
        if await t.is_visible():
            return t
    return None

async def _search_kppp_once(criteria: dict, max_pages=None, stop_before_date=None) -> tuple:
    """
    Run one full attempt at searching KPPP using its real search form
    fields. Returns (tenders, filter_verified) — filter_verified is False
    when a procuring entity or tender-name filter was requested but didn't
    actually stick (KPPP's Angular app can silently reset/reload either
    field, the entity dropdown right after the category changes and the
    tender-name box for reasons not fully pinned down), in which case
    `tenders` is the unfiltered/wrong-filter list and must NOT be trusted
    by the caller.

    max_pages: None means "page through every result KPPP has" (its own
    ngx-pagination widget, confirmed live to run into the dozens of pages
    for a broad category), same unlimited-by-default meaning as GeM's
    search_gem. A number caps it.

    stop_before_date: optional datetime — once a full page comes back with
    not a single row's "NIT Published" date newer than this, pagination
    stops after that page (KPPP lists newest-published first, confirmed
    live — page 2 continued in descending order from where page 1 left
    off). Used for a filter's repeat searches; a first-ever search leaves
    this None and pages through everything.

    KPPP's own "Search Tenders" form takes Category, Procuring Entity,
    Tender Number, a tender-closure date range, a Regular/Prequalification/
    Reserved choice (see portals/data/kppp_options.py for the real option
    values, scraped from the live form), and — under its "Advanced Search"
    toggle, in the "Other Details" block — a free-text Tender Name field
    (KPPP's own keyword search). `criteria` carries whichever of these the
    CEO set: {category, procuring_entity, tender_number, from_date, to_date,
    tender_type, tender_name} (all optional/blank-able except category,
    which KPPP requires).
    """
    category = (criteria or {}).get("category", "")
    procuring_entity = (criteria or {}).get("procuring_entity", "")
    tender_number = (criteria or {}).get("tender_number", "")
    from_date = (criteria or {}).get("from_date", "")
    to_date = (criteria or {}).get("to_date", "")
    tender_type = (criteria or {}).get("tender_type", "")
    tender_name_filter = (criteria or {}).get("tender_name", "")
    found_via_label = "KPPP: " + ", ".join(
        f"{k}={v}" for k, v in (criteria or {}).items() if v
    ) if criteria else "KPPP search"
    try:
        from playwright.async_api import async_playwright

        tenders = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage"
                ]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 768},
                accept_downloads=True
            )
            page = await context.new_page()

            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)

            print(f"[KPPP] Opening search page with filter: {criteria}")

            await page.goto(
                "https://kppp.karnataka.gov.in/#/portal/searchTender/live",
                timeout=60000,
                wait_until="domcontentloaded"
            )
            await page.wait_for_timeout(3000)

            # ── SOLVE CAPTCHA ─────────────────────────────
            captcha_solved = False

            # 6 attempts, not 3 — now that each retry actually gets a fresh
            # CAPTCHA image (see the reload click above) instead of
            # re-guessing the same one, more attempts meaningfully raise
            # the odds of the local OCR fallback landing on a readable one.
            for attempt in range(6):
                try:
                    print(f"[KPPP] CAPTCHA attempt {attempt + 1}...")

                    if attempt > 0:
                        # A wrong guess must get a genuinely new CAPTCHA
                        # before retrying — confirmed live that simply
                        # re-screenshotting the canvas after a failed
                        # attempt captures the exact same (still-wrong)
                        # image every time, since KPPP doesn't auto-refresh
                        # it on failure. The page has its own reload control
                        # for this (the "↻" icon next to the CAPTCHA,
                        # a.cpt-btn.reload) — without clicking it, all 3
                        # attempts were really just 3 guesses at one image.
                        reload_btn = await page.query_selector("a.cpt-btn.reload")
                        if reload_btn:
                            await reload_btn.click()
                            await page.wait_for_timeout(1000)
                        else:
                            print("[KPPP] CAPTCHA reload button not found — retrying against the same image")

                    await page.wait_for_selector("canvas", timeout=5000)
                    captcha_container = await page.query_selector("canvas")

                    if not captcha_container:
                        print("[KPPP] No CAPTCHA canvas found")
                        break

                    img_bytes = await captcha_container.screenshot()
                    solution = solve_captcha(img_bytes)
                    print(f"[KPPP] CAPTCHA solution: {solution}")

                    if not solution:
                        continue

                    captcha_input = await page.query_selector("input[type='text']")
                    if captcha_input:
                        await captcha_input.fill("")
                        await captcha_input.type(solution, delay=100)
                        await page.wait_for_timeout(500)

                    check_btn = await page.query_selector(
                        "input[type='button'], input[value='Check']"
                    )
                    if check_btn:
                        await check_btn.click()
                        await page.wait_for_timeout(2000)

                    page_text = await page.inner_text("body")
                    if "Please Enter correct Captcha" in page_text:
                        print(f"[KPPP] Wrong CAPTCHA, retrying...")
                    else:
                        print(f"[KPPP] CAPTCHA solved!")
                        captcha_solved = True
                        break

                except Exception as e:
                    print(f"[KPPP] CAPTCHA error: {e}")

            if not captcha_solved:
                print("[KPPP] CAPTCHA failed after 6 attempts")
                await browser.close()
                return [], False

            # ── SEARCH ────────────────────────────────────
            # KPPP's Angular app calls its own real JSON API for every
            # search and "Next" click — confirmed live via the browser's
            # network tab: POST .../search-eproc-tenders?page=N&size=20
            # with a small JSON body, returning a plain array of tender
            # records. Calling that endpoint directly (via the same
            # browser context's cookies, right after solving the CAPTCHA
            # above) replaces the old approach of clicking through form
            # fields and an ngx-pagination widget — removing entirely the
            # two most fragile parts of this scraper: the procuring-entity
            # dropdown and Tender Name field "silently didn't stick" retry
            # loops, and a "Next" click landing on a stale/detached
            # element. Procuring Entity isn't a field the API accepts
            # (confirmed: selecting it live did not change the request
            # body at all — KPPP's own frontend filters by entity
            # client-side against the same unfiltered results), so it's
            # applied the same way here, after fetching.
            filter_verified = True

            api_url = (
                "https://kppp.karnataka.gov.in/supplier-registration-service"
                "/v1/api/portal-service/search-eproc-tenders"
            )

            # "Regular Tenders" is the form's default and confirmed to map
            # to "OPEN"; the other two aren't currently used by any saved
            # filter in this app, so they're a best-effort guess following
            # the same naming convention as the confirmed enum values
            # (GOODS/PUBLISHED/OPEN) — flag for re-verification if either
            # is ever actually selected and returns unexpected results.
            tender_type_map = {
                "Regular Tenders": "OPEN",
                "Prequalification Tenders": "PREQUALIFICATION",
                "Reserved Tenders": "RESERVED",
            }
            api_tender_type = tender_type_map.get(tender_type, "OPEN")

            def _build_kppp_body() -> str:
                return json.dumps({
                    "tenderNumber": tender_number or "",
                    "category": category.upper() if category else "",
                    "status": "PUBLISHED",
                    "publishedFromDate": from_date or None,
                    "publishedToDate": to_date or None,
                    "tenderType": api_tender_type,
                    "title": tender_name_filter or "",
                    "location": None,
                    "tenderClosureFromDate": None,
                    "tenderClosureToDate": None,
                })

            # "CODE - Full Entity Name" is how the dropdown presents each
            # option (see portals/data/kppp_options.py) — match against the
            # name half, since that's what the API's own deptName field
            # returns for each tender.
            entity_name_filter = None
            if procuring_entity:
                entity_name_filter = (
                    procuring_entity.split(" - ", 1)[1]
                    if " - " in procuring_entity else procuring_entity
                ).strip().lower()

            print(f"[KPPP] Searching via API with filter: {criteria}")

            # ── EXTRACT RESULTS ───────────────────────────
            HARD_PAGE_CEILING = 500
            page_num = 0
            page_size = 20

            while max_pages is None or page_num < max_pages:
                if page_num >= HARD_PAGE_CEILING:
                    print(f"[KPPP] Hit hard safety ceiling of {HARD_PAGE_CEILING} pages, stopping")
                    break

                docs = None
                for attempt in range(1, 4):
                    try:
                        resp = await context.request.post(
                            f"{api_url}?page={page_num}&size={page_size}&order-by-tender-publish=true",
                            headers={"Content-Type": "application/json"},
                            data=_build_kppp_body(),
                        )
                        if resp.status != 200:
                            print(f"[KPPP] Page {page_num + 1} API returned status {resp.status} (attempt {attempt}/3)")
                            await page.wait_for_timeout(3000)
                            continue
                        docs = await resp.json()
                        break
                    except Exception as e:
                        print(f"[KPPP] Page {page_num + 1} API call failed (attempt {attempt}/3): {e}")
                        docs = None
                        await page.wait_for_timeout(3000)

                if docs is None:
                    print(f"[KPPP] Giving up on page {page_num + 1} after 3 failed attempts")
                    break

                print(f"[KPPP] Page {page_num + 1}: found {len(docs)} results")

                if not docs:
                    break

                page_has_newer = stop_before_date is None
                for doc in docs:
                    try:
                        tender_number_val = doc.get("tenderNumber") or ""
                        if not tender_number_val:
                            continue

                        dept_name = doc.get("deptName") or ""
                        published_date = doc.get("publishedDate") or "Not available"
                        deadline = doc.get("tenderClosureDate") or "Not available"

                        if stop_before_date is not None:
                            parsed = _parse_kppp_datetime(published_date)
                            if parsed is None or parsed > stop_before_date:
                                page_has_newer = True

                        if entity_name_filter and entity_name_filter not in dept_name.strip().lower():
                            continue

                        # Skip if already in database
                        from database.db import get_conn
                        conn = get_conn()
                        existing = conn.execute(
                            "SELECT id FROM tenders WHERE tender_number=?", (tender_number_val,)
                        ).fetchone()
                        conn.close()
                        if existing:
                            print(f"[KPPP] Skipping {tender_number_val} — already in database")
                            continue

                        ecv = doc.get("ecv")
                        estimated_value = str(ecv) if ecv is not None else "Not available"

                        tenders.append({
                            "title": doc.get("title") or "Not available",
                            "portal": "KPPP",
                            "url": "https://kppp.karnataka.gov.in/#/portal/searchTender/live",
                            "category": category,
                            "found_via": found_via_label,
                            "tender_number": tender_number_val,
                            "department": dept_name or "Not available",
                            "location": doc.get("locationName") or "Not available",
                            "tender_type": doc.get("invitingStrategyText") or "Not available",
                            "value": estimated_value,
                            "deadline": deadline,
                            "published_date": published_date,
                            "emd": "See tender document",
                            "description": f"KPPP tender by {dept_name}",
                            "downloaded_files": [],
                            "relevance_score": 0.75
                        })

                        try:
                            print(f"[KPPP] Extracted: {tender_number_val} - {(doc.get('title') or '')[:50]}...")
                        except Exception:
                            pass

                    except Exception as e:
                        try:
                            print(f"[KPPP] Doc extraction error: {e}")
                        except Exception:
                            pass
                        continue

                if not page_has_newer:
                    print(
                        f"[KPPP] Page {page_num + 1} had no tenders newer than "
                        f"{stop_before_date} — stopping (date-limited repeat search)"
                    )
                    break

                if len(docs) < page_size:
                    # A short page (fewer results than requested) is KPPP's
                    # own signal there's nothing more to page through.
                    break

                page_num += 1

            await browser.close()
            print(f"[KPPP] Done. Total: {len(tenders)} tenders across {page_num + 1} page(s)")
            return tenders, filter_verified

    except Exception as e:
        print(f"[KPPP] Fatal error: {e}")
        return [], False

async def search_kppp(criteria: dict, max_attempts: int = 7, max_pages=None, stop_before_date=None) -> list:
    """
    Search KPPP, retrying the whole attempt (fresh browser + CAPTCHA) when
    a requested procuring-entity filter didn't actually stick, instead of
    giving up and returning results from the wrong (unfiltered) search.
    Only returns tenders once the filter is confirmed applied — or once
    max_attempts is exhausted, in which case it returns nothing rather than
    ever return mismatched-department tenders under the requested filter.

    max_pages / stop_before_date: passed straight through to
    _search_kppp_once — see its docstring. Left None (unlimited, full
    search) for a first-ever search of a filter; a repeat search passes
    stop_before_date instead.
    """
    for attempt in range(1, max_attempts + 1):
        tenders, filter_verified = await _search_kppp_once(criteria, max_pages=max_pages, stop_before_date=stop_before_date)
        if filter_verified:
            return tenders
        print(f"[KPPP] Attempt {attempt}/{max_attempts} did not confirm the requested filter, retrying search...")
        if attempt < max_attempts:
            await asyncio.sleep(5)
    print(f"[KPPP] Giving up after {max_attempts} attempts — not returning unfiltered results")
    return []

def search_kppp_sync(criteria: dict, max_pages=None, stop_before_date=None) -> list:
    """Synchronous wrapper for search_kppp."""
    return asyncio.run(search_kppp(criteria, max_pages=max_pages, stop_before_date=stop_before_date))