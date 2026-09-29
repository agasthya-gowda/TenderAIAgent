"""
GeM Portal Searcher
Searches Government e-Marketplace (gem.gov.in)
Flow:
1. Go to bidplus.gem.gov.in
2. Search keyword
3. Extract bid details from cards
4. Download PDF 1 for each bid
5. Read PDF 1 → find PDF 2 link
6. Download PDF 2 (full details)
7. Save everything to database
"""

import asyncio
import os
import re
import requests
from dotenv import load_dotenv
from captcha.solver import solve_captcha

load_dotenv()

DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "downloads")

def clean(text: str) -> str:
    """Clean extracted text."""
    if not text:
        return "Not available"
    return " ".join(text.strip().split())

_START_DATE_RE = re.compile(
    r"Start\s+Date[:\s]+(\d{2})-(\d{2})-(\d{4})\s+(\d{1,2}):(\d{2})\s*([AP]M)",
    re.IGNORECASE
)


def _extract_start_datetime(card_text: str):
    """Parses the "Start Date" GeM prints on a bid card (DD-MM-YYYY
    hh:mm AM/PM) into a datetime, or None if the card has no parseable
    Start Date — used to decide, page by page, whether a date-limited
    search has reached tenders already covered by an earlier run."""
    from datetime import datetime
    match = _START_DATE_RE.search(card_text)
    if not match:
        return None
    day, month, year, hour, minute, meridiem = match.groups()
    try:
        return datetime.strptime(
            f"{day}-{month}-{year} {hour}:{minute} {meridiem.upper()}",
            "%d-%m-%Y %I:%M %p"
        )
    except ValueError:
        return None

