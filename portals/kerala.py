"""
Kerala eTenders Portal Searcher
Searches etenders.kerala.gov.in (Govt of Kerala eProcurement, NIC's
"GePNIC" platform) for tenders matching a keyword — the exact same match
as typing into the portal's own "Tender Search" box (Search with ID/
Title/Reference no) and hitting Go.

Unlike AP/Telangana (plain DataTables JSON API, no browser needed), this
portal is a server-rendered, session-stateful Java/Tapestry app — every
page is real HTML and every link is tied to the current browser session,
so this needs a real browser (Playwright) the same way KPPP does, not
plain requests calls. The one real difference from KPPP: the search
itself needs no CAPTCHA at all (confirmed live — typing a keyword and
hitting Go returns real results with no gate in front of it); a CAPTCHA
only shows up on the "Download as zip file" step, so only
download_kerala_docs needs the CAPTCHA-solving loop, not the search.
"""

import asyncio
import os
import re
import zipfile
from captcha.solver import solve_captcha

DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "downloads")
BASE = "https://etenders.kerala.gov.in/nicgep/app"

# Confirmed live: Playwright's default page (whose user-agent announces
# itself as HeadlessChrome) regularly times out loading this site at all,
# while a normal desktop Chrome user-agent loads it reliably - this
# portal's own asset-heavy pages are also just genuinely slower than
# AP/Telangana/KPPP's, so the navigation timeout is longer too.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_NAV_TIMEOUT = 60000

# Each search-result row's one combined cell reads like
# "[Title text] [Reference/No][2026_PWD_847678_2]" - the last two bracket
# groups are always plain reference-number/tender-ID strings (no nested
# brackets), so anchoring on them from the end is reliable even though the
# title itself can contain almost anything.
_ROW_CELL_RE = re.compile(r"^\[(.+)\]\s*\[([^\[\]]*)\]\[([^\[\]]*)\]$")


