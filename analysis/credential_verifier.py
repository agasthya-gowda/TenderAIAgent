"""
Credential Cross-Check
Cross-verifies each row in the company credentials register against the
actual scanned certificate PDF you shared, so the bid-recommendation pages
can say whether a credential was independently confirmed against its real
certificate, or is only as-recorded in the spreadsheet.

Several of these certificates are scanned images with no extractable text
via normal PDF text extraction — those are read with OCR (Tesseract)
instead. If even OCR can't get usable text (e.g. a very low-quality scan),
that's honestly flagged as "not independently verifiable" rather than
silently assumed correct or silently skipped.
"""

import json
import os
import re
from datetime import datetime

from analysis.company_profile import COMPANY_DATA_DIR, load_company_profile

CREDENTIAL_DOCS_DIR = os.path.join(COMPANY_DATA_DIR, "Chipscape Credential Docs")
VERIFICATION_CACHE_FILE = os.path.join(COMPANY_DATA_DIR, "credential_verification_cache.json")

# Keywords used to match a credential register row to its certificate file
# by filename — the filenames aren't standardized, so this is a best-effort
# lookup, not exact matching. `exclude` guards against a hint that's a
# substring of an unrelated file's name (e.g. "registration_certificate"
# appears in both the Firm Registration AND the Udyam certificate filenames).
_FILENAME_HINTS = {
    "Firm Registration": {"include": ["css_registration"], "exclude": ["udyam"]},
    "GSTIN": {"include": ["gst"], "exclude": []},
    "PAN": {"include": ["pan"], "exclude": []},
    "MSME-UDYAM": {"include": ["udyam"], "exclude": []},
    "NSIC": {"include": ["nsic"], "exclude": []},
    "ISO 9001:2015": {"include": ["9001"], "exclude": []},
    "ISO 14001:2015": {"include": ["14001"], "exclude": []},
    "ISO/IEC 20000-1:2018": {"include": ["20000"], "exclude": []},
    "ISO/IEC 27001:2022": {"include": ["27001"], "exclude": []},
    "ISO 45001:2018": {"include": ["45000", "45001"], "exclude": []},
    "CMMI Maturity Level 5": {"include": ["cmmi"], "exclude": []},
}


_TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def _ocr_pdf(pdf_path: str, max_pages: int = 2) -> str:
    """Renders a scanned PDF's pages to images and runs OCR — the fallback
    for certificates that have no extractable text at all (a photographed
    or scanned document, not a born-digital PDF)."""
    try:
        import io
        import fitz
        import pytesseract
        from PIL import Image

        if os.path.exists(_TESSERACT_CMD):
            pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD

        text = ""
        doc = fitz.open(pdf_path)
        for page in doc[:max_pages]:
            pix = page.get_pixmap(dpi=300)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            text += pytesseract.image_to_string(img) + "\n"
        doc.close()
        return text
    except Exception as e:
        print(f"[Credential Verifier] OCR failed for {pdf_path}: {e}")
        return ""


def _find_matching_pdf(credential_name: str):
    if not os.path.isdir(CREDENTIAL_DOCS_DIR):
        return None
    rule = _FILENAME_HINTS.get(credential_name)
    if not rule:
        return None
    for f in os.listdir(CREDENTIAL_DOCS_DIR):
        fl = f.lower()
        if any(hint in fl for hint in rule["include"]) and not any(x in fl for x in rule["exclude"]):
            return os.path.join(CREDENTIAL_DOCS_DIR, f)
    return None


def _extract_cert_facts(pdf_text: str, credential_name: str) -> dict:
    from analysis.analyzer import call_ai

    prompt = f"""You are verifying a scanned Indian government/certification body document.
The certificate is expected to be: {credential_name}

DOCUMENT TEXT (extracted from the PDF via OCR, may contain OCR noise and jumbled characters):
{pdf_text[:4000]}

Extract these fields. If the extracted text is too garbled/noisy to find a field with confidence, use "" — never guess.

For "certificate_no": some documents show MULTIPLE different numbers — an internal document/reference number used for tracking correspondence (often labeled "Ref.No", "Sr.No", "S.No" etc.) versus the actual registration/certificate/license number itself (often labeled things like "Registration No", "Certificate No", "License No", "Government Purchase Registration No"). Always prefer the latter — the real registration/certificate number, not an internal reference number — since that's what the credential is actually identified by.

Respond ONLY with valid JSON in exactly this shape:
{{"certificate_no": "", "issue_date": "", "expiry_date": "", "issuing_authority": ""}}"""

    try:
        response = call_ai(prompt).strip()
        if "```json" in response:
            response = response.split("```json")[1].split("```")[0]
        elif "```" in response:
            response = response.split("```")[1].split("```")[0]
        return json.loads(response)
    except Exception as e:
        print(f"[Credential Verifier] AI extraction failed for {credential_name}: {e}")
        return {}


