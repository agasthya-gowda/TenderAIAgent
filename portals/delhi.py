"""
Delhi eProcurement Portal Searcher
Searches govtprocurement.delhi.gov.in (Govt of NCT of Delhi
eProcurement, the same NIC "GePNIC" platform Kerala's/Tamil Nadu's/
Maharashtra's/Goa's own portals run on) for tenders matching a keyword —
the exact same match as typing into the portal's own "Tender Search" box
(Search with ID/Title/Reference no) and hitting Go.

Confirmed live (not just assumed from "same platform"): identical
#SearchDescription/#Go search box ids, identical result-row shape
("[Title] [Ref.No][Tender ID]" in one combined cell), identical
component=$DirectLink_0 result-link pattern, identical "Download as zip
file" link on the detail page. This is portals/goa.py's own logic with
only the base URL, portal name and log-prefix changed — see that file's
docstring (or portals/kerala.py's, the original of this family) for the
full reasoning behind the Playwright/CAPTCHA/re-search-by-Tender-ID
design.
"""

import asyncio
import os
import re
import zipfile
from captcha.solver import solve_captcha

DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "downloads")
BASE = "https://govtprocurement.delhi.gov.in/nicgep/app"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_NAV_TIMEOUT = 60000

_ROW_CELL_RE = re.compile(r"^\[(.+)\]\s*\[([^\[\]]*)\]\[([^\[\]]*)\]$")


async def _search_delhi_once(keyword: str, max_results: int = 40) -> list:
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

            links = await page.query_selector_all('a[href*="component=%24DirectLink_0"]')
            print(f"[Delhi] Found {len(links)} result(s) for '{keyword}'")

            for link in links[:max_results]:
                try:
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
                        print(f"[Delhi] Row didn't match expected format, skipping: {combined_cell[:80]}")
                        continue
                    title, ref_no, tender_id = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
                    if not tender_id:
                        continue

                    from database.db import get_conn
                    conn = get_conn()
                    existing = conn.execute(
                        "SELECT id FROM tenders WHERE tender_number=? AND portal='Delhi'", (tender_id,)
                    ).fetchone()
                    conn.close()
                    if existing:
                        print(f"[Delhi] Skipping {tender_id} — already in database")
                        continue

                    tenders.append({
                        "title": title or "Not available",
                        "portal": "Delhi",
                        "url": f"{BASE}?page=FrontEndAdvancedSearchResult&service=direct",
                        "category": "Not available",
                        "found_via": f"Delhi eProcurement: {keyword}",
                        "tender_number": tender_id,
                        "department": org_chain or "Not available",
                        "location": "Not available",
                        "tender_type": ref_no or "Not available",
                        "value": "Not available",
                        "deadline": closing_date or "Not available",
                        "published_date": published_date or "Not available",
                        "emd": "See tender document",
                        "description": f"Delhi eProcurement tender by {org_chain or 'Not available'}",
                        "downloaded_files": [],
                        "relevance_score": 0.75,
                    })
                    print(f"[Delhi] Extracted: {tender_id} - {title[:50]}...")
                except Exception as e:
                    print(f"[Delhi] Row extraction error: {e}")
                    continue

            print(f"[Delhi] Done. Total: {len(tenders)} new tender(s) for '{keyword}'")
            return tenders
        finally:
            await browser.close()


def search_delhi(keyword: str, max_results: int = 40) -> list:
    """Searches Delhi eProcurement's own public tender search for
    `keyword` — the exact same free-text match the live portal's own
    "Tender Search" box returns.

    Always returns the keyword's full current match list (same reasoning
    as search_kerala/search_tamilnadu/search_maharashtra/search_goa) -
    save_tender()'s own dedup (by tender_number + portal) silently skips
    anything already saved."""
    try:
        return asyncio.run(_search_delhi_once(keyword, max_results=max_results))
    except Exception as e:
        print(f"[Delhi] Fatal error: {e}")
        return []


