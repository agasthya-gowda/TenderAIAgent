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
    """Download a file from URL and save to path."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, headers=headers, timeout=30, stream=True)
        if response.status_code == 200:
            with open(save_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        return False
    except Exception as e:
        print(f"[GeM] Download error: {e}")
        return False

def find_pdf2_link_in_pdf1(pdf1_path: str) -> str:
    """
    Read PDF 1 and find the bid document link.
    Looks for pattern: GEM/2026/B/XXXXXXX or GEM/YYYY/B/XXXXXXX
    """
    try:
        import pdfplumber
        with pdfplumber.open(pdf1_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if not text:
                    continue

                # Look for bid document reference
                # Pattern: GEM/2026/B/7696220
                patterns = [
                    r"GEM/\d{4}/B/\d+",
                    r"GEM/\d{4}/R/\d+",
                    r"Bid\s+[Nn]o[.:]\s*(GEM/\d{4}/[A-Z]/\d+)",
                    r"refer\s+to\s+Bid\s+document\s+of\s+(GEM/\d{4}/[A-Z]/\d+)"
                ]

                for pattern in patterns:
                    matches = re.findall(pattern, text)
                    if matches:
                        # Return the last match
                        # (usually the referenced document)
                        bid_ref = matches[-1]
                        print(f"[GeM] Found bid reference in PDF 1: {bid_ref}")
                        return bid_ref

                # Also check hyperlinks in PDF
                for annot in page.annots or []:
                    uri = annot.get("uri", "")
                    if uri and "gem.gov.in" in uri:
                        print(f"[GeM] Found link in PDF 1: {uri}")
                        return uri

    except Exception as e:
        print(f"[GeM] PDF 1 read error: {e}")
    return ""

def construct_pdf2_url(bid_ref: str) -> str:
    """
    Construct PDF 2 download URL from bid reference.
    bid_ref can be:
    - GEM/2026/B/7696220
    - Full URL
    """
    if bid_ref.startswith("http"):
        return bid_ref

    # Convert GEM/2026/B/7696220 to URL
    # Try both known URL patterns
    bid_clean = bid_ref.replace("/", "_")
    urls = [
        f"https://bidplus.gem.gov.in/bidding/bid/getBidDocument/{bid_ref}",
        f"https://bidplus.gem.gov.in/showbidDocument/{bid_clean}",
        f"https://bidplus.gem.gov.in/downloadBidDocument/{bid_ref}",
    ]
    return urls[0]  # Try first, fallback in download function

async def search_gem(keyword: str, category: str = "") -> list:
    """
    Search GeM portal for tenders matching keyword.
    Extracts all details from bid cards.
    Downloads PDF 1 and PDF 2 for each bid.
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

            # Step 3 — Extract bid cards from results
            # From screenshot: each card has
            # BID NO, Items, Quantity, Department, Start/End Date
            bid_cards = await page.query_selector_all("div.card")

            print(f"[GeM] Found {len(bid_cards)} bid cards")

            # If no cards found try getting all divs with bid info
            if not bid_cards:
                # Try to get page text and parse
                page_text = await page.inner_text("body")
                print(f"[GeM] Page loaded, trying alternate extraction...")

                # Try table rows
                bid_cards = await page.query_selector_all("tr")

            for card in bid_cards[:8]:
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

                    # Get bid URL
                    bid_link = await card.query_selector(
                        f"a[href*='{bid_no}'], "
                        "a[href*='getBidResultView'], "
                        "a[href*='bidding/bid']"
                    )
                    bid_url = ""
                    if bid_link:
                        href = await bid_link.get_attribute("href") or ""
                        if href:
                            bid_url = href if href.startswith("http") else \
                                f"https://bidplus.gem.gov.in{href}"

                    if not bid_url:
                        bid_url = f"https://bidplus.gem.gov.in/all-bids?search_keyword={keyword}"

                    print(f"[GeM] Found bid: {bid_no}")

                    # Skip if already in database — avoid re-downloading
                    from database.db import get_conn
                    conn = get_conn()
                    existing = conn.execute(
                        "SELECT id FROM tenders WHERE tender_number=?", (bid_no,)
                    ).fetchone()
                    conn.close()
                    if existing:
                        print(f"[GeM] Skipping {bid_no} — already in database")
                        continue

                    # Step 4 — Download PDF 1
                    downloaded_files = []
                    pdf1_path = ""
                    pdf2_path = ""

                    try:
                        # Create folder for this bid
                        safe_name = bid_no.replace("/", "_")
                        bid_folder = os.path.join(DOWNLOAD_PATH, safe_name)
                        os.makedirs(bid_folder, exist_ok=True)

                        # Find and click bid number link
                        bid_no_link = await card.query_selector(
                            f"a:has-text('{bid_no}'), "
                            "a[href*='getBidResultView']"
                        )

                        if bid_no_link:
                            # Download PDF 1
                            async with context.expect_page() as new_page_info:
                                async with page.expect_download(timeout=15000) as dl1:
                                    await bid_no_link.click()

                                download1 = await dl1.value
                                pdf1_path = os.path.join(
                                    bid_folder,
                                    f"GeM-Bidding-{safe_name}.pdf"
                                )
                                await download1.save_as(pdf1_path)
                                downloaded_files.append(pdf1_path)
                                print(f"[GeM] PDF 1 downloaded: {pdf1_path}")

                    except Exception as e:
                        print(f"[GeM] PDF 1 download error for {bid_no}: {e}")

                        # Try direct URL download
                        if bid_url:
                            bid_id = re.search(r"/(\d+)$", bid_url)
                            if bid_id:
                                pdf1_url = f"https://bidplus.gem.gov.in/bidding/bid/getBidResultView/{bid_id.group(1)}"
                                pdf1_path = os.path.join(
                                    bid_folder,
                                    f"GeM-Bidding-{safe_name}.pdf"
                                )
                                if download_file(pdf1_url, pdf1_path):
                                    downloaded_files.append(pdf1_path)
                                    print(f"[GeM] PDF 1 downloaded via direct URL")

                    # Try to get full title from PDF 1 if current title is truncated
                   
                    # Step 5 — Read PDF 1 → find PDF 2 link
                    if pdf1_path and os.path.exists(pdf1_path):
                        bid_ref = find_pdf2_link_in_pdf1(pdf1_path)

                        if bid_ref:
                            # Step 6 — Download PDF 2
                            try:
                                pdf2_url = construct_pdf2_url(bid_ref)
                                pdf2_path = os.path.join(
                                    bid_folder,
                                    f"GeM-BidDocument-{safe_name}.pdf"
                                )

                                # Try multiple URL patterns
                                pdf2_urls = [
                                    f"https://bidplus.gem.gov.in/bidding/bid/getBidDocument/{bid_ref}",
                                    f"https://bidplus.gem.gov.in/showbidDocument/{bid_ref.replace('/', '_')}",
                                    f"https://bidplus.gem.gov.in/downloadBidDocument/{bid_ref}",
                                ]

                                for url in pdf2_urls:
                                    if download_file(url, pdf2_path):
                                        downloaded_files.append(pdf2_path)
                                        print(f"[GeM] PDF 2 downloaded: {pdf2_path}")
                                        break

                            except Exception as e:
                                print(f"[GeM] PDF 2 download error: {e}")

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

def search_gem_sync(keyword: str, category: str = "") -> list:
    """Synchronous wrapper for search_gem."""
    return asyncio.run(search_gem(keyword, category))