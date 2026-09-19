"""
Tender <-> Company Eligibility Matcher
Compares a tender's extracted eligibility/scope/financial requirements
against Chipscape's own turnover, certifications and past-project history,
and produces a structured recommendation for the analysis PDF's
"Company Match" pages.
"""

import json

from analysis.company_profile import load_company_profile, company_profile_as_text

MATCH_SCHEMA_EXAMPLE = """{
  "overall_recommendation": "Recommend Bid",
  "executive_summary": "One plain-English sentence summarizing the verdict and the main blocker if any.",
  "requirement_checklist": [
    {"category": "Bidder Eligibility (Criteria 1)", "requirement": "Minimum 3 years experience in Supply of Body Worn/Surveillance Cameras", "met": true},
    {"category": "Bidder Eligibility (Criteria 1)", "requirement": "Company/Firm/establishment Registration certificate", "met": true},
    {"category": "Certifications (Criteria 7)", "requirement": "ISO 9001:2015", "met": true},
    {"category": "Certifications (Criteria 7)", "requirement": "ISO 14001:2015", "met": false}
  ],
  "why_to_bid": ["Concrete real reason 1", "Concrete real reason 2"],
  "risks_and_gaps": ["Concrete real gap or risk 1", "Concrete real gap or risk 2"],
  "action_needed": ["Concrete actionable step 1"],
  "closest_matching_project": "Name and short detail of the single closest past project, or 'None found' if nothing relevant.",
  "financial_risk_note": "One sentence comparing tender value to recent turnover, or 'Not applicable' if tender value unknown.",
  "comparison_table": [
    {"requirement": "e.g. Minimum turnover of Rs 1 Cr", "company_status": "e.g. Rs 13.7 Cr (FY2024-25, audited)", "met": true},
    {"requirement": "e.g. Class-3 Electrical Contractor License", "company_status": "Not found in company credentials", "met": false}
  ]
}"""


def _build_prompt(tender: dict, details: dict, company_text: str) -> str:
    overview = details.get("overview", {}) or {}
    financial = details.get("financial", {}) or {}
    scope = details.get("scope", {}) or {}
    eligibility = details.get("eligibility", {}) or {}
    checklist_items = details.get("eligibility_checklist") or []

    if checklist_items:
        checklist_block = "\n".join(
            f"{i+1}. [{item.get('category', 'General')}] {item.get('requirement', '')}"
            for i, item in enumerate(checklist_items)
        )
    else:
        checklist_block = "(No itemized eligibility checklist was extracted from this tender's documents.)"

    return f"""You are a senior bid-strategy analyst for an Indian government-tender bidding company called Chipscape.

Your job: decide whether Chipscape SHOULD bid on the tender below, by comparing what the tender actually requires against Chipscape's real company data. Be honest and specific — never invent a requirement the tender doesn't state, and never invent a company credential/project that isn't in the data given. If the tender document doesn't clearly state a requirement, say so instead of guessing.

TENDER DETAILS:
Title: {tender.get('title', '')}
Department: {overview.get('department') or tender.get('department', '')}
Tender Type: {overview.get('tender_type') or tender.get('tender_type', '')}
Estimated Value: {financial.get('estimated_value') or 'Not disclosed in tender document'}
EMD Amount: {financial.get('emd_amount') or 'Not disclosed in tender document'}
Scope of Work: {scope.get('description', '')}
Items Required: {scope.get('items_required', '')}
Turnover Required (from tender, if stated): {eligibility.get('turnover_required', '')}
Experience Needed (from tender, if stated): {eligibility.get('experience_needed', '')}
Certifications Required (from tender, if stated): {eligibility.get('certifications', '')}
Documents Required (from tender, if stated): {eligibility.get('documents_required', '')}
Special Clauses: {eligibility.get('special_clauses', '')}

ELIGIBILITY CHECKLIST — every individual requirement/mandatory document pulled from the tender's own Eligibility Criteria section, numbered in the order they appeared:
{checklist_block}

{company_text}

Now produce a bid-eligibility comparison. Respond ONLY with valid JSON in exactly this shape ("met" as true/false, no other value):
{MATCH_SCHEMA_EXAMPLE}

Rules:
- Always write currency amounts as "Rs" (e.g. "Rs 1.5 Cr") — never use the Rupee symbol, it does not render correctly in the PDF report.
- Use only plain ASCII punctuation — a plain hyphen "-" instead of an em/en dash, and straight quotes ' and " instead of curly/smart quotes. Special characters do not render correctly in the PDF report.
- requirement_checklist MUST contain exactly one output entry for every single numbered item in the ELIGIBILITY CHECKLIST above, in the same order, with the same "category" and "requirement" text carried over unchanged — you are only adding the "met" true/false decision for each one, never merging, dropping, reordering, or adding items. If the ELIGIBILITY CHECKLIST above is empty, return an empty list for requirement_checklist.
- Decide "met": true only when Chipscape's real company data (given above) actually satisfies that specific requirement. If the company data is silent on a requirement (no matching turnover figure, no matching certificate, no matching document/experience), mark it "met": false — never assume something is satisfied just because the tender doesn't specify a strict minimum.
- comparison_table should list every concrete requirement the tender actually states, matched against the real company data, with a short explanation of the company's real status for each. Do not fabricate requirements that aren't in the tender text.
- why_to_bid and risks_and_gaps must be concrete and specific (mention real numbers, real cert names, real project names) — never generic filler.
- Each certification line in the company data below carries a "Cross-check" note showing whether it was independently verified against the real certificate PDF, found to mismatch it, or could not be verified (e.g. a scanned certificate with no readable text). Treat "Cross-check: Mismatch Found" as a genuine risk worth listing under risks_and_gaps, and mark that specific certificate's requirement_checklist entry as "met": false — a certificate that disagrees with its own register entry is a real compliance concern, not a minor detail. Treat "Cross-check: Not Independently Verifiable" as a normal certification (still counts as "met": true) but you may note in passing that it wasn't independently confirmed, without treating it as a strong risk.
- overall_recommendation must be exactly one of: "Recommend Bid", "Recommend with Caution", "Do Not Recommend"."""


