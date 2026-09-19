"""
One-time data migration: copies every row from the existing SQLite
database (database/tenders.db) into the new PostgreSQL database, preserving
original ids (so existing references like static analysis PDF filenames
`analysis_<id>.pdf` keep matching the right tender), then resets each
table's SERIAL sequence so future inserts continue from the right number.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.db import get_conn as get_pg_conn

SQLITE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "database", "tenders.db")

TABLES = [
    "ceo_searches",
    "learned_patterns",
    "gem_keyword_full_search_log",
    "kppp_search_criteria",
    "tenders",
]


def migrate():
    sconn = sqlite3.connect(SQLITE_PATH)
    sconn.row_factory = sqlite3.Row
    pconn = get_pg_conn()
    pcur = pconn.cursor()

    for table in TABLES:
        rows = sconn.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            print(f"{table}: 0 rows, skipping")
            continue

        columns = rows[0].keys()
        col_list = ", ".join(columns)
        placeholders = ", ".join(["?"] * len(columns))

        count = 0
        for row in rows:
            values = tuple(row[c] for c in columns)
            pcur.execute(
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                values
            )
            count += 1
        pconn.commit()
        print(f"{table}: migrated {count} rows")

        # Reset the SERIAL sequence for tables with an auto-increment id,
        # so the next insert doesn't collide with the ids we just copied.
        if "id" in columns:
            pcur.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 1))"
            )
            pconn.commit()

    sconn.close()
    pconn.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    migrate()