def search_delhi_sync(keyword: str, max_results: int = 40) -> list:
    """Named *_sync for parity with search_gem_sync/search_kppp_sync -
    search_delhi already runs its own event loop synchronously, so this
    is just a thin alias."""
    return search_delhi(keyword, max_results=max_results)


async def _download_delhi_docs_once(tender_number: str, tender_folder: str) -> list:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(accept_downloads=True, user_agent=_UA)
        page = await context.new_page()
        try:
            await page.goto(BASE, timeout=_NAV_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            await page.fill("#SearchDescription", tender_number)
            await page.click("#Go")
            await page.wait_for_timeout(2500)

            link = await page.query_selector('a[href*="component=%24DirectLink_0"]')
            if not link:
                print(f"[Delhi] Could not re-find tender {tender_number} by its own Tender ID — download skipped")
                return []
            await link.click()
            await page.wait_for_timeout(2000)

            zip_link = await page.query_selector('a:has-text("Download as zip file")')
            if not zip_link:
                print(f"[Delhi] No documents/zip link on {tender_number}'s detail page")
                return []
            await zip_link.click()
            await page.wait_for_timeout(1500)

            solved = False
            for attempt in range(6):
                print(f"[Delhi] CAPTCHA attempt {attempt + 1}...")
                if attempt > 0:
                    refresh = await page.query_selector('a:has-text("Refresh")')
                    if refresh:
                        await refresh.click()
                        await page.wait_for_timeout(1000)

                captcha_input = await page.query_selector('input[type="text"]')
                if not captcha_input:
                    print("[Delhi] No CAPTCHA input found")
                    break

                captcha_row = await captcha_input.evaluate_handle("el => el.closest('tr') || el.parentElement")
                captcha_el = captcha_row.as_element()
                img_bytes = await (captcha_el or captcha_input).screenshot()
                solution = solve_captcha(img_bytes)
                print(f"[Delhi] CAPTCHA solution: {solution}")
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
                    print("[Delhi] CAPTCHA solved, zip downloaded!")
                    solved = True

                    extracted = []
                    try:
                        with zipfile.ZipFile(zip_path) as z:
                            z.extractall(tender_folder)
                            extracted = [os.path.join(tender_folder, n) for n in z.namelist() if not n.endswith("/")]
                        os.remove(zip_path)
                    except Exception as e:
                        print(f"[Delhi] Zip extraction failed, keeping raw zip: {e}")
                        extracted = [zip_path]
                    return extracted
                except Exception:
                    page_text = await page.inner_text("body")
                    if "Enter Captcha" in page_text:
                        print("[Delhi] Wrong CAPTCHA, retrying...")
                        continue
                    print("[Delhi] Download step failed unexpectedly, stopping")
                    break

            if not solved:
                print(f"[Delhi] CAPTCHA failed after 6 attempts for {tender_number}")
            return []
        finally:
            await browser.close()


def download_delhi_docs(tender: dict) -> list:
    """
    Downloads this Delhi tender's real documents on demand (called from
    analysis/analyzer.py's analyze_tender, same "on demand at Get Full
    Analysis time" pattern as download_kerala_docs/
    download_tamilnadu_docs/download_maharashtra_docs/download_goa_docs/
    download_ap_docs/download_telangana_docs/download_kppp_docs).

    Same CAPTCHA-gated "Download as zip file" flow as Kerala's/Tamil
    Nadu's/Maharashtra's/Goa's own download functions — see
    download_kerala_docs's docstring for the full reasoning. Returns the
    list of local file paths extracted (empty list if the tender
    couldn't be re-found or the CAPTCHA couldn't be solved).
    """
    tender_number = tender.get("tender_number", "")
    if not tender_number:
        print("[Delhi] Download skipped — no tender_number on this tender")
        return []

    safe_name = "".join(c for c in tender_number if c.isalnum() or c in "-_")
    tender_folder = os.path.join(DOWNLOAD_PATH, safe_name)
    os.makedirs(tender_folder, exist_ok=True)

    try:
        return asyncio.run(_download_delhi_docs_once(tender_number, tender_folder))
    except Exception as e:
        print(f"[Delhi] Download error for {tender_number}: {e}")
        return []
