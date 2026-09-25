"""
One-time backfill: fills the new typed columns (start_date_parsed,
end_date_parsed, quantity_count, item_tags) for tenders saved before those
columns existed. Bid value / EMD / turnover requirement are intentionally
NOT backfilled here — they can only come from actually reading a tender's
real document (L2 / Get Full Analysis), not from re-parsing the existing
L1 text fields, which are just portal placeholders ("See bid document" etc.)
for the vast majority of rows.

Safe to re-run — only touches rows where start_date_parsed is still NULL.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database.db import (
    get_conn, _parse_tender_datetime, _parse_tender_int, _parse_item_tags,
)


def backfill():
    conn = get_conn()
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT id, title, published_date, deadline, quantity FROM tenders WHERE start_date_parsed IS NULL"
    ).fetchall()

    updated = 0
    for r in rows:
        cur.execute(
            """UPDATE tenders
               SET start_date_parsed=?, end_date_parsed=?, quantity_count=?, item_tags=?
               WHERE id=?""",
            (
                _parse_tender_datetime(r["published_date"]),
                _parse_tender_datetime(r["deadline"]),
                _parse_tender_int(r["quantity"]),
                _parse_item_tags(r["title"]),
                r["id"],
            )
        )
        updated += 1

    conn.commit()
    conn.close()
    print(f"[Backfill] Updated {updated} tender(s) with typed start/end date, quantity and item tags.")


if __name__ == "__main__":
    backfill()
