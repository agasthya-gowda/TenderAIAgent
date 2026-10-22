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

def _patch_reportlab_culprit_bug():
    # reportlab's own Table._culprit() (used only to build a diagnostic
    # message when a table splits across a page) crashes with
    # "'>' not supported between instances of 'NoneType' and 'int'" on
    # `max(rh)` whenever a row height list contains None — which aborts
    # the entire PDF build even though the underlying page split itself
    # is harmless. Long eligibility/comparison tables (50+ rows) that
    # span multiple pages trigger this reliably. Wrapping it so a failure
    # here can't take down the whole build.
    try:
        from reportlab.platypus.tables import Table
        if getattr(Table, "_culprit_patched", False):
            return
        original_culprit = Table._culprit

        def _safe_culprit(self):
            try:
                return original_culprit(self)
            except Exception:
                return "<table row too large to describe>"

        Table._culprit = _safe_culprit
        Table._culprit_patched = True
    except Exception:
        pass

_patch_reportlab_culprit_bug()

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
        # Government tender PDFs routinely run 30-50+ pages with the actual
        # Eligibility Criteria table appearing well past page 8-10 (behind a
        # table of contents, glossary and tender notification) — even the
        # 30000-char cap cut off a real 23-page GeM bid document (65000+
        # chars) right before its eligibility section, so this is raised
        # further to comfortably cover full-length bid documents.
        return text[:70000]
    except Exception as e:
        print(f"[Analyzer] PDF read error: {e}")
        return ""

LIBREOFFICE_PATH = r"C:\Program Files\LibreOffice\program\soffice.exe"

def _convert_doc_with_libreoffice(file_path: str) -> str:
    """A legacy .doc file (the pre-2007 binary Word format) isn't a zip
    archive at all, so it can't be read the way .docx is below — this
    handles it (and works as a general fallback for any Word format)
    by converting it to plain text with LibreOffice running headless."""
    import subprocess
    import tempfile

    if not os.path.exists(LIBREOFFICE_PATH):
        return ""
    try:
        with tempfile.TemporaryDirectory() as out_dir:
            subprocess.run(
                [LIBREOFFICE_PATH, "--headless", "--convert-to", "txt",
                 "--outdir", out_dir, file_path],
                capture_output=True, timeout=60
            )
            base = os.path.splitext(os.path.basename(file_path))[0]
            txt_path = os.path.join(out_dir, base + ".txt")
            if os.path.exists(txt_path):
                with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read()
    except Exception as e:
        print(f"[Analyzer] LibreOffice conversion error: {e}")
    return ""

def read_docx(file_path: str) -> str:
    """Extract text from a Word document. The modern .docx format is
    just a zip archive, read directly below; a legacy .doc file (not a
    zip at all) falls back to LibreOffice conversion."""
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
        if text.strip():
            # See the matching comment in read_pdf() — the Eligibility
            # Criteria section routinely sits tens of thousands of
            # characters into a full RFP .docx, well past the old 4000-char
            # cap (a real KPPP tender's criteria section started at
            # character ~14,500 of a ~100,000-char document).
            return text[:70000]
    except Exception as e:
        print(f"[Analyzer] DOCX read error: {e}, trying LibreOffice conversion...")

    return _convert_doc_with_libreoffice(file_path)[:70000]

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
    # Raised alongside the per-file caps above for the same reason — a
    # single full RFP document can legitimately need 20000+ characters just
    # to reach its Eligibility Criteria section, and tenders often ship as
    # 2-3 separate files (notification + full RFP + corrigendum).
    return combined[:150000]

