"""
One-time backfill: refreshes the title of already-saved GeM tenders with
the fuller item list GeM only reveals via its hover popover (data-content
attribute) — see the fix in portals/gem.py. Older rows were saved before
that fix and are stuck with GeM's short truncated blurb as their title.

Re-searches each keyword given, matches results back to existing DB rows
by tender_number (stable, unlike title which is exactly what's changing),
and updates the title in place — never inserts a new row.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from portals.gem import search_gem_sync
from database.db import get_conn, update_tender_title


def backfill_keyword(keyword: str):
    print(f"=== Backfilling titles for keyword: '{keyword}' ===")
    tenders = search_gem_sync(keyword, max_pages=None)
    print(f"Fetched {len(tenders)} tenders for '{keyword}'")

    conn = get_conn()
    cur = conn.cursor()
    updated = 0
    for t in tenders:
        tender_number = t.get("tender_number")
        new_title = t.get("title")
        if not tender_number or not new_title:
            continue
        row = cur.execute(
            "SELECT id, title FROM tenders WHERE tender_number=?", (tender_number,)
        ).fetchone()
        if not row:
            continue
        if len(new_title) > len(row["title"] or ""):
            update_tender_title(row["id"], new_title)
            updated += 1
    conn.close()
    print(f"Updated {updated} tender title(s) for '{keyword}'")


if __name__ == "__main__":
    for kw in ["camera", "body camera"]:
        backfill_keyword(kw)
    print("Done.")