async def _search_kerala_once(keyword: str, max_results: int = 40) -> list:
    from playwright.async_api import async_playwright

    tenders = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page(user_agent=_UA)
        try:
            await page.goto(BASE, timeout=_NAV_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            await page.fill("#SearchDescription", keyword)
            await page.click("#Go")
            await page.wait_for_timeout(2500)

            # Real tender-result links only - excludes the "Back" link,
            # which shares the same page but has no "_0" component suffix.
            links = await page.query_selector_all('a[href*="component=%24DirectLink_0"]')
            print(f"[Kerala] Found {len(links)} result(s) for '{keyword}'")

            for link in links[:max_results]:
                try:
                    href = await link.get_attribute("href")
                    row = await link.evaluate_handle("el => el.closest('tr')")
                    row_el = row.as_element()
                    if not row_el:
                        continue
                    cells = await row_el.query_selector_all("td")
                    if len(cells) < 6:
                        continue
                    cell_texts = [await c.inner_text() for c in cells]
                    published_date = cell_texts[1].strip()
                    closing_date = cell_texts[2].strip()
                    combined_cell = cell_texts[4].strip()
                    org_chain = cell_texts[5].strip()

                    m = _ROW_CELL_RE.match(combined_cell)
                    if not m:
                        print(f"[Kerala] Row didn't match expected format, skipping: {combined_cell[:80]}")
                        continue
                    title, ref_no, tender_id = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
                    if not tender_id:
                        continue

                    # Skip if already in database
                    from database.db import get_conn
                    conn = get_conn()
                    existing = conn.execute(
                        "SELECT id FROM tenders WHERE tender_number=? AND portal='Kerala'", (tender_id,)
                    ).fetchone()
                    conn.close()
                    if existing:
                        print(f"[Kerala] Skipping {tender_id} — already in database")
                        continue

                    tenders.append({
                        "title": title or "Not available",
                        "portal": "Kerala",
                        "url": f"{BASE}?page=FrontEndAdvancedSearchResult&service=direct",
                        "category": "Not available",
                        "found_via": f"Kerala eTenders: {keyword}",
                        "tender_number": tender_id,
                        "department": org_chain or "Not available",
                        "location": "Not available",
                        "tender_type": ref_no or "Not available",
                        "value": "Not available",
                        "deadline": closing_date or "Not available",
                        "published_date": published_date or "Not available",
                        "emd": "See tender document",
                        "description": f"Kerala eTenders tender by {org_chain or 'Not available'}",
                        "downloaded_files": [],
                        "relevance_score": 0.75,
                    })
                    print(f"[Kerala] Extracted: {tender_id} - {title[:50]}...")
                except Exception as e:
                    print(f"[Kerala] Row extraction error: {e}")
                    continue

            print(f"[Kerala] Done. Total: {len(tenders)} new tender(s) for '{keyword}'")
            return tenders
        finally:
            await browser.close()


def search_kerala(keyword: str, max_results: int = 40) -> list:
    """Searches Kerala eTenders' own public tender search for `keyword` —
    the exact same free-text match the live portal's own "Tender Search"
    box returns.

    Always returns the keyword's full current match list (same reasoning
    as search_ap/search_telangana: a specific keyword search returns a
    small result set, so there's no incremental "only new since last
    time" cutoff needed) - save_tender()'s own dedup (by tender_number +
    portal) silently skips anything already saved."""
    try:
        return asyncio.run(_search_kerala_once(keyword, max_results=max_results))
    except Exception as e:
        print(f"[Kerala] Fatal error: {e}")
        return []


def search_kerala_sync(keyword: str, max_results: int = 40) -> list:
    """Named *_sync for parity with search_gem_sync/search_kppp_sync -
    search_kerala already runs its own event loop synchronously, so this
    is just a thin alias."""
    return search_kerala(keyword, max_results=max_results)


async def _download_kerala_docs_once(tender_number: str, tender_folder: str) -> list:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(accept_downloads=True, user_agent=_UA)
        page = await context.new_page()
        try:
            await page.goto(BASE, timeout=_NAV_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # Re-search by this tender's own stable Tender ID (stored as
            # tender_number) rather than reusing any link/session pointer
            # captured at the original search time - those are tied to
            # that specific browser session and would already be stale by
            # the time "Get Full Analysis" is clicked later. Same idea as
            # KPPP's download_kppp_docs re-authenticating fresh each call
            # instead of trying to reuse an old session.
            await page.fill("#SearchDescription", tender_number)
            await page.click("#Go")
            await page.wait_for_timeout(2500)

            link = await page.query_selector('a[href*="component=%24DirectLink_0"]')
            if not link:
                print(f"[Kerala] Could not re-find tender {tender_number} by its own Tender ID — download skipped")
                return []
            await link.click()
            await page.wait_for_timeout(2000)

            zip_link = await page.query_selector('a:has-text("Download as zip file")')
            if not zip_link:
                print(f"[Kerala] No documents/zip link on {tender_number}'s detail page")
                return []
            await zip_link.click()
            await page.wait_for_timeout(1500)

            # Solve CAPTCHA (same reload-between-retries, 3-method hybrid
            # solver as KPPP's own CAPTCHA loop - see captcha/solver.py).
            solved = False
            for attempt in range(6):
                print(f"[Kerala] CAPTCHA attempt {attempt + 1}...")
                if attempt > 0:
                    refresh = await page.query_selector('a:has-text("Refresh")')
                    if refresh:
                        await refresh.click()
                        await page.wait_for_timeout(1000)

                captcha_input = await page.query_selector('input[type="text"]')
                if not captcha_input:
                    print("[Kerala] No CAPTCHA input found")
                    break

                # The CAPTCHA is a styled inline image/canvas right next to
                # "Enter Captcha" - screenshotting the input's own parent
                # row captures it reliably without needing its exact tag.
                captcha_row = await captcha_input.evaluate_handle("el => el.closest('tr') || el.parentElement")
                captcha_el = captcha_row.as_element()
                img_bytes = await (captcha_el or captcha_input).screenshot()
                solution = solve_captcha(img_bytes)
                print(f"[Kerala] CAPTCHA solution: {solution}")
                if not solution:
                    continue

                await captcha_input.fill("")
                await captcha_input.type(solution, delay=100)

                try:
                    async with page.expect_download(timeout=15000) as dl_info:
                        submit_btn = await page.query_selector('input[value="Submit"]')
                        if submit_btn:
                            await submit_btn.click()
                    download = await dl_info.value
                    zip_path = os.path.join(tender_folder, f"{tender_number}_documents.zip")
                    await download.save_as(zip_path)
                    print("[Kerala] CAPTCHA solved, zip downloaded!")
                    solved = True

                    extracted = []
                    try:
                        with zipfile.ZipFile(zip_path) as z:
                            z.extractall(tender_folder)
                            extracted = [os.path.join(tender_folder, n) for n in z.namelist() if not n.endswith("/")]
                        os.remove(zip_path)
                    except Exception as e:
                        print(f"[Kerala] Zip extraction failed, keeping raw zip: {e}")
                        extracted = [zip_path]
                    return extracted
                except Exception:
                    # No download fired - either a wrong CAPTCHA (page
                    # reloaded the same form) or a genuine error; either
                    # way, retry with a fresh CAPTCHA image.
                    page_text = await page.inner_text("body")
                    if "Enter Captcha" in page_text:
                        print("[Kerala] Wrong CAPTCHA, retrying...")
                        continue
                    print("[Kerala] Download step failed unexpectedly, stopping")
                    break

            if not solved:
                print(f"[Kerala] CAPTCHA failed after 6 attempts for {tender_number}")
            return []
        finally:
            await browser.close()


def download_kerala_docs(tender: dict) -> list:
    """
    Downloads this Kerala tender's real documents on demand (called from
    analysis/analyzer.py's analyze_tender, same "on demand at Get Full
    Analysis time" pattern as download_ap_docs/download_telangana_docs/
    download_kppp_docs).

    Confirmed live (clicking through the real site, not guessed): a
    tender's detail page has a "Download as zip file" link that's open to
    view without login, but gated by a CAPTCHA before the actual zip is
    served - solved the same way KPPP's own CAPTCHA is (6 attempts, a
    fresh image via the page's own Refresh link between wrong guesses,
    the same 3-method hybrid solver). The zip is extracted into this
    tender's own downloads folder and the raw zip removed, so
    analysis/analyzer.py's read_all_files (which only knows .pdf/.docx)
    can read the real files inside it directly.

    Returns the list of local file paths extracted (empty list if the
    tender couldn't be re-found or the CAPTCHA couldn't be solved) - same
    shape analyze_tender expects from the other portals' download
    functions.
    """
    tender_number = tender.get("tender_number", "")
    if not tender_number:
        print("[Kerala] Download skipped — no tender_number on this tender")
        return []

    safe_name = "".join(c for c in tender_number if c.isalnum() or c in "-_")
    tender_folder = os.path.join(DOWNLOAD_PATH, safe_name)
    os.makedirs(tender_folder, exist_ok=True)

    try:
        return asyncio.run(_download_kerala_docs_once(tender_number, tender_folder))
    except Exception as e:
        print(f"[Kerala] Download error for {tender_number}: {e}")
        return []
