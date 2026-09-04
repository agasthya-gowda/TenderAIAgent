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

async def search_gem(keyword: str, category: str = "", max_pages: int = 5, should_stop=None) -> list:
    """
    Search GeM portal for tenders matching keyword.
    Extracts all details from bid cards.
    Downloads PDF 1 and PDF 2 for each bid.

    should_stop: optional zero-arg callable checked between pages and
    between cards — when it returns True, the search stops early and
    returns whatever tenders were already extracted, instead of running
    the remaining pages/cards to completion regardless.
    """
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

            print(f"[GeM] Searching for: {keyword}")

            # Step 1 — Go to bid listing page
            await page.goto(
                "https://bidplus.gem.gov.in/all-bids",
                timeout=60000,
                wait_until="networkidle"
            )
            await page.wait_for_timeout(5000)

            # Step 2 — Find search box and type keyword
            # From screenshot: search box is top right
            # with "Contains" dropdown and search input
            await page.wait_for_selector(
                "input[name='searchBid']",
                timeout=15000
            )
            search_box = await page.query_selector("input[name='searchBid']")

            if search_box:
                await search_box.scroll_into_view_if_needed()
                await search_box.click()
                await search_box.fill("")
                await search_box.fill(keyword)
                await search_box.press("Enter")
                await page.wait_for_timeout(6000)
                print(f"[GeM] Search submitted for: {keyword}")
            else:
                print("[GeM] Search box not found")
                await browser.close()
                return []

            # Step 3 — Extract bid cards across multiple pages
            all_bid_cards = []
            # max_pages now comes from function parameter

            for page_num in range(max_pages):
                bid_cards = await page.query_selector_all("div.card")

                if not bid_cards:
                    page_text = await page.inner_text("body")
                    bid_cards = await page.query_selector_all("tr")

                all_bid_cards.extend(bid_cards)
                print(f"[GeM] Page {page_num + 1}: found {len(bid_cards)} cards (total so far: {len(all_bid_cards)})")

                if should_stop and should_stop():
                    print("[GeM] Stop requested — ending pagination early")
                    break

                # Try to go to next page
                next_btn = await page.query_selector("a:has-text('Next')")
                if next_btn and page_num < max_pages - 1:
                    try:
                        await next_btn.click()
                        await page.wait_for_timeout(3000)
                    except Exception:
                        break
                else:
                    break

            print(f"[GeM] Found {len(all_bid_cards)} total bid cards across pages")
            bid_cards = all_bid_cards

            for card in bid_cards:
                if should_stop and should_stop():
                    print(f"[GeM] Stop requested — ending card extraction early ({len(tenders)} tenders so far)")
                    break
                try:
                    card_text = await card.inner_text()

                    # Skip if too short
                    if len(card_text) < 20:
                        continue

                    # Extract BID NO using regex
                    bid_no_match = re.search(
                        r"GEM/\d{4}/[A-Z]/\d+",
                        card_text
                    )
                    if not bid_no_match:
                        continue

                    bid_no = bid_no_match.group()

                    # Extract Items
                    items = "Not available"
                    items_match = re.search(
                        r"Items?[:\s]+(.+?)(?:\n|Quantity|Department)",
                        card_text,
                        re.IGNORECASE | re.DOTALL
                    )
                    if items_match:
                        items = clean(items_match.group(1))[:200]
                    
                    # Try to get full title from the link element instead (often not truncated)
                    try:
                        item_link = await card.query_selector("a[href*='searchresult'], .item-title a, a.bid-item")
                        if item_link:
                            full_title = await item_link.get_attribute("title") or await item_link.inner_text()
                            if full_title and len(full_title.strip()) > len(items.replace("...", "").strip()):
                                items = clean(full_title)[:300]
                    except Exception:
                        pass

                    # Extract Quantity
                    quantity = "Not available"
                    qty_match = re.search(
                        r"Quantity[:\s]+(\d+)",
                        card_text,
                        re.IGNORECASE
                    )
                    if qty_match:
                        quantity = qty_match.group(1)

                    # Extract Department
                    department = "Not available"
                    dept_match = re.search(
                        r"Department\s+Name\s+And\s+Address[:\s]+(.+?)(?:\n|Start Date|End Date)",
                        card_text,
                        re.IGNORECASE | re.DOTALL
                    )
                    if dept_match:
                        department = clean(dept_match.group(1))[:200]

                    # Extract Start Date
                    start_date = "Not available"
                    start_match = re.search(
                        r"Start\s+Date[:\s]+(\d{2}-\d{2}-\d{4}\s+\d{1,2}:\d{2}\s*[AP]M)",
                        card_text,
                        re.IGNORECASE
                    )
                    if start_match:
                        start_date = start_match.group(1)

                    # Extract End Date (deadline)
                    end_date = "Not available"
                    end_match = re.search(
                        r"End\s+Date[:\s]+(\d{2}-\d{2}-\d{4}\s+\d{1,2}:\d{2}\s*[AP]M)",
                        card_text,
                        re.IGNORECASE
                    )
                    if end_match:
                        end_date = end_match.group(1)

                    # Get bid URL — the "Bid No." link (a.bid_no_hover) carries the
                    # real document link, e.g. href="showbidDocument/9638906".
                    # It does NOT contain the bid number text itself, so matching
                    # on bid_no in the href (as done previously) never succeeds.
                    bid_url = ""
                    bid_no_links = await card.query_selector_all("a.bid_no_hover")
                    if bid_no_links:
                        href = await bid_no_links[0].get_attribute("href") or ""
                        if href:
                            bid_url = href if href.startswith("http") else \
                                f"https://bidplus.gem.gov.in/{href.lstrip('/')}"

                    if not bid_url:
                        bid_url = f"https://bidplus.gem.gov.in/all-bids?search_keyword={keyword}"

                    print(f"[GeM] Found bid: {bid_no}")

                    # Skip if already in database
                    from database.db import get_conn
                    conn = get_conn()
                    existing = conn.execute(
                        "SELECT id FROM tenders WHERE tender_number=?", (bid_no,)
                    ).fetchone()
                    conn.close()
                    if existing:
                        continue

                    downloaded_files = []

                    tenders.append({
                        "title": items,
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
                        "description": f"GeM bid for {items[:100]}",
                        "downloaded_files": downloaded_files,
                        "relevance_score": 0.75
                    })

                    print(f"[GeM] ✅ Extracted: {bid_no} — {items[:50]}...")

                except Exception as e:
                    print(f"[GeM] Card extraction error: {e}")
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


def search_gem_sync(keyword: str, category: str = "", max_pages: int = 5, should_stop=None) -> list:
    """Synchronous wrapper for search_gem."""
    return asyncio.run(search_gem(keyword, category, max_pages, should_stop=should_stop))