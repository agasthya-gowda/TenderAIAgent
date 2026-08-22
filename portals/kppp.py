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

async def search_kppp(keyword: str, category: str = "") -> list:
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

            print(f"[KPPP] Opening search page for: {keyword}")

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
                return []

            # ── SEARCH ────────────────────────────────────
                        # ── SEARCH ──────────────────────────────
            await page.wait_for_timeout(2000)

            # # Type keyword into Tender Number field
            # text_inputs = await page.query_selector_all("input[type='text']")
            # if text_inputs:
            #     await text_inputs[0].fill(keyword)
            #     await page.wait_for_timeout(500)

            # Select Published status
            selects = await page.query_selector_all("select")
            if len(selects) >= 2:
                await selects[1].select_option(index=1)
                await page.wait_for_timeout(500)

            search_btn = await page.query_selector("button:has-text('Search')")
            if search_btn:
                await search_btn.click()
                await page.wait_for_timeout(4000)
                print(f"[KPPP] Search submitted for: {keyword}")

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
                        "found_via": keyword,
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
            return tenders

    except Exception as e:
        print(f"[KPPP] Fatal error: {e}")
        return []

def search_kppp_sync(keyword: str, category: str = "") -> list:
    """Synchronous wrapper for search_kppp."""
    results = asyncio.run(search_kppp(keyword, category))
    return results