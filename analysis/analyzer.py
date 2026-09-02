"""
Tender Analyzer
Reads all downloaded tender files (PDF + Word docs)
Uses Claude/DeepSeek to extract complete details
Generates a clean 6-page analysis PDF
"""

import os
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "downloads")

def read_pdf(file_path: str) -> str:
    """Extract text from PDF file."""
    try:
        import pdfplumber
        text = ""
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
        return text[:8000]  # Limit to 8000 chars
    except Exception as e:
        print(f"[Analyzer] PDF read error: {e}")
        return ""

def read_docx(file_path: str) -> str:
    """Extract text from Word document."""
    try:
        import zipfile
        from xml.etree import ElementTree as ET

        text = ""
        with zipfile.ZipFile(file_path) as z:
            with z.open("word/document.xml") as f:
                tree = ET.parse(f)
                root = tree.getroot()
                ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
                for para in root.iter(f"{ns}p"):
                    para_text = ""
                    for run in para.iter(f"{ns}t"):
                        if run.text:
                            para_text += run.text
                    if para_text.strip():
                        text += para_text + "\n"
        return text[:4000]
    except Exception as e:
        print(f"[Analyzer] DOCX read error: {e}")
        return ""

def read_all_files(file_paths: list) -> str:
    """Read all downloaded files and combine text."""
    combined = ""
    for path in file_paths:
        if not os.path.exists(path):
            continue
        ext = os.path.splitext(path)[1].lower()
        filename = os.path.basename(path)
        combined += f"\n\n=== FILE: {filename} ===\n"
        if ext == ".pdf":
            combined += read_pdf(path)
        elif ext in [".docx", ".doc"]:
            combined += read_docx(path)
    return combined[:15000]

def call_ai(prompt: str) -> str:
    """Call DeepSeek first, Claude as fallback."""
    # Try DeepSeek first (cheaper for extraction)
    try:
        response = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 3000
            },
            timeout=40
        )
        result = response.json()["choices"][0]["message"]["content"]
        if result and len(result) > 50:
            return result
    except Exception as e:
        print(f"[Analyzer] DeepSeek failed: {e}")

    # Fallback to Claude
    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 3000,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=40
        )
        return response.json()["content"][0]["text"]
    except Exception as e:
        print(f"[Analyzer] Claude failed: {e}")
        return ""

def extract_tender_details(tender: dict, file_text: str) -> dict:
    """Use AI to extract structured details from tender documents."""
    prompt = f"""You are a government tender analyst in India.

Analyze this tender and extract all details.

TENDER INFO FROM PORTAL:
Title: {tender.get('title', '')}
Portal: {tender.get('portal', '')}
Tender Number: {tender.get('tender_number', '')}
Department: {tender.get('department', '')}
Location: {tender.get('location', '')}
Value: {tender.get('value', '')}
Deadline: {tender.get('deadline', '')}

TENDER DOCUMENTS TEXT:
{file_text}

Extract ALL available details and respond ONLY in this exact JSON format:
{{
  "overview": {{
    "title": "",
    "tender_number": "",
    "portal": "",
    "department": "",
    "location": "",
    "tender_type": "",
    "published_date": ""
  }},
  "financial": {{
    "estimated_value": "",
    "emd_amount": "",
    "performance_security": "",
    "payment_terms": "",
    "gst_details": ""
  }},
  "scope": {{
    "description": "",
    "items_required": "",
    "quantity": "",
    "specifications": "",
    "delivery_timeline": ""
  }},
  "eligibility": {{
    "turnover_required": "",
    "experience_needed": "",
    "certifications": "",
    "documents_required": "",
    "special_clauses": ""
  }},
  "dates": {{
    "published_date": "",
    "document_download_start": "",
    "pre_bid_meeting": "",
    "last_date_queries": "",
    "submission_deadline": "",
    "opening_date": ""
  }},
  "terms": {{
    "warranty": "",
    "amc_terms": "",
    "penalty_clauses": "",
    "liquidated_damages": "",
    "special_conditions": ""
  }}
}}

Use "Not mentioned in document" for any field not found.
Never leave any field empty."""

    response = call_ai(prompt)

    try:
        response = response.strip()
        if "```json" in response:
            response = response.split("```json")[1].split("```")[0]
        elif "```" in response:
            response = response.split("```")[1].split("```")[0]
        return json.loads(response)
    except Exception as e:
        print(f"[Analyzer] JSON parse error: {e}")
        return {}