def call_ai(prompt: str, temperature: float = None) -> str:
    """Call DeepSeek first, Claude as fallback.

    `temperature` is left at the API default (unset) for most callers —
    factual extraction from a tender document doesn't need it touched.
    Pass a low value (e.g. 0.1) for a call whose output should stay
    consistent across repeated runs on the same input, like a scoring/
    recommendation judgment — otherwise the model's own sampling
    randomness alone can shift a score by 10+ points between two
    analyses of the identical tender, with nothing in the input having
    changed."""
    deepseek_payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        # 3000 was enough for the old short eligibility summary, but the
        # granular per-item eligibility_checklist (one entry per mandatory
        # document/requirement in the tender, sometimes 40-60+ for a large
        # RFP) can produce a much longer JSON response — a real KPPP tender
        # truncated mid-JSON at the old cap, breaking the whole extraction.
        "max_tokens": 8000
    }
    if temperature is not None:
        deepseek_payload["temperature"] = temperature

    # Try DeepSeek first (cheaper for extraction)
    try:
        response = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json=deepseek_payload,
            timeout=40
        )
        result = response.json()["choices"][0]["message"]["content"]
        if result and len(result) > 50:
            return result
    except Exception as e:
        print(f"[Analyzer] DeepSeek failed: {e}")

    claude_payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": prompt}]
    }
    if temperature is not None:
        claude_payload["temperature"] = temperature

    # Fallback to Claude
    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json=claude_payload,
            timeout=40
        )
        return response.json()["content"][0]["text"]
    except Exception as e:
        print(f"[Analyzer] Claude failed: {e}")
        return ""

def extract_tender_details(tender: dict, file_text: str) -> dict:
    """Use AI to extract structured details from tender documents."""
    # GeM never has a real value at scrape time — "value" is hardcoded to the
    # placeholder "See bid document" (see portals/gem.py) since GeM's listing
    # page never shows it, only the bid PDF does. Feeding that placeholder
    # into the prompt as if it were real portal data caused the AI to
    # sometimes echo it straight back as its "estimated_value" answer instead
    # of correctly reporting that the real document doesn't state one. KPPP's
    # "value" is genuine portal data, so it's still included there.
    portal_value = tender.get("value", "")
    value_line = f"Value: {portal_value}\n" if portal_value and portal_value != "See bid document" else ""

    prompt = f"""You are a government tender analyst in India.

Analyze this tender and extract all details.

TENDER INFO FROM PORTAL:
Title: {tender.get('title', '')}
Portal: {tender.get('portal', '')}
Tender Number: {tender.get('tender_number', '')}
Department: {tender.get('department', '')}
Location: {tender.get('location', '')}
{value_line}Deadline: {tender.get('deadline', '')}

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
  "eligibility_checklist": [
    {{"category": "short label for the SI/criteria this belongs to, e.g. 'Bidder Eligibility (Criteria 1)' or 'Turnover Eligibility (Criteria 2)'", "requirement": "exactly ONE atomic, individually-checkable requirement or mandatory supporting document, in plain words"}}
  ],
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
Never leave any field empty.

For "eligibility_checklist": read the tender's full Eligibility Criteria
section (usually a numbered table of SI/criteria with a "Mandatory
Supporting Documents" column) and list out EVERY individual item as its
own separate entry — do not summarize, merge, or group multiple items
into one line. If one criteria number requires 4 separate mandatory
documents (e.g. sub-points a, b, c, d), that is 4 separate checklist
entries, not one. Include every distinct: minimum turnover figure,
minimum experience/years requirement, each individual certificate or
certification standard named (e.g. ISO 9001:2015 and ISO 14001:2015 are
2 separate entries, not one), each individual mandatory document, each
distinct eligibility condition (OEM/dealer status, local presence,
blacklisting declaration, EMD, warranty period, country-of-origin
restriction, land-border-country restriction, etc). If the tender has no
identifiable eligibility criteria section at all, return an empty list
for eligibility_checklist rather than inventing items."""

    response = call_ai(prompt)

    try:
        response = response.strip()
        if "```json" in response:
            response = response.split("```json")[1].split("```")[0]
        elif "```" in response:
            response = response.split("```")[1].split("```")[0]
        return sanitize_pdf_text(json.loads(response))
    except Exception as e:
        print(f"[Analyzer] JSON parse error: {e}")
        return {}


