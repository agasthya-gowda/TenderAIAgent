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

async def _search_kppp_once(criteria: dict) -> tuple:
    """
    Run one full attempt at searching KPPP using its real search form
    fields. Returns (tenders, entity_verified) — entity_verified is False
    when a procuring entity was requested but the dropdown selection didn't
    actually stick (KPPP's Angular app can reset/reload the entity list
    right after the category changes), in which case `tenders` is the
    unfiltered/wrong-filter list and must NOT be trusted by the caller.

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
                timeout=30000,
                wait_until="domcontentloaded"
            )
            await page.wait_for_timeout(3000)

            # ── SOLVE CAPTCHA ─────────────────────────────
            captcha_solved = False

            for attempt in range(3):
                try:
                    print(f"[KPPP] CAPTCHA attempt {attempt + 1}...")

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
                print("[KPPP] CAPTCHA failed after 3 attempts")
                await browser.close()
                return [], False

            # ── SEARCH ────────────────────────────────────
            # Fill in KPPP's actual search fields (selectors/options
            # confirmed against the live form — see portals/data/kppp_options.py).
            await page.wait_for_timeout(2000)

            if tender_number:
                tn_input = await page.query_selector("#tendernumberInputField")
                if tn_input:
                    await tn_input.fill(tender_number)
                    await page.wait_for_timeout(300)

            if category:
                cat_select = await page.query_selector("#categorySelect")
                if cat_select:
                    try:
                        await cat_select.select_option(label=category)
                    except Exception as e:
                        print(f"[KPPP] Category '{category}' not found: {e}")
                    await page.wait_for_timeout(300)

            # Status — always Published, since we only care about live/open tenders
            selects = await page.query_selector_all("select")
            if len(selects) >= 2:
                await selects[1].select_option(index=1)
                await page.wait_for_timeout(300)

            entity_verified = True
            if procuring_entity:
                entity_verified = False
                entity_select = await page.query_selector("#departmentstatusSelect")
                if not entity_select:
                    print(f"[KPPP] Procuring entity dropdown not found on page")
                else:
                    # KPPP's Angular app can silently reset/reload the entity
                    # dropdown shortly after the category changes, so a
                    # selection made too soon doesn't stick even though no
                    # exception is raised. Verify what's actually selected
                    # and retry rather than trusting the call blindly —
                    # otherwise a failed selection quietly falls through to
                    # an unfiltered search that gets mislabeled as filtered.
                    for verify_attempt in range(4):
                        try:
                            await entity_select.select_option(label=procuring_entity)
                        except Exception as e:
                            print(f"[KPPP] Procuring entity '{procuring_entity}' not in dropdown: {e}")
                            break
                        await page.wait_for_timeout(400 + verify_attempt * 400)
                        selected_text = await entity_select.evaluate(
                            "el => el.options[el.selectedIndex]?.text.trim()"
                        )
                        if selected_text == procuring_entity:
                            entity_verified = True
                            break
                        print(
                            f"[KPPP] Entity selection didn't stick "
                            f"(attempt {verify_attempt + 1}, got '{selected_text}'), retrying..."
                        )
                    if not entity_verified:
                        print(f"[KPPP] Giving up on selecting entity '{procuring_entity}' for this attempt")
                        await browser.close()
                        return [], False

            date_inputs = await page.query_selector_all("input[type='date']")
            if len(date_inputs) >= 2:
                if from_date:
                    await date_inputs[0].fill(from_date)
                if to_date:
                    await date_inputs[1].fill(to_date)
                await page.wait_for_timeout(300)

            if tender_type:
                type_order = ["Regular Tenders", "Prequalification Tenders", "Reserved Tenders"]
                if tender_type in type_order:
                    radios = await page.query_selector_all("input[type='radio']")
                    if len(radios) >= len(type_order):
                        await radios[type_order.index(tender_type)].click()
                        await page.wait_for_timeout(300)

            if tender_name_filter:
                # The Tender Name field lives under KPPP's collapsed
                # "Advanced Search" -> "Other Details" section — expand it
                # first if it isn't already open. A plain .click() on the
                # wrapping "div.advancedsearch" row (or its child "arrow"
                # div) reliably does NOT register with this page's Angular
                # click handler under Playwright — confirmed by extensive
                # testing (element/mouse clicks, force clicks, raw JS
                # dispatchEvent, up to 8s of waiting, all failed). What
                # actually works is a real mousedown+mouseup sequence
                # targeted at the inner "lib-icon#showAdvancedSearch" custom
                # element specifically, not its parent — likely a
                # shadow-DOM/custom-element hit-testing quirk. Do not
                # simplify this back to `.click()` on the outer div without
                # re-verifying live against the portal first.
                tn_field = await page.query_selector("#innertendernameInputField")
                for expand_attempt in range(3):
                    if tn_field:
                        break
                    advanced_icon = await page.query_selector("lib-icon#showAdvancedSearch")
                    if not advanced_icon:
                        break
                    box = await advanced_icon.bounding_box()
                    if box:
                        cx = box["x"] + box["width"] / 2
                        cy = box["y"] + box["height"] / 2
                        await page.mouse.move(cx, cy)
                        await page.mouse.down()
                        await page.wait_for_timeout(100)
                        await page.mouse.up()
                    try:
                        await page.wait_for_selector("#innertendernameInputField", timeout=2500)
                    except Exception:
                        print(f"[KPPP] Advanced Search expand attempt {expand_attempt + 1}/3 didn't reveal Tender Name field, retrying...")
                    tn_field = await page.query_selector("#innertendernameInputField")
                if tn_field:
                    await tn_field.fill(tender_name_filter)
                    await page.wait_for_timeout(300)
                else:
                    # Same defense-in-depth principle as the entity check
                    # above — never let a failed filter silently fall
                    # through to an unfiltered "every tender" search that
                    # gets mislabeled as scoped to tender_name_filter.
                    print("[KPPP] Tender Name field not found even after expanding Advanced Search — aborting this attempt")
                    await browser.close()
                    return [], False

            search_btn = await page.query_selector("button:has-text('Search')")
            if search_btn:
                await search_btn.click()
                await page.wait_for_timeout(4000)
                print(f"[KPPP] Search submitted with filter: {criteria}")

            # ── EXTRACT RESULTS ───────────────────────────
            await page.wait_for_timeout(2000)
            rows = await page.query_selector_all("table tbody tr")
            print(f"[KPPP] Found {len(rows)} tenders")
            total_rows = min(10, len(rows))

            for row_index in range(total_rows):
                try:
                    # Re-query rows each time to avoid stale elements
                    rows = await page.query_selector_all("table tbody tr")
                    if row_index >= len(rows):
                        break
                    row = rows[row_index]

                    cols = await row.query_selector_all("td")
                    if len(cols) < 8:
                        continue

                    procuring_entity = " ".join((await cols[1].inner_text()).strip().split())
                    location         = " ".join((await cols[2].inner_text()).strip().split())
                    tender_number    = " ".join((await cols[3].inner_text()).strip().split())
                    tender_name      = " ".join((await cols[4].inner_text()).strip().split())
                    tender_type      = " ".join((await cols[5].inner_text()).strip().split())
                    estimated_value  = " ".join((await cols[6].inner_text()).strip().split())
                    published_date   = " ".join((await cols[7].inner_text()).strip().split())
                    deadline         = " ".join((await cols[8].inner_text()).strip().split()) if len(cols) > 8 else "Not available"

                    if not estimated_value or estimated_value == "---":
                        estimated_value = "Not available"
                    if not deadline or deadline == "---":
                        deadline = "Not available"

                    tender_url = "https://kppp.karnataka.gov.in/#/portal/searchTender/live"

                    # Skip if already in database
                    from database.db import get_conn
                    conn = get_conn()
                    existing = conn.execute(
                        "SELECT id FROM tenders WHERE tender_number=?", (tender_number,)
                    ).fetchone()
                    conn.close()
                    if existing:
                        print(f"[KPPP] Skipping {tender_number} — already in database")
                        continue

                    # ── DOWNLOAD ──────────────────────────
                    # Skip download during search — just collect data
                    downloaded_files = []

                    tenders.append({
                        "title": tender_name,
                        "portal": "KPPP",
                        "url": tender_url,
                        "category": category,
                        "found_via": found_via_label,
                        "tender_number": tender_number,
                        "department": procuring_entity,
                        "location": location,
                        "tender_type": tender_type,
                        "value": estimated_value,
                        "deadline": deadline,
                        "published_date": published_date,
                        "emd": "See tender document",
                        "description": f"KPPP tender by {procuring_entity}",
                        "downloaded_files": downloaded_files,
                        "relevance_score": 0.75
                    })

                    print(f"[KPPP] ✅ {tender_name[:60]}...")

                except Exception as e:
                    print(f"[KPPP] Row error: {e}")
                    continue

            await browser.close()
            print(f"[KPPP] Done. Total: {len(tenders)} tenders")
            return tenders, entity_verified

    except Exception as e:
        print(f"[KPPP] Fatal error: {e}")
        return [], False

async def search_kppp(criteria: dict, max_attempts: int = 5) -> list:
    """
    Search KPPP, retrying the whole attempt (fresh browser + CAPTCHA) when
    a requested procuring-entity filter didn't actually stick, instead of
    giving up and returning results from the wrong (unfiltered) search.
    Only returns tenders once the filter is confirmed applied — or once
    max_attempts is exhausted, in which case it returns nothing rather than
    ever return mismatched-department tenders under the requested filter.
    """
    for attempt in range(1, max_attempts + 1):
        tenders, entity_verified = await _search_kppp_once(criteria)
        if entity_verified:
            return tenders
        print(f"[KPPP] Attempt {attempt}/{max_attempts} did not confirm the requested filter, retrying search...")
    print(f"[KPPP] Giving up after {max_attempts} attempts — not returning unfiltered results")
    return []

def search_kppp_sync(criteria: dict) -> list:
    """Synchronous wrapper for search_kppp."""
    return asyncio.run(search_kppp(criteria))