def generate_analysis_pdf(tender: dict, details: dict, output_path: str):
    """Generate clean 6-page analysis PDF using reportlab."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer,
            Table, TableStyle, PageBreak
        )

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            rightMargin=2*cm,
            leftMargin=2*cm,
            topMargin=2*cm,
            bottomMargin=2*cm
        )

        styles = getSampleStyleSheet()

        # Custom styles
        title_style = ParagraphStyle(
            "CustomTitle",
            parent=styles["Title"],
            fontSize=18,
            textColor=colors.HexColor("#2C3E50"),
            spaceAfter=12
        )
        heading_style = ParagraphStyle(
            "CustomHeading",
            parent=styles["Heading1"],
            fontSize=14,
            textColor=colors.HexColor("#8E44AD"),
            spaceBefore=12,
            spaceAfter=6,
            borderPad=4
        )
        normal_style = ParagraphStyle(
            "CustomNormal",
            parent=styles["Normal"],
            fontSize=10,
            leading=16
        )
        label_style = ParagraphStyle(
            "Label",
            parent=styles["Normal"],
            fontSize=10,
            textColor=colors.HexColor("#555555"),
            leading=16
        )

        story = []

        def add_heading(text):
            story.append(Spacer(1, 0.3*cm))
            story.append(Paragraph(text, heading_style))
            story.append(Spacer(1, 0.2*cm))

        def add_field(label, value):
            if not value or value == "Not mentioned in document":
                return
            story.append(Paragraph(
                f"<b>{label}:</b> {value}", normal_style
            ))
            story.append(Spacer(1, 0.15*cm))

        def add_section_table(data):
            """Add a clean table for section data."""
            table_data = []
            for label, value in data:
                if value and value != "Not mentioned in document":
                    table_data.append([
                        Paragraph(f"<b>{label}</b>", label_style),
                        Paragraph(str(value), normal_style)
                    ])
            if not table_data:
                return
            t = Table(table_data, colWidths=[5*cm, 12*cm])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F8F9FA")),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#2C3E50")),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ROWBACKGROUNDS", (0, 0), (-1, -1),
                 [colors.HexColor("#FFFFFF"), colors.HexColor("#F8F9FA")]),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DEE2E6")),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            story.append(t)
            story.append(Spacer(1, 0.3*cm))

        # ── HEADER ──────────────────────────────────────────
        story.append(Paragraph("TENDER ANALYSIS REPORT", title_style))
        story.append(Paragraph(
            f"Generated by Tender Agent on "
            f"{datetime.now().strftime('%d %B %Y, %I:%M %p')}",
            label_style
        ))
        story.append(Spacer(1, 0.5*cm))

        # ── PAGE 1: OVERVIEW ─────────────────────────────────
        add_heading("1. Tender Overview")
        overview = details.get("overview", {})
        add_section_table([
            ("Tender Title", overview.get("title") or tender.get("title")),
            ("Tender Number", overview.get("tender_number") or tender.get("tender_number")),
            ("Portal", overview.get("portal") or tender.get("portal")),
            ("Department", overview.get("department") or tender.get("department")),
            ("Location", overview.get("location") or tender.get("location")),
            ("Tender Type", overview.get("tender_type") or tender.get("tender_type")),
            ("Published Date", overview.get("published_date") or tender.get("published_date")),
            ("Portal Link", tender.get("url", "")),
            ("Found Via Search", tender.get("found_via", ""))
        ])

        story.append(PageBreak())

        # ── PAGE 2: FINANCIAL DETAILS ─────────────────────────
        add_heading("2. Financial Details")
        financial = details.get("financial", {})
        add_section_table([
            ("Estimated Value", financial.get("estimated_value") or tender.get("value")),
            ("EMD Amount", financial.get("emd_amount") or tender.get("emd")),
            ("Performance Security", financial.get("performance_security")),
            ("Payment Terms", financial.get("payment_terms")),
            ("GST Details", financial.get("gst_details"))
        ])

        story.append(PageBreak())

        # ── PAGE 3: SCOPE OF WORK ─────────────────────────────
        add_heading("3. Scope of Work")
        scope = details.get("scope", {})
        add_section_table([
            ("Description", scope.get("description")),
            ("Items Required", scope.get("items_required")),
            ("Quantity", scope.get("quantity")),
            ("Specifications", scope.get("specifications")),
            ("Delivery Timeline", scope.get("delivery_timeline"))
        ])

        story.append(PageBreak())

        # ── PAGE 4: ELIGIBILITY ───────────────────────────────
        add_heading("4. Eligibility and Requirements")
        eligibility = details.get("eligibility", {})
        add_section_table([
            ("Turnover Required", eligibility.get("turnover_required")),
            ("Experience Needed", eligibility.get("experience_needed")),
            ("Certifications", eligibility.get("certifications")),
            ("Documents Required", eligibility.get("documents_required")),
            ("Special Clauses", eligibility.get("special_clauses"))
        ])

        story.append(PageBreak())

        # ── PAGE 5: IMPORTANT DATES ───────────────────────────
        add_heading("5. Important Dates")
        dates = details.get("dates", {})
        add_section_table([
            ("Published Date", dates.get("published_date") or tender.get("published_date")),
            ("Document Download Start", dates.get("document_download_start")),
            ("Pre-bid Meeting", dates.get("pre_bid_meeting")),
            ("Last Date for Queries", dates.get("last_date_queries")),
            ("Submission Deadline", dates.get("submission_deadline") or tender.get("deadline")),
            ("Opening Date", dates.get("opening_date"))
        ])

        story.append(PageBreak())

        # ── PAGE 6: TERMS AND CONDITIONS ─────────────────────
        add_heading("6. Key Terms and Conditions")
        terms = details.get("terms", {})
        add_section_table([
            ("Warranty", terms.get("warranty")),
            ("AMC Terms", terms.get("amc_terms")),
            ("Penalty Clauses", terms.get("penalty_clauses")),
            ("Liquidated Damages", terms.get("liquidated_damages")),
            ("Special Conditions", terms.get("special_conditions"))
        ])

        # Build PDF
        doc.build(story)
        print(f"[Analyzer] Analysis PDF generated: {output_path}")
        return True

    except Exception as e:
        print(f"[Analyzer] PDF generation error: {e}")
        return False
def download_kppp_docs(tender: dict) -> str:
    """Download KPPP tender documents on demand for analysis."""
    try:
        import asyncio
        from playwright.async_api import async_playwright
        from captcha.solver import solve_captcha
        import zipfile

        tender_number = tender.get("tender_number", "")
        safe_name = "".join(c for c in tender_number if c.isalnum() or c in "-_")
        tender_folder = os.path.join(DOWNLOAD_PATH, safe_name)
        os.makedirs(tender_folder, exist_ok=True)

        async def _download():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(accept_downloads=True)
                page = await context.new_page()

                await page.goto(
                    "https://kppp.karnataka.gov.in/#/portal/searchTender/live",
                    timeout=30000, wait_until="domcontentloaded"
                )
                await page.wait_for_timeout(3000)

                # Solve CAPTCHA
                for attempt in range(3):
                    canvas = await page.query_selector("canvas")
                    if not canvas:
                        break
                    img_bytes = await canvas.screenshot()
                    solution = solve_captcha(img_bytes)
                    if not solution:
                        continue

                    captcha_input = await page.query_selector("input[type='text']")
                    if captcha_input:
                        await captcha_input.fill(solution)
                    check_btn = await page.query_selector("input[type='button']")
                    if check_btn:
                        await check_btn.click()
                    await page.wait_for_timeout(2000)

                    page_text = await page.inner_text("body")
                    if "Please Enter correct Captcha" not in page_text:
                        print("[Analyzer] KPPP CAPTCHA solved!")
                        break

                # Search by tender number directly
                inputs = await page.query_selector_all("input[type='text']")
                if inputs:
                    await inputs[0].fill(tender_number)
                    await page.wait_for_timeout(500)

                # Select Published status
                selects = await page.query_selector_all("select")
                if len(selects) >= 2:
                    await selects[1].select_option(index=1)
                    await page.wait_for_timeout(500)

                search_btn = await page.query_selector("button:has-text('Search')")
                if search_btn:
                    await search_btn.click()
                    await page.wait_for_timeout(4000)

                # Find the specific tender row
                rows = await page.query_selector_all("table tbody tr")
                downloaded_files = []

                for row in rows:
                    cols = await row.query_selector_all("td")
                    if len(cols) < 4:
                        continue
                    row_tender_number = " ".join((await cols[3].inner_text()).strip().split())
                    if row_tender_number != tender_number:
                        continue

                    # Found the tender — click download
                    action_col = cols[-1]
                    download_icon = await action_col.query_selector(
                        "lib-icon[title='Download Tender Documents']"
                    )
                    if download_icon:
                        await download_icon.click()
                        await page.wait_for_timeout(2000)

                        zip_btn = await page.query_selector("button:has-text('Download Zip')")
                        if zip_btn:
                            async with page.expect_download(timeout=30000) as dl:
                                await zip_btn.click()
                            download = await dl.value
                            zip_path = os.path.join(tender_folder, f"{safe_name}.zip")
                            await download.save_as(zip_path)

                            with zipfile.ZipFile(zip_path, 'r') as z:
                                z.extractall(tender_folder)
                            os.remove(zip_path)

                            for f in os.listdir(tender_folder):
                                full_path = os.path.join(tender_folder, f)
                                downloaded_files.append(full_path)

                            print(f"[Analyzer] Downloaded {len(downloaded_files)} KPPP files")
                    break

                await browser.close()
                return downloaded_files

        files = asyncio.run(_download())
        if files:
            from database.db import update_downloaded_files
            update_downloaded_files(tender.get("id"), files)
            return read_all_files(files)
        return ""

    except Exception as e:
        print(f"[Analyzer] KPPP download error: {e}")
        return ""

def analyze_tender(tender: dict) -> str:
    """
    Main function — analyzes a tender and returns path to analysis PDF.
    Called when CEO clicks 'Get Full Analysis' button.
    """
    print(f"[Analyzer] Starting analysis for: {tender.get('title', '')[:60]}")

    # Step 1 — Read all downloaded files
    downloaded_files = tender.get("downloaded_files", [])
    if isinstance(downloaded_files, str):
        try:
            downloaded_files = json.loads(downloaded_files)
        except Exception:
            downloaded_files = []

    file_text = ""
    if downloaded_files:
        print(f"[Analyzer] Reading {len(downloaded_files)} files...")
        file_text = read_all_files(downloaded_files)
    elif tender.get("portal") == "KPPP" and tender.get("tender_number"):
        # For KPPP tenders — download documents now
        print("[Analyzer] Downloading KPPP documents for analysis...")
        file_text = download_kppp_docs(tender)
    elif tender.get("portal") == "GeM" and tender.get("tender_number"):
        # For GeM tenders — download documents now (on-demand)
        print("[Analyzer] Downloading GeM documents for analysis...")
        from portals.gem import download_gem_documents_sync
        from database.db import update_downloaded_files
        new_files = download_gem_documents_sync(tender.get("tender_number"), tender.get("url", ""))
        if new_files:
            update_downloaded_files(tender.get("id"), new_files)
            file_text = read_all_files(new_files)

    # Step 2 — Extract details using AI
    print("[Analyzer] Extracting details with AI...")
    details = extract_tender_details(tender, file_text)

    if not details:
        print("[Analyzer] AI extraction failed, using portal data only")
        details = {
            "overview": {}, "financial": {},
            "scope": {}, "eligibility": {},
            "dates": {}, "terms": {}
        }

    # Step 3 — Generate PDF
    safe_name = "".join(
        c for c in tender.get("tender_number", "tender")
        if c.isalnum() or c in "-_"
    )
    if not safe_name:
        safe_name = "tender_analysis"

    output_path = os.path.join(
        DOWNLOAD_PATH,
        f"{safe_name}_ANALYSIS_{datetime.now().strftime('%d%m%Y')}.pdf"
    )

    success = generate_analysis_pdf(tender, details, output_path)

    if success:
        print(f"[Analyzer] Done! PDF saved at: {output_path}")
        return output_path
    else:
        print("[Analyzer] PDF generation failed")
        return ""