_PDF_UNSAFE_CHAR_REPLACEMENTS = {
    "₹": "Rs ",
    "—": "-",   # em dash
    "–": "-",   # en dash
    "‘": "'", "’": "'",   # smart single quotes
    "“": '"', "”": '"',   # smart double quotes
    "•": "-",                  # bullet character
}


def sanitize_pdf_text(value):
    """The PDF's base font (Helvetica) doesn't reliably render several
    characters the AI likes to use (Rupee sign, em/en dashes, smart quotes,
    bullets) — they come out as corrupted glyphs. Recursively swap them for
    safe plain-ASCII equivalents everywhere in any AI-generated text before
    it reaches reportlab. Shared by extract_tender_details() here and by
    eligibility_matcher.py's company-match output."""
    if isinstance(value, str):
        for bad, good in _PDF_UNSAFE_CHAR_REPLACEMENTS.items():
            value = value.replace(bad, good)
        return value
    if isinstance(value, dict):
        return {k: sanitize_pdf_text(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_pdf_text(v) for v in value]
    return value

def generate_analysis_pdf(tender: dict, details: dict, output_path: str, match: dict = None):
    """Generate clean analysis PDF using reportlab — 6 core pages, plus 3
    more (Company Match, Why/Risks, Detailed Comparison) when a `match`
    dict from eligibility_matcher.match_tender_to_company() is supplied."""
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

        def _clip_cell_text(text, limit=600):
            # An unusually long, AI-generated cell value (occasionally the
            # model dumps an entire clause into one field) can make a table
            # row taller than a single page, which reportlab can't split —
            # it then fails with a LayoutError, and a bug in reportlab's own
            # error-reporting code (tables.py _culprit()) masks that as an
            # unrelated "'>' not supported between NoneType and int" crash.
            # Capping cell text keeps every row well within one page.
            text = str(text or "")
            return text if len(text) <= limit else text[:limit].rstrip() + "..."

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
        # A Paragraph carries its own text color baked in — a table's
        # TEXTCOLOR style command has no effect once a cell's content is
        # already a styled Paragraph, so header cells on a dark background
        # need this dedicated white style rather than reusing label_style
        # (gray) and hoping TableStyle overrides it.
        header_cell_style = ParagraphStyle(
            "HeaderCell",
            parent=styles["Normal"],
            fontSize=10,
            textColor=colors.white,
            leading=14
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
                        # A single very long field (e.g. a raw Specifications
                        # dump copied from the tender) can make its table row
                        # taller than a whole page — a Table row is atomic and
                        # can't split across pages, so reportlab fails with
                        # "too large on page N". Capped generously (a few
                        # pages worth of text) so this can't happen while
                        # still showing effectively the full value normally.
                        Paragraph(_clip_cell_text(value, limit=2500), normal_style)
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

        # ── Company-match helpers (pages 7-9) ─────────────────
        def add_category_breakdown(checklist):
            # One row per individual requirement pulled straight from the
            # tender's own eligibility criteria (not a rolled-up percentage
            # per broad category) — a plain Met/Not Met binary call per line,
            # so the table alone answers "do we have this or not" without
            # needing to read a paragraph of notes. Consecutive rows under
            # the same category get their Category cell visually merged
            # (via SPAN) so the grouping still reads clearly.
            if not checklist:
                story.append(Paragraph(
                    "No specific, individually-stated eligibility requirements "
                    "were found in the tender document to check off one by one.",
                    normal_style
                ))
                story.append(Spacer(1, 0.3*cm))
                return
            table_data = [[
                Paragraph("<b>Category</b>", header_cell_style),
                Paragraph("<b>Mentioned Specification in Tender</b>", header_cell_style),
                Paragraph("<b>Met / Not Met</b>", header_cell_style),
            ]]
            for row_idx, item in enumerate(checklist, start=1):
                category = str(item.get("category", ""))
                met = item.get("met") is True
                met_text = "MET" if met else "NOT MET"
                met_style = ParagraphStyle(
                    f"MetCell{row_idx}", parent=normal_style, alignment=1,
                    textColor=colors.HexColor("#16A34A") if met else colors.HexColor("#DC2626")
                )
                # The category is repeated on every row rather than visually
                # merged (SPAN) across consecutive same-category rows — a
                # SPANned block can't be split across a page break, so a
                # category with many items could force an unsplittable block
                # taller than a page and crash the whole PDF build.
                table_data.append([
                    Paragraph(_clip_cell_text(category), normal_style),
                    Paragraph(_clip_cell_text(item.get("requirement", "")), normal_style),
                    Paragraph(f"<b>{met_text}</b>", met_style),
                ])
            t = Table(table_data, colWidths=[4.5*cm, 10*cm, 2.5*cm], repeatRows=1)
            style_cmds = [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2C3E50")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#2C3E50")),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("ALIGN", (2, 0), (2, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DEE2E6")),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.HexColor("#FFFFFF"), colors.HexColor("#F8F9FA")]),
            ]
            t.setStyle(TableStyle(style_cmds))
            story.append(t)
            story.append(Spacer(1, 0.3*cm))

        def add_comparison_table(rows):
            if not rows:
                return
            table_data = [[
                Paragraph("<b>Tender Requires</b>", header_cell_style),
                Paragraph("<b>Our Company Status</b>", header_cell_style),
                Paragraph("<b>Met</b>", header_cell_style),
            ]]
            for r in rows:
                met = r.get("met")
                met_text = "PASS" if met else "GAP"
                met_style = ParagraphStyle(
                    "MetCell", parent=normal_style, alignment=1,
                    textColor=colors.HexColor("#16A34A") if met else colors.HexColor("#DC2626")
                )
                table_data.append([
                    Paragraph(_clip_cell_text(r.get("requirement", "")), normal_style),
                    Paragraph(_clip_cell_text(r.get("company_status", "")), normal_style),
                    Paragraph(f"<b>{met_text}</b>", met_style),
                ])
            t = Table(table_data, colWidths=[6.5*cm, 8.5*cm, 2*cm])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2C3E50")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("ALIGN", (2, 0), (2, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DEE2E6")),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.HexColor("#FFFFFF"), colors.HexColor("#F8F9FA")]),
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

        def _financial_value(ai_value):
            # AI genuinely checked the real document and found nothing for
            # this field — say so honestly instead of falling back to the
            # scrape-time "See bid document" placeholder, which is
            # misleading once the document has actually been read (many
            # GeM tenders, especially multi-item ones, never publish a
            # fixed estimated value/EMD upfront at all).
            if not ai_value or ai_value == "Not mentioned in document":
                return "Not disclosed in tender document"
            return ai_value

        add_section_table([
            ("Estimated Value", _financial_value(financial.get("estimated_value"))),
            ("EMD Amount", _financial_value(financial.get("emd_amount"))),
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

        # ── PAGES 7-9: COMPANY MATCH & BID RECOMMENDATION ─────
        if match:
            story.append(PageBreak())

            # PAGE 7 — Verdict & Summary
            add_heading("7. Company Match & Bid Recommendation")
            summary = match.get("executive_summary")
            if summary:
                story.append(Paragraph(f"<i>{summary}</i>", normal_style))
                story.append(Spacer(1, 0.3*cm))
            story.append(Paragraph("Match Breakdown by Category", heading_style))
            add_category_breakdown(match.get("requirement_checklist"))

            story.append(PageBreak())

            # PAGE 8 — Detailed Requirement Comparison
            add_heading("8. Detailed Requirement Comparison")
            comparison_rows = match.get("comparison_table")
            if comparison_rows:
                add_comparison_table(comparison_rows)
            else:
                story.append(Paragraph(
                    "No specific, individually-stated requirements were found "
                    "in the tender document to compare line by line.",
                    normal_style
                ))

        # Build PDF
        doc.build(story)
        print(f"[Analyzer] Analysis PDF generated: {output_path}")
        return True

    except Exception as e:
        print(f"[Analyzer] PDF generation error: {e}")
        return False
def download_kppp_docs(tender: dict) -> str:
    """Download KPPP tender documents on demand for analysis.

    Rewritten to call KPPP's real document APIs directly, instead of the
    old approach (fill the search form, click through to the results page,
    find the matching row, click its download icon, download a zip) — that
    was the same kind of fragile page-clicking the main search already
    moved away from (see the API-call comment in portals/kppp.py's
    _search_kppp_once), just never updated here too. Confirmed live (via
    the real site's own Network tab) that once logged in:
      1. GET .../portal-service/{nit_id}/get-{category}-tender-files
         returns a JSON list of this tender's files, each with its own
         `uuid` and `fileName`.
      2. GET .../portal-service/{nit_id}/{category}-tender-file/{uuid}/
         download-file returns that one file's raw bytes directly.
    `nit_id` comes from the tender row (captured at search time — see
    portals/kppp.py); a tender saved before that existed has none, so this
    returns nothing for those, same as it always has."""
    tender_number = tender.get("tender_number", "")
    nit_id = tender.get("nit_id")
    category = (tender.get("category") or "").strip().lower()
    if not nit_id or not category:
        print(f"[Analyzer] KPPP download skipped for {tender_number} — no nit_id/category on this tender")
        return ""

    try:
        import asyncio
        from playwright.async_api import async_playwright
        from captcha.solver import solve_captcha

        safe_name = "".join(c for c in tender_number if c.isalnum() or c in "-_")
        tender_folder = os.path.join(DOWNLOAD_PATH, safe_name)
        os.makedirs(tender_folder, exist_ok=True)

        async def _download_once():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
                context = await browser.new_context(accept_downloads=True)
                page = await context.new_page()

                await page.goto(
                    "https://kppp.karnataka.gov.in/#/portal/searchTender/live",
                    timeout=30000, wait_until="domcontentloaded"
                )
                await page.wait_for_timeout(3000)

                # Solve CAPTCHA (needed once, to get an authenticated
                # session/cookies the file APIs below also rely on) — same
                # reload-between-retries fix as the main search's CAPTCHA
                # loop in portals/kppp.py.
                solved = False
                for attempt in range(6):
                    if attempt > 0:
                        reload_btn = await page.query_selector("a.cpt-btn.reload")
                        if reload_btn:
                            await reload_btn.click()
                            await page.wait_for_timeout(1000)

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
                        solved = True
                        break

                if not solved:
                    await browser.close()
                    return []

                base = f"https://kppp.karnataka.gov.in/supplier-registration-service/v1/api/portal-service/{nit_id}"

                list_resp = await context.request.get(f"{base}/get-{category}-tender-files")
                if list_resp.status != 200:
                    print(f"[Analyzer] KPPP file list failed (status {list_resp.status}) for {tender_number}")
                    await browser.close()
                    return []
                file_list = await list_resp.json()

                downloaded_files = []
                for f in file_list:
                    file_uuid = f.get("uuid")
                    file_name = f.get("fileName") or f"{file_uuid}.pdf"
                    if not file_uuid:
                        continue
                    dl_resp = await context.request.get(f"{base}/{category}-tender-file/{file_uuid}/download-file")
                    if dl_resp.status != 200:
                        print(f"[Analyzer] KPPP file download failed (status {dl_resp.status}) for {file_name}")
                        continue
                    file_bytes = await dl_resp.body()
                    file_path = os.path.join(tender_folder, file_name)
                    with open(file_path, "wb") as out:
                        out.write(file_bytes)
                    downloaded_files.append(file_path)

                print(f"[Analyzer] Downloaded {len(downloaded_files)} KPPP file(s)")
                await browser.close()
                return downloaded_files

        async def _download():
            # Same 5-whole-attempt retry as search_kppp() — a fresh browser
            # + CAPTCHA each time — since one failed CAPTCHA attempt
            # shouldn't sink the whole download when the portal's CAPTCHA
            # is this failure-prone (each of the 5 attempts also gets its
            # own inner 6-try CAPTCHA loop with a reload between tries, same
            # as the search flow — up to 30 real CAPTCHA attempts total).
            max_attempts = 5
            for attempt in range(1, max_attempts + 1):
                try:
                    files = await _download_once()
                except Exception as e:
                    print(f"[Analyzer] KPPP download attempt {attempt}/{max_attempts} crashed: {e}")
                    files = []
                if files:
                    return files
                print(f"[Analyzer] KPPP download attempt {attempt}/{max_attempts} failed, retrying...")
            print(f"[Analyzer] Giving up on KPPP download after {max_attempts} attempts")
            return []

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
    elif tender.get("portal") == "AP" and tender.get("tender_number"):
        # For AP eProcurement tenders — download documents now (on-demand)
        print("[Analyzer] Downloading AP eProcurement documents for analysis...")
        from portals.ap_eproc import download_ap_docs
        from database.db import update_downloaded_files
        new_files = download_ap_docs(tender)
        if new_files:
            update_downloaded_files(tender.get("id"), new_files)
            file_text = read_all_files(new_files)
    elif tender.get("portal") == "Telangana" and tender.get("tender_number"):
        # For Telangana eProcurement tenders — download documents now (on-demand)
        print("[Analyzer] Downloading Telangana eProcurement documents for analysis...")
        from portals.telangana import download_telangana_docs
        from database.db import update_downloaded_files
        new_files = download_telangana_docs(tender)
        if new_files:
            update_downloaded_files(tender.get("id"), new_files)
            file_text = read_all_files(new_files)
    elif tender.get("portal") == "Kerala" and tender.get("tender_number"):
        # For Kerala eTenders tenders — download documents now (on-demand)
        print("[Analyzer] Downloading Kerala eTenders documents for analysis...")
        from portals.kerala import download_kerala_docs
        from database.db import update_downloaded_files
        new_files = download_kerala_docs(tender)
        if new_files:
            update_downloaded_files(tender.get("id"), new_files)
            file_text = read_all_files(new_files)
    elif tender.get("portal") == "TamilNadu" and tender.get("tender_number"):
        # For Tamil Nadu eTenders tenders — download documents now (on-demand)
        print("[Analyzer] Downloading Tamil Nadu eTenders documents for analysis...")
        from portals.tamilnadu import download_tamilnadu_docs
        from database.db import update_downloaded_files
        new_files = download_tamilnadu_docs(tender)
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
    elif tender.get("id") is not None:
        # Bid value / EMD / turnover requirement genuinely aren't on either
        # portal's search results page (see the typed-column comment in
        # database/db.py) — only knowable once the real document is read,
        # i.e. right here at L2. Fill in the typed columns now that they are.
        from database.db import update_tender_l2_financials
        financial = details.get("financial", {})
        eligibility = details.get("eligibility", {})
        update_tender_l2_financials(
            tender["id"],
            estimated_value=financial.get("estimated_value"),
            emd_amount=financial.get("emd_amount"),
            turnover_required=eligibility.get("turnover_required"),
        )

    # Step 2b — Compare against Chipscape's own company profile (turnover,
    # certifications, past projects) to produce the bid-recommendation pages.
    print("[Analyzer] Matching tender against company profile...")
    try:
        from analysis.eligibility_matcher import match_tender_to_company
        match = match_tender_to_company(tender, details)
    except Exception as e:
        print(f"[Analyzer] Company-match step failed: {e}")
        match = None

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

    success = generate_analysis_pdf(tender, details, output_path, match=match)

    if success:
        print(f"[Analyzer] Done! PDF saved at: {output_path}")
        return output_path
    else:
        print("[Analyzer] PDF generation failed")
        return ""