"""
Telangana eProcurement Portal Searcher
Searches tender.telangana.gov.in (Govt of Telangana eProcurement) for
tenders matching a keyword — the exact same free-text match as typing into
the portal's own "Tender Search" box (Tender ID/ IFB No/ Name of Work) and
hitting Search.

Same shape as AP eProcurement (portals/ap_eproc.py) — no login wall and no
CAPTCHA on either the public tender search or document download, both
plain unauthenticated requests once a session cookie is primed by one
normal page load. Two real differences confirmed live (Browser pane +
direct fetch() calls against the actual site, not guessed from the HTML):
  - TenderDetailsHomeJson.html's response here is already plain JSON (no
    base64 wrapping like AP's).
  - The document-download flow needs a CSRFToken (scraped off the file-list
    page) and posts to DownLoadFile.html (AP's is DownLoadFile1.html, no
    CSRFToken).
"""

import os
import re
import json
import requests

BASE = "https://tender.telangana.gov.in"
DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "downloads")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# The search results table's Action column embeds this tender's internal
# procurement/indent ids as a JS onclick call — these (not the displayed
# Tender ID) are what the document-download endpoints actually need. See
# download_telangana_docs for where they get used.
_ACTION_IDS_RE = re.compile(r"fnviewTenderDocuments\((\d+),\s*(\d+)\)")

_CSRF_RE = re.compile(r"name=[\"']CSRFToken[\"']\s+(?:id=[\"'][^\"']*[\"']\s+)?value=[\"']([^\"']*)[\"']")
# Telangana's downloadFun carries all three fields the download POST needs
# right in its own call — doc id, doc name, and the sub-path to fetch it
# from — unlike AP's version, which only passes the id and reads the name
# back out of the surrounding link text instead.
_DOC_ROW_RE = re.compile(r"downloadFun\('(\d+)',\s*'([^']*)',\s*'([^']*)'\)")


def _new_session() -> requests.Session:
    """A fresh session primed with the portal's own session cookie — every
    search/document call below needs this, the same way a real browser
    picks one up just from loading the page once."""
    s = requests.Session()
    s.get(f"{BASE}/login.html", headers=_HEADERS, timeout=20)
    return s


def _search_params(keyword: str, page_start: int, page_size: int) -> dict:
    """The exact query string the portal's own 'Tender Search' box fires
    (confirmed live via its Network tab) against TenderDetailsHomeJson.html
    — a DataTables-style request, same family as AP's own search params
    but with one extra column (Published Date, between the work name/value
    columns and the two bid-submission date columns)."""
    return {
        "nTenderID": keyword, "nDepartmentID": "0", "subDeptId": "", "ddlDistrict": "",
        "ddlMandal": "", "biddingType": "", "sProcurementType": "", "mECVValue1": "",
        "mECVValue2": "", "dtBidClosingselect": "", "dtBidClosing1": "", "dtBidClosing2": "",
        "dtTenderOpening1": "", "dtTenderOpening2": "", "hdnSearch4": "", "hdnSearch": "",
        "hdncorrigendumsDetails": "", "hdncorrigendumsDetails1": "", "hdnnoSearch": "1",
        "hdncorrigendumsDetails2": "", "hdnPreviousPage": "", "hdnIndentID": "",
        "hdnTenderCategory": "", "hdnProcurementID": "", "hdnType": "current",
        "hdnPreviousPge": "TenderDetailsHome.html", "hdnadvsearch": "", "hdnFromStatus": "",
        "typeOfWorkFromConsolidation": "", "popUPRequestParameter": "", "selectedCircleDivison": "",
        "selectedDepartmentID": "0", "selectedProcurementType": "", "selectedTypeofWork": "",
        "aid": "", "hdnEncryptNames": "hdnEncryptNames", "hdnEncryptValues": "hdnEncryptValues",
        "sEcho": "1", "iColumns": "10", "sColumns": ",,,,,,,,,",
        "iDisplayStart": str(page_start), "iDisplayLength": str(page_size),
        "mDataProp_0": "0", "bSortable_0": "true", "mDataProp_1": "1", "bSortable_1": "true",
        "mDataProp_2": "2", "bSortable_2": "true", "mDataProp_3": "3", "bSortable_3": "true",
        "mDataProp_4": "4", "bSortable_4": "true", "mDataProp_5": "5", "bSortable_5": "true",
        "mDataProp_6": "6", "bSortable_6": "true", "mDataProp_7": "7", "bSortable_7": "true",
        "mDataProp_8": "8", "bSortable_8": "true", "mDataProp_9": "9", "bSortable_9": "false",
        "iSortCol_0": "5", "sSortDir_0": "desc", "iSortingCols": "1",
    }


