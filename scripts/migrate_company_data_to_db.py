"""
One-time migration: moves Chipscape's turnover/credentials/project data out
of the Excel files in company_data/ and into the database, per the manager's
request that all company-credential data be insertable/updatable/queryable
from the app instead of hand-edited spreadsheets.

Safe to re-run — skips any table that already has rows, so it never
duplicates data on a second run.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from analysis.company_profile import (
    _load_turnover, _load_credentials, _load_project_references, _load_project_quantities,
)
from database.db import (
    get_company_turnover_list, add_company_turnover,
    get_company_credentials_list, add_company_credential,
    get_project_references_list, add_project_reference,
    get_project_quantities_list, add_project_quantity,
)


def migrate_turnover():
    if get_company_turnover_list():
        print("[Migrate] company_turnover already has data — skipping")
        return
    years = _load_turnover()
    for y in years:
        add_company_turnover(
            y["financial_year"], y["turnover"], y.get("total_with_gst"), y.get("status") or "Audited"
        )
    print(f"[Migrate] Inserted {len(years)} turnover row(s)")


def migrate_credentials():
    if get_company_credentials_list():
        print("[Migrate] company_credentials already has data — skipping")
        return
    creds = _load_credentials()
    for c in creds:
        add_company_credential(
            c["name"], c["type"], c["issuing_authority"], c["certificate_no"],
            c["issue_date"], c["expiry_date"], c["status"]
        )
    print(f"[Migrate] Inserted {len(creds)} credential(s)")


def migrate_project_references():
    if get_project_references_list():
        print("[Migrate] project_references already has data — skipping")
        return
    projects = _load_project_references()
    for p in projects:
        add_project_reference(p)
    print(f"[Migrate] Inserted {len(projects)} project reference(s)")


def migrate_project_quantities():
    if get_project_quantities_list():
        print("[Migrate] project_quantities already has data — skipping")
        return
    quantities = _load_project_quantities()
    for q in quantities:
        add_project_quantity(q)
    print(f"[Migrate] Inserted {len(quantities)} project quantity row(s)")


if __name__ == "__main__":
    migrate_turnover()
    migrate_credentials()
    migrate_project_references()
    migrate_project_quantities()
    print("[Migrate] Done.")