def download_file(url: str, save_path: str) -> bool:
    """Download a file from URL and save to path.

    The GeM "PDF 2" URLs are guessed patterns tried in sequence — some return
    HTTP 200 with an empty or non-PDF body (e.g. an error/login page) instead
    of 404ing. Without validating the body, a save always "succeeded" even
    for a 0-byte or HTML response, leaving a corrupt file on disk that PDF
    readers then refuse to open. Only accept a response that actually starts
    with the PDF magic bytes, so a bad guess correctly falls through to the
    next URL instead of masquerading as a successful download.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, headers=headers, timeout=30, stream=True)
        if response.status_code != 200:
            return False
        content = response.content
        if not content or not content.lstrip().startswith(b"%PDF"):
            print(f"[GeM] Rejected non-PDF response from {url}")
            return False
        with open(save_path, "wb") as f:
            f.write(content)
        return True
    except Exception as e:
        print(f"[GeM] Download error: {e}")
        return False

def find_supporting_documents_in_pdf1(pdf1_path: str) -> list:
    """
    Read PDF 1 and find links to the real supporting documents (technical
    specification, drawings, annexures, tender notice, etc.) that GeM
    attaches to the bid. These are embedded as hyperlink annotations on the
    PDF pages, always hosted on mkp.gem.gov.in — NOT on bidplus.gem.gov.in
    (there is no such endpoint; guessing bidplus.gem.gov.in URLs here was
    the previous approach and never worked, since that page doesn't exist).
    A bid can attach more than one document, so this returns all of them
    (deduplicated), not just one.
    """
    urls = []
    seen = set()
    try:
        import pdfplumber
        with pdfplumber.open(pdf1_path) as pdf:
            for page in pdf.pages:
                for annot in page.annots or []:
                    uri = annot.get("uri", "")
                    if uri and "mkp.gem.gov.in" in uri and uri.lower().endswith(".pdf") and uri not in seen:
                        seen.add(uri)
                        urls.append(uri)
    except Exception as e:
        print(f"[GeM] PDF 1 read error: {e}")
    if urls:
        print(f"[GeM] Found {len(urls)} supporting document(s) in PDF 1")
    return urls

async def search_gem(keyword: str, category: str = "", max_pages: int = 5, should_stop=None, stop_before_date=None) -> list:
    """
    Search GeM portal for tenders matching keyword.
    Extracts all details from bid cards.
    Downloads PDF 1 and PDF 2 for each bid.

    should_stop: optional zero-arg callable checked between pages and
    between cards — when it returns True, the search stops early and
    returns whatever tenders were already extracted, instead of running
    the remaining pages/cards to completion regardless.

    stop_before_date: optional datetime — once a full page of results comes
    back with not a single card's Start Date newer than this, pagination
    stops after that page (GeM's own listing sorts newest Start Date
    first, so once a page has nothing newer, every following page would
    too). Used for a keyword's repeat searches — the first-ever search for
    a keyword always leaves this None and pages through everything.
    """
    try:
        from playwright.async_api import async_playwright
        from datetime import datetime, timedelta
        import json as _json

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

            print(f"[GeM] Searching for: {keyword}")

            # Step 1 — Load the bid listing page once, purely to establish a
            # real browser session (cookies past GeM's WAF challenge) and to
            # read the CSRF token GeM embeds inline in this page's own
            # <script>. Every page of results below is then fetched
            # directly from GeM's own JSON search API instead of clicking
            # through the UI — confirmed live via the browser's network
            # tab: the "Next" button itself just POSTs {"page": N, ...} to
            # /all-bids-data and renders whatever JSON comes back. Driving
            # that endpoint directly removes an entire class of flakiness
            # this scraper used to hit (a slow-loading page rendering 0
            # cards, a "Next" link briefly missing from the DOM, a click
            # landing on a detached element) — a keyword with 79 real pages
            # now reliably returns all 79, not however many pages happened
            # to finish rendering in time.
            await page.goto(
                "https://bidplus.gem.gov.in/all-bids",
                timeout=60000,
                wait_until="networkidle"
            )
            await page.wait_for_timeout(3000)

            html = await page.content()
            csrf_match = re.search(r"csrf_bd_gem_nk['\"]?\s*:\s*['\"]([a-f0-9]+)['\"]", html)
            if not csrf_match:
                print("[GeM] Could not find the CSRF token on the bid listing page — aborting search")
                await browser.close()
                return []
            csrf_token = csrf_match.group(1)

            api_url = "https://bidplus.gem.gov.in/all-bids-data"
            api_headers = {
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://bidplus.gem.gov.in/all-bids",
            }

            def _build_payload(page_num: int) -> str:
                payload = {
                    "page": page_num,
                    "param": {"searchBid": keyword, "searchType": "fullText"},
                    "filter": {
                        "bidStatusType": "ongoing_bids",
                        "byType": "all",
                        "highBidValue": "",
                        "byEndDate": {"from": "", "to": ""},
                        # Newest Start Date first — matches the assumption
                        # stop_before_date makes below (that a page with
                        # nothing newer means every later page won't have
                        # anything new either). It was previously
                        # "Bid-End-Date-Oldest" (soonest-expiring first),
                        # which buried tenders with far-off deadlines deep
                        # in the pages regardless of how recently they were
                        # actually published — some weren't caught until
                        # the day they expired.
                        "sort": "Bid-Start-Date-Latest",
                    },
                }
                return "payload=" + _json.dumps(payload) + "&csrf_bd_gem_nk=" + csrf_token

            def _parse_gem_utc(value):
                # GeM's JSON API gives dates as ISO-8601 UTC
                # ("2026-09-17T18:09:00Z"); the rest of the app stores
                # dates as GeM's own display string (IST, "DD-MM-YYYY
                # hh:mm AM/PM") — convert so a tender found this way reads
                # identically to one found the old way.
                if not value:
                    return None
                try:
                    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ") + timedelta(hours=5, minutes=30)
                except ValueError:
                    return None

            # Step 2 — Page through GeM's real search API directly. A hard
            # ceiling still applies underneath so a page-detection bug can
            # never spin forever, but it's high enough to never be the
            # thing that actually cuts a real keyword short.
            all_docs = []
            HARD_PAGE_CEILING = 500
            page_num = 1
            num_found = None
            search_confirmed = False

            while max_pages is None or (page_num - 1) < max_pages:
                if page_num > HARD_PAGE_CEILING:
                    print(f"[GeM] Hit hard safety ceiling of {HARD_PAGE_CEILING} pages, stopping")
                    break

                body = _build_payload(page_num)

                data = None
                for attempt in range(1, 4):
                    try:
                        resp = await context.request.post(api_url, headers=api_headers, data=body)
                        if resp.status != 200:
                            print(f"[GeM] Page {page_num} API returned status {resp.status} (attempt {attempt}/3)")
                            await page.wait_for_timeout(3000)
                            continue
                        data = await resp.json()
                        if data.get("status") != 1:
                            print(f"[GeM] Page {page_num} API returned an error payload (attempt {attempt}/3): {data.get('message')}")
                            data = None
                            await page.wait_for_timeout(3000)
                            continue
                        break
                    except Exception as e:
                        print(f"[GeM] Page {page_num} API call failed (attempt {attempt}/3): {e}")
                        data = None
                        await page.wait_for_timeout(3000)

                if data is None:
                    print(f"[GeM] Giving up on page {page_num} after 3 failed attempts")
                    break

                response_block = (data.get("response") or {}).get("response") or {}
                num_found = response_block.get("numFound", num_found)
                docs = response_block.get("docs") or []

                if page_num == 1:
                    # GeM's own numFound for this exact search IS the
                    # confirmation the keyword matched something real — no
                    # more guessing relevance from a handful of visible
                    # cards on screen.
                    search_confirmed = True
                    print(f"[GeM] '{keyword}' matches {num_found} tender(s) on GeM")

                if not docs:
                    print(f"[GeM] Page {page_num}: 0 results — reached the end")
                    break

                all_docs.extend(docs)
                print(f"[GeM] Page {page_num}: found {len(docs)} results (total so far: {len(all_docs)}/{num_found})")

                if should_stop and should_stop():
                    print("[GeM] Stop requested — ending pagination early")
                    break

                # Date-limited repeat search: once a whole page has nothing
                # with a Start Date newer than the last time this keyword
                # was searched, stop instead of paging further — checked
                # directly against each doc's own structured start date now,
                # not a regex over scraped card text.
                if stop_before_date is not None:
                    page_has_newer = any(
                        (_parse_gem_utc((d.get("final_start_date_sort") or [None])[0]) or datetime.min) > stop_before_date
                        for d in docs
                    )
                    if not page_has_newer:
                        print(
                            f"[GeM] Page {page_num} had no tenders newer than "
                            f"{stop_before_date} — stopping (date-limited repeat search)"
                        )
                        break

                if num_found is not None and len(all_docs) >= num_found:
                    break

                page_num += 1

            if not search_confirmed:
                print(f"[GeM] Giving up — could not get a confirmed response for '{keyword}'")
                await browser.close()
                return []

            print(f"[GeM] Found {len(all_docs)} total results across pages")

            # Step 3 — Turn each JSON doc into a tender dict. Structured
            # fields straight from GeM's own API now — no more regex-parsing
            # scraped card text for title/department/dates/quantity, and
            # bd_category_name (when present) already carries the FULL item
            # list GeM only used to reveal on hover, same fix as before but
            # for free this time since the API gives it directly.
            for doc in all_docs:
                if should_stop and should_stop():
                    print(f"[GeM] Stop requested — ending extraction early ({len(tenders)} tenders so far)")
                    break
                try:
                    bid_numbers = doc.get("b_bid_number") or []
                    bid_no = bid_numbers[0] if bid_numbers else None
                    if not bid_no or "/B/" not in bid_no:
                        # A Reverse Auction (or malformed) entry with no
                        # real Bid number of its own — fall back to the
                        # parent Bid it's actually linked to.
                        parent_numbers = doc.get("b_bid_number_parent") or []
                        if parent_numbers:
                            bid_no = parent_numbers[0]
                    if not bid_no:
                        continue

                    title_field = doc.get("bd_category_name") or doc.get("b_category_name") or [""]
                    title = clean(title_field[0])[:300]

                    qty_list = doc.get("b_total_quantity") or []
                    quantity = str(qty_list[0]) if qty_list else "Not available"

                    min_name = (doc.get("ba_official_details_minName") or [""])[0]
                    dept_name = (doc.get("ba_official_details_deptName") or [""])[0]
                    department = clean(
                        f"{min_name}{dept_name}" if dept_name and dept_name != "NA" else min_name
                    )

                    start_dt = _parse_gem_utc((doc.get("final_start_date_sort") or [None])[0])
                    end_dt = _parse_gem_utc((doc.get("final_end_date_sort") or [None])[0])
                    start_date = start_dt.strftime("%d-%m-%Y %I:%M %p") if start_dt else "Not available"
                    end_date = end_dt.strftime("%d-%m-%Y %I:%M %p") if end_dt else "Not available"

                    # showbidDocument links use the parent Bid's own
                    # internal id when this doc is RA-linked, otherwise the
                    # doc's own id — matches the real link GeM renders for
                    # both cases.
                    parent_id_list = doc.get("b_id_parent") or []
                    doc_id = parent_id_list[0] if parent_id_list else (doc.get("id") or (doc.get("b_id") or [None])[0])
                    bid_url = f"https://bidplus.gem.gov.in/showbidDocument/{doc_id}" if doc_id else \
                        f"https://bidplus.gem.gov.in/all-bids?search_keyword={keyword}"

                    try:
                        print(f"[GeM] Found bid: {bid_no}")
                    except Exception:
                        pass

                    # Skip if already in database
                    from database.db import get_conn
                    conn = get_conn()
                    existing = conn.execute(
                        "SELECT id FROM tenders WHERE tender_number=?", (bid_no,)
                    ).fetchone()
                    conn.close()
                    if existing:
                        continue

                    tenders.append({
                        "title": title,
                        "portal": "GeM",
                        "url": bid_url,
                        "category": category,
                        "found_via": keyword,
                        "tender_number": bid_no,
                        "department": department,
                        "location": "Pan India",
                        "tender_type": "Open Bid",
                        "value": "See bid document",
                        "deadline": end_date,
                        "published_date": start_date,
                        "emd": "See bid document",
                        "quantity": quantity,
                        "description": f"GeM bid for {title[:100]}",
                        "downloaded_files": [],
                        "relevance_score": 0.75
                    })

                    # A print() encoding crash here (e.g. a stray non-ASCII
                    # character pulled from the tender's own title) must
                    # never cost the doc we just successfully extracted
                    # above — isolated in its own try so a logging failure
                    # can't fall through to the doc-loop's except and
                    # skip/lose an already-appended tender.
                    try:
                        print(f"[GeM] Extracted: {bid_no} - {title[:50]}...")
                    except Exception:
                        pass

                except Exception as e:
                    try:
                        print(f"[GeM] Doc extraction error: {e}")
                    except Exception:
                        pass
                    continue

            await browser.close()
            print(f"[GeM] Done. Total bids found: {len(tenders)}")
            return tenders

    except Exception as e:
        print(f"[GeM] Fatal error: {e}")
        return []

async def download_gem_documents(bid_no: str, bid_url: str = "") -> list:
    """
    Download PDF1 and PDF2 for a specific GeM bid number on demand.
    Called when CEO clicks 'Get Full Analysis'.

    If `bid_url` (the direct showbidDocument/<id> link captured during search)
    is available, navigate straight to it instead of re-searching the bid
    listing by bid number — the search box only matches item/keyword text,
    not the bid number, so re-searching was unreliable.
    """
    try:
        from playwright.async_api import async_playwright

        downloaded_files = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"]
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

            print(f"[GeM] Downloading documents for: {bid_no}")

            safe_name = bid_no.replace("/", "_")
            bid_folder = os.path.join(DOWNLOAD_PATH, safe_name)
            os.makedirs(bid_folder, exist_ok=True)

            pdf1_path = ""

            if bid_url and "showbidDocument" in bid_url:
                # Direct route: navigate to the stored document link and
                # capture whatever it downloads. Playwright's page.goto()
                # always raises "Download is starting" when the target URL
                # triggers a file download instead of loading a page — that's
                # expected here, not a failure, so it must not be treated as
                # one (it was previously always sending this path to the
                # slower/flakier search fallback below).
                try:
                    async with page.expect_download(timeout=20000) as dl1:
                        try:
                            await page.goto(bid_url, timeout=20000)
                        except Exception as nav_err:
                            if "Download is starting" not in str(nav_err):
                                raise
                    download1 = await dl1.value
                    pdf1_path = os.path.join(bid_folder, f"GeM-Bidding-{safe_name}.pdf")
                    await download1.save_as(pdf1_path)
                    downloaded_files.append(pdf1_path)
                    print(f"[GeM] PDF 1 downloaded via direct link: {pdf1_path}")
                except Exception as e:
                    print(f"[GeM] Direct link download error: {e}, falling back to search")
                    pdf1_path = ""

            if not pdf1_path:
                # Fallback: re-search the bid listing by bid number (legacy path).
                await page.goto("https://bidplus.gem.gov.in/all-bids", timeout=60000, wait_until="networkidle")
                await page.wait_for_timeout(3000)

                search_box = await page.query_selector("input[name='searchBid']")
                if search_box:
                    await search_box.click()
                    await search_box.fill("")
                    await search_box.fill(bid_no)
                    await search_box.press("Enter")
                    await page.wait_for_timeout(6000)

                # Verify the bid link actually appears before trying to click
                found_link = await page.query_selector(f"a:has-text('{bid_no}')")
                if not found_link:
                    print(f"[GeM] Bid {bid_no} not found in search results, retrying with longer wait...")
                    await page.wait_for_timeout(4000)

                try:
                    bid_no_link = await page.query_selector(f"a:has-text('{bid_no}')")
                    if bid_no_link:
                        async with page.expect_download(timeout=15000) as dl1:
                            await bid_no_link.click()
                        download1 = await dl1.value
                        pdf1_path = os.path.join(bid_folder, f"GeM-Bidding-{safe_name}.pdf")
                        await download1.save_as(pdf1_path)
                        downloaded_files.append(pdf1_path)
                        print(f"[GeM] PDF 1 downloaded: {pdf1_path}")
                except Exception as e:
                    print(f"[GeM] PDF 1 download error: {e}")

            if pdf1_path and os.path.exists(pdf1_path):
                from urllib.parse import urlparse, unquote

                doc_urls = find_supporting_documents_in_pdf1(pdf1_path)
                for i, doc_url in enumerate(doc_urls, start=1):
                    try:
                        raw_name = unquote(os.path.basename(urlparse(doc_url).path)) or f"document_{i}.pdf"
                        safe_doc_name = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name)[:150]
                        doc_path = os.path.join(bid_folder, f"GeM-Doc{i}-{safe_doc_name}")
                        if download_file(doc_url, doc_path):
                            downloaded_files.append(doc_path)
                            print(f"[GeM] Supporting document {i} downloaded: {doc_path}")
                    except Exception as e:
                        print(f"[GeM] Supporting document download error: {e}")

            await browser.close()
            return downloaded_files

    except Exception as e:
        print(f"[GeM] Document download error: {e}")
        return []


def download_gem_documents_sync(bid_no: str, bid_url: str = "") -> list:
    """Synchronous wrapper for download_gem_documents."""
    return asyncio.run(download_gem_documents(bid_no, bid_url))


def search_gem_sync(keyword: str, category: str = "", max_pages: int = 5, should_stop=None, stop_before_date=None) -> list:
    """Synchronous wrapper for search_gem."""
    return asyncio.run(search_gem(keyword, category, max_pages, should_stop=should_stop, stop_before_date=stop_before_date))