def match_tender_to_company(tender: dict, details: dict, max_attempts: int = 3) -> dict:
    """Calls the AI to compare this tender against Chipscape's real company
    profile, retrying a few times the same way relevance-checking does
    (a single malformed/truncated AI response shouldn't sink the whole
    report). Falls back to a clearly-labeled 'could not be generated'
    result rather than fabricating a verdict when every attempt fails."""
    from analysis.analyzer import call_ai, sanitize_pdf_text

    company_text = company_profile_as_text(load_company_profile())
    prompt = _build_prompt(tender, details, company_text)

    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            # Low temperature — this is a scoring/recommendation judgment,
            # not creative writing, and should give a consistent verdict
            # across repeated re-analyses of the same tender rather than
            # drifting between "Recommend Bid" and "Recommend with Caution"
            # purely from the model's own sampling randomness.
            response = call_ai(prompt, temperature=0.1).strip()
            if "```json" in response:
                response = response.split("```json")[1].split("```")[0]
            elif "```" in response:
                response = response.split("```")[1].split("```")[0]
            result = json.loads(response)
            if "requirement_checklist" in result and result.get("overall_recommendation"):
                checklist = result.get("requirement_checklist") or []
                # Derived directly from the binary checklist rather than a
                # separate AI guess, so the headline percentage can never
                # disagree with what the table itself shows.
                if checklist:
                    met_count = sum(1 for item in checklist if item.get("met") is True)
                    result["overall_match_percent"] = round(100 * met_count / len(checklist))
                else:
                    result["overall_match_percent"] = None
                return sanitize_pdf_text(result)
            last_error = "response missing required fields"
        except Exception as e:
            last_error = e
        print(f"[Eligibility Matcher] Attempt {attempt}/{max_attempts} failed: {last_error}")

    print(f"[Eligibility Matcher] All {max_attempts} attempts failed, returning fallback")
    return {
        "overall_recommendation": "Unable to Determine",
        "overall_match_percent": None,
        "executive_summary": "Company-match comparison could not be generated for this tender — please review manually.",
        "requirement_checklist": [],
        "why_to_bid": [],
        "risks_and_gaps": [],
        "action_needed": [],
        "closest_matching_project": "Not available",
        "financial_risk_note": "Not available",
        "comparison_table": [],
    }