def _normalize_date(value):
    """Best-effort parse of a date string to a plain date for comparison.
    Returns None if it can't be confidently parsed — never guesses."""
    if not value or not isinstance(value, str):
        return None
    try:
        from dateutil import parser as date_parser
        return date_parser.parse(value, dayfirst=True, fuzzy=True).date()
    except Exception:
        return None


def _compare_field(register_value, extracted_value, is_date=False):
    if not extracted_value:
        return "not_found"
    if is_date:
        reg_d, ext_d = _normalize_date(register_value), _normalize_date(extracted_value)
        if reg_d is None or ext_d is None:
            return "not_found"
        return "match" if reg_d == ext_d else "mismatch"
    reg_clean = re.sub(r"[\s\-/]", "", str(register_value or "")).upper()
    ext_clean = re.sub(r"[\s\-/]", "", str(extracted_value or "")).upper()
    if not reg_clean or not ext_clean:
        return "not_found"
    return "match" if reg_clean == ext_clean else "mismatch"


def verify_credentials(force: bool = False) -> dict:
    """Cross-checks every credential in the register against its real PDF
    certificate. Cached to disk since this doesn't change per-tender —
    re-run only when the underlying documents change, or force=True."""
    if not force and os.path.exists(VERIFICATION_CACHE_FILE):
        try:
            with open(VERIFICATION_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    from analysis.analyzer import read_pdf

    profile = load_company_profile(force_reload=True)
    results = []

    for cred in profile["credentials"]:
        name = cred["name"]
        pdf_path = _find_matching_pdf(name)

        if not pdf_path:
            results.append({
                "name": name, "pdf_file": None,
                "status": "No Certificate File Found",
                "detail": "No matching certificate PDF was provided for this credential.",
            })
            continue

        pdf_text = read_pdf(pdf_path)
        used_ocr = False
        if len(pdf_text.strip()) < 50:
            print(f"[Credential Verifier] {name}: no extractable text, trying OCR...")
            pdf_text = _ocr_pdf(pdf_path)
            used_ocr = True

        if len(pdf_text.strip()) < 50:
            results.append({
                "name": name, "pdf_file": os.path.basename(pdf_path),
                "status": "Not Independently Verifiable",
                "detail": "Certificate is a scanned image and OCR could not extract usable text from it.",
            })
            continue

        facts = _extract_cert_facts(pdf_text, name)
        cert_no_result = _compare_field(cred["certificate_no"], facts.get("certificate_no"))
        expiry_result = _compare_field(cred["expiry_date"], facts.get("expiry_date"), is_date=True)

        mismatches = []
        if cert_no_result == "mismatch":
            mismatches.append(
                f"Certificate No. differs: register says '{cred['certificate_no']}', "
                f"certificate shows '{facts.get('certificate_no')}'"
            )
        if expiry_result == "mismatch":
            mismatches.append(
                f"Expiry date differs: register says '{cred['expiry_date']}', "
                f"certificate shows '{facts.get('expiry_date')}'"
            )

        source_note = " (via OCR)" if used_ocr else ""
        if mismatches:
            status = "Mismatch Found"
            detail = "; ".join(mismatches) + source_note
        elif cert_no_result == "match" or expiry_result == "match":
            status = "Verified"
            detail = f"Certificate details confirmed against the original document{source_note}."
        else:
            status = "Not Independently Verifiable"
            detail = "Certificate text was readable but key fields could not be confidently matched."

        results.append({
            "name": name, "pdf_file": os.path.basename(pdf_path),
            "status": status, "detail": detail,
        })

    cache = {"generated_at": datetime.now().isoformat(), "results": results}
    os.makedirs(COMPANY_DATA_DIR, exist_ok=True)
    try:
        with open(VERIFICATION_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"[Credential Verifier] Failed to write cache: {e}")

    return cache


def verification_by_name() -> dict:
    """Convenience lookup: credential name -> verification result dict."""
    cache = verify_credentials()
    return {r["name"]: r for r in cache.get("results", [])}