def search_telangana(keyword: str, max_pages=None) -> list:
    """
    Searches Telangana eProcurement's own public tender search for
    `keyword` — the exact same free-text match the live portal's "Tender
    Search" box returns.

    Always pages through every result for this keyword (max_pages only caps
    it for testing) — same reasoning as search_ap: a specific keyword
    search returns a small result set, so there's no need for an
    incremental "only fetch what's new since last time" cutoff. Every run
    just re-confirms the keyword's full, current match list, and
    save_tender()'s own dedup (by tender_number + portal) silently skips
    anything already saved.
    """
    found_via_label = f"Telangana eProcurement: {keyword}"
    tenders = []
    try:
        session = _new_session()
        page_size = 20
        page_num = 0
        HARD_PAGE_CEILING = 200

        while max_pages is None or page_num < max_pages:
            if page_num >= HARD_PAGE_CEILING:
                print(f"[Telangana] Hit hard safety ceiling of {HARD_PAGE_CEILING} pages, stopping")
                break

            params = _search_params(keyword, page_num * page_size, page_size)
            try:
                resp = session.get(
                    f"{BASE}/TenderDetailsHomeJson.html", params=params,
                    headers=_HEADERS, timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"[Telangana] Page {page_num + 1} fetch/parse failed: {e}")
                break

            rows = data.get("aaData") or []
            print(f"[Telangana] Page {page_num + 1}: found {len(rows)} results")
            if not rows:
                break

            for row in rows:
                try:
                    if len(row) < 10:
                        continue
                    (department, tender_id, notice_no, category, title, value,
                     published_dt, start_dt, closing_dt, action_html) = row[:10]
                    if not tender_id:
                        continue

                    # Skip if already in database
                    from database.db import get_conn
                    conn = get_conn()
                    existing = conn.execute(
                        "SELECT id FROM tenders WHERE tender_number=? AND portal='Telangana'", (tender_id,)
                    ).fetchone()
                    conn.close()
                    if existing:
                        print(f"[Telangana] Skipping {tender_id} — already in database")
                        continue

                    ids_match = _ACTION_IDS_RE.search(action_html or "")
                    procurement_id = ids_match.group(1) if ids_match else None
                    indent_id = ids_match.group(2) if ids_match else None

                    tenders.append({
                        "title": title or "Not available",
                        "portal": "Telangana",
                        "url": f"{BASE}/TenderDetailsHome.html",
                        "category": category or "Not available",
                        "found_via": found_via_label,
                        "tender_number": tender_id,
                        "department": department or "Not available",
                        "location": "Not available",
                        "tender_type": notice_no or "Not available",
                        "value": value or "Not available",
                        "deadline": closing_dt or "Not available",
                        "published_date": start_dt or published_dt or "Not available",
                        "emd": "See tender document",
                        "description": f"Telangana eProcurement tender by {department}",
                        "downloaded_files": [],
                        "relevance_score": 0.75,
                        # Needed by download_telangana_docs to fetch this
                        # tender's document list.
                        "telangana_procurement_id": procurement_id,
                        "telangana_indent_id": indent_id,
                    })
                    print(f"[Telangana] Extracted: {tender_id} - {(title or '')[:50]}...")
                except Exception as e:
                    print(f"[Telangana] Row extraction error: {e}")
                    continue

            if len(rows) < page_size:
                # A short page is this portal's own signal there's nothing
                # more to page through (same rule AP/KPPP's search uses).
                break
            page_num += 1

        print(f"[Telangana] Done. Total: {len(tenders)} new tender(s) for '{keyword}'")
        return tenders

    except Exception as e:
        print(f"[Telangana] Fatal error: {e}")
        return tenders


def search_telangana_sync(keyword: str, max_pages=None) -> list:
    """Named *_sync for parity with search_gem_sync/search_kppp_sync/
    search_ap_sync — this one is plain requests calls, so it's synchronous
    already and this is just a thin alias."""
    return search_telangana(keyword, max_pages=max_pages)


def download_telangana_docs(tender: dict) -> list:
    """
    Downloads this Telangana tender's real documents on demand (called from
    analysis/analyzer.py's analyze_tender, same "on demand at Get Full
    Analysis time" pattern as download_ap_docs/download_kppp_docs).

    Confirmed live (direct requests, not just clicking the page):
      1. POST .../ViewTenderDocuments.html with hdnProcurementID/
         hdnIndentID returns a page listing this tender's files (doc id +
         name + sub-path, all three right in each downloadFun(...) call)
         and a one-time CSRFToken.
      2. POST .../DownLoadFile.html with that docID/docName/sPath +
         CSRFToken (plus the same procurement/indent/tender ids) returns
         that one file's raw bytes.

    Returns the list of local file paths downloaded (empty list if this
    tender has no captured procurement/indent id, or nothing could be
    fetched) — same shape analyze_tender expects from the other portals'
    download functions.
    """
    tender_number = tender.get("tender_number", "")
    procurement_id = tender.get("telangana_procurement_id")
    indent_id = tender.get("telangana_indent_id")
    if not procurement_id or not indent_id:
        print(f"[Telangana] Download skipped for {tender_number} — no procurement/indent id on this tender")
        return []

    try:
        session = _new_session()

        list_resp = session.post(
            f"{BASE}/ViewTenderDocuments.html",
            data={
                "hdnProcurementID": procurement_id,
                "hdnIndentID": indent_id,
                "hdnTenderId": tender_number,
                "popUPRequestParameter": "popUPRequestParameter",
                "hdnFromStatus": "moretenders",
                "hdnPreviousPage": "TenderDetailsHome.html",
            },
            headers=_HEADERS, timeout=20,
        )
        if list_resp.status_code != 200:
            print(f"[Telangana] Document list failed (status {list_resp.status_code}) for {tender_number}")
            return []

        csrf_match = _CSRF_RE.search(list_resp.text)
        csrf_token = csrf_match.group(1) if csrf_match else ""
        doc_rows = _DOC_ROW_RE.findall(list_resp.text)
        if not doc_rows:
            print(f"[Telangana] No documents listed for {tender_number}")
            return []

        safe_name = "".join(c for c in tender_number if c.isalnum() or c in "-_")
        tender_folder = os.path.join(DOWNLOAD_PATH, safe_name)
        os.makedirs(tender_folder, exist_ok=True)

        downloaded_files = []
        for doc_id, doc_name, doc_path in doc_rows:
            doc_name = doc_name.strip() or f"{doc_id}.pdf"
            try:
                dl_resp = session.post(
                    f"{BASE}/DownLoadFile.html",
                    data={
                        "hdnProcurementID": procurement_id,
                        "hdnIndentID": indent_id,
                        "hdnTenderId": tender_number,
                        "hdnPreviousPage": "TenderDetailsHome.html",
                        "hdndocIds": doc_id,
                        "hdndocName": doc_name,
                        "hdnsPath": doc_path or "TenderDocuments",
                        "CSRFToken": csrf_token,
                        "hdnEncryptNames": "hdnEncryptNames",
                        "hdnEncryptValues": "hdnEncryptValues",
                    },
                    headers=_HEADERS, timeout=60,
                )
                if dl_resp.status_code != 200 or not dl_resp.content:
                    print(f"[Telangana] File download failed (status {dl_resp.status_code}) for {doc_name}")
                    continue
                file_path = os.path.join(tender_folder, doc_name)
                with open(file_path, "wb") as out:
                    out.write(dl_resp.content)
                downloaded_files.append(file_path)
            except Exception as e:
                print(f"[Telangana] File download error for {doc_name}: {e}")
                continue

        print(f"[Telangana] Downloaded {len(downloaded_files)} file(s) for {tender_number}")
        return downloaded_files

    except Exception as e:
        print(f"[Telangana] Download error for {tender_number}: {e}")
        return []
