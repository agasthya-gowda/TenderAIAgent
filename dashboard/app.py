"""
Tender Agent Dashboard
"""

import streamlit as st
from streamlit_autorefresh import st_autorefresh
import os
import json
import html
import hmac
import hashlib
import time
import pandas as pd
import altair as alt
from rapidfuzz import fuzz
from database.db import (
    get_all_tenders, update_tender_status,
    update_tender_pdfs, get_all_ceo_searches, get_learned_patterns,
    get_kppp_criteria_list, get_gem_last_searched_at,
    get_department_filters, add_department_filter, delete_department_filter,
    delete_tender, delete_pattern, delete_ceo_search, clear_tender_analysis,
    get_stats as _get_stats,
    get_department_counts as _get_department_counts,
    get_daily_trend as _get_daily_trend,
    get_company_turnover_list, add_company_turnover, update_company_turnover, delete_company_turnover,
    get_company_credentials_list, add_company_credential, update_company_credential, delete_company_credential,
    get_partners_list, add_partner, update_partner, delete_partner,
    get_partner_credentials_list, add_partner_credential, update_partner_credential, delete_partner_credential,
    get_project_references_list, add_project_reference, update_project_reference, delete_project_reference,
    get_project_quantities_list, add_project_quantity, update_project_quantity, delete_project_quantity,
    get_relevant_keywords_list, delete_relevant_keyword,
    get_relevant_tenders_list,
    verify_user_login, create_user, update_user_password,
    update_last_active, get_all_users, update_user, delete_user, count_admins,
    get_user_by_username,
)


# Short-TTL caches for the chart queries — these get re-run on every
# full-page rerun (including the one a Trend-chart drill-down click forces),
# but the underlying counts don't meaningfully change within a few seconds,
# so this avoids redundant DB round-trips without ever showing stale-feeling
# data (the background agent's writes still show up within the TTL window).
# get_stats() is deliberately NOT cached here — it drives the 5 stat cards
# (Total/New/Saved/...), and caching it meant Save/Unsave/Delete and newly
# found tenders didn't reflect there until the cache happened to expire,
# which read as the cards being stuck/laggy. It's just a handful of cheap
# COUNT(*) queries, so there's no real cost to always running it fresh.
get_stats = _get_stats


@st.cache_data(ttl=4)
def get_department_counts(portal: str, top_n: int = 5):
    return _get_department_counts(portal, top_n)


@st.cache_data(ttl=4)
def get_daily_trend(portal: str, days: int = 7):
    return _get_daily_trend(portal, days)


# ── ADMIN: generic insert/update/query CRUD section ────────
# One reusable renderer instead of five near-identical blocks — the manager
# asked that Chipscape's own credentials/turnover and any partner's
# credentials live as real, editable database records ("if we got any new
# certificate than it'll easily we can add that or update"). Each call below
# just supplies the field list and the matching db.py functions.
def _admin_crud_section(key_prefix, get_list_fn, add_fn, update_fn, delete_fn, fields, row_title_fn, entity_label="Record"):
    """
    fields: list of dicts, each {"key", "label", "type": "text"|"number"|"textarea"|"select",
            "options": [...] (for select)}.
    add_fn(**{field key: value}) / update_fn(row_id, **{field key: value}) / delete_fn(row_id)

    The Modify action sits inside the table, one "✏️" button per row — not
    a separate selection step. Add is the one button below the whole table.
    Both open as a popup dialog, not an inline form taking up page space.
    """
    rows = get_list_fn()

    @st.dialog(f"➕ Add {entity_label}")
    def _add_dialog():
        values = {}
        for f in fields:
            values[f["key"]] = _admin_field_input(f, default=None, key=f"{key_prefix}_dlg_add_{f['key']}")
        if st.button("➕ Add", type="primary", use_container_width=True, key=f"{key_prefix}_dlg_add_submit"):
            required_ok = all(values.get(f["key"]) not in (None, "") for f in fields if f.get("required"))
            if not required_ok:
                st.warning("Please fill in all required fields.")
            else:
                add_fn(**values)
                st.rerun()

    @st.dialog(f"✏️ Edit {entity_label}")
    def _edit_dialog(row):
        st.caption(row_title_fn(row))
        values = {}
        for f in fields:
            values[f["key"]] = _admin_field_input(f, default=row.get(f["key"]), key=f"{key_prefix}_dlg_edit_{row['id']}_{f['key']}")
        col_save, col_del = st.columns(2)
        if col_save.button("💾 Update", type="primary", use_container_width=True, key=f"{key_prefix}_dlg_update"):
            update_fn(row["id"], **values)
            st.rerun()
        if col_del.button("🗑️ Delete", use_container_width=True, key=f"{key_prefix}_dlg_delete"):
            delete_fn(row["id"])
            st.rerun()

    if rows:
        col_widths = [2] * len(fields) + [1]
        header_cols = st.columns(col_widths)
        for col, f in zip(header_cols[:-1], fields):
            col.markdown(f"**{f['label']}**")
        header_cols[-1].markdown("**Action**")
        st.markdown("<hr style='margin:2px 0 8px 0;'>", unsafe_allow_html=True)

        for r in rows:
            row_cols = st.columns(col_widths)
            for col, f in zip(row_cols[:-1], fields):
                value = r.get(f["key"])
                col.write(value if value not in (None, "") else "—")
            if row_cols[-1].button("✏️", key=f"{key_prefix}_modify_{r['id']}", help=f"Modify {row_title_fn(r)}"):
                _edit_dialog(r)
    else:
        st.caption("No records yet — add the first one below.")

    st.markdown("<div style='margin-top:10px;'></div>", unsafe_allow_html=True)
    if st.button(f"➕ Add {entity_label}", use_container_width=True, key=f"{key_prefix}_add_btn"):
        _add_dialog()


def _admin_field_input(f, default, key):
    ftype = f.get("type", "text")
    label = f["label"]
    if ftype == "number":
        return st.number_input(label, value=float(default) if default not in (None, "") else 0.0, key=key)
    if ftype == "textarea":
        return st.text_area(label, value=default or "", key=key)
    if ftype == "select":
        options = f["options"]
        index = options.index(default) if default in options else 0
        return st.selectbox(label, options, index=index, key=key)
    return st.text_input(label, value=default or "", key=key, placeholder=f.get("placeholder", ""))


def render_admin_page():
    # A little breathing room above the heading — the page's tight
    # block-container top padding (set globally for the main dashboard)
    # otherwise crops the top of this heading's tall letters.
    st.markdown("<div style='margin-top:18px;'></div>", unsafe_allow_html=True)
    if st.session_state.get("user_create_success"):
        st.success(st.session_state.user_create_success)
        st.session_state.user_create_success = ""
    header_col, create_user_col = st.columns([5, 1])
    with header_col:
        st.markdown("## 🛠️ Admin — Company Profile & Credentials")
    with create_user_col:
        st.markdown("<div style='margin-top:22px;'></div>", unsafe_allow_html=True)
        if st.button("➕ Create User", key="admin_create_user_btn", use_container_width=True):
            st.session_state.show_admin = False
            st.session_state.show_create_user = True
            st.rerun()
    st.caption(
        "Chipscape's turnover, credentials, past-project references and any "
        "subcontractor/system-integrator partner credentials — all stored as "
        "database records here so a new certificate or next year's turnover "
        "can be added or updated any time, instead of editing a spreadsheet."
    )
    if st.button("← Back to Dashboard", key="admin_back_btn"):
        st.session_state.show_admin = False
        st.rerun()

    st.markdown("---")

    # Pushes the "Users" tab to the far right of the tab row, separate from
    # the other (company-profile) tabs which stay grouped on the left — this
    # is the only st.tabs() in the app, so the selector is safe to be global.
    st.markdown(
        """
        <style>
        div[data-testid="stTabs"] { width: 100% !important; }
        div[data-testid="stTabs"] div[role="tablist"] { width: 100% !important; }
        div[data-testid="stTabs"] div[role="tablist"] > div[data-testid="stTab"]:last-child {
            margin-left: auto !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    tab_turnover, tab_creds, tab_partners, tab_projects, tab_quantities, tab_users, tab_db_table = st.tabs([
        "💰 Turnover", "📜 Chipscape Credentials", "🤝 Partner Credentials",
        "📁 Project References", "📦 Project Quantities", "👤 Users", "🗄️ DB Table"
    ])

    with tab_turnover:
        _admin_crud_section(
            "turnover", get_company_turnover_list, add_company_turnover, update_company_turnover, delete_company_turnover,
            fields=[
                {"key": "financial_year", "label": "Financial Year", "type": "text", "placeholder": "e.g. 2026-27", "required": True},
                {"key": "turnover", "label": "Turnover", "type": "number", "required": True},
                {"key": "total_with_gst", "label": "Total incl. GST", "type": "number"},
                {"key": "status", "label": "Status", "type": "select", "options": ["Audited", "Unaudited", "Provisional"]},
            ],
            row_title_fn=lambda r: f"{r['financial_year']} — Rs {r['turnover']:,.0f}" if r.get("turnover") is not None else r['financial_year'],
            entity_label="Turnover Year",
        )

    with tab_creds:
        # db.py's add/update_company_credential take a "ctype" keyword (not
        # "type", which would shadow the builtin) — bridge the field key here.
        def _add_company_cred(**kw):
            kw["ctype"] = kw.pop("type")
            add_company_credential(**kw)

        def _update_company_cred(rid, **kw):
            kw["ctype"] = kw.pop("type")
            update_company_credential(rid, **kw)

        _admin_crud_section(
            "company_cred", get_company_credentials_list,
            _add_company_cred, _update_company_cred, delete_company_credential,
            fields=[
                {"key": "name", "label": "Certificate/Registration Name", "type": "text", "required": True},
                {"key": "type", "label": "Type", "type": "text", "placeholder": "e.g. Certification, License, Registration"},
                {"key": "issuing_authority", "label": "Issuing Authority", "type": "text"},
                {"key": "certificate_no", "label": "Certificate No.", "type": "text"},
                {"key": "issue_date", "label": "Issue Date", "type": "text", "placeholder": "DD-MM-YYYY"},
                {"key": "expiry_date", "label": "Expiry Date", "type": "text", "placeholder": "DD-MM-YYYY or 'No expiry'"},
                {"key": "status", "label": "Status", "type": "select", "options": ["Valid", "Expired"]},
            ],
            row_title_fn=lambda r: f"{r['name']} (expires {r.get('expiry_date') or 'N/A'})",
            entity_label="Credential",
        )

    with tab_partners:
        st.markdown("#### Partners")
        partners = get_partners_list()
        _admin_crud_section(
            "partner", get_partners_list, add_partner, update_partner, delete_partner,
            fields=[
                {"key": "name", "label": "Partner Name", "type": "text", "required": True},
                {"key": "role", "label": "Role", "type": "text", "placeholder": "e.g. Subcontractor, System Integrator, OEM Dealer"},
                {"key": "notes", "label": "Notes", "type": "textarea"},
            ],
            row_title_fn=lambda r: f"{r['name']} ({r.get('role') or 'Partner'})",
            entity_label="Partner",
        )

        st.markdown("---")
        st.markdown("#### Partner Credentials")
        partners = get_partners_list()
        if not partners:
            st.caption("Add a partner above first, then its credentials here.")
        else:
            partner_names = {p["id"]: p["name"] for p in partners}
            selected_partner_id = st.selectbox(
                "Select Partner", options=list(partner_names.keys()),
                format_func=lambda pid: partner_names[pid], key="admin_partner_select"
            )

            # Same "type" -> "ctype" bridge as the Chipscape credentials tab.
            def _add_partner_cred(**kw):
                kw["ctype"] = kw.pop("type")
                add_partner_credential(selected_partner_id, **kw)

            def _update_partner_cred(rid, **kw):
                kw["ctype"] = kw.pop("type")
                update_partner_credential(rid, **kw)

            _admin_crud_section(
                f"partner_cred_{selected_partner_id}",
                lambda: get_partner_credentials_list(selected_partner_id),
                _add_partner_cred, _update_partner_cred, delete_partner_credential,
                fields=[
                    {"key": "name", "label": "Certificate/Authorization Name", "type": "text", "required": True},
                    {"key": "type", "label": "Type", "type": "text"},
                    {"key": "issuing_authority", "label": "Issuing Authority", "type": "text"},
                    {"key": "certificate_no", "label": "Certificate No.", "type": "text"},
                    {"key": "issue_date", "label": "Issue Date", "type": "text", "placeholder": "DD-MM-YYYY"},
                    {"key": "expiry_date", "label": "Expiry Date", "type": "text", "placeholder": "DD-MM-YYYY or 'No expiry'"},
                    {"key": "status", "label": "Status", "type": "select", "options": ["Valid", "Expired"]},
                ],
                row_title_fn=lambda r: f"{r['name']} (expires {r.get('expiry_date') or 'N/A'})",
                entity_label="Partner Credential",
            )

    with tab_projects:
        _admin_crud_section(
            "project_ref", get_project_references_list,
            lambda **kw: add_project_reference(kw),
            lambda rid, **kw: update_project_reference(rid, kw),
            delete_project_reference,
            fields=[
                {"key": "project_name", "label": "Project Name", "type": "text", "required": True},
                {"key": "customer", "label": "Customer", "type": "text"},
                {"key": "department_type", "label": "Department Type", "type": "text"},
                {"key": "location", "label": "Location", "type": "text"},
                {"key": "solution_category", "label": "Solution Category", "type": "text"},
                {"key": "eligibility_tags", "label": "Eligibility Tags", "type": "text"},
                {"key": "project_scope", "label": "Project Scope", "type": "textarea"},
                {"key": "project_value", "label": "Project Value", "type": "number"},
                {"key": "government_project", "label": "Government Project", "type": "select", "options": ["Yes", "No"]},
                {"key": "financial_year", "label": "Financial Year", "type": "text", "placeholder": "e.g. 2025-26"},
                {"key": "project_status", "label": "Project Status", "type": "text", "placeholder": "e.g. Completed, Ongoing"},
            ],
            row_title_fn=lambda r: f"{r['project_name']} ({r.get('customer') or 'N/A'})",
            entity_label="Project Reference",
        )

    with tab_quantities:
        _admin_crud_section(
            "project_qty", get_project_quantities_list,
            lambda **kw: add_project_quantity(kw),
            lambda rid, **kw: update_project_quantity(rid, kw),
            delete_project_quantity,
            fields=[
                {"key": "project_name", "label": "Project Name", "type": "text", "required": True},
                {"key": "solution_category", "label": "Solution Category", "type": "text"},
                {"key": "quantity_category", "label": "Quantity Category", "type": "text", "placeholder": "e.g. Cameras Supplied"},
                {"key": "quantity_executed", "label": "Quantity", "type": "text"},
                {"key": "unit", "label": "Unit", "type": "text", "placeholder": "e.g. Units, Nodes"},
                {"key": "customer", "label": "Customer", "type": "text"},
            ],
            row_title_fn=lambda r: f"{r['project_name']} — {r.get('quantity_executed') or ''} {r.get('unit') or ''}",
            entity_label="Quantity Record",
        )

    with tab_users:
        users = get_all_users()

        @st.dialog("✏️ Edit User")
        def _edit_user_dialog(u):
            st.caption(f"{u['username']} ({u['role'].capitalize()})")
            edit_username = st.text_input("Username", value=u["username"], key=f"user_dlg_edit_{u['id']}_username")
            edit_role = st.selectbox(
                "Role", ["Regular", "Admin"],
                index=1 if u["role"] == "admin" else 0,
                key=f"user_dlg_edit_{u['id']}_role",
            )
            edit_new_password = st.text_input(
                "New Password", value="", type="password",
                placeholder="Leave blank to keep the current password",
                key=f"user_dlg_edit_{u['id']}_password",
            )
            col_save, col_del = st.columns(2)
            if col_save.button("💾 Update", type="primary", use_container_width=True, key=f"user_dlg_update_{u['id']}"):
                if not edit_username.strip():
                    st.warning("Username cannot be empty.")
                elif u["role"] == "admin" and edit_role == "Regular" and count_admins() <= 1:
                    st.warning("Can't demote the last remaining admin — create another admin first.")
                else:
                    ok = update_user(u["id"], edit_username.strip(), edit_role.lower(), edit_new_password or None)
                    if not ok:
                        st.warning(f"Username \"{edit_username.strip()}\" is already taken.")
                    else:
                        if u["username"] == st.session_state.username:
                            st.session_state.username = edit_username.strip()
                            st.session_state.user_role = edit_role.lower()
                        st.rerun()
            if col_del.button("🗑️ Delete", use_container_width=True, key=f"user_dlg_delete_{u['id']}"):
                if u["username"] == st.session_state.username:
                    st.warning("You can't delete the account you're currently logged in as.")
                elif u["role"] == "admin" and count_admins() <= 1:
                    st.warning("Can't delete the last remaining admin.")
                else:
                    delete_user(u["id"])
                    st.rerun()

        if not users:
            st.info("No users found.")
        else:
            col_widths = [2, 2, 2, 2, 1]
            header_cols = st.columns(col_widths)
            for col, label in zip(header_cols, ["Username", "Role", "Last Active", "Created At", "Action"]):
                col.markdown(f"**{label}**")
            st.markdown("<hr style='margin:2px 0 8px 0;'>", unsafe_allow_html=True)

            for u in users:
                row_cols = st.columns(col_widths)
                row_cols[0].write(u["username"])
                row_cols[1].write(u["role"].capitalize())
                row_cols[2].write(u["last_active"] or "Never logged in")
                row_cols[3].write(u["created_at"])
                if row_cols[4].button("✏️", key=f"user_modify_{u['id']}", help=f"Edit {u['username']}"):
                    _edit_user_dialog(u)

    with tab_db_table:
        # Read-only view of exactly what the agent auto-searches with on
        # every cycle — KPPP's saved category/entity/tender-name
        # combinations (kppp_search_criteria, what _run_kppp_criteria_search
        # replays as-is) and GeM's own raw saved keywords (ceo_searches,
        # what _scheduled_search/_run_full_catalog_search build the GeM
        # search list from — see agent/orchestrator.py). Pure SELECT +
        # display, doesn't touch or call anything that writes to the
        # database.
        title_col, dropdown_col = st.columns([4, 2])
        with title_col:
            st.markdown("#### What the agent searches with")
            st.caption("Every saved keyword/filter combination the agent replays on each auto-search cycle.")
        with dropdown_col:
            db_table_portal = st.selectbox(
                "Portal", ["KPPP (KA)", "GeM"], key="admin_db_table_portal",
                label_visibility="collapsed",
            )

        if db_table_portal == "KPPP (KA)":
            kppp_rows = get_kppp_criteria_list()
            if not kppp_rows:
                st.info("No saved KPPP search filters yet.")
            else:
                df = pd.DataFrame([{
                    "Category": r.get("category") or "",
                    "Procuring Entity": r.get("procuring_entity") or "",
                    "Tender Name": r.get("tender_name") or "",
                    "Tender Number": r.get("tender_number") or "",
                    "Tender Type": r.get("tender_type") or "",
                    "From Date": r.get("from_date") or "",
                    "To Date": r.get("to_date") or "",
                    "Last Searched At": r.get("last_searched_at") or "Never",
                } for r in kppp_rows])
                st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            gem_rows = [p for p in get_learned_patterns() if (p.get("portal") or "GeM") == "GeM"]
            if not gem_rows:
                st.info("No saved GeM keyword patterns yet.")
            else:
                def _kw_list(r):
                    try:
                        return ", ".join(json.loads(r["keywords"]))
                    except Exception:
                        return r.get("keywords") or ""
                def _last_searched(r):
                    # One keyword per row now (see the cleanup above), so
                    # its own search-log entry is the real "last searched"
                    # time the agent actually uses as this keyword's next
                    # search cutoff (see get_gem_last_searched_at).
                    kw = _kw_list(r).strip()
                    return get_gem_last_searched_at(kw) or "Never"
                df = pd.DataFrame([{
                    "Category": r.get("category") or "",
                    "Weight": r.get("weight"),
                    "Keywords": _kw_list(r),
                    "Last Searched At": _last_searched(r),
                } for r in gem_rows])
                st.dataframe(df, use_container_width=True, hide_index=True)


from agent.orchestrator import (
    start_agent, stop_agent, get_state,
    get_log, trigger_manual_search,
    add_relevant_keyword_and_scan, update_relevant_keyword_and_rescan,
)
from analysis.analyzer import analyze_tender


def render_relevant_keywords_page():
    """Full-screen page (sidebar 'Relevant Tenders Keyword' entry) for
    managing the standing keyword list. Adding or editing a keyword here
    kicks off a one-time full scan of the Total Tenders table in the
    background (see agent/orchestrator.py's run_relevant_keyword_matching),
    so it also catches tenders already in the database before the keyword
    existed — not just tenders fetched from now on."""
    st.markdown("<div style='margin-top:18px;'></div>", unsafe_allow_html=True)
    st.markdown("## 🎯 Relevant Tenders — Keywords")
    st.caption(
        "Keywords here are matched (fuzzy search) against tenders already saved in "
        "Total Tenders, and every match gets flagged into the Relevant Tenders card "
        "with full L2 analysis (documents + analysis PDF) run automatically. "
        "New tenders fetched on every Start Agent run are checked against this list too."
    )
    if st.button("← Back to Dashboard", key="relkw_back_btn"):
        st.session_state.show_relevant_keywords = False
        st.rerun()

    st.markdown("---")

    _admin_crud_section(
        "relevant_kw", get_relevant_keywords_list,
        lambda **kw: add_relevant_keyword_and_scan(kw["keyword"]),
        lambda rid, **kw: update_relevant_keyword_and_rescan(rid, kw["keyword"]),
        delete_relevant_keyword,
        fields=[
            {"key": "keyword", "label": "Keyword", "type": "text", "required": True, "placeholder": "e.g. cctv camera"},
        ],
        row_title_fn=lambda r: r["keyword"],
        entity_label="Keyword",
    )


def render_patterns_learned_page():
    """Full-screen page (sidebar 'Patterns Learned' entry) — same content
    that used to live in a sidebar dropdown, moved here for more room and
    consistency with Admin/Relevant Tenders Keyword."""
    st.markdown("<div style='margin-top:18px;'></div>", unsafe_allow_html=True)
    st.markdown("## 🧠 Patterns Learned")
    st.caption("Categories and keyword weights the agent has learned from CEO search activity.")
    if st.button("← Back to Dashboard", key="patterns_back_btn"):
        st.session_state.show_patterns_learned = False
        st.rerun()

    st.markdown("---")

    patterns = get_learned_patterns()
    if patterns:
        for p in patterns:
            try:
                kws = json.loads(p['keywords'])
            except Exception:
                kws = []
            pattern_portal = p['portal'] if p['portal'] else "GeM"
            portal_badge = "🔵 GeM" if pattern_portal == "GeM" else "🟠 KPPP (KA)"
            col_p, col_d = st.columns([4, 1])
            with col_p:
                st.markdown(f"""
                <div class='tender-card'>
                    <div class='tender-title'>📂 {p['category']} <span style="font-size:0.75em;font-weight:500;color:#64748B;">— {portal_badge}</span></div>
                    <div class='tender-meta'>Weight: {p['weight']} | Keywords: {', '.join(kws[:5])}</div>
                </div>
                """, unsafe_allow_html=True)
            with col_d:
                if st.button("🗑️ Delete", key=f"page_del_pattern_{p['id']}", help="Delete this pattern"):
                    delete_pattern(p['id'])
                    st.rerun()
    else:
        st.info("No patterns learned yet.")


def render_search_history_page():
    """Full-screen page (sidebar 'Search History' entry) — same content
    that used to live in a sidebar dropdown, moved here for more room and
    consistency with Admin/Relevant Tenders Keyword."""
    st.markdown("<div style='margin-top:18px;'></div>", unsafe_allow_html=True)
    st.markdown("## 🔎 Search History")
    st.caption("Every keyword the CEO has searched for — the agent keeps re-running these automatically.")
    if st.button("← Back to Dashboard", key="searchhist_back_btn"):
        st.session_state.show_search_history = False
        st.rerun()

    st.markdown("---")

    searches = get_all_ceo_searches()
    if searches:
        for s in searches:
            col_s, col_d = st.columns([4, 1])
            with col_s:
                st.markdown(f"""
                <div class='tender-card'>
                    <div class='tender-title'>🔍 {s['keyword']}</div>
                    <div class='tender-meta'>Portal: {s['portal']} | Searched {s['frequency']} times | Last: {s['searched_at'][:16]}</div>
                </div>
                """, unsafe_allow_html=True)
            with col_d:
                if st.button("🗑️ Delete", key=f"page_del_search_{s['id']}", help="Delete this search"):
                    delete_ceo_search(s['id'])
                    st.success(f"Deleted '{s['keyword']}' — the agent will stop searching for it.")
                    st.rerun()
    else:
        st.info("No searches yet.")


from portals.data.kppp_options import (
    CATEGORIES as KPPP_CATEGORIES,
    PROCURING_ENTITIES as KPPP_ENTITIES,
)

PORTAL_FILTER_OPTIONS = ["🟠 KPPP (KA)", "🔵 GeM"]
PORTAL_FILTER_DEFAULT = "🟠 KPPP (KA)"
PORTAL_FILTER_MAP = {
    "🔵 GeM": "GeM",
    "🟠 KPPP (KA)": "KPPP",
}


_PORTAL_VALUE_TO_LABEL = {v: k for k, v in PORTAL_FILTER_MAP.items()}


def _sync_main_portal_dropdown(portal_plain: str):
    """Drilling into a chart (Tenders by Department / Tenders Trend) shows
    tenders for whichever portal that CHART's own toggle was set to, which
    can differ from the main table's portal dropdown above it. portal_filter_value
    is the single source of truth the main dropdown's display is computed
    from on every render (see its index= below), so just updating it here
    is enough to keep the dropdown and the rows it's showing in agreement."""
    st.session_state.portal_filter_value = portal_plain


# Quantity buckets for the table's Filters popover — half-open ranges
# (lower bound inclusive, upper bound exclusive) so a tender can only ever
# land in exactly one bucket, even at a boundary value like 50 or 100.
QUANTITY_FILTER_OPTIONS = ["All Quantities", "Under 10", "10-50", "50-100", "100-200", "300+"]
QUANTITY_FILTER_RANGES = {
    "Under 10": (None, 10),
    "10-50": (10, 50),
    "50-100": (50, 100),
    "100-200": (100, 200),
    "300+": (300, None),
}


def _apply_quantity_filter(tenders, bucket):
    """Filters the table's tender list down to one quantity_count bucket —
    see QUANTITY_FILTER_RANGES. A tender with no parsed quantity_count
    (quantity wasn't a clean number at L1 scrape time) is excluded from
    every specific bucket, same as it would be from any numeric range
    filter, but still shows up under "All Quantities"."""
    if bucket == "All Quantities" or bucket not in QUANTITY_FILTER_RANGES:
        return tenders
    low, high = QUANTITY_FILTER_RANGES[bucket]
    result = []
    for t in tenders:
        qty = t.get("quantity_count")
        if qty is None:
            continue
        if low is not None and qty < low:
            continue
        if high is not None and qty >= high:
            continue
        result.append(t)
    return result


def _apply_department_filter(tenders, portal, selected_label):
    """Filters the table's tender list down to one department — KPPP
    matches via that department's saved match_terms (short code + known
    full name, see department_filters seed data), GeM matches the literal
    department string the dropdown was built from (get_department_counts),
    both case-insensitive substring checks against tender["department"]."""
    if selected_label == "All Departments":
        return tenders
    if portal == "KPPP":
        match_terms = None
        for d in get_department_filters("KPPP"):
            if d["label"] == selected_label:
                match_terms = d["match_terms"]
                break
        if not match_terms:
            return tenders
        terms_lower = [t.lower() for t in match_terms]
        return [
            t for t in tenders
            if any(term in (t.get("department") or "").lower() for term in terms_lower)
        ]
    else:
        target = selected_label.lower()
        return [t for t in tenders if target in (t.get("department") or "").lower()]


def _format_tender_price(numeric_value):
    """Formats a raw rupee amount (from L2 document analysis) in
    Lakh/Crore, the way these figures are normally written in Indian
    tender documents — e.g. 373660000 -> "₹37.37 Cr"."""
    amount = float(numeric_value)
    if amount >= 1_00_00_000:
        return f"₹{amount / 1_00_00_000:.2f} Cr"
    if amount >= 1_00_000:
        return f"₹{amount / 1_00_000:.2f} Lakhs"
    return f"₹{amount:,.0f}"


def fuzzy_filter_tenders(tenders, query, threshold=80):
    """Filter tenders by a substring/fuzzy match over the visible table columns.

    Scores each field separately (not one concatenated string) and takes the
    best per-tender score — concatenating first let a short query "get lucky"
    against the combined length of every field (e.g. a long department name),
    surfacing unrelated tenders as fuzzy matches.

    Fuzzy scoring (fuzz.partial_ratio) only runs against free-text fields
    (title, department) — structured/templated fields (tender_number,
    quantity, published_date, deadline) are exact-substring-only. Every GeM
    bid number shares the same "GEM/2026/B/XXXXXXX" template, so
    partial_ratio scores ~85-90+ between ANY two of them regardless of the
    actual digits — fuzzy-matching tender_number made an exact bid-number
    search return every tender in the table instead of the one match.
    """
    query = (query or "").strip().lower()
    if not query:
        return tenders
    exact_only_fields = ("tender_number", "quantity", "published_date", "deadline")
    fuzzy_fields = ("title", "department")
    out = []
    for t in tenders:
        exact_values = [str(t.get(f, "")).lower() for f in exact_only_fields]
        fuzzy_values = [str(t.get(f, "")).lower() for f in fuzzy_fields]
        if any(query in v for v in exact_values) or \
                any(query in v for v in fuzzy_values) or \
                max(fuzz.partial_ratio(query, v) for v in fuzzy_values) >= threshold:
            out.append(t)
    return out

st.set_page_config(
    page_title="Tender AI Agent",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── "STAY LOGGED IN" COOKIE ───────────────────────────────
# st.session_state alone doesn't survive a full browser reload (a fresh
# page load gets a brand-new Streamlit session, wiping it) — so without
# this, every reload sent an already-logged-in CEO back to the login
# screen. A signed cookie fixes that: once set, it's read back on every
# future page load/session and silently re-authenticates before the login
# form would even render, exactly like how most apps (e.g. Instagram) stay
# signed in until you explicitly log out, across closing/reopening/
# reloading the app. The cookie only ever carries the username + an
# expiry, HMAC-signed with SESSION_SECRET (.env) so it can't be forged or
# edited client-side — it is NOT a password or anything sensitive, just
# "the browser that holds this exact signed value already proved who it
# is once."
_SESSION_SECRET = os.getenv("SESSION_SECRET", "")
_SESSION_COOKIE_NAME = "chipscape_session"
_SESSION_MAX_AGE_SECONDS = 365 * 24 * 60 * 60  # 1 year — "until logout", in practice


def _make_session_token(username: str) -> str:
    expiry = int(time.time()) + _SESSION_MAX_AGE_SECONDS
    payload = f"{username}:{expiry}"
    sig = hmac.new(_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def _verify_session_token(token: str):
    """Returns the username if the token is validly signed and not expired,
    else None. Never trusts the token's username without checking the
    signature first — that's the whole point of signing it."""
    if not token or not _SESSION_SECRET:
        return None
    parts = token.split(":")
    if len(parts) != 3:
        return None
    username, expiry_str, sig = parts
    payload = f"{username}:{expiry_str}"
    expected_sig = hmac.new(_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        if int(expiry_str) < int(time.time()):
            return None
    except ValueError:
        return None
    return username


def _set_session_cookie(token: str):
    # document.cookie set from inside an iframe's own document would be
    # scoped to that iframe's (different/opaque) origin and never reach the
    # main page's requests — reaching into window.parent.document, same
    # trick already used elsewhere in this file (the chat auto-scroll),
    # is what actually sets it on the app's own origin.
    st.iframe(f"""
    <!-- nonce:{time.time()} -->
    <script>
    (function() {{
        const expires = new Date(Date.now() + {_SESSION_MAX_AGE_SECONDS * 1000}).toUTCString();
        window.parent.document.cookie = "{_SESSION_COOKIE_NAME}={token}; expires=" + expires + "; path=/; SameSite=Lax";
    }})();
    </script>
    """, height=1)


def _clear_session_cookie():
    st.iframe(f"""
    <!-- nonce:{time.time()} -->
    <script>
    (function() {{
        window.parent.document.cookie = "{_SESSION_COOKIE_NAME}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; SameSite=Lax";
    }})();
    </script>
    """, height=1)


# ── LOGIN GATE ────────────────────────────────────────────
# Added ahead of moving this app onto a server other people/devices can
# reach — without this, anyone with the URL would have full access, no
# password. Accounts live in the app_users DB table (username/password
# hash/role) — the very first account is seeded once from .env's
# LOGIN_USERNAME/LOGIN_PASSWORD (see database/db.py's init_db()), but every
# login after that checks the DB, not .env. 'admin' role sees the Admin
# section and can create new accounts; 'regular' sees everything else.
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "username" not in st.session_state:
    st.session_state.username = ""
if "user_role" not in st.session_state:
    st.session_state.user_role = ""

# A fresh session (new tab, reload, browser reopened) starts unauthenticated
# above — before falling through to the login form, check for a still-valid
# "stay logged in" cookie from a previous login and silently restore the
# session from it instead.
if not st.session_state.authenticated:
    _cookie_username = _verify_session_token(st.context.cookies.get(_SESSION_COOKIE_NAME, ""))
    if _cookie_username:
        _cookie_user = get_user_by_username(_cookie_username)
        if _cookie_user:
            st.session_state.authenticated = True
            st.session_state.username = _cookie_user["username"]
            st.session_state.user_role = _cookie_user["role"]

if not st.session_state.authenticated:

    import base64 as _b64
    _logo_path = os.path.join(os.path.dirname(__file__), "assets", "chipscape_logo_transparent.png")
    try:
        with open(_logo_path, "rb") as _f:
            _logo_b64 = _b64.b64encode(_f.read()).decode()
    except Exception:
        _logo_b64 = ""

    st.markdown(
        f"""
        <style>

        #MainMenu {{ visibility: hidden; }}
        footer {{ visibility: hidden; }}
        header {{ visibility: hidden; }}
        [data-testid="stSidebar"] {{ display: none; }}

        .stApp {{
            background:
                radial-gradient(circle at 12% 18%, rgba(59,130,246,0.22), transparent 38%),
                radial-gradient(circle at 88% 82%, rgba(37,99,235,0.18), transparent 38%),
                linear-gradient(180deg, #060810 0%, #0A0E1A 100%);
            min-height: 100vh;
        }}
        .block-container {{ max-width: 100% !important; padding: 0 !important; }}

        .st-key-login_card_dark {{
            width: min(92%, 420px);
            margin: 60px auto 0;
            background: rgba(17, 21, 36, 0.85);
            border: 1px solid rgba(148, 163, 184, 0.14);
            border-radius: 20px;
            padding: 34px 30px 22px;
            box-shadow: 0 25px 70px rgba(0,0,0,0.55);
            backdrop-filter: blur(14px);
        }}

        .login-logo {{ display: block; margin: 0 auto 14px; height: 56px; width: auto; }}

        .login-title-dark {{ text-align: center; font-size: 21px; font-weight: 700; color: #E7ECF5; margin-bottom: 2px; }}
        .login-title-dark .accent {{
            background: linear-gradient(90deg, #48bdf4, #4d8cf5, #7652ef);
            -webkit-background-clip: text; background-clip: text; color: transparent;
        }}
        .login-subtitle-dark {{ text-align: center; font-size: 12px; color: #7C8AA5; margin-bottom: 26px; }}
        .login-footer-dark {{ text-align: center; font-size: 12px; color: #8B96B5; margin: 18px auto 4px; }}
        .login-contact-dark {{ text-align: center; font-size: 12px; color: #60A5FA; margin: 0 auto 40px; }}

        .st-key-login_card_dark [data-testid="stForm"] {{
            border: none !important; background: transparent !important; padding: 0 !important; box-shadow: none !important;
        }}
        .st-key-login_card_dark [data-testid="stForm"] label {{
            color: #9AA6C3 !important; font-size: 13px !important; font-weight: 500 !important;
        }}
        .st-key-login_card_dark [data-testid="stTextInputRootElement"] {{
            background: rgba(255,255,255,0.04) !important;
            border-radius: 10px !important;
            border: 1px solid rgba(148,163,184,0.20) !important;
        }}
        .st-key-login_card_dark input {{
            height: 44px !important; border-radius: 10px !important;
            border: none !important;
            background: transparent !important;
            color: #E7ECF5 !important; font-size: 14px !important;
            padding-left: 38px !important; box-shadow: none !important;
        }}
        .st-key-login_card_dark [data-testid="stTextInputRootElement"]:focus-within {{
            border-color: #3B82F6 !important; box-shadow: 0 0 0 2px rgba(59,130,246,0.20) !important;
        }}
        .st-key-login_card_dark input::placeholder {{ color: #5B6580 !important; }}
        .st-key-login_card_dark [data-testid="stTextInput"] {{ margin-bottom: 10px; position: relative; }}
        .st-key-login_card_dark [data-testid="stTextInput"]::before {{
            content: ""; position: absolute; left: 12px; top: 33px; width: 16px; height: 16px;
            z-index: 2; opacity: 0.55;
            background-repeat: no-repeat; background-position: center; background-size: contain;
        }}
        .st-key-login_user_field [data-testid="stTextInput"]::before {{
            background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2394A3B8' stroke-width='2'><circle cx='12' cy='8' r='4'/><path d='M4 21c0-4 4-6 8-6s8 2 8 6'/></svg>");
        }}
        .st-key-login_pass_field [data-testid="stTextInput"]::before {{
            background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2394A3B8' stroke-width='2'><rect x='4' y='11' width='16' height='9' rx='2'/><path d='M8 11V7a4 4 0 0 1 8 0v4'/></svg>");
        }}

        .st-key-login_pass_field .forgot-link {{
            text-align: right; font-size: 12px; color: #60A5FA; margin-top: -20px;
        }}

        .st-key-login_card_dark [data-testid="stFormSubmitButton"] button {{
            width: 100% !important; height: 46px !important; border: none !important;
            border-radius: 10px !important;
            background: linear-gradient(90deg, #38BDF8, #2563EB) !important;
            color: white !important; font-size: 14px !important; font-weight: 600 !important;
            margin-top: 6px !important;
            transition: transform 0.15s ease, box-shadow 0.15s ease;
        }}
        .st-key-login_card_dark [data-testid="stFormSubmitButton"] button:hover {{
            transform: translateY(-1px); box-shadow: 0 8px 22px rgba(37,99,235,0.40);
        }}

        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.container(key="login_card_dark"):
        st.markdown(
            f"""
            <img class="login-logo" src="data:image/png;base64,{_logo_b64}"/>
            <div class="login-title-dark">Tender <span class="accent">Intelligence</span></div>
            <div class="login-subtitle-dark">AI-Powered Tender Intelligence Platform</div>
            """,
            unsafe_allow_html=True,
        )
        with st.form("login_form"):
            with st.container(key="login_user_field"):
                login_username = st.text_input("Username", placeholder="Enter your username")
            with st.container(key="login_pass_field"):
                login_password = st.text_input("Password", type="password", placeholder="Enter your password")
                st.markdown('<div class="forgot-link">Forgot password?</div>', unsafe_allow_html=True)
            login_submitted = st.form_submit_button("Sign In →", use_container_width=True)
            if login_submitted:
                user = verify_user_login(login_username, login_password)
                if user:
                    st.session_state.authenticated = True
                    st.session_state.username = user["username"]
                    st.session_state.user_role = user["role"]
                    update_last_active(user["username"])
                    _set_session_cookie(_make_session_token(user["username"]))
                    # The iframe above needs a moment to actually mount and
                    # run its script in the browser before st.rerun() tears
                    # it straight back out again - without this, the cookie
                    # never gets set (confirmed live: the iframe was gone by
                    # the very next render, having never had a chance to
                    # execute).
                    time.sleep(0.25)
                    st.rerun()
                else:
                    st.error("Incorrect username or password.")
        st.markdown(
            '<div class="login-footer-dark">ChipScape Security Management System</div>'
            '<div class="login-contact-dark">Contact us: admin@chipscape.com</div>',
            unsafe_allow_html=True,
        )

    st.stop()


# ── CHANGE PASSWORD ──────────────────────────────────────────
def render_change_password_page():
    import base64 as _b64
    _logo_path = os.path.join(os.path.dirname(__file__), "assets", "chipscape_logo_transparent.png")
    try:
        with open(_logo_path, "rb") as _f:
            _logo_b64 = _b64.b64encode(_f.read()).decode()
    except Exception:
        _logo_b64 = ""

    st.markdown(
        f"""
        <style>

        #MainMenu {{ visibility: hidden; }}
        footer {{ visibility: hidden; }}
        header {{ visibility: hidden; }}
        [data-testid="stSidebar"] {{ display: none; }}

        .stApp {{
            background:
                radial-gradient(circle at 12% 18%, rgba(59,130,246,0.22), transparent 38%),
                radial-gradient(circle at 88% 82%, rgba(37,99,235,0.18), transparent 38%),
                linear-gradient(180deg, #060810 0%, #0A0E1A 100%);
            min-height: 100vh;
        }}
        .block-container {{ max-width: 100% !important; padding: 0 !important; }}

        .st-key-pwd_back_wrap {{ width: min(92%, 420px); margin: 24px auto 0; }}
        .st-key-pwd_back_wrap button {{
            background: transparent !important; border: none !important;
            color: #60A5FA !important; font-size: 13px !important;
            box-shadow: none !important; padding: 0 !important;
        }}

        .st-key-pwd_card_dark {{
            width: min(92%, 420px);
            margin: 16px auto 0;
            background: rgba(17, 21, 36, 0.85);
            border: 1px solid rgba(148, 163, 184, 0.14);
            border-radius: 20px;
            padding: 34px 30px 22px;
            box-shadow: 0 25px 70px rgba(0,0,0,0.55);
            backdrop-filter: blur(14px);
        }}

        .login-logo {{ display: block; margin: 0 auto 14px; height: 56px; width: auto; }}

        .login-title-dark {{ text-align: center; font-size: 21px; font-weight: 700; color: #E7ECF5; margin-bottom: 2px; }}
        .login-title-dark .accent {{
            background: linear-gradient(90deg, #48bdf4, #4d8cf5, #7652ef);
            -webkit-background-clip: text; background-clip: text; color: transparent;
        }}
        .login-subtitle-dark {{ text-align: center; font-size: 12px; color: #7C8AA5; margin-bottom: 26px; }}

        .st-key-pwd_card_dark [data-testid="stForm"] {{
            border: none !important; background: transparent !important; padding: 0 !important; box-shadow: none !important;
        }}
        .st-key-pwd_card_dark [data-testid="stForm"] label {{
            color: #9AA6C3 !important; font-size: 13px !important; font-weight: 500 !important;
        }}
        .st-key-pwd_card_dark [data-testid="stTextInputRootElement"] {{
            background: rgba(255,255,255,0.04) !important;
            border-radius: 10px !important;
            border: 1px solid rgba(148,163,184,0.20) !important;
        }}
        .st-key-pwd_card_dark input {{
            height: 44px !important; border-radius: 10px !important;
            border: none !important;
            background: transparent !important;
            color: #E7ECF5 !important; font-size: 14px !important;
            padding-left: 38px !important; box-shadow: none !important;
        }}
        .st-key-pwd_card_dark [data-testid="stTextInputRootElement"]:focus-within {{
            border-color: #3B82F6 !important; box-shadow: 0 0 0 2px rgba(59,130,246,0.20) !important;
        }}
        .st-key-pwd_card_dark input::placeholder {{ color: #5B6580 !important; }}
        .st-key-pwd_card_dark [data-testid="stTextInput"] {{ margin-bottom: 10px; position: relative; }}
        .st-key-pwd_card_dark [data-testid="stTextInput"]::before {{
            content: ""; position: absolute; left: 12px; top: 33px; width: 16px; height: 16px;
            z-index: 2; opacity: 0.55;
            background-repeat: no-repeat; background-position: center; background-size: contain;
            background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2394A3B8' stroke-width='2'><rect x='4' y='11' width='16' height='9' rx='2'/><path d='M8 11V7a4 4 0 0 1 8 0v4'/></svg>");
        }}

        .st-key-pwd_card_dark [data-testid="stFormSubmitButton"] button {{
            width: 100% !important; height: 46px !important; border: none !important;
            border-radius: 10px !important;
            background: linear-gradient(90deg, #38BDF8, #2563EB) !important;
            color: white !important; font-size: 14px !important; font-weight: 600 !important;
            margin-top: 6px !important;
            transition: transform 0.15s ease, box-shadow 0.15s ease;
        }}
        .st-key-pwd_card_dark [data-testid="stFormSubmitButton"] button:hover {{
            transform: translateY(-1px); box-shadow: 0 8px 22px rgba(37,99,235,0.40);
        }}

        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.container(key="pwd_back_wrap"):
        if st.button("← Back to Dashboard", key="pwd_back_btn"):
            st.session_state.show_change_password = False
            st.rerun()

    with st.container(key="pwd_card_dark"):
        st.markdown(
            f"""
            <img class="login-logo" src="data:image/png;base64,{_logo_b64}"/>
            <div class="login-title-dark">Change <span class="accent">Password</span></div>
            <div class="login-subtitle-dark">Update your account password</div>
            """,
            unsafe_allow_html=True,
        )
        with st.form("change_password_form"):
            current_password = st.text_input("Current Password", type="password", placeholder="Enter current password")
            new_password = st.text_input("New Password", type="password", placeholder="Enter new password")
            confirm_password = st.text_input("Confirm Password", type="password", placeholder="Re-enter new password")
            changed = st.form_submit_button("Change Password →", use_container_width=True)
            if changed:
                if not verify_user_login(st.session_state.username, current_password):
                    st.error("Current password is incorrect.")
                elif not new_password:
                    st.error("New password cannot be empty.")
                elif new_password != confirm_password:
                    st.error("New password and confirm password do not match.")
                elif new_password == current_password:
                    st.error("New password must be different from the current password.")
                else:
                    update_user_password(st.session_state.username, new_password)
                    st.session_state.show_change_password = False
                    st.session_state.authenticated = False
                    st.session_state.username = ""
                    st.session_state.user_role = ""
                    _clear_session_cookie()
                    time.sleep(0.25)
                    st.rerun()


# ── CREATE USER (admin-only) ──────────────────────────────────
def render_create_user_page():
    import base64 as _b64
    _logo_path = os.path.join(os.path.dirname(__file__), "assets", "chipscape_logo_transparent.png")
    try:
        with open(_logo_path, "rb") as _f:
            _logo_b64 = _b64.b64encode(_f.read()).decode()
    except Exception:
        _logo_b64 = ""

    st.markdown(
        f"""
        <style>

        #MainMenu {{ visibility: hidden; }}
        footer {{ visibility: hidden; }}
        header {{ visibility: hidden; }}
        [data-testid="stSidebar"] {{ display: none; }}

        .stApp {{
            background:
                radial-gradient(circle at 12% 18%, rgba(59,130,246,0.22), transparent 38%),
                radial-gradient(circle at 88% 82%, rgba(37,99,235,0.18), transparent 38%),
                linear-gradient(180deg, #060810 0%, #0A0E1A 100%);
            min-height: 100vh;
        }}
        .block-container {{ max-width: 100% !important; padding: 0 !important; }}

        .st-key-newuser_back_wrap {{ width: min(92%, 420px); margin: 24px auto 0; }}
        .st-key-newuser_back_wrap button {{
            background: transparent !important; border: none !important;
            color: #60A5FA !important; font-size: 13px !important;
            box-shadow: none !important; padding: 0 !important;
        }}

        .st-key-newuser_card_dark {{
            width: min(92%, 420px);
            margin: 16px auto 0;
            background: rgba(17, 21, 36, 0.85);
            border: 1px solid rgba(148, 163, 184, 0.14);
            border-radius: 20px;
            padding: 34px 30px 22px;
            box-shadow: 0 25px 70px rgba(0,0,0,0.55);
            backdrop-filter: blur(14px);
        }}

        .login-logo {{ display: block; margin: 0 auto 14px; height: 56px; width: auto; }}

        .login-title-dark {{ text-align: center; font-size: 21px; font-weight: 700; color: #E7ECF5; margin-bottom: 2px; }}
        .login-title-dark .accent {{
            background: linear-gradient(90deg, #48bdf4, #4d8cf5, #7652ef);
            -webkit-background-clip: text; background-clip: text; color: transparent;
        }}
        .login-subtitle-dark {{ text-align: center; font-size: 12px; color: #7C8AA5; margin-bottom: 26px; }}

        .st-key-newuser_card_dark [data-testid="stForm"] {{
            border: none !important; background: transparent !important; padding: 0 !important; box-shadow: none !important;
        }}
        .st-key-newuser_card_dark [data-testid="stForm"] label {{
            color: #9AA6C3 !important; font-size: 13px !important; font-weight: 500 !important;
        }}
        .st-key-newuser_card_dark [data-testid="stTextInputRootElement"] {{
            background: rgba(255,255,255,0.04) !important;
            border-radius: 10px !important;
            border: 1px solid rgba(148,163,184,0.20) !important;
        }}
        .st-key-newuser_card_dark input {{
            height: 44px !important; border-radius: 10px !important;
            border: none !important;
            background: transparent !important;
            color: #E7ECF5 !important; font-size: 14px !important;
            padding-left: 14px !important; box-shadow: none !important;
        }}
        .st-key-newuser_card_dark [data-testid="stTextInputRootElement"]:focus-within {{
            border-color: #3B82F6 !important; box-shadow: 0 0 0 2px rgba(59,130,246,0.20) !important;
        }}
        .st-key-newuser_card_dark input::placeholder {{ color: #5B6580 !important; }}
        .st-key-newuser_card_dark [data-testid="stTextInput"] {{ margin-bottom: 10px; }}

        .st-key-newuser_card_dark [data-testid="stSelectbox"] {{ margin-bottom: 10px; }}
        .st-key-newuser_card_dark .react-aria-ComboBox {{
            background: rgba(255,255,255,0.04) !important;
            border-radius: 10px !important;
            border: 1px solid rgba(148,163,184,0.20) !important;
            min-height: 44px !important;
            overflow: hidden;
        }}
        .st-key-newuser_card_dark .react-aria-ComboBox * {{
            background-color: transparent !important;
            border-color: transparent !important;
            color: #E7ECF5 !important;
        }}
        .st-key-newuser_card_dark .react-aria-ComboBox:focus-within {{
            border-color: #3B82F6 !important; box-shadow: 0 0 0 2px rgba(59,130,246,0.20) !important;
        }}

        .st-key-newuser_card_dark [data-testid="stFormSubmitButton"] button {{
            width: 100% !important; height: 46px !important; border: none !important;
            border-radius: 10px !important;
            background: linear-gradient(90deg, #38BDF8, #2563EB) !important;
            color: white !important; font-size: 14px !important; font-weight: 600 !important;
            margin-top: 6px !important;
            transition: transform 0.15s ease, box-shadow 0.15s ease;
        }}
        .st-key-newuser_card_dark [data-testid="stFormSubmitButton"] button:hover {{
            transform: translateY(-1px); box-shadow: 0 8px 22px rgba(37,99,235,0.40);
        }}

        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.container(key="newuser_back_wrap"):
        if st.button("← Back to Admin", key="newuser_back_btn"):
            st.session_state.show_create_user = False
            st.session_state.show_admin = True
            st.rerun()

    with st.container(key="newuser_card_dark"):
        st.markdown(
            f"""
            <img class="login-logo" src="data:image/png;base64,{_logo_b64}"/>
            <div class="login-title-dark">Create <span class="accent">User</span></div>
            <div class="login-subtitle-dark">Add a new dashboard account</div>
            """,
            unsafe_allow_html=True,
        )
        with st.form("create_user_form"):
            new_username = st.text_input("Username", placeholder="Enter a username")
            create_password = st.text_input("Create Password", type="password", placeholder="Enter a password")
            new_password_confirm = st.text_input("New Password", type="password", placeholder="Re-enter the password")
            role = st.selectbox("Role", ["Regular", "Admin"])
            created = st.form_submit_button("Create User →", use_container_width=True)
            if created:
                if not new_username.strip():
                    st.error("Username cannot be empty.")
                elif not create_password:
                    st.error("Password cannot be empty.")
                elif create_password != new_password_confirm:
                    st.error("Passwords do not match.")
                else:
                    new_id = create_user(new_username.strip(), create_password, role.lower())
                    if new_id is None:
                        st.error(f"Username \"{new_username.strip()}\" already exists.")
                    else:
                        st.session_state.user_create_success = f"User \"{new_username.strip()}\" ({role}) created successfully."
                        st.session_state.show_create_user = False
                        st.session_state.show_admin = True
                        st.rerun()


st.markdown("""
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap">
<style>
    html, body, [class*="css"] {
        font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif !important;
    }
    .mono { font-family: 'JetBrains Mono', monospace !important; }
    .main { background-color: #F8FAFC; }

    /* ══════════════ SIDEBAR: slate-900 command panel ══════════════ */
    section[data-testid="stSidebar"] {
        background: #0F172A !important;
        border-right: 1px solid #1E293B;
    }
    /* The collapse-arrow header reserves a fixed 60px band above the
       sidebar's own content by default — shrinks it so the logo row sits
       right under the collapse button instead of leaving empty space. */
    section[data-testid="stSidebar"] [data-testid="stSidebarHeader"] {
        min-height: 32px !important;
        height: 32px !important;
    }
    section[data-testid="stSidebar"] * {
        color: #E2E8F0 !important;
    }
    /* The wildcard rule above forces solid text on every sidebar element,
       including this gradient-clip span — without this override it would
       paint over the gradient and "Intelligence" would render as plain
       flat text instead of the blue-to-purple accent. */
    section[data-testid="stSidebar"] .sidebar-logo-accent {
        color: transparent !important;
        -webkit-text-fill-color: transparent !important;
    }
    section[data-testid="stSidebar"] svg {
        fill: #94A3B8 !important;
    }

    /* Section heading — small icon-chip label, not a plain h3 */
    section[data-testid="stSidebar"] h3 {
        font-size: 12px !important;
        font-weight: 700 !important;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: #E2E8F0 !important;
        padding-bottom: 0 !important;
        margin-bottom: 4px !important;
        border-bottom: none !important;
    }

    section[data-testid="stSidebar"] hr {
        border: none !important;
        height: 1px;
        background: #1E293B !important;
        margin: 18px 0 !important;
    }

    /* Expander panels */
    section[data-testid="stSidebar"] div[data-testid="stExpander"] {
        background: rgba(15, 23, 42, 0.9);
        border: 1px solid #1E293B;
        border-radius: 12px;
        box-shadow: none;
        transition: border-color 0.15s ease;
    }
    section[data-testid="stSidebar"] div[data-testid="stExpander"]:hover {
        border-color: #334155;
    }
    section[data-testid="stSidebar"] div[data-testid="stExpander"] summary {
        background-color: transparent;
    }
    section[data-testid="stSidebar"] div[data-testid="stExpander"] summary p {
        font-size: 12px !important;
        font-weight: 600 !important;
        color: #CBD5E1 !important;
    }

    /* Text inputs — the visible box is actually stTextInputRootElement, a
       separate wrapper around the <input> that keeps the page's light
       background unless overridden too (otherwise the input's own light
       text sits on that leftover light box and becomes unreadable). */
    section[data-testid="stSidebar"] div[data-testid="stTextInputRootElement"] {
        background-color: rgba(2, 6, 23, 0.7) !important;
        border: 1px solid #334155 !important;
        border-radius: 8px !important;
        height: 34px !important;
        min-height: 34px !important;
    }
    section[data-testid="stSidebar"] div[data-testid="stTextInputRootElement"]:focus-within {
        border-color: #6366F1 !important;
        box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.25) !important;
    }
    section[data-testid="stSidebar"] input {
        background-color: transparent !important;
        border: none !important;
        font-size: 12px !important;
    }
    section[data-testid="stSidebar"] input::placeholder {
        color: #64748B !important;
    }
    div[data-testid="InputInstructions"] {
        display: none !important;
    }
    section[data-testid="stSidebar"] label p {
        font-size: 11px !important;
        font-weight: 500 !important;
        color: #94A3B8 !important;
    }

    /* Select / combobox — fix the unstyled trigger box (react-aria ComboBox
       renders the visible box as the input's own parent div, separate from
       the input element itself; without this it stays page-background
       white behind the dropdown chevron). */
    section[data-testid="stSidebar"] .react-aria-ComboBox {
        background-color: rgba(2, 6, 23, 0.7) !important;
        border: 1px solid #334155 !important;
        border-radius: 8px !important;
        height: 34px !important;
        min-height: 34px !important;
        overflow: hidden;
    }
    section[data-testid="stSidebar"] .react-aria-ComboBox * {
        background-color: transparent !important;
        border-color: transparent !important;
    }
    section[data-testid="stSidebar"] .react-aria-ComboBox input {
        border: none !important;
        font-size: 12px !important;
    }
    section[data-testid="stSidebar"] .react-aria-ComboBox:focus-within {
        border-color: #6366F1 !important;
        box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.25) !important;
    }

    /* Buttons */
    section[data-testid="stSidebar"] div[data-testid="stButton"] button[kind="secondary"] {
        background: #1E293B !important;
        border: 1px solid #334155 !important;
        border-radius: 10px !important;
        font-size: 12.5px !important;
        padding: 8px 12px !important;
        transition: background 0.15s ease, border-color 0.15s ease;
    }
    section[data-testid="stSidebar"] div[data-testid="stButton"] button[kind="secondary"]:hover {
        background: #263449 !important;
        border-color: #475569 !important;
    }

    /* Admin / Relevant Tenders Keyword — plain nav-style buttons, matched
       to the same transparent panel look as the expanders around them
       instead of the more solid default secondary-button fill. */
    section[data-testid="stSidebar"] .st-key-btn_open_admin div[data-testid="stButton"] button[kind="secondary"],
    section[data-testid="stSidebar"] .st-key-btn_open_relevant_keywords div[data-testid="stButton"] button[kind="secondary"],
    section[data-testid="stSidebar"] .st-key-btn_open_patterns div[data-testid="stButton"] button[kind="secondary"],
    section[data-testid="stSidebar"] .st-key-btn_open_search_history div[data-testid="stButton"] button[kind="secondary"],
    section[data-testid="stSidebar"] .st-key-btn_change_password div[data-testid="stButton"] button[kind="secondary"] {
        background: rgba(15, 23, 42, 0.9) !important;
        border: 1px solid #1E293B !important;
    }
    section[data-testid="stSidebar"] .st-key-btn_open_admin div[data-testid="stButton"] button[kind="secondary"]:hover,
    section[data-testid="stSidebar"] .st-key-btn_open_relevant_keywords div[data-testid="stButton"] button[kind="secondary"]:hover,
    section[data-testid="stSidebar"] .st-key-btn_open_patterns div[data-testid="stButton"] button[kind="secondary"]:hover,
    section[data-testid="stSidebar"] .st-key-btn_open_search_history div[data-testid="stButton"] button[kind="secondary"]:hover,
    section[data-testid="stSidebar"] .st-key-btn_change_password div[data-testid="stButton"] button[kind="secondary"]:hover {
        background: rgba(15, 23, 42, 0.9) !important;
        border-color: #334155 !important;
    }
    section[data-testid="stSidebar"] div[data-testid="stButton"] button[kind="primary"] {
        background: linear-gradient(90deg, #7C3AED, #4F46E5) !important;
        border: 1px solid rgba(129, 140, 248, 0.4) !important;
        border-radius: 10px !important;
        font-size: 12.5px !important;
        padding: 8px 12px !important;
        box-shadow: 0 4px 14px rgba(79, 70, 229, 0.35);
        transition: box-shadow 0.15s ease, transform 0.15s ease;
    }
    section[data-testid="stSidebar"] div[data-testid="stButton"] button[kind="primary"]:hover {
        box-shadow: 0 6px 18px rgba(79, 70, 229, 0.5);
        transform: translateY(-1px);
    }

    /* GeM/KPPP Search buttons — cobalt blue instead of the violet gradient
       used for Start Agent, for a calmer, more professional accent.
       (Selector chain matches the base sidebar rule's specificity so this
       actually wins the cascade instead of losing to it.) */
    section[data-testid="stSidebar"] .st-key-gem_search_btn div[data-testid="stButton"] button[kind="primary"],
    section[data-testid="stSidebar"] .st-key-kppp_search_btn div[data-testid="stButton"] button[kind="primary"] {
        background: #2563EB !important;
        border: 1px solid rgba(96, 165, 250, 0.4) !important;
        box-shadow: 0 4px 14px rgba(37, 99, 235, 0.3) !important;
    }
    section[data-testid="stSidebar"] .st-key-gem_search_btn div[data-testid="stButton"] button[kind="primary"]:hover,
    section[data-testid="stSidebar"] .st-key-kppp_search_btn div[data-testid="stButton"] button[kind="primary"]:hover {
        background: #3B82F6 !important;
        box-shadow: 0 6px 18px rgba(37, 99, 235, 0.45) !important;
    }

    /* Start Agent — idle state: dark slate instead of a colored gradient,
       since nothing is running yet. */
    section[data-testid="stSidebar"] .st-key-start_agent_btn div[data-testid="stButton"] button[kind="primary"] {
        background: rgba(30, 41, 59, 0.9) !important;
        border: 1px solid #334155 !important;
        box-shadow: none !important;
    }
    section[data-testid="stSidebar"] .st-key-start_agent_btn div[data-testid="stButton"] button[kind="primary"]:hover {
        background: #263449 !important;
        border-color: #475569 !important;
        transform: none !important;
    }

    /* Stop Agent — running state: emerald with a live pulsing dot, reading
       as an operational status rather than a plain destructive action. */
    @keyframes sidebar-pulse-dot {
        0%   { box-shadow: 0 0 0 0 rgba(110, 231, 183, 0.65); }
        70%  { box-shadow: 0 0 0 7px rgba(110, 231, 183, 0); }
        100% { box-shadow: 0 0 0 0 rgba(110, 231, 183, 0); }
    }
    section[data-testid="stSidebar"] .st-key-stop_agent_btn div[data-testid="stButton"] button[kind="secondary"] {
        background: #059669 !important;
        border: 1px solid rgba(110, 231, 183, 0.4) !important;
        color: #ECFDF5 !important;
        position: relative;
        padding-left: 30px !important;
    }
    section[data-testid="stSidebar"] .st-key-stop_agent_btn div[data-testid="stButton"] button[kind="secondary"]:hover {
        background: #10B981 !important;
        border-color: rgba(110, 231, 183, 0.6) !important;
    }
    section[data-testid="stSidebar"] .st-key-stop_agent_btn div[data-testid="stButton"] button[kind="secondary"]::before {
        content: "";
        position: absolute;
        left: 14px;
        top: 50%;
        width: 8px;
        height: 8px;
        margin-top: -4px;
        border-radius: 50%;
        background: #6EE7B7;
        animation: sidebar-pulse-dot 1.8s infinite;
    }

    /* Status badge — soft outlined pill instead of a solid block; each
       state keeps its own color (set below), just restyled to an outline. */
    section[data-testid="stSidebar"] .status-badge {
        font-size: 11px !important;
        letter-spacing: 0.05em;
        border: 1px solid currentColor;
        box-shadow: none;
    }
    section[data-testid="stSidebar"] .status-watching {
        background: rgba(16, 185, 129, 0.1) !important;
        color: #34D399 !important;
    }
    section[data-testid="stSidebar"] .status-searching {
        background: rgba(245, 158, 11, 0.1) !important;
        color: #FBBF24 !important;
    }
    section[data-testid="stSidebar"] .status-learning {
        background: rgba(59, 130, 246, 0.1) !important;
        color: #60A5FA !important;
    }
    section[data-testid="stSidebar"] .status-expanding {
        background: rgba(124, 58, 237, 0.1) !important;
        color: #A78BFA !important;
    }
    section[data-testid="stSidebar"] .status-idle {
        background: rgba(148, 163, 184, 0.1) !important;
        color: #94A3B8 !important;
    }
    /* Pins Change Password / Log out to the bottom of the sidebar instead of
       wherever they fall in the normal top-down button flow — the sidebar's
       own content block becomes a flex column and this wrapper's
       margin-top:auto pushes it (and only it) down to the end. */
    [data-testid="stSidebarUserContent"] > div > [data-testid="stVerticalBlock"] {
        min-height: calc(100vh - 120px);
        display: flex !important;
        flex-direction: column !important;
    }
    div[data-testid="stLayoutWrapper"]:has(.st-key-sidebar_bottom_actions) {
        margin-top: auto !important;
    }
    .block-container {
        padding-top: 1rem !important;
        padding-left: 2rem !important;
        padding-right: 2rem !important;
        max-width: 100% !important;
    }
    /* Streamlit wraps every element in a snug per-element box that's barely
       taller than the element itself, so plain position:sticky falls out of
       its own containing block within a few dozen pixels of scroll — a JS
       helper further down keeps this row's position/size synced instead;
       this just supplies the fixed-position look once JS sets top/left/width. */
    div[data-testid="stHorizontalBlock"]:has(div.stats-sticky) {
        position: fixed !important;
        /* top/left/width are plain (non-!important) fallbacks only — the JS
           helper below sets them precisely via inline style once it runs,
           and !important here would out-rank that inline style and pin the
           row in the wrong place permanently. */
        top: 60px;
        left: 0;
        width: 100%;
        z-index: 999 !important;
        background-color: #F8FAFC !important;
        padding: 10px 2rem !important;
        margin: 0 !important;
        box-sizing: border-box !important;
        flex-wrap: nowrap !important;
    }
    /* The now out-of-flow fixed row no longer reserves its own vertical
       space, so its (now-empty) original slot needs an explicit height to
       stop the rest of the page from jumping up underneath it. Fixed rather
       than JS-computed so it can't drift or flicker between reruns. */
    div[data-testid="stLayoutWrapper"]:has(div.stats-sticky) {
        min-height: 84px !important;
    }
    /* Halves the empty gap between the stat-card row and the charts below
       it (per user request) — Streamlit's own default spacing between
       consecutive top-level blocks accounts for most of it. */
    div[data-testid="stLayoutWrapper"]:has(div.stats-sticky) + div {
        margin-top: -32px !important;
    }
    /* The invisible marker itself still gets Streamlit's normal per-element
       vertical margin, which was making only the "Total Tenders" column
       (the one column that has this extra element) render taller than the
       other four. Collapse its own wrapper to zero size. */
    div[data-testid="stElementContainer"]:has(> div.stats-sticky) {
        height: 0 !important;
        min-height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
        overflow: hidden !important;
    }
    /* Collapsing the marker's own box isn't enough — its column's inner
       vertical block still applies its normal gap *between* the marker and
       the button below it, so that one column was still ~16px taller than
       the other four. Zero that gap out too. */
    div[data-testid="stColumn"]:has(div.stats-sticky) div[data-testid="stVerticalBlock"] {
        gap: 0 !important;
    }

    /* Stat cards — plain white/slate by default, each key gets its own
       accent color (border + ring + label) only when it's the active
       filter, matching the reference's per-card color coding.
       (.st-key-btn_* is a class on the widget's own container — not a
       descendant of the .stats-sticky marker, which is just a sibling
       element, not an ancestor — so these must be scoped standalone.) */
    .st-key-btn_all button,
    .st-key-btn_new button,
    .st-key-btn_saved button,
    .st-key-btn_searches button,
    .st-key-btn_relevant button {
        border-radius: 12px !important;
        border: 1px solid #E2E8F0 !important;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
        transition: border-color 0.15s ease, box-shadow 0.15s ease;
        padding: 10px 8px !important;
        font-size: 12px !important;
        line-height: 1.5 !important;
    }
    .st-key-btn_all button[kind="secondary"]:hover,
    .st-key-btn_new button[kind="secondary"]:hover,
    .st-key-btn_saved button[kind="secondary"]:hover,
    .st-key-btn_searches button[kind="secondary"]:hover,
    .st-key-btn_relevant button[kind="secondary"]:hover {
        border-color: #CBD5E1 !important;
    }
    .st-key-btn_all button[kind="primary"],
    .st-key-btn_new button[kind="primary"],
    .st-key-btn_saved button[kind="primary"],
    .st-key-btn_searches button[kind="primary"],
    .st-key-btn_relevant button[kind="primary"] {
        background-color: #FFFFFF !important;
    }
    .st-key-btn_all button[kind="primary"] {
        color: #6D28D9 !important;
        border: 1.5px solid #7C3AED !important;
        box-shadow: 0 0 0 3px rgba(124, 58, 237, 0.12) !important;
    }
    .st-key-btn_new button[kind="primary"] {
        color: #047857 !important;
        border: 1.5px solid #10B981 !important;
        box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.12) !important;
    }
    .st-key-btn_saved button[kind="primary"] {
        color: #4338CA !important;
        border: 1.5px solid #4F46E5 !important;
        box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.12) !important;
    }
    .st-key-btn_searches button[kind="primary"] {
        color: #1D4ED8 !important;
        border: 1.5px solid #3B82F6 !important;
        box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.12) !important;
    }
    .st-key-btn_relevant button[kind="primary"] {
        color: #6D28D9 !important;
        border: 1.5px solid #7C3AED !important;
        box-shadow: 0 0 0 3px rgba(124, 58, 237, 0.12) !important;
    }
    .agent-header {
        background: linear-gradient(135deg, #2C3E50, #8E44AD);
        padding: 20px 30px;
        border-radius: 12px;
        color: white;
        margin-bottom: 20px;
    }
    .agent-header h1 { margin: 0; font-size: 28px; font-weight: 700; }
    .agent-header p { margin: 5px 0 0 0; opacity: 0.85; font-size: 14px; }
    .status-badge {
        display: inline-block;
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 12px;
        font-weight: 600;
        text-transform: uppercase;
    }
    .status-watching { background: #27AE60; color: white; }
    .status-searching { background: #E67E22; color: white; }
    .status-learning { background: #3498DB; color: white; }
    .status-expanding { background: #9B59B6; color: white; }
    .status-idle { background: #95A5A6; color: white; }
    .tender-card {
        background: white;
        border-radius: 10px;
        padding: 16px 20px;
        margin-bottom: 8px;
        box-shadow: 0 2px 6px rgba(0,0,0,0.06);
        border-left: 4px solid #8E44AD;
    }
    .tender-title { font-size: 15px; font-weight: 600; color: #2C3E50; }
    .tender-meta { font-size: 12px; color: #7F8C8D; margin-top: 4px; }
    .log-box {
        background: #1E1E1E;
        color: #00FF00;
        font-family: monospace;
        font-size: 12px;
        padding: 15px;
        border-radius: 8px;
        height: 400px;
        overflow-y: auto;
    }
    .detail-label { font-weight: 600; color: #555; font-size: 13px; }
    .detail-value { color: #2C3E50; font-size: 13px; }
    div[data-testid="stButton"] button {
        height: auto;
        padding: 7px 12px;
        font-size: 13px;
        white-space: pre-wrap;
    }
    /* Gap between the chart row (donut + trend) and the portal/search/sort
       filter bar below it: an invisible spacer sits between them, so the
       page's flex `gap` (16px) applies twice — once on each side of the
       spacer's own empty slot — for 32px of blank space. Hiding that one
       spacer (matched narrowly via :has(+ ...) so no other spacer on the
       page is affected) collapses it to a single 16px gap; pulling the
       filter bar up the rest of the way tightens it to a few px. Applies
       on every screen size, unlike the mobile-only rules further down. */
    div[data-testid="stElementContainer"]:has(+ div[data-testid="stLayoutWrapper"] div.col-marker-portal) {
        display: none !important;
    }
    div[data-testid="stLayoutWrapper"]:has(> div[data-testid="stHorizontalBlock"] div.col-marker-portal) {
        margin-top: -13px !important;
    }
    /* Dropdown-only now (no pill control to switch between on screen
       size), so this column just needs dropdown-sized width on every
       screen — the old wide-vs-narrow pill/dropdown toggling is gone. */
    div[data-testid="stColumn"]:has(.col-marker-portal) {
        flex: 0 0 155px !important;
        min-width: 155px !important;
        max-width: 170px !important;
    }
    div[data-testid="stColumn"]:has(.col-marker-search) {
        flex: 1 1 0% !important;
        min-width: 180px !important;
        max-width: none !important;
    }
    div[data-testid="stColumn"]:has(.col-marker-search) div[data-testid="stTextInput"] input {
        border: 1px solid #E2E8F0 !important;
        border-radius: 10px !important;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
        height: 38px !important;
        font-size: 12.5px !important;
    }
    div[data-testid="stColumn"]:has(.col-marker-search) div[data-testid="stTextInput"] input:focus {
        border-color: #6366F1 !important;
        box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.15) !important;
    }
    div[data-testid="stColumn"]:has(.col-marker-sort) {
        flex: 0 0 150px !important;
        min-width: 150px !important;
        max-width: 165px !important;
    }
    div[data-testid="stColumn"]:has(.col-marker-search-btn),
    div[data-testid="stColumn"]:has(.col-marker-filter-btn) {
        flex: 0 0 44px !important;
        min-width: 44px !important;
        max-width: 44px !important;
    }
    div[class*="st-key-apply_table_search_btn"] button,
    div[class*="st-key-open_filters_btn"] button {
        height: 38px !important;
        min-height: 38px !important;
        padding: 0 !important;
        border-radius: 10px !important;
        background-color: #2563EB !important;
        border: 1px solid rgba(96, 165, 250, 0.4) !important;
        color: #FFFFFF !important;
    }
    div[class*="st-key-apply_table_search_btn"] button:hover,
    div[class*="st-key-open_filters_btn"] button:hover {
        background-color: #3B82F6 !important;
    }
    div[class*="st-key-apply_table_search_btn"] button span[data-testid="stIconMaterial"],
    div[class*="st-key-open_filters_btn"] button span[data-testid="stIconMaterial"] {
        font-size: 18px !important;
    }
    div[data-testid="stColumn"]:has(.col-marker-sort) div[data-baseweb],
    div[data-testid="stColumn"]:has(.col-marker-portal) .react-aria-ComboBox {
        border-radius: 10px !important;
    }
    div[data-testid="stColumn"]:has(.col-marker-sort) .react-aria-ComboBox,
    div[data-testid="stColumn"]:has(.col-marker-portal) .react-aria-ComboBox {
        border: 1px solid #E2E8F0 !important;
        border-radius: 10px !important;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
        height: 38px !important;
        min-height: 38px !important;
    }
    div[data-testid="stColumn"]:has(.col-marker-sort) .react-aria-ComboBox input {
        font-size: 12.5px !important;
    }
    div[data-testid="stColumn"]:has(.col-marker-sort) .react-aria-ComboBox:focus-within,
    div[data-testid="stColumn"]:has(.col-marker-portal) .react-aria-ComboBox:focus-within {
        border-color: #6366F1 !important;
        box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.15) !important;
    }
    div[data-testid="stButton"] button:has-text("🗑️") {
        background-color: #E74C3C !important;
        color: white !important;
        border: none !important;
    }

    /* Gap between the "N tenders — Page X of Y" caption and the table
       below it: a manual spacer div sits between the caption and the
       table's own card (the whole header+rows now live inside their own
       st.container(), so the card wrapper — not the header row itself —
       is the caption's actual flex sibling), each claiming a full 16px
       gap-slot. Hiding the spacer collapses that to one 16px gap; pulling
       the card wrapper itself up the rest of the way tightens it further.
       Applies on every screen size, unlike the mobile-only rules below. */
    div[data-testid="stElementContainer"]:has(+ div[data-testid="stLayoutWrapper"] div.table-card-marker) {
        display: none !important;
    }
    div[data-testid="stLayoutWrapper"]:has(> div[data-testid="stVerticalBlock"] > div[data-testid="stElementContainer"] div.table-card-marker) {
        margin-top: -13px !important;
    }

    /* ══════════════ TENDERS TABLE: white card, refined cells ══════════════ */
    div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.table-card-marker) {
        background: #FFFFFF;
        border: 1px solid #E2E8F0;
        border-radius: 16px;
        box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
        padding: 14px 18px 6px;
    }
    div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.table-card-marker) [data-testid="stMarkdownContainer"] p {
        font-size: 12.5px !important;
    }
    .th-cell {
        font-size: 11px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: #64748B;
    }
    .td-slno { color: #94A3B8; font-weight: 500; }
    .td-bid { font-size: 12.5px; font-weight: 700; color: #0F172A; }
    .td-dept { font-weight: 600; color: #1E293B; }
    .td-qty { font-weight: 700; color: #0F172A; }
    .td-date { font-size: 11.5px; color: #475569; }
    .portal-chip {
        display: inline-block;
        font-size: 9.5px;
        font-weight: 700;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        padding: 1px 6px;
        border-radius: 4px;
        margin-top: 2px;
    }
    .portal-chip-gem { background: #EFF6FF; color: #1D4ED8; }
    .portal-chip-kppp { background: #FFFBEB; color: #92400E; }
    .enddate-chip {
        display: inline-block;
        font-size: 11px;
        font-weight: 600;
        color: #92400E;
        background: rgba(251, 191, 36, 0.12);
        border: 1px solid rgba(251, 191, 36, 0.35);
        padding: 2px 7px;
        border-radius: 5px;
    }

    /* Row action buttons — View / Save / Delete */
    [class*="st-key-view_"] button {
        border-radius: 8px !important;
        border: 1px solid #E2E8F0 !important;
        font-weight: 600 !important;
        font-size: 12px !important;
    }
    [class*="st-key-view_"] button:hover {
        border-color: #C7D2FE !important;
        color: #4F46E5 !important;
    }
    [class*="st-key-table_save_"] button,
    [class*="st-key-table_unsave_"] button {
        border-radius: 8px !important;
        border: 1px solid #E2E8F0 !important;
    }
    [class*="st-key-table_save_"] button:hover {
        background: #EEF2FF !important;
        border-color: #C7D2FE !important;
    }
    [class*="st-key-table_unsave_"] button {
        background: #EEF2FF !important;
        border-color: #C7D2FE !important;
    }
    [class*="st-key-table_delete_"] button {
        border-radius: 8px !important;
        border: 1px solid #E2E8F0 !important;
    }
    [class*="st-key-table_delete_"] button:hover {
        background: #FEF2F2 !important;
        border-color: #FECACA !important;
        color: #DC2626 !important;
    }

    /* Pagination */
    [class*="st-key-prev_btn2"] button,
    [class*="st-key-next_btn2"] button {
        border-radius: 8px !important;
        border: 1px solid #E2E8F0 !important;
        font-weight: 600 !important;
    }
    [class*="st-key-prev_btn2"] button:hover,
    [class*="st-key-next_btn2"] button:hover {
        border-color: #C7D2FE !important;
        color: #4F46E5 !important;
    }

    /* Streamlit's own generated CSS (not ours — a class like
       .st-emotion-cache-1permvm) puts flex-wrap:wrap on every
       stHorizontalBlock unconditionally, for its own responsive behavior.
       That's normally harmless since columns just shrink, but the Trend
       card's title/toggle/range row can end up narrower than expected
       (it's half of the two-column chart section, further squeezed by
       whatever width the sidebar happens to be), and once the 3 columns'
       combined width no longer fits, Streamlit's native wrap silently
       breaks it into two lines — on any desktop width, not just narrow
       ones. Forcing nowrap here keeps it a single row regardless; the
       mobile media query below still overrides this back to wrap, since
       it's declared later in the stylesheet. */
    div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"] > div[data-testid="stElementContainer"] div.trend-toggle-col-marker) {
        flex-wrap: nowrap !important;
    }

    /* ══════════════ RESPONSIVE — phones & small tablets ══════════════
       Everything above this block was built and styled for desktop first;
       these overrides make the same page usable and legible on a narrow
       screen instead of the page overflowing or content getting crushed
       into unreadable slivers. Nothing here changes desktop layout —
       every rule is scoped inside the media query. */
    @media (max-width: 900px) {
        .block-container {
            padding-left: 0.9rem !important;
            padding-right: 0.9rem !important;
            /* Streamlit's own top toolbar ("Deploy" / ⋮ menu) is a fixed,
               very-high-z-index bar spanning 0-60px — with only 0.75rem
               (12px) of top padding, the first stat card's top edge landed
               right underneath it and got visually clipped. 2rem clears it
               with a small gap. */
            padding-top: 2rem !important;
        }

        /* Stat cards: the sticky/fixed-position treatment (built for a wide
           desktop row that scrolls under a fixed header) doesn't translate
           to a narrow screen — drop it back into the normal page flow and
           let the 5 cards wrap into a 2-per-row grid instead of forcing
           them onto one unreadably cramped row. */
        div[data-testid="stHorizontalBlock"]:has(div.stats-sticky) {
            position: static !important;
            top: auto !important;
            left: auto !important;
            width: 100% !important;
            flex-wrap: wrap !important;
            row-gap: 8px !important;
            padding: 0 !important;
        }
        div[data-testid="stLayoutWrapper"]:has(div.stats-sticky) {
            min-height: 0 !important;
            /* Pulls the chart row up closer, shrinking the remaining
               single 16px flex gap (left after the two spacers below are
               hidden) down to about 3px. */
            margin-bottom: -13px !important;
        }
        /* The gap between the stat cards and the charts row below them is
           NOT built from each element's own margin — the shared parent
           spaces every child apart with a flex `gap`, and there are two
           empty/invisible spacer elements (an old <br> and a blank
           container) sitting between the two, each still claiming a full
           gap on either side even though they render nothing. Hiding them
           removes their gap slots too, collapsing 3 stacked gaps + their
           own height down to one normal gap — this is why the fix is
           display:none here rather than a margin/negative-margin tweak,
           which fought unpredictably against the parent's own gap sizing. */
        div[data-testid="stLayoutWrapper"]:has(div.stats-sticky) + div,
        div[data-testid="stLayoutWrapper"]:has(div.stats-sticky) + div + div {
            display: none !important;
        }
        /* 5 cards as 3 + 2: the first three fill row one (~33% each), the
           last two fill row two (~50% each) — a fixed layout rather than
           however many happen to fit, so it looks deliberate at any
           mobile width instead of reflowing unpredictably. */
        div[data-testid="stHorizontalBlock"]:has(div.stats-sticky) div[data-testid="stColumn"] {
            /* Streamlit's own column-gap here is 16px, not 8px — 3 cards
               in one row means 2 of those gaps (32px total) eating into
               the row, so each card only gets a third of what's left, not
               a flat 8px subtracted — the earlier flat subtraction left
               the 3rd card just over the edge and wrapping to its own row. */
            flex: 1 1 calc(33.333% - 11px) !important;
            min-width: calc(33.333% - 11px) !important;
            max-width: calc(33.333% - 11px) !important;
        }
        div[data-testid="stHorizontalBlock"]:has(div.stats-sticky) div[data-testid="stColumn"]:nth-child(4),
        div[data-testid="stHorizontalBlock"]:has(div.stats-sticky) div[data-testid="stColumn"]:nth-child(5) {
            flex: 1 1 calc(50% - 8px) !important;
            min-width: calc(50% - 8px) !important;
            max-width: calc(50% - 8px) !important;
        }
        .st-key-btn_all button,
        .st-key-btn_new button,
        .st-key-btn_saved button,
        .st-key-btn_searches button,
        .st-key-btn_relevant button {
            font-size: 13px !important;
            padding: 12px 10px !important;
            min-height: 44px !important;
        }

        /* Portal dropdown / sort dropdown / search box+button: two rows
           instead of squeezing 4 controls sideways into a screen that isn't
           wide enough for any of them. Portal and sort share row 1 (both
           are dropdowns, same width); the search box and its button stay
           paired side by side on row 2 since they're one action. Row-gap
           kept very tight since both rows belong to the one filter bar. */
        div[data-testid="stHorizontalBlock"]:has(.col-marker-portal) {
            flex-wrap: wrap !important;
            row-gap: 3px !important;
        }
        div[data-testid="stColumn"]:has(.col-marker-portal) {
            order: 1;
            flex: 1 1 calc(50% - 8px) !important;
            min-width: calc(50% - 8px) !important;
            max-width: calc(50% - 8px) !important;
        }
        div[data-testid="stColumn"]:has(.col-marker-sort) {
            order: 2;
            flex: 1 1 calc(50% - 8px) !important;
            min-width: calc(50% - 8px) !important;
            max-width: calc(50% - 8px) !important;
        }
        div[data-testid="stColumn"]:has(.col-marker-search) {
            order: 3;
            /* Reserve both buttons' 44px plus Streamlit's own 16px gap
               each, or they overflow the row's real width and wrap onto
               their own line below. */
            flex: 1 1 calc(100% - 120px) !important;
            min-width: 0 !important;
            max-width: none !important;
        }
        div[data-testid="stColumn"]:has(.col-marker-search-btn) {
            order: 4;
        }
        div[data-testid="stColumn"]:has(.col-marker-filter-btn) {
            order: 5;
        }

        /* Gap between the Tenders Trend card and the filter bar below it:
           an invisible zero-height spacer sits between them, so the
           parent's flex `gap` (16px) applies twice — once on each side of
           the spacer's own empty slot — for ~32px of blank space total.
           Hiding that one specific spacer (matched via :has(+ ...) so only
           the spacer immediately before the filter bar is affected, not
           any other spacer elsewhere on the page) collapses that to a
           single 16px gap; pulling the filter bar up the rest of the way
           then tightens it to 3px, matching the row-gap inside the bar. */
        div[data-testid="stElementContainer"]:has(+ div[data-testid="stLayoutWrapper"] > div[data-testid="stHorizontalBlock"] div.col-marker-portal) {
            display: none !important;
        }
        div[data-testid="stLayoutWrapper"]:has(> div[data-testid="stHorizontalBlock"] div.col-marker-portal) {
            margin-top: -13px !important;
        }

        /* Shorter portal/sort dropdowns and search box on mobile — the
           38px desktop height reads oversized on a narrow screen. */
        div[data-testid="stColumn"]:has(.col-marker-portal) .react-aria-ComboBox,
        div[data-testid="stColumn"]:has(.col-marker-sort) .react-aria-ComboBox {
            height: 28px !important;
            min-height: 28px !important;
        }
        div[data-testid="stColumn"]:has(.col-marker-portal) .react-aria-ComboBox input,
        div[data-testid="stColumn"]:has(.col-marker-sort) .react-aria-ComboBox input {
            font-size: 12px !important;
        }
        div[data-testid="stColumn"]:has(.col-marker-search) div[data-testid="stTextInput"] input {
            height: 28px !important;
            min-height: 28px !important;
            padding-top: 2px !important;
            padding-bottom: 2px !important;
        }
        div[class*="st-key-apply_table_search_btn"] button {
            height: 28px !important;
            min-height: 28px !important;
        }

        /* Tender table: rather than crushing 10 columns into illegible
           slivers, keep them at a readable width and let the table's own
           card scroll sideways — a normal, familiar pattern for wide data
           tables on a small screen (the rest of the page never scrolls
           horizontally, only this box does). */
        div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.table-card-marker) {
            overflow-x: auto !important;
            /* Streamlit's own generated class sets a plain overflow:auto
               (both axes) on this element — its content is a few px taller
               than the box, so that native overflow-y:auto was adding a
               second, unwanted vertical scrollbar inside the card on top
               of the intended horizontal one. */
            overflow-y: hidden !important;
            -webkit-overflow-scrolling: touch;
            padding: 12px 12px 4px !important;
        }
        div[data-testid="stHorizontalBlock"]:has(div.tbl-header-row),
        div[data-testid="stHorizontalBlock"]:has(div.tbl-data-row) {
            flex-wrap: nowrap !important;
            width: 780px !important;
            /* Streamlit's own class caps this at max-width:100% — a
               separate property from width, so !important on width alone
               doesn't beat it; max-width must be cleared too or the row
               stays clamped to the (narrow) viewport regardless. */
            max-width: none !important;
            flex: none !important;
        }
        /* Streamlit gives every column a large built-in min-width of its
           own (its native safeguard that would otherwise auto-stack them
           on a narrow screen) — forcing the row above to a fixed width
           does nothing until each column's own min-width is cleared too,
           otherwise the row still balloons to fit all 10 columns' native
           minimums (~700px+ each) instead of sharing the 780px total. */
        div[data-testid="stHorizontalBlock"]:has(div.tbl-header-row) div[data-testid="stColumn"],
        div[data-testid="stHorizontalBlock"]:has(div.tbl-data-row) div[data-testid="stColumn"] {
            min-width: 0 !important;
        }
        /* The View/Save/Delete action columns are narrow enough that their
           button label wraps onto one letter per line — shrink the text
           and kill the wrap so "View" reads as one word, not a stack. */
        div[data-testid="stHorizontalBlock"]:has(div.tbl-data-row) [class*="st-key-view_"] button {
            font-size: 10.5px !important;
            padding: 6px 4px !important;
            white-space: nowrap !important;
        }

        /* Tenders Trend header: title / portal toggle / date-range dropdown
           natively stack into 3 separate rows on a narrow screen — put the
           toggle and dropdown on one shared row instead (title keeps its
           own full-width row above them), so the card's header takes less
           vertical space and the chart below reads as more prominent. */
        /* :has() matches an ancestor at ANY depth, not just the nearest
           one — a plain :has(.st-key-chart_trend_portal) also matched the
           much bigger OUTER 2-column row (dept chart | trend chart) that
           wraps this whole card, since it too technically "has" that
           descendant far below. That silently forced the entire Trend
           card's outer column to shrink to content width instead of
           staying full-width. Requiring the marker to be a DIRECT CHILD's
           descendant (the same proven pattern used for table-card-marker
           and dept-chart-card-marker above) avoids that — the outer
           column's direct child is a different element type entirely, so
           it can no longer match by accident. */
        div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"] > div[data-testid="stElementContainer"] div.trend-toggle-col-marker) {
            flex-wrap: wrap !important;
            row-gap: 8px !important;
        }
        /* "Tenders Trend" heading itself is dropped on mobile (the toggle
           and dropdown alone make it clear what the chart shows), which
           also removes its own full-width row and shrinks the card. */
        div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"] > div[data-testid="stElementContainer"] div.trend-toggle-col-marker) > div[data-testid="stColumn"]:nth-child(1) {
            display: none !important;
        }
        div[data-testid="stColumn"]:has(> div[data-testid="stVerticalBlock"] > div[data-testid="stElementContainer"] div.trend-toggle-col-marker) {
            order: 2;
            flex: 1 1 auto !important;
            min-width: 0 !important;
            max-width: none !important;
        }
        div[data-testid="stColumn"]:has(> div[data-testid="stVerticalBlock"] > div[data-testid="stElementContainer"] div.trend-range-col-marker) {
            order: 3;
            flex: 0 0 auto !important;
            min-width: 0 !important;
            max-width: none !important;
        }
        /* The Trend card's own uniform 15px padding leaves a tall blank
           strip above the GeM/KPPP toggle now that the heading above it is
           gone. Can't shrink the card's own padding directly — the only
           selector reaching the card container from the marker also
           matches an unrelated outer page wrapper (Streamlit repeats the
           same VerticalBlock>LayoutWrapper>HorizontalBlock nesting pattern
           at multiple depths, and nested :has() isn't supported here to
           disambiguate them). Pulling the toggle row itself up with a
           negative margin — on the one selector already proven unique
           above — gives the same visual result instead. */
        div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"] > div[data-testid="stElementContainer"] div.trend-toggle-col-marker) {
            margin-top: -9px !important;
        }
        /* Same 15px card padding on the bottom side, below the chart —
           a plain marker (rather than fighting :has() ambiguity again)
           right after the chart gives a clean, unambiguous handle to pull
           the card's visual bottom edge up. */
        div[data-testid="stElementContainer"]:has(div.trend-chart-bottom-marker) {
            margin-bottom: -10px !important;
        }

        /* Touch targets — every button gets a comfortable minimum tap
           height on top of whatever desktop sizing it already has. */
        div[data-testid="stButton"] button {
            min-height: 40px;
        }

        /* "Tenders by Department" donut chart card — hidden entirely on
           mobile (heading, GeM/KPPP toggle, donut, and its legend list all
           together), per explicit request. Desktop/laptop is untouched —
           this rule only exists inside this mobile media query. */
        div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.dept-chart-card-marker) {
            display: none !important;
        }

        /* Prev / page-number / Next pagination row below the table: the
           4-column split (a wide empty spacer + 3 narrow columns) wraps
           each control onto its own row on a narrow screen — force one
           row instead, dropping the spacer and letting each control size
           to its own content. */
        div[data-testid="stHorizontalBlock"]:has(div.pgn-prev-marker) {
            flex-wrap: nowrap !important;
            justify-content: center !important;
            gap: 10px !important;
        }
        div[data-testid="stHorizontalBlock"]:has(div.pgn-prev-marker) > div[data-testid="stColumn"]:first-child {
            display: none !important;
        }
        /* flex-basis:auto would normally shrink-to-fit each column to its
           content's natural width, but Streamlit's own element wrapper
           inside the column carries an explicit width:100% — that breaks
           the browser's intrinsic-size calculation (a definite width
           doesn't contribute to the parent's shrink-to-fit sizing the way
           natural content would), collapsing the column to near 0 instead
           of the button's real width. Fixed pixel widths sized to each
           label sidestep that entirely. */
        div[data-testid="stColumn"]:has(div.pgn-prev-marker),
        div[data-testid="stColumn"]:has(div.pgn-next-marker) {
            flex: 0 0 100px !important;
            min-width: 0 !important;
            max-width: none !important;
        }
        div[data-testid="stColumn"]:has(div.pgn-page-marker) {
            flex: 0 0 60px !important;
            min-width: 0 !important;
            max-width: none !important;
            display: flex !important;
            align-items: center !important;
            justify-content: center !important;
        }
        div[data-testid="stColumn"]:has(div.pgn-prev-marker) button,
        div[data-testid="stColumn"]:has(div.pgn-next-marker) button {
            white-space: nowrap !important;
            width: 100% !important;
            padding: 8px 6px !important;
        }
        /* Gap between the table and the Prev/Next row: a "---" divider
           (an <hr> with its own ~1em top/bottom margin) sits between them,
           each side also claiming a full 16px flex gap — hiding it and
           pulling the row up closes the ~81px blank strip down to 2px. */
        div[data-testid="stElementContainer"]:has(+ div[data-testid="stLayoutWrapper"] div.pgn-prev-marker) {
            display: none !important;
        }
        div[data-testid="stHorizontalBlock"]:has(div.pgn-prev-marker) {
            margin-top: -14px !important;
        }
    }


</style>
""", unsafe_allow_html=True)

# ── SESSION STATE ─────────────────────────────────────────
if "agent_started" not in st.session_state:
    st.session_state.agent_started = False
if "analyzing" not in st.session_state:
    st.session_state.analyzing = {}
if "active_section" not in st.session_state:
    st.session_state.active_section = "all"
if "page_num" not in st.session_state:
    st.session_state.page_num = 1
if "viewing_tender_id" not in st.session_state:
    st.session_state.viewing_tender_id = None
# A tender number clicked inside a chatbot table (a plain markdown link,
# not a Streamlit widget - there's no Python callback for a click inside
# raw HTML/markdown) lands here as a query param instead, the standard way
# to make a link trigger app state: the browser does a real navigation to
# "?open_tender=<id>", which Streamlit reads back on the next run. Consumed
# immediately and cleared so it doesn't keep re-opening on every later
# rerun (e.g. clicking elsewhere, sending another chat message).
_open_tender_param = st.query_params.get("open_tender")
if _open_tender_param:
    try:
        st.session_state.viewing_tender_id = int(_open_tender_param)
    except ValueError:
        pass
    st.query_params.clear()
    st.session_state.active_section = "all"
if "chart_drill_filter" not in st.session_state:
    st.session_state.chart_drill_filter = None
if "trend_chart_reset_counter" not in st.session_state:
    st.session_state.trend_chart_reset_counter = 0
if "trend_chart_last_portal" not in st.session_state:
    st.session_state.trend_chart_last_portal = "GeM"
if "dept_chart_reset_counter" not in st.session_state:
    st.session_state.dept_chart_reset_counter = 0
if "dept_chart_last_portal" not in st.session_state:
    st.session_state.dept_chart_last_portal = "GeM"
if "scroll_to_table" not in st.session_state:
    st.session_state.scroll_to_table = False
if "show_admin" not in st.session_state:
    st.session_state.show_admin = False
if "show_relevant_keywords" not in st.session_state:
    st.session_state.show_relevant_keywords = False
if "show_patterns_learned" not in st.session_state:
    st.session_state.show_patterns_learned = False
if "show_search_history" not in st.session_state:
    st.session_state.show_search_history = False
if "show_change_password" not in st.session_state:
    st.session_state.show_change_password = False
if "show_create_user" not in st.session_state:
    st.session_state.show_create_user = False
if "portal_filter_value" not in st.session_state:
    st.session_state.portal_filter_value = PORTAL_FILTER_MAP.get(PORTAL_FILTER_DEFAULT, "KPPP")

_SIDEBAR_PANEL_KEYS = [
    "show_admin", "show_relevant_keywords",
    "show_patterns_learned", "show_search_history", "show_change_password",
    "show_create_user",
]


def _open_sidebar_panel(panel_key):
    """Opens one sidebar full-screen panel (Admin / Relevant Tenders Keyword /
    Patterns Learned / Search History) and closes any other one that was
    left open — only one of these should ever be on screen at a time."""
    for k in _SIDEBAR_PANEL_KEYS:
        st.session_state[k] = (k == panel_key)


# ── SIDEBAR ───────────────────────────────────────────────
with st.sidebar:
    import base64 as _b64
    _sidebar_logo_path = os.path.join(os.path.dirname(__file__), "assets", "chipscape_logo_transparent.png")
    try:
        with open(_sidebar_logo_path, "rb") as _f:
            _sidebar_logo_b64 = _b64.b64encode(_f.read()).decode()
    except Exception:
        _sidebar_logo_b64 = ""

    st.markdown(
        f"""
        <div style='display:flex; align-items:center; gap:16px; margin:2px 0 20px;'>
            <img src="data:image/png;base64,{_sidebar_logo_b64}" style="height:36px; width:auto; object-fit:contain;"/>
            <div style='font-size:15px; font-weight:700; color:#E2E8F0; line-height:1.2;'>
                Tender <span class="sidebar-logo-accent" style="background:linear-gradient(90deg,#48bdf4,#4d8cf5,#7652ef); -webkit-background-clip:text; background-clip:text;">Intelligence</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("### ⚙️ Agent Control")

    # Start Agent always triggers an immediate full catalog fetch on click
    # (every saved GeM keyword + every saved KPPP filter), whether it's the
    # first start of the day or a restart later — use it any time you want
    # to force a fetch (e.g. the 2 AM run was missed, or the network was
    # down earlier). The daily 2 AM scheduled sweep runs automatically in
    # the background on top of that. Neither is required to use the search
    # boxes below — both work standalone, any time.
    if not st.session_state.agent_started:
        if st.button("▶️ Start Agent", use_container_width=True, type="primary", key="start_agent_btn"):
            start_agent()
            st.session_state.agent_started = True
            st.success("Agent started!")
            st.rerun()
    else:
        if st.button("⏹️ Stop Agent", use_container_width=True, key="stop_agent_btn"):
            stop_agent()
            st.session_state.agent_started = False
            st.rerun()

    with st.expander("🔍 GeM Search Filters", expanded=False):
        keyword_input = st.text_input("Search Tenders", placeholder="Enter keyword e.g. cctv")
        if st.button("Search", icon=":material/search:", use_container_width=True, type="primary", key="gem_search_btn"):
            if keyword_input.strip():
                from agent.orchestrator import on_ceo_search
                on_ceo_search(keyword_input.strip(), "GeM")
                st.success(f"Searching for '{keyword_input}'...")
            else:
                st.warning("Please enter a keyword first")

    # KPPP has no keyword search of its own — its real search form takes
    # Category and Procuring Entity (Tender Number/dates/Tender Type
    # exist on KPPP's form too but aren't needed here per the manager's
    # call). Category defaults to "Goods" to match what KPPP's own page
    # shows pre-selected. Nothing here is mandatory — filling in just
    # one field and searching is enough, same as on KPPP's own site.
    # Saved filters are re-applied automatically on every future search
    # cycle, same as GeM keywords.
    with st.expander("🏛️ KPPP Search Filters", expanded=False):
        kppp_category = st.selectbox(
            "Category", KPPP_CATEGORIES, key="kppp_category"
        )
        kppp_entity = st.selectbox(
            "Procuring Entity", ["-- Select an entity --"] + KPPP_ENTITIES, key="kppp_entity"
        )
        # Mirrors KPPP's own "Advanced Search" -> Other Details -> Tender
        # Name field, so a filter can be scoped by keyword instead of (or as
        # well as) a procuring entity, same as GeM's keyword search.
        kppp_tender_name = st.text_input(
            "Tender Name (Advanced Search)", key="kppp_tender_name",
            placeholder="e.g. cctv camera"
        )
        if st.button("Search KPPP", icon=":material/search:", use_container_width=True, type="primary", key="kppp_search_btn"):
            entity_selected = kppp_entity != "-- Select an entity --"
            tender_name_entered = bool(kppp_tender_name.strip())
            if not entity_selected and not tender_name_entered:
                # A blank entity AND a blank tender name turns this into an
                # unscoped, every-department search that then runs forever
                # on every future cycle — KPPP searches must always be
                # scoped to at least one of the two.
                st.warning("Please select a Procuring Entity or enter a Tender Name — KPPP searches must be scoped.")
            else:
                from agent.orchestrator import on_kppp_search
                criteria = {"category": kppp_category}
                if entity_selected:
                    criteria["procuring_entity"] = kppp_entity
                if tender_name_entered:
                    criteria["tender_name"] = kppp_tender_name.strip()
                on_kppp_search(criteria)
                st.success("Searching KPPP — this filter will keep running automatically from now on.")

    if st.session_state.user_role == "admin":
        if st.button("🛠️ Admin", use_container_width=True, key="btn_open_admin"):
            _open_sidebar_panel("show_admin")
            st.rerun()

    if st.button("🎯 Relevant Tenders Keyword", use_container_width=True, key="btn_open_relevant_keywords"):
        _open_sidebar_panel("show_relevant_keywords")
        st.rerun()

    if st.button(f"🧠 Patterns Learned ({get_stats()['patterns_learned']})", use_container_width=True, key="btn_open_patterns"):
        _open_sidebar_panel("show_patterns_learned")
        st.rerun()

    if st.button(f"🔎 Search History ({get_stats()['ceo_searches']})", use_container_width=True, key="btn_open_search_history"):
        _open_sidebar_panel("show_search_history")
        st.rerun()

    state = get_state()
    status = state.get("status", "idle")

    # Lets a search that finished while nobody was watching still get
    # noticed: compares the orchestrator's own completion timestamp against
    # the last one this browser session has already shown a toast for, so
    # each completion is announced exactly once per session, not on every
    # rerun. Auto-refreshing below is what makes that timestamp change
    # visible without the user having to click anything themselves.
    last_completed = state.get("last_search_completed_at")
    if "last_seen_search_completion" not in st.session_state:
        st.session_state.last_seen_search_completion = None
    if last_completed and last_completed != st.session_state.last_seen_search_completion:
        if st.session_state.last_seen_search_completion is not None:
            st.toast("✅ Search complete — check New Tenders!", icon="✅")
        st.session_state.last_seen_search_completion = last_completed

    status_colors = {
        "watching": "status-watching",
        "searching": "status-searching",
        "learning": "status-learning",
        "expanding": "status-expanding",
        "idle": "status-idle"
    }
    css_class = status_colors.get(status, "status-idle")
    st.markdown(f"""
    <div style='margin-top:10px;'>
        <span class='status-badge {css_class}'>● {status}</span>
    </div>
    """, unsafe_allow_html=True)

    # While a search is actively running, quietly re-run this page every
    # few seconds so the status badge and the toast above actually update
    # without the user having to click anything — otherwise Streamlit only
    # refreshes on the next manual interaction, and a search that finishes
    # while the user is just sitting there watching would go unnoticed
    # until they happened to click something. Keyed off search_in_progress
    # rather than "status" — status flickers between "searching"/"watching"
    # from one keyword to the next, so gating on it directly could land on
    # a "watching" render mid-search and then never refresh again.
    # Stops on its own once the search truly ends, so it isn't polling
    # forever — search_in_progress is guaranteed to flip back to False even
    # if the search hits an error. Uses streamlit_autorefresh (a soft
    # rerun over Streamlit's own websocket) rather than a <meta refresh>
    # tag — a meta refresh is a real browser navigation, which drops the
    # session (and logs the user straight back out) on every tick.
    if state.get("search_in_progress"):
        st_autorefresh(interval=5000, key="search_progress_autorefresh")

    with st.container(key="sidebar_bottom_actions"):
        st.markdown("---")

        if st.button("🔑 Change Password", use_container_width=True, key="btn_change_password"):
            _open_sidebar_panel("show_change_password")
            st.rerun()

        if st.button("🚪 Log out", use_container_width=True, key="btn_logout"):
            st.session_state.authenticated = False
            # Chat history is per-user and loaded from the DB — clear the
            # in-memory copy so the next login never sees this user's chat.
            for _k in ("chatbot_open", "chatbot_messages", "chatbot_pending",
                       "chatbot_fullscreen", "chatbot_session_id",
                       "chatbot_sidebar_open", "chatbot_menu_for"):
                st.session_state.pop(_k, None)
            _clear_session_cookie()
            time.sleep(0.25)
            st.rerun()



# ── ADMIN FULL-SCREEN OVERRIDE ──────────────────────────────
# Takes over the whole main area instead of the normal dashboard content
# when open, same idea as the single-tender detail view further below.
# Role is re-checked here too (not just at the sidebar button) so a
# regular user can't reach it by fiddling with session state.
if st.session_state.show_admin and st.session_state.user_role == "admin":
    render_admin_page()
    st.stop()

if st.session_state.show_create_user and st.session_state.user_role == "admin":
    render_create_user_page()
    st.stop()

if st.session_state.show_change_password:
    render_change_password_page()
    st.stop()

if st.session_state.show_relevant_keywords:
    render_relevant_keywords_page()
    st.stop()

if st.session_state.show_patterns_learned:
    render_patterns_learned_page()
    st.stop()

if st.session_state.show_search_history:
    render_search_history_page()
    st.stop()

# ── FILTER BAR ────────────────────────────────────────────


st.markdown("<br>", unsafe_allow_html=True)
# ── STATS ROW ─────────────────────────────────────────────
# ── STATS ROW ─────────────────────────────────────────────
stats = get_stats()
active = st.session_state.active_section
c1, c2, c3, c4, c5 = st.columns(5)

with c1:
    st.markdown('<div class="stats-sticky"></div>', unsafe_allow_html=True)
    if st.button(
        f"📋  {stats['total_tenders']}\nTotal Tenders",
        key="btn_all", use_container_width=True,
        type="primary" if active == "all" else "secondary"
    ):
        st.session_state.active_section = "all"
        st.session_state.page_num = 1
        st.session_state.chart_drill_filter = None
        st.session_state.trend_chart_reset_counter += 1
        st.session_state.dept_chart_reset_counter += 1
        st.session_state.scroll_to_table = True
        st.session_state.viewing_tender_id = None
        st.rerun()

with c2:
    if st.button(
        f"🟢  {stats['new']}\nNew Tenders",
        key="btn_new", use_container_width=True,
        type="primary" if active == "new" else "secondary"
    ):
        st.session_state.active_section = "new"
        st.session_state.page_num = 1
        st.session_state.chart_drill_filter = None
        st.session_state.trend_chart_reset_counter += 1
        st.session_state.dept_chart_reset_counter += 1
        st.session_state.scroll_to_table = True
        st.session_state.viewing_tender_id = None
        st.rerun()

with c3:
    if st.button(
        f"💾  {stats['saved']}\nSaved",
        key="btn_saved", use_container_width=True,
        type="primary" if active == "saved" else "secondary"
    ):
        st.session_state.active_section = "saved"
        st.session_state.page_num = 1
        st.session_state.chart_drill_filter = None
        st.session_state.trend_chart_reset_counter += 1
        st.session_state.dept_chart_reset_counter += 1
        st.session_state.scroll_to_table = True
        st.session_state.viewing_tender_id = None
        st.rerun()

with c4:
    if st.button(
        f"🕒  {stats['expired']}\nExpired Tenders",
        key="btn_searches", use_container_width=True,
        type="primary" if active == "expired" else "secondary"
    ):
        st.session_state.active_section = "expired"
        st.session_state.page_num = 1
        st.session_state.chart_drill_filter = None
        st.session_state.trend_chart_reset_counter += 1
        st.session_state.dept_chart_reset_counter += 1
        st.session_state.scroll_to_table = True
        st.session_state.viewing_tender_id = None
        st.rerun()

with c5:
    if st.button(
        f"🎯  {stats['relevant']}\nRelevant Tenders",
        key="btn_relevant", use_container_width=True,
        type="primary" if active == "relevant" else "secondary"
    ):
        st.session_state.active_section = "relevant"
        st.session_state.page_num = 1
        st.session_state.chart_drill_filter = None
        st.session_state.trend_chart_reset_counter += 1
        st.session_state.dept_chart_reset_counter += 1
        st.session_state.scroll_to_table = True
        st.session_state.viewing_tender_id = None
        st.rerun()

# Keeps the stat-card row's fixed position/size synced to the actual main
# content area. Plain CSS position:sticky doesn't work here because
# Streamlit wraps every element in its own snug per-element box (barely
# taller than the row itself), which caps how far a sticky element can
# travel before falling out of view — nowhere near the length of the page.
# st.markdown can't run <script> tags (innerHTML-inserted scripts are inert
# in browsers), so this runs inside an st.iframe and reaches back into the
# real page via window.parent.document instead.
st.iframe("""
<script>
(function() {
    function syncStickyStats() {
        const doc = window.parent.document;
        const marker = doc.querySelector('div.stats-sticky');
        const main = doc.querySelector('section[data-testid="stMain"]');
        if (!marker || !main) return;
        const row = marker.closest('[data-testid="stHorizontalBlock"]');
        if (!row) return;
        const rect = main.getBoundingClientRect();
        // stMain's own box starts at the very top of the viewport, hidden
        // behind Streamlit's fixed header overlay — without this offset the
        // row renders underneath that header instead of below it.
        const header = doc.querySelector('header[data-testid="stHeader"]');
        const headerHeight = header ? header.getBoundingClientRect().height : 0;
        row.style.top = Math.max(rect.top, headerHeight) + 'px';
        row.style.left = rect.left + 'px';
        row.style.width = rect.width + 'px';
    }
    syncStickyStats();
    window.parent.addEventListener('resize', syncStickyStats);
    try {
        const main = window.parent.document.querySelector('section[data-testid="stMain"]');
        if (main) new ResizeObserver(syncStickyStats).observe(main);
    } catch (e) {}
    // Streamlit reruns replace the row's DOM node on every interaction;
    // a short poll keeps it positioned without needing to hook Streamlit's
    // internal rerun lifecycle.
    clearInterval(window.parent.__statsStickySync);
    window.parent.__statsStickySync = window.parent.setInterval(syncStickyStats, 400);
})();
</script>
""", height=1)

# ── OVERVIEW CHARTS — real data from the tenders table, one GeM/KPPP
# toggle per chart (defaults to GeM). Purely additive/visual: no existing
# query, filter, or button behavior above/below this block is touched. ──
DEPARTMENT_BAR_COLORS = ["#4F46E5", "#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#94A3B8"]
# KPPP only ever has these two saved procuring entities, so unlike GeM's
# positional palette above, these get fixed identity colors regardless of
# which one has more tenders this week.
KPPP_DEPARTMENT_COLORS = {
    "Karnataka State Police": "#EF4444",  # red
    "Karnataka State Road Transport Corporation": "#10B981",  # green
}
# GeM's palette is otherwise positional (Nth-largest department gets the Nth
# color) — these two specifically always swap to the opposite color from
# whatever position would normally assign them, regardless of which one is
# currently bigger.
GEM_DEPARTMENT_COLOR_OVERRIDES = {
    "Ministry of DefenceDepartment of Military Affairs": "#F59E0B",
    "Ministry of DefenceDepartment of Defence Production": "#4F46E5",
    "Ministry of Petroleum and Natural Gas": "#EF4444",
    "Ministry of EducationDepartment of Higher Education": "#A855F7",
}

st.markdown("""
<style>
.st-key-chart_trend_range { margin-left: auto; max-width: 128px; }
.st-key-chart_trend_range .react-aria-ComboBox,
.st-key-chart_trend_range .react-aria-ComboBox > div {
    height: 32px !important;
    min-height: 32px !important;
    background-color: #F1F5F9 !important;
    border: none !important;
    border-radius: 7px !important;
    box-shadow: none !important;
}
.st-key-chart_trend_range input {
    font-size: 11px !important;
    padding-top: 0 !important;
    padding-bottom: 0 !important;
}
.st-key-chart_trend_range svg { width: 11px !important; height: 11px !important; }
</style>
""", unsafe_allow_html=True)


def _chart_portal_toggle(key: str) -> str:
    # A dropdown rather than pills — adding more portals later only grows
    # this list, instead of needing more and more pills to fit in the row.
    # KPPP first so it's the default (st.selectbox defaults to its first
    # option).
    label = st.selectbox(
        "Portal", ["KPPP (KA)", "GeM"],
        key=key, label_visibility="collapsed",
    )
    return "KPPP" if label and "KPPP" in label else "GeM"


chart_col1, chart_col2 = st.columns(2)

@st.fragment
def _render_dept_chart():
    # Same fragment-scoped pattern as the Trend chart: the portal toggle and
    # re-clicking an already-selected slice stay fast/local, and only a
    # genuine new department click escalates to a full app rerun (needed so
    # the drilled-down table below can update).
    with st.container(border=True):
        st.markdown('<div class="dept-chart-card-marker"></div>', unsafe_allow_html=True)
        dept_title_col, dept_toggle_col = st.columns([2, 2])
        with dept_title_col:
            st.markdown("**Tenders by Department**")
        with dept_toggle_col:
            dept_portal = _chart_portal_toggle("chart_dept_portal")

        # Remount with a clean selection whenever the portal changes, so a
        # stale click from the previous portal's data can't get replayed.
        if st.session_state.dept_chart_last_portal != dept_portal:
            st.session_state.dept_chart_last_portal = dept_portal
            st.session_state.dept_chart_reset_counter += 1
            # scope="fragment" is only valid when THIS run was itself
            # triggered as a fragment-scoped rerun (e.g. the portal toggle
            # click) — this same code also runs during ordinary full-page
            # reruns (a stat-card click, Clear, etc.), where it isn't, and
            # Streamlit raises for that. Fall back to a full rerun then.
            try:
                st.rerun(scope="fragment")
            except st.errors.StreamlitAPIException:
                st.rerun()

        dept_data = get_department_counts(dept_portal)
        if dept_data:
            dept_df = pd.DataFrame(dept_data, columns=["Department", "Count"])
            total_dept = int(dept_df["Count"].sum())
            dept_df["Pct"] = (dept_df["Count"] / total_dept * 100).round().astype(int) if total_dept else 0
            dept_df["idx"] = range(len(dept_df))
            dept_df["CountLabel"] = dept_df["Count"].astype(str) + " (" + dept_df["Pct"].astype(str) + "%)"
            dept_color_domain = dept_df["Department"].tolist()
            named_departments = [d for d in dept_color_domain if d != "Others"]
            if dept_portal == "KPPP":
                # KSP/KSRTC keep their fixed identity colors; any other KPPP
                # department (there can be more than two once other saved
                # filters bring new ones in) cycles through the fallback
                # palette instead of all collapsing onto the same grey —
                # grey is reserved for the literal "Others" bucket.
                fallback_i = 0
                dept_range_colors = []
                for d in dept_color_domain:
                    if d in KPPP_DEPARTMENT_COLORS:
                        dept_range_colors.append(KPPP_DEPARTMENT_COLORS[d])
                    elif d == "Others":
                        dept_range_colors.append("#94A3B8")
                    else:
                        dept_range_colors.append(DEPARTMENT_BAR_COLORS[fallback_i % len(DEPARTMENT_BAR_COLORS)])
                        fallback_i += 1
            else:
                dept_range_colors = [
                    GEM_DEPARTMENT_COLOR_OVERRIDES.get(d, DEPARTMENT_BAR_COLORS[i % len(DEPARTMENT_BAR_COLORS)])
                    for i, d in enumerate(dept_color_domain)
                ]

            donut_col, legend_col = st.columns([1, 1])

            with donut_col:
                # mark_arc renders one arc per row already, so (unlike the
                # Trend line) the param can bind directly to it — no need for
                # a separate overlay mark to get real per-datum click targets.
                dept_click = alt.selection_point(
                    name="dept_click", on="click", fields=["Department"], empty=False,
                )
                donut = alt.Chart(dept_df).mark_arc(innerRadius=52, outerRadius=82).encode(
                    theta=alt.Theta("Count:Q", stack=True),
                    color=alt.Color(
                        "Department:N",
                        scale=alt.Scale(domain=dept_color_domain, range=dept_range_colors),
                        legend=None,
                    ),
                    order=alt.Order("Count:Q", sort="descending"),
                    tooltip=[alt.Tooltip("Department:N"), alt.Tooltip("Count:Q"), alt.Tooltip("Pct:Q", title="Percent")],
                ).add_params(dept_click)
                center_total = alt.Chart(pd.DataFrame({"v": [str(total_dept)]})).mark_text(
                    size=24, fontWeight="bold", color="#0F172A", dy=-5,
                ).encode(text="v:N")
                center_label = alt.Chart(pd.DataFrame({"v": ["TOTAL"]})).mark_text(
                    size=10, color="#64748B", dy=13,
                ).encode(text="v:N")
                donut_chart = (donut + center_total + center_label).properties(height=190, width="container")
                dept_event = st.altair_chart(
                    donut_chart, width="stretch", on_select="rerun",
                    key=f"dept_chart_select_{st.session_state.dept_chart_reset_counter}",
                )

            def _drill_into_department(clicked_dept: str):
                current_drill = st.session_state.chart_drill_filter
                if not clicked_dept or (
                    current_drill
                    and current_drill.get("type") == "department"
                    and current_drill.get("department") == clicked_dept
                    and current_drill.get("portal") == dept_portal
                ):
                    return
                is_others = clicked_dept == "Others"
                st.session_state.chart_drill_filter = {
                    "type": "department",
                    "department": clicked_dept,
                    "portal": dept_portal,
                    "label": clicked_dept,
                    "is_others": is_others,
                    "named_departments": named_departments if is_others else [],
                }
                _sync_main_portal_dropdown(dept_portal)
                st.session_state.active_section = "all"
                st.session_state.viewing_tender_id = None
                st.session_state.page_num = 1
                st.session_state.scroll_to_table = True
                st.rerun()

            with legend_col:
                st.markdown("""
                <style>
                div[class*="st-key-dept_legend_btn_"] button {
                    background: transparent !important;
                    border: none !important;
                    box-shadow: none !important;
                    padding: 3px 6px !important;
                    min-height: 0 !important;
                    height: auto !important;
                    font-size: 12.5px !important;
                    font-weight: 400 !important;
                    color: #334155 !important;
                    justify-content: flex-start !important;
                    text-align: left !important;
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    display: block !important;
                    width: 100% !important;
                }
                div[class*="st-key-dept_legend_btn_"] button:hover {
                    background: #F1F5F9 !important;
                    color: #334155 !important;
                    border: none !important;
                }
                div[class*="st-key-dept_legend_btn_"] button:focus:not(:active) {
                    box-shadow: none !important;
                    color: #334155 !important;
                }
                div[class*="st-key-dept_legend_btn_"] button div,
                div[class*="st-key-dept_legend_btn_"] button span,
                div[class*="st-key-dept_legend_btn_"] button [data-testid="stMarkdownContainer"] {
                    justify-content: flex-start !important;
                    text-align: left !important;
                    width: 100% !important;
                    display: block !important;
                }
                div[class*="st-key-dept_legend_btn_"] p {
                    font-size: 12.5px !important;
                    text-align: left !important;
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                }
                div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.dept-legend-marker) {
                    gap: 0.1rem !important;
                }
                </style>
                """, unsafe_allow_html=True)
                st.markdown('<div class="dept-legend-marker"></div>', unsafe_allow_html=True)
                for i, row in dept_df.iterrows():
                    color = dept_range_colors[i % len(dept_range_colors)]
                    dot_col, name_col, count_col = st.columns([0.07, 0.63, 0.30], vertical_alignment="center")
                    with dot_col:
                        st.markdown(
                            f'<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:{color};margin-top:2px;"></span>',
                            unsafe_allow_html=True,
                        )
                    with name_col:
                        if st.button(row["Department"], key=f"dept_legend_btn_{i}", use_container_width=True):
                            _drill_into_department(row["Department"])
                    with count_col:
                        st.markdown(
                            f'<div style="font-size:12.5px;color:#64748B;text-align:right;padding-top:3px;">{int(row["Count"])} ({int(row["Pct"])}%)</div>',
                            unsafe_allow_html=True,
                        )

            clicked_slices = dept_event.selection.get("dept_click") if dept_event and dept_event.selection else None
            if clicked_slices:
                _drill_into_department(clicked_slices[0].get("Department"))
        else:
            st.info(f"No {dept_portal} tenders yet.")


with chart_col1:
    _render_dept_chart()

@st.fragment
def _render_trend_chart():
    # Fragment-scoped: the portal toggle, the range dropdown, and re-clicking
    # an already-selected (or empty) point all rerun ONLY this box instead of
    # the whole page (sidebar, stat cards, department chart...) — that full
    # top-to-bottom rerun on every interaction was the real source of the lag.
    # A plain st.rerun() call still escalates to a full app rerun (its
    # default scope), which we use deliberately, and only once an actual new
    # date is picked, since the drilled-down table lives outside this
    # fragment and needs a full rerun to update.
    with st.container(border=True):
        trend_title_col, trend_toggle_col, trend_range_col = st.columns([2, 2, 2], vertical_alignment="center")
        with trend_title_col:
            st.markdown("**Tenders Trend**")
        with trend_toggle_col:
            st.markdown('<div class="trend-toggle-col-marker"></div>', unsafe_allow_html=True)
            trend_portal = _chart_portal_toggle("chart_trend_portal")
        with trend_range_col:
            st.markdown('<div class="trend-range-col-marker"></div>', unsafe_allow_html=True)
            trend_range = st.selectbox(
                "Range", ["Last 7 Days", "Last 14 Days", "Last 30 Days"],
                key="chart_trend_range", label_visibility="collapsed", filter_mode=None,
            )

        # A stale click selection from before a portal switch would otherwise
        # get replayed against the new portal's data on the very next event —
        # remount the chart (fresh, empty selection) whenever the portal changes.
        if st.session_state.trend_chart_last_portal != trend_portal:
            st.session_state.trend_chart_last_portal = trend_portal
            st.session_state.trend_chart_reset_counter += 1
            # Same caveat as the department chart: scope="fragment" is only
            # valid when this run was itself a fragment-scoped rerun, which
            # isn't always the case here (e.g. a stat-card click also runs
            # this code, as an ordinary full-page rerun).
            try:
                st.rerun(scope="fragment")
            except st.errors.StreamlitAPIException:
                st.rerun()

        days = {"Last 7 Days": 7, "Last 14 Days": 14, "Last 30 Days": 30}[trend_range]
        trend_data = get_daily_trend(trend_portal, days=days)
        trend_df = pd.DataFrame(trend_data, columns=["Date", "Count"])
        trend_df["Label"] = pd.to_datetime(trend_df["Date"]).dt.strftime("%d %b")
        trend_x = alt.X("Label:N", sort=list(trend_df["Label"]), title=None)
        trend_y = alt.Y("Count:Q", title="No. of Tenders")
        trend_tooltip = [alt.Tooltip("Label:N", title="Date"), alt.Tooltip("Count:Q")]
        trend_click = alt.selection_point(
            name="trend_click", on="click", nearest=True, empty=False,
            fields=["Date", "Label"],
        )
        base_line = alt.Chart(trend_df).mark_line(color="#4F46E5").encode(x=trend_x, y=trend_y)
        # A dedicated, larger point layer carries the click-selection param —
        # mark_line's own point=True overlay isn't a real per-datum hit target
        # for Vega-Lite selections, so clicks on it never registered.
        click_points = alt.Chart(trend_df).mark_point(size=120, filled=True, color="#4F46E5", opacity=1).encode(
            x=trend_x, y=trend_y, tooltip=trend_tooltip,
        ).add_params(trend_click)
        # 190 would match the donut chart's own plot height, but the
        # rotated "05 Sep" x-axis tick labels below it add extra vertical
        # space the donut side doesn't have (it has a legend instead) —
        # trimmed here so the two cards end up the same overall height.
        trend_chart_obj = (base_line + click_points).properties(height=174)
        trend_event = st.altair_chart(
            trend_chart_obj, width='stretch', on_select="rerun",
            key=f"trend_chart_select_{st.session_state.trend_chart_reset_counter}",
        )
        st.markdown('<div class="trend-chart-bottom-marker"></div>', unsafe_allow_html=True)

        clicked_points = trend_event.selection.get("trend_click") if trend_event and trend_event.selection else None
        if clicked_points:
            clicked = clicked_points[0]
            clicked_date, clicked_label = clicked.get("Date"), clicked.get("Label")
            current_drill = st.session_state.chart_drill_filter
            if clicked_date and (
                not current_drill
                or current_drill.get("type") != "date"
                or current_drill.get("date") != clicked_date
                or current_drill.get("portal") != trend_portal
            ):
                st.session_state.chart_drill_filter = {
                    "type": "date", "date": clicked_date, "portal": trend_portal, "label": clicked_label,
                }
                _sync_main_portal_dropdown(trend_portal)
                st.session_state.active_section = "all"
                st.session_state.viewing_tender_id = None
                st.session_state.page_num = 1
                st.session_state.scroll_to_table = True
                st.rerun()


with chart_col2:
    _render_trend_chart()

# Auto-refresh while agent is actively searching
if state.get("status") in ("searching", "expanding", "learning"):
    st.caption("🔄 Agent is searching... dashboard will refresh automatically")
    import time
    time.sleep(40)
    st.rerun()
# ── SECTION CONTENT ───────────────────────────────────────
st.markdown('<div id="tenders-table-anchor"></div>', unsafe_allow_html=True)
if st.session_state.scroll_to_table:
    st.session_state.scroll_to_table = False
    import time as _time
    # Streamlit reuses/skips re-mounting an iframe when its rendered content
    # is byte-for-byte identical to a previous one at the same script
    # position — which this always is, since the script text never changes —
    # so the embedded <script> only ever ran on the very first click. The
    # nonce (invisible, inside an HTML comment) makes each render unique so
    # the browser always treats it as fresh content and actually re-runs it.
    st.iframe(f"""
    <!-- nonce:{_time.time()} -->
    <script>
    (function() {{
        const doc = window.parent.document;
        const main = doc.querySelector('section[data-testid="stMain"]');
        const anchor = doc.getElementById('tenders-table-anchor');
        if (!main || !anchor) return;
        // stMain scrolls internally (not the window), and the stat-card row
        // is position:fixed on top of it, so a plain scrollIntoView would
        // still leave the target tucked underneath that fixed bar — offset
        // by its height so the table lands just below it instead.
        const barRow = doc.querySelector('div.stats-sticky');
        const bar = barRow ? barRow.closest('[data-testid="stHorizontalBlock"]') : null;
        const barHeight = bar ? bar.getBoundingClientRect().height : 0;
        const targetTop = anchor.getBoundingClientRect().top - main.getBoundingClientRect().top + main.scrollTop - barHeight - 12;
        main.scrollTo({{top: targetTop, behavior: 'smooth'}});
    }})();
    </script>
    """, height=1)

section = st.session_state.active_section

# ── PATTERNS ──────────────────────────────────────────────
if section == "patterns":
    patterns = get_learned_patterns()
    if patterns:
        for p in patterns:
            try:
                kws = json.loads(p['keywords'])
            except:
                kws = []
            pattern_portal = p['portal'] if p['portal'] else "GeM"
            portal_badge = "🔵 GeM" if pattern_portal == "GeM" else "🟠 KPPP (KA)"
            col_p, col_d = st.columns([4, 1])
            with col_p:
                st.markdown(f"""
                <div class='tender-card'>
                    <div class='tender-title'>📂 {p['category']} <span style="font-size:0.75em;font-weight:500;color:#64748B;">— {portal_badge}</span></div>
                    <div class='tender-meta'>Weight: {p['weight']} | Keywords: {', '.join(kws[:5])}</div>
                </div>
                """, unsafe_allow_html=True)
            with col_d:
                if st.button("🗑️ Delete", key=f"del_pattern_{p['id']}"):
                    delete_pattern(p['id'])
                    st.success("Pattern deleted!")
                    st.rerun()
    else:
        st.info("No patterns learned yet.")

# ── LOG ───────────────────────────────────────────────────
elif section == "log":
    st.markdown("### 📡 Live Agent Activity")
    st.caption("Real-time log of what the agent is doing")
    log_entries = get_log()
    if log_entries:
        log_text = "\n".join(reversed(log_entries[-50:]))
        st.markdown(
            f"<div class='log-box'>{log_text}</div>",
            unsafe_allow_html=True
        )
    else:
        st.info("Agent log is empty. Start the agent to see activity.")
    if st.button("🔄 Refresh Log", use_container_width=True):
        st.rerun()

# ── TENDERS ───────────────────────────────────────────────
# ── TENDERS ───────────────────────────────────────────────
else:
    # Drilled in from a Tenders Trend point or Department slice click — show
    # only the matching tenders, regardless of which stat-card section was
    # active before the click. Fetched directly instead of via the section
    # branches below since those would just be discarded (avoids two
    # full-table reads per rerun).
    drill = st.session_state.chart_drill_filter
    if drill and drill.get("type") == "date":
        tenders = [
            t for t in get_all_tenders()
            if t["portal"] == drill["portal"] and str(t.get("found_at", "")).startswith(drill["date"])
        ]
        current_portal_filter = drill["portal"]
        section_title = f"📅 Tenders found on {drill.get('label', drill['date'])} — {drill['portal']}"
    elif drill and drill.get("type") == "department":
        if drill.get("is_others"):
            named = set(drill.get("named_departments", []))
            tenders = [
                t for t in get_all_tenders()
                if t["portal"] == drill["portal"] and t.get("department") not in named
            ]
        else:
            tenders = [
                t for t in get_all_tenders()
                if t["portal"] == drill["portal"] and t.get("department") == drill["department"]
            ]
        current_portal_filter = drill["portal"]
        section_title = f"🏢 Tenders for {drill.get('label', drill['department'])} — {drill['portal']}"
    else:
        if section == "new":
            tenders = get_all_tenders("new")
            section_title = "🟢 New Tenders"
        elif section == "saved":
            tenders = get_all_tenders("saved")
            section_title = "💾 Saved Tenders"
        elif section == "expired":
            tenders = get_all_tenders("expired")
            section_title = "🕒 Expired Tenders"
        elif section == "relevant":
            tenders = get_relevant_tenders_list()
            section_title = "🎯 Relevant Tenders"
        else:
            # "All Tenders" means everything still active — a tender past
            # its deadline moves to its own Expired card instead of
            # cluttering this list with closed-out bids.
            tenders = [t for t in get_all_tenders() if t.get("status") != "expired"]
            section_title = "📋 All Tenders"

        current_portal_filter = st.session_state.get("portal_filter_value", "GeM")
        tenders = [t for t in tenders if t["portal"] == current_portal_filter]

    sort_by = st.session_state.get("sort_by_select", "Relevance")
    if sort_by == "Newest First":
        tenders = sorted(tenders, key=lambda x: x["found_at"], reverse=True)
    elif sort_by == "Deadline":
        tenders = sorted(tenders, key=lambda x: x.get("deadline", ""))
    else:
        tenders = sorted(tenders, key=lambda x: x.get("relevance_score", 0), reverse=True)

    # A single st.empty() placeholder around this whole detail/list
    # toggle — clicking View/Back-to-list otherwise showed a brief flash
    # of the old table mixed with the new page while Streamlit streamed
    # the rerun's content in piece by piece. Writing into one placeholder
    # replaces its entire contents as a single atomic swap instead, so
    # the old and new content never render mixed together mid-transition.
    _detail_or_list_placeholder = st.empty()
    with _detail_or_list_placeholder.container():
        # ── DETAIL PAGE ───────────────────────────────────────
        if st.session_state.viewing_tender_id is not None:
            tid = st.session_state.viewing_tender_id
            match = [t for t in tenders if t["id"] == tid]
            if not match:
                match = [t for t in get_all_tenders() if t["id"] == tid]
            if not match:
                st.warning("Tender not found.")
                if st.button("◀ Back to list"):
                    st.session_state.viewing_tender_id = None
                    st.rerun()
            else:
                tender = match[0]
                portal = tender.get("portal", "")
                value = tender.get("value", "N/A")
                deadline = tender.get("deadline", "N/A")
                found_via = tender.get("found_via", "")
                status_val = tender.get("status", "new")

                if st.button("◀ Back to list"):
                    st.session_state.viewing_tender_id = None
                    st.rerun()

                st.markdown(f"### {tender.get('title', 'Unknown')}")
                st.markdown("---")

                col_a, col_b = st.columns(2)

                with col_a:
                    st.markdown("#### 📋 Tender Details")
                    details_data = [
                        ("Tender Title", tender.get("title")),
                        ("Tender Number", tender.get("tender_number")),
                        ("Portal", portal),
                        ("Department", tender.get("department")),
                        ("Location", tender.get("location")),
                        ("Tender Type", tender.get("tender_type")),
                        ("Estimated Value", value),
                        ("EMD Amount", tender.get("emd")),
                        ("Published Date", tender.get("published_date")),
                        ("Submission Deadline", deadline),
                        ("Category", tender.get("category")),
                        ("Found Via Search", found_via),
                    ]
                    # GeM has no page that displays a single tender's info — the
                    # bid number link on GeM's own site always downloads the bid
                    # PDF, even there. So this points at GeM's generic search
                    # landing page rather than the stored per-tender doc URL,
                    # which would silently trigger a PDF download instead of
                    # opening a page.
                    portal_home_urls = {
                        "GeM": "https://bidplus.gem.gov.in/all-bids",
                        "KPPP": "https://kppp.karnataka.gov.in/#/portal/searchTender/live",
                    }
                    portal_home_url = portal_home_urls.get(portal, "")

                    for label, val in details_data:
                        if val and val not in ["", "None", "null"]:
                            if label == "Tender Number" and portal_home_url:
                                value_html = f"<a href='{portal_home_url}' target='_blank'>{val}</a>"
                            else:
                                value_html = f"<span class='detail-value'>{val}</span>"
                            st.markdown(
                                f"<span class='detail-label'>{label}:</span> {value_html}",
                                unsafe_allow_html=True
                            )

                with col_b:
                    st.markdown("#### 📁 Downloads")

                    downloaded_files = tender.get("downloaded_files", [])
                    if isinstance(downloaded_files, str):
                        try:
                            downloaded_files = json.loads(downloaded_files)
                        except:
                            downloaded_files = []

                    if portal == "KPPP":
                        fresh = [t for t in get_all_tenders() if t['id'] == tid]
                        if fresh:
                            fresh_files = fresh[0].get("downloaded_files", [])
                            if isinstance(fresh_files, str):
                                try:
                                    fresh_files = json.loads(fresh_files)
                                except:
                                    fresh_files = []
                            if fresh_files:
                                downloaded_files = fresh_files

                    if not downloaded_files and portal == "KPPP":
                        st.caption("📄 Documents will appear after analysis")

                    if downloaded_files:
                        st.markdown("**📁 Downloaded Tender Files:**")
                        for file_path in downloaded_files:
                            if os.path.exists(file_path):
                                filename = os.path.basename(file_path)
                                with open(file_path, "rb") as f:
                                    file_bytes = f.read()
                                st.download_button(
                                    label=f"⬇️ {filename}",
                                    data=file_bytes,
                                    file_name=filename,
                                    key=f"file_{tid}_{filename}_detail"
                                )
                    else:
                        if portal == "GeM":
                            st.caption("No files downloaded yet")

                    st.markdown("---")

                    analysis_path = tender.get("analysis_pdf_path", "")

                    if analysis_path and os.path.exists(analysis_path):
                        with open(analysis_path, "rb") as f:
                            pdf_bytes = f.read()

                        # Chrome blocks top-level navigation to both data: URLs
                        # and file:// URLs from an http(s) page (both were tried
                        # and both silently failed to open a new tab). The only
                        # reliable way to open the PDF in its own tab is to serve
                        # it over http like a real resource — Streamlit serves
                        # anything placed in dashboard/static/ at /app/static/
                        # when server.enableStaticServing is on (see
                        # .streamlit/config.toml), so copy the report there under
                        # a stable per-tender name and link to that.
                        static_dir = os.path.join(os.path.dirname(__file__), "static")
                        os.makedirs(static_dir, exist_ok=True)
                        static_name = f"analysis_{tid}.pdf"
                        static_path = os.path.join(static_dir, static_name)
                        if not os.path.exists(static_path) or os.path.getmtime(analysis_path) > os.path.getmtime(static_path):
                            with open(static_path, "wb") as f:
                                f.write(pdf_bytes)

                        # Cache-bust with the file's own mtime so the browser can't
                        # keep serving a stale cached copy of this same URL after
                        # Re-analyze produces a newer file — without this, "View
                        # Analysis" could silently keep showing an old report.
                        cache_bust = int(os.path.getmtime(analysis_path))
                        st.markdown(
                            f'<a href="app/static/{static_name}?v={cache_bust}" target="_blank" style="text-decoration:none;">'
                            f'<button style="width:100%;padding:10px;background:#8E44AD;'
                            f'color:white;border:none;border-radius:6px;cursor:pointer;'
                            f'font-size:14px;margin-bottom:8px;">👁️ View Analysis</button></a>',
                            unsafe_allow_html=True
                        )

                        st.download_button(
                            label="⬇️ Download Analysis",
                            data=pdf_bytes,
                            file_name=os.path.basename(analysis_path),
                            mime="application/pdf",
                            key=f"analysis_{tid}_detail",
                        )

                        if st.session_state.analyzing.get(tid):
                            st.info("⏳ Re-analyzing... please wait")
                        elif st.button(
                            "🔄 Re-analyze",
                            key=f"reanalyze_{tid}_detail",
                            help="Redo the analysis from scratch — use this if a "
                                 "past attempt failed partway (e.g. a KPPP CAPTCHA "
                                 "failure left no real documents to read).",
                        ):
                            clear_tender_analysis(tid)
                            st.session_state.analyzing[tid] = True
                            with st.spinner("🤖 Re-analyzing tender documents..."):
                                fresh_tender = dict(tender)
                                fresh_tender["downloaded_files"] = []
                                fresh_tender["analysis_pdf_path"] = None
                                path = analyze_tender(fresh_tender)
                            if path and os.path.exists(path):
                                update_tender_pdfs(tid, analysis_path=path)
                                st.session_state.analyzing[tid] = False
                                st.success("✅ Re-analysis ready!")
                                st.rerun()
                            else:
                                st.session_state.analyzing[tid] = False
                                st.error("❌ Re-analysis failed. Try again.")
                    else:
                        if st.session_state.analyzing.get(tid):
                            st.info("⏳ Generating analysis... please wait")
                        else:
                            if st.button(
                                "📊 Get Full Analysis",
                                key=f"analyze_{tid}_detail_{status_val}",
                                type="primary"
                            ):
                                st.session_state.analyzing[tid] = True
                                with st.spinner("🤖 Analyzing tender documents..."):
                                    path = analyze_tender(tender)
                                if path and os.path.exists(path):
                                    update_tender_pdfs(tid, analysis_path=path)
                                    st.session_state.analyzing[tid] = False
                                    st.success("✅ Analysis ready!")
                                    st.rerun()
                                else:
                                    st.error("❌ Analysis failed. Try again.")
                                    st.session_state.analyzing[tid] = False

                st.markdown("---")
                col_x, col_y, col_z = st.columns(3)

                with col_x:
                    if status_val == "saved":
                        if st.button("🔓 Unsave", key=f"unsave_{tid}_detail"):
                            update_tender_status(tid, "new")
                            st.rerun()
                    else:
                        if st.button("💾 Save", key=f"save_{tid}_detail"):
                            update_tender_status(tid, "saved")
                            st.success("Saved!")
                            st.rerun()

                with col_y:
                    if st.button("🗑️ Delete", key=f"delete_{tid}_detail"):
                        st.session_state[f"confirm_delete_{tid}"] = True

                    if st.session_state.get(f"confirm_delete_{tid}"):
                        st.warning("Are you sure you want to delete?")
                        col_yes, col_no = st.columns(2)
                        with col_yes:
                            if st.button("✅ Yes Delete", key=f"yes_{tid}_detail"):
                                delete_tender(tid)
                                st.session_state[f"confirm_delete_{tid}"] = False
                                st.session_state.viewing_tender_id = None
                                st.rerun()
                        with col_no:
                            if st.button("❌ Cancel", key=f"no_{tid}_detail"):
                                st.session_state[f"confirm_delete_{tid}"] = False
                                st.rerun()

                with col_z:
                    if status_val == "saved":
                        st.success("✅ Saved")
                    elif status_val == "skipped":
                        st.warning("⬜ Skipped")
                    else:
                        st.info("🟢 New")

        # ── TABLE LIST VIEW ───────────────────────────────────
        else:
            if drill:
                if drill.get("type") == "date":
                    banner_text = f"📅 Showing {len(tenders)} tender(s) found on **{drill.get('label', drill['date'])}** — {drill['portal']}"
                else:
                    banner_text = f"🏢 Showing {len(tenders)} tender(s) for **{drill.get('label', drill['department'])}** — {drill['portal']}"
                banner_col, clear_col = st.columns([5, 1], vertical_alignment="center")
                with banner_col:
                    st.info(banner_text)
                with clear_col:
                    st.markdown("""
                    <style>
                    div[class*="st-key-clear_chart_drill"] button {
                        background-color: #EF4444 !important;
                        border-color: #EF4444 !important;
                        color: #FFFFFF !important;
                    }
                    div[class*="st-key-clear_chart_drill"] button:hover {
                        background-color: #DC2626 !important;
                        border-color: #DC2626 !important;
                        color: #FFFFFF !important;
                    }
                    div[class*="st-key-clear_chart_drill"] button:focus:not(:active) {
                        color: #FFFFFF !important;
                        box-shadow: none !important;
                    }
                    </style>
                    """, unsafe_allow_html=True)
                    if st.button("✖ Clear", key="clear_chart_drill", use_container_width=True):
                        st.session_state.chart_drill_filter = None
                        st.session_state.trend_chart_reset_counter += 1
                        st.session_state.dept_chart_reset_counter += 1
                        st.rerun()

            col_portal, col_search, col_search_btn, col_sort, col_filter_btn = st.columns(
                [0.15, 0.5, 0.08, 0.17, 0.08], vertical_alignment="center"
            )
            with col_portal:
                st.markdown('<div class="col-marker-portal"></div>', unsafe_allow_html=True)
                # Dropdown always, on every screen size — a pill control
                # (the old wide-screen behaviour) only fits 2-3 options
                # comfortably; a dropdown scales to more portals later
                # without needing a layout redesign. KPPP is first in
                # PORTAL_FILTER_OPTIONS so it's the default.
                #
                # Driven by index= from portal_filter_value (which survives
                # reliably) rather than trusting the widget's own key to
                # remember its value — a stat-card button (Total/New/
                # Saved/...) calls st.rerun() as soon as its click is
                # handled, well before the script reaches this selectbox;
                # Streamlit drops a widget's session_state the moment a run
                # ends without instantiating it, so that early rerun wiped
                # this dropdown back to its default on the very next run,
                # even though portal_filter_value itself (not tied to any
                # widget) stayed correct the whole time. Recomputing the
                # shown option from that reliable value every render means
                # it can never drift from what the table is actually
                # showing, regardless of which button triggered the rerun.
                current_portal_label = _PORTAL_VALUE_TO_LABEL.get(
                    st.session_state.get("portal_filter_value", "KPPP"), PORTAL_FILTER_DEFAULT
                )
                picked_portal_label = st.selectbox(
                    "Portal",
                    PORTAL_FILTER_OPTIONS,
                    index=PORTAL_FILTER_OPTIONS.index(current_portal_label),
                    key="portal_filter_dropdown",
                    label_visibility="collapsed",
                    filter_mode=None,
                )
                if picked_portal_label != current_portal_label:
                    st.session_state.portal_filter_value = PORTAL_FILTER_MAP.get(picked_portal_label, "KPPP")
                    st.rerun()
            with col_search:
                st.markdown('<div class="col-marker-search"></div>', unsafe_allow_html=True)
                # st_keyup (a third-party "Components v2" widget) was used here for
                # live-as-you-type filtering, but its browser<->Python round-trip
                # can silently fail to land a typed value in Python — reproduced
                # with a full bid number like "GEM/2026/B/7915789": the input
                # correctly showed the typed text, but st.session_state never
                # picked it up, so the table stayed unfiltered. st.text_input is
                # Streamlit's own well-tested widget — it updates on Enter/blur
                # instead of every keystroke, which is a minor UX trade for
                # actually returning the right results every time.
                search_query = st.text_input(
                    "Search tenders",
                    key="table_search_query",
                    placeholder="🔍 Search by bid number, item name, department...",
                    on_change=lambda: st.session_state.update(page_num=1),
                    label_visibility="collapsed",
                )
            with col_search_btn:
                st.markdown('<div class="col-marker-search-btn"></div>', unsafe_allow_html=True)
                # Clicking this button blurs the text input first (same as
                # clicking anywhere else), which is what actually commits its
                # typed value in Streamlit — the button itself doesn't need to
                # do anything beyond that click triggering the rerun.
                st.button(
                    "", icon=":material/search:", key="apply_table_search_btn",
                    use_container_width=True, type="primary", help="Search"
                )
            with col_sort:
                st.markdown('<div class="col-marker-sort"></div>', unsafe_allow_html=True)
                st.selectbox(
                    "Sort By",
                    ["Relevance", "Newest First", "Deadline"],
                    key="sort_by_select",
                    label_visibility="collapsed",
                    filter_mode=None,
                )
            with col_filter_btn:
                st.markdown('<div class="col-marker-filter-btn"></div>', unsafe_allow_html=True)
                current_portal = st.session_state.get("portal_filter_value", "KPPP")
                with st.popover(
                    "", icon=":material/filter_alt:", key="open_filters_btn",
                    use_container_width=True, type="primary", help="Filters"
                ):
                    st.selectbox(
                        "Quantity",
                        QUANTITY_FILTER_OPTIONS,
                        key="quantity_filter_bucket",
                    )
                    ADD_DEPT_SENTINEL = "➕ Add Department..."
                    if current_portal == "KPPP":
                        # Plain session_state the app manages itself, not a
                        # widget's own bound value — a widget's key gets its
                        # session_state entry silently reset by Streamlit
                        # once that widget stops being instantiated for a
                        # run (which happens here every time add-mode
                        # swaps the selectbox out for the text box), so
                        # relying on the selectbox's own key to remember
                        # "add mode is on" or "which department is chosen"
                        # across that swap doesn't actually hold.
                        st.session_state.setdefault("kppp_dept_selected", "All Departments")
                        st.session_state.setdefault("kppp_dept_add_mode", False)
                        dept_rows = get_department_filters("KPPP")
                        dept_options = ["All Departments"] + [d["label"] for d in dept_rows] + [ADD_DEPT_SENTINEL]

                        if st.session_state["kppp_dept_add_mode"]:
                            st.markdown("Department")
                            # The text box + "+" need to be inside a form —
                            # a plain st.button next to a text_input can
                            # fire its click before the text input's own
                            # change event has committed the typed value to
                            # session_state, so the add silently saw an
                            # empty string and did nothing. A form only
                            # reads its widgets' values at submit time,
                            # which sidesteps that race entirely.
                            with st.form(key="add_dept_filter_form", clear_on_submit=True, border=False):
                                new_dept_col, new_dept_btn_col = st.columns([4, 1])
                                new_dept_name = new_dept_col.text_input(
                                    "Department", placeholder="Enter department name",
                                    label_visibility="collapsed", key="new_dept_filter_input",
                                )
                                if new_dept_btn_col.form_submit_button("➕", use_container_width=True):
                                    if new_dept_name.strip():
                                        add_department_filter("KPPP", new_dept_name.strip())
                                        st.session_state["kppp_dept_selected"] = new_dept_name.strip()
                                        st.session_state["kppp_dept_add_mode"] = False
                                        st.rerun()
                            if st.button("← Back to department list", key="back_to_dept_list_btn"):
                                st.session_state["kppp_dept_add_mode"] = False
                                st.rerun()
                        else:
                            picked = st.selectbox(
                                "Department", dept_options,
                                index=dept_options.index(st.session_state["kppp_dept_selected"])
                                if st.session_state["kppp_dept_selected"] in dept_options else 0,
                                key="department_filter_kppp_select",
                            )
                            if picked == ADD_DEPT_SENTINEL:
                                st.session_state["kppp_dept_add_mode"] = True
                                st.rerun()
                            else:
                                st.session_state["kppp_dept_selected"] = picked
                                if picked != "All Departments":
                                    if st.button(
                                        "🗑️ Remove this department", key="delete_dept_filter_btn",
                                        use_container_width=True,
                                    ):
                                        delete_department_filter("KPPP", picked)
                                        st.session_state["kppp_dept_selected"] = "All Departments"
                                        st.rerun()
                    else:
                        # GeM's dropdown is data-driven, not a saved list —
                        # the busiest departments right now, recomputed each
                        # time (no "Others" bucket here, that's chart-only;
                        # no add-new option either, same as before). Always
                        # rendered every run, so it doesn't hit the same
                        # widget-state issue KPPP's conditional swap does.
                        gem_counts = [
                            d for d in get_department_counts("GeM", top_n=10)
                            if d[0] != "Others"
                        ]
                        dept_options = ["All Departments"] + [d[0] for d in gem_counts]
                        st.selectbox("Department", dept_options, key="department_filter_gem")
            department_filter_value = (
                st.session_state.get("kppp_dept_selected", "All Departments") if current_portal == "KPPP"
                else st.session_state.get("department_filter_gem", "All Departments")
            )
            tenders = fuzzy_filter_tenders(tenders, search_query)
            tenders = _apply_quantity_filter(tenders, st.session_state.get("quantity_filter_bucket", "All Quantities"))
            tenders = _apply_department_filter(tenders, current_portal, department_filter_value)

            if not tenders:
                st.info("🔍 No tenders found yet.")
            else:
                page_size = 10
                total_pages = max(1, (len(tenders) - 1) // page_size + 1)

                if st.session_state.page_num > total_pages:
                    st.session_state.page_num = 1

                st.caption(f"{len(tenders)} tenders — Page {st.session_state.page_num} of {total_pages}")
                st.markdown("<div style='margin-bottom:8px;'></div>", unsafe_allow_html=True)

                start = (st.session_state.page_num - 1) * page_size
                end = start + page_size
                page_tenders = tenders[start:end]

                # Table header — the header + all data rows live inside their
                # own container so the horizontal-scroll CSS (mobile only) can
                # scope overflow-x to just this box instead of the whole page;
                # previously these were plain top-level elements, so scrolling
                # the wide table on mobile dragged the entire page sideways too.
                # Price only shows in the Relevant Tenders view — KPPP
                # tenders often carry a real estimated value (GeM's don't,
                # at L1 it's just "See bid document"), so this column is
                # only meaningfully useful on this one filtered view rather
                # than cluttering every tender list with mostly-blank cells.
                # The list is always filtered to one portal at a time (the
                # GeM/KPPP pill above), so this only needs the KPPP case —
                # a GeM-filtered view has nothing to put in a Price column
                # (GeM never exposes an amount at this stage), so the
                # column is left out entirely rather than showing empty.
                show_price = active == "relevant" and current_portal_filter == "KPPP"
                col_widths = [0.5, 1.5, 2.1, 1.3, 1.6, 1.2, 1.2]
                if show_price:
                    col_widths.append(1.1)
                col_widths += [0.8, 0.8, 0.8]

                with st.container():
                    st.markdown('<div class="table-card-marker"></div>', unsafe_allow_html=True)
                    header_cols = st.columns(col_widths)
                    h1, h2, h3, h4, h5, h6, h7 = header_cols[:7]
                    h_rest = header_cols[7:]
                    if show_price:
                        h_price, h8, h9, h10 = h_rest
                    else:
                        h8, h9, h10 = h_rest
                    h1.markdown('<div class="tbl-header-row"></div><span class="th-cell">Sl No</span>', unsafe_allow_html=True)
                    h2.markdown('<span class="th-cell">Bid Number</span>', unsafe_allow_html=True)
                    h3.markdown('<span class="th-cell">Item Name</span>', unsafe_allow_html=True)
                    h4.markdown('<span class="th-cell">Department</span>', unsafe_allow_html=True)
                    qty_col_label = "Location" if current_portal_filter == "KPPP" else "Qty"
                    h5.markdown(f'<span class="th-cell">{qty_col_label}</span>', unsafe_allow_html=True)
                    h6.markdown('<span class="th-cell">Start Date</span>', unsafe_allow_html=True)
                    h7.markdown('<span class="th-cell">End Date</span>', unsafe_allow_html=True)
                    if show_price:
                        h_price.markdown('<span class="th-cell">Price</span>', unsafe_allow_html=True)
                    h8.markdown("")
                    h9.markdown("")
                    h10.markdown("")
                    # st.markdown("---")

                    for idx, tender in enumerate(page_tenders):
                        tid = tender["id"]
                        sl_no = start + idx + 1
                        bid_number = tender.get("tender_number", "N/A")
                        title = tender.get("title", "Unknown")
                        department = tender.get("department", "N/A")
                        # KPPP tenders have no quantity field at all (never scraped —
                        # the portal doesn't expose one); its own site shows Location
                        # in that spot instead, which we do have real data for.
                        if (tender.get("portal") or "").upper() == "KPPP":
                            quantity = tender.get("location") or "N/A"
                        else:
                            quantity = tender.get("quantity", "N/A")
                        start_date = tender.get("published_date", "N/A")
                        end_date = tender.get("deadline", "N/A")
                        portal = tender.get("portal", "")
                        portal_chip_class = "portal-chip-kppp" if portal.upper() == "KPPP" else "portal-chip-gem"
                        # Price only ever shown for KPPP rows — GeM doesn't
                        # expose an amount at this stage (its "value" is
                        # always just "See bid document"), so that's not
                        # worth a column cell, only KPPP's real figure is.
                        # Prefers bid_value_numeric (the real estimated
                        # value, read out of the tender's own document by
                        # Get Full Analysis) over the raw "value" field —
                        # that one's only ever KPPP's own L1 search-result
                        # figure, which is frequently blank/"Not available"
                        # even when the document itself states a number.
                        if portal.upper() == "KPPP":
                            bid_value_numeric = tender.get("bid_value_numeric")
                            emd_numeric = tender.get("emd_numeric")
                            if bid_value_numeric:
                                price = _format_tender_price(bid_value_numeric)
                            elif emd_numeric:
                                # Some documents (rate-contract/AMC style
                                # tenders especially) never state an overall
                                # estimated value at all, only an EMD amount
                                # — still real, useful data, so it's shown
                                # rather than "Not available", just labelled
                                # so it isn't mistaken for the tender's
                                # actual value.
                                price = f"EMD {_format_tender_price(emd_numeric)}"
                            else:
                                price = tender.get("value") or "Not available"
                        else:
                            price = ""

                        row_cols = st.columns(col_widths)
                        r1, r2, r3, r4, r5, r6, r7 = row_cols[:7]
                        r_rest = row_cols[7:]
                        if show_price:
                            r_price, r8, r9, r10 = r_rest
                        else:
                            r8, r9, r10 = r_rest
                        r1.markdown(f'<div class="tbl-data-row"></div><span class="td-slno">{sl_no}</span>', unsafe_allow_html=True)
                        r2.markdown(
                            f'<div class="mono td-bid">{bid_number}</div>'
                            f'<span class="portal-chip {portal_chip_class}">{portal or "N/A"}</span>',
                            unsafe_allow_html=True,
                        )
                        # Hover shows the full stored title (same idea as
                        # GeM's own site) — the visible cell still shows
                        # just the first 60 chars so the table stays
                        # compact, but a long bundled item list (GeM bids
                        # often pack many items into one title) is no
                        # longer only visible by opening the tender.
                        title_suffix = "..." if len(title) > 60 else ""
                        r3.markdown(
                            f'<span title="{html.escape(title)}">{html.escape(title[:60])}{title_suffix}</span>',
                            unsafe_allow_html=True,
                        )
                        r4.markdown(f'<span class="td-dept">{department[:30] if department else "N/A"}</span>', unsafe_allow_html=True)
                        r5.markdown(f'<span class="mono td-qty">{quantity}</span>', unsafe_allow_html=True)
                        r6.markdown(f'<span class="mono td-date">{start_date}</span>', unsafe_allow_html=True)
                        r7.markdown(f'<span class="mono enddate-chip">{end_date}</span>', unsafe_allow_html=True)
                        if show_price:
                            r_price.markdown(f'<span class="mono td-qty">{price or "—"}</span>', unsafe_allow_html=True)
                        with r8:
                            if st.button("View", key=f"view_{tid}_{section}"):
                                st.session_state.viewing_tender_id = tid
                                st.rerun()
                        with r9:
                            tender_status = tender.get("status", "new")
                            if tender_status == "saved":
                                if st.button("🔓", key=f"table_unsave_{tid}_{section}", help="Unsave this tender"):
                                    update_tender_status(tid, "new")
                                    st.rerun()
                            else:
                                if st.button("💾", key=f"table_save_{tid}_{section}", help="Save this tender"):
                                    update_tender_status(tid, "saved")
                                    st.rerun()
                        with r10:
                            if st.button("🗑️", key=f"table_delete_{tid}_{section}", help="Delete this tender"):
                                delete_tender(tid)
                                st.rerun()

                st.markdown("---")
                _, col_prev2, col_page2, col_next2 = st.columns([5, 1, 1, 1])
                with col_prev2:
                    st.markdown('<div class="pgn-prev-marker"></div>', unsafe_allow_html=True)
                    if st.button("◀ Prev", disabled=st.session_state.page_num <= 1, key="prev_btn2"):
                        st.session_state.page_num -= 1
                        st.rerun()
                with col_page2:
                    st.markdown('<div class="pgn-page-marker"></div>', unsafe_allow_html=True)
                    st.markdown(f"**{st.session_state.page_num}/{total_pages}**")
                with col_next2:
                    st.markdown('<div class="pgn-next-marker"></div>', unsafe_allow_html=True)
                    if st.button("Next ▶", disabled=st.session_state.page_num >= total_pages, key="next_btn2"):
                        st.session_state.page_num += 1
                        st.rerun()


# ── FLOATING AI CHATBOT ──────────────────────────────────────────────────
# A bottom-left floating "Chat here" tab that opens a compact chat panel
# (DeepSeek tool-calling against the read-only DB lookups in database/db.py
# — see agent/chatbot.py). Reachable from every dashboard view since it's
# rendered at the very end of the script, outside any section branch.
# Wrapped in st.fragment: clicks inside the chat (send, ☰, fullscreen, picking
# a chat…) rerun only this function instead of the whole 4000-line dashboard
# script, which is what made every chat click take ~1s. Every st.rerun() in
# here uses scope="fragment" for the same reason.
@st.fragment
def _chatbot_widget():
    if "chatbot_open" not in st.session_state:
        # Open by default, showing the same ongoing conversation, every
        # time - a page reload (e.g. clicking a tender link in a table)
        # used to always land back on a closed, empty chat even though the
        # actual messages were safely in the DB the whole time. Starting a
        # genuinely new chat is still just a click away via the panel's own
        # "＋ New chat" control; this only changes what greets a fresh page
        # load.
        st.session_state.chatbot_open = True
    if "chatbot_session_id" not in st.session_state:
        from database.db import list_chat_sessions, get_chat_messages
        _resume_user = st.session_state.get("username", "")
        _existing_sessions = list_chat_sessions(_resume_user) if _resume_user else []
        if _existing_sessions:
            st.session_state.chatbot_session_id = _existing_sessions[0]["id"]
            st.session_state.chatbot_messages = get_chat_messages(
                st.session_state.chatbot_session_id, _resume_user
            )
        else:
            st.session_state.chatbot_session_id = None
            st.session_state.chatbot_messages = []
    if "chatbot_messages" not in st.session_state:
        st.session_state.chatbot_messages = []
    if "chatbot_pending" not in st.session_state:
        st.session_state.chatbot_pending = False
    if "chatbot_fullscreen" not in st.session_state:
        st.session_state.chatbot_fullscreen = False
    if "chatbot_sidebar_open" not in st.session_state:
        st.session_state.chatbot_sidebar_open = False
    if "chatbot_menu_for" not in st.session_state:
        st.session_state.chatbot_menu_for = None

    # History sidebar: slides in/out via transform (the element is always
    # rendered, so its CSS transition actually animates). In the small panel it
    # overlays the chat like a drawer; in fullscreen there's room, so it pushes
    # the chat over instead.
    SIDEBAR_WIDTH = 270
    if st.session_state.chatbot_sidebar_open:
        sb_transform = "translateX(0)"
        sb_visibility = "visible"
        sb_vis_transition = "visibility 0s linear 0s"
    else:
        sb_transform = "translateX(-100%)"
        sb_visibility = "hidden"
        sb_vis_transition = "visibility 0s linear 0.25s"
    if st.session_state.chatbot_sidebar_open and st.session_state.chatbot_fullscreen:
        panel_pad_left = f"{SIDEBAR_WIDTH + 14}px"
        # Chat is already pushed to the right of the sidebar, so the title
        # clears the top-left ☰ button without extra indent.
        title_pad_left = "0px"
    else:
        panel_pad_left = "14px"
        # Leaves room for the ☰ button that sits in the panel's top-left corner.
        title_pad_left = "44px"

    if st.session_state.chatbot_open:
        if st.session_state.chatbot_fullscreen:
            panel_width = "100vw"
            panel_height = "100vh"
            panel_inset = "0px"
            panel_radius = "0px"
            # Fullscreen is wide enough that edge-to-edge text reads badly
            # (ChatGPT-style apps cap this too) — center a fixed-width column
            # instead, same as the reference screenshot. The normal small
            # panel is already narrow, so it stays unconstrained there.
            content_max_width = "760px"
            msg_font_size = "16px"
            msg_code_font_size = "14px"
        else:
            panel_width = "50vw"
            panel_height = "82vh"
            panel_inset = "24px"
            panel_radius = "16px"
            content_max_width = "100%"
            msg_font_size = "13px"
            msg_code_font_size = "12px"
    else:
        panel_width = panel_height = "0"
        panel_inset = "24px"
        panel_radius = "16px"
        content_max_width = "100%"
        msg_font_size = "13px"
        msg_code_font_size = "12px"

    st.markdown(
        f"""
        <style>
        .st-key-chatbot_toggle_btn {{
            position: fixed !important;
            right: 24px !important;
            bottom: 24px !important;
            /* Streamlit's own sidebar sits at z-index: 999991, so anything
               meant to float above the whole app (not just the main content
               area) has to clear that, not just the page's ordinary content. */
            z-index: 2000000 !important;
            width: auto !important;
        }}
        .st-key-chatbot_toggle_btn button {{
            border-radius: 999px !important;
            padding: 10px 22px !important;
            background: #000000 !important;
            color: #FFFFFF !important;
            border: none !important;
            box-shadow: 0 6px 18px rgba(0,0,0,0.45) !important;
            font-weight: 600 !important;
        }}
        /* Full-viewport click-catcher behind the panel — clicking anywhere on
           it (i.e. outside the panel) closes the chat, the same way a modal's
           backdrop click normally would. */
        .st-key-chatbot_overlay {{
            position: fixed !important;
            inset: 0 !important;
            z-index: 1999998 !important;
        }}
        .st-key-chatbot_overlay button {{
            width: 100vw !important;
            height: 100vh !important;
            background: transparent !important;
            border: none !important;
            cursor: default !important;
        }}
        .st-key-chatbot_panel {{
            position: fixed !important;
            right: {panel_inset} !important;
            bottom: {panel_inset} !important;
            z-index: 1999999 !important;
            width: {panel_width} !important;
            height: {panel_height} !important;
            background: #000000 !important;
            border-radius: {panel_radius} !important;
            box-shadow: 0 10px 40px rgba(0,0,0,0.3) !important;
            overflow: hidden !important;
            display: flex !important;
            flex-direction: column !important;
            padding: 14px 14px 14px {panel_pad_left} !important;
            transition: width 0.15s ease, height 0.15s ease, padding-left 0.25s ease;
        }}
        /* History sidebar (absolute inside the fixed panel, so it sits over the
           panel's left edge regardless of the panel's padding). */
        .st-key-chatbot_panel > [data-testid="stLayoutWrapper"]:has(.st-key-chatbot_history) {{
            height: 0 !important;
            margin-bottom: -1rem !important;
            overflow: visible !important;
        }}
        .st-key-chatbot_panel .st-key-chatbot_history {{
            position: absolute !important;
            top: 0 !important;
            left: 0 !important;
            bottom: 0 !important;
            width: {SIDEBAR_WIDTH}px !important;
            z-index: 5 !important;
            background: #0B0B0B !important;
            border-right: 1px solid #262626 !important;
            padding: 58px 10px 14px 10px !important;
            overflow-y: auto !important;
            overflow-x: hidden !important;
            transform: {sb_transform} !important;
            visibility: {sb_visibility} !important;
            transition: transform 0.25s ease, {sb_vis_transition} !important;
            gap: 4px !important;
        }}
        .st-key-chatbot_history button {{
            background: transparent !important;
            color: #E5E5E5 !important;
            border: none !important;
            border-radius: 8px !important;
            justify-content: flex-start !important;
            text-align: left !important;
            padding: 6px 10px !important;
            min-height: 34px !important;
            font-size: 13px !important;
            box-shadow: none !important;
        }}
        .st-key-chatbot_history button:hover {{
            background: #1F1F1F !important;
        }}
        .st-key-chatbot_history button p {{
            margin: 0 !important;
            white-space: nowrap !important;
            overflow: hidden !important;
            text-overflow: ellipsis !important;
        }}
        .st-key-chatbot_history .st-key-chatbot_new_chat button {{
            border: 1px solid #333333 !important;
            font-weight: 600 !important;
            margin-bottom: 6px !important;
        }}
        .st-key-chatbot_history [class*="st-key-chatbot_active_"] button {{
            background: #262626 !important;
        }}
        .st-key-chatbot_history [data-testid="stMarkdown"] > div {{
            display: block !important;
        }}
        /* Streamlit's per-element wrapper reported a height (~14px) shorter than
           the label's own padded box (~30px), so the next row painted over the
           label's lower half. A min-height gives the wrapper the room it needs. */
        .st-key-chatbot_history [data-testid="stElementContainer"]:has(.chatbot-history-group) {{
            min-height: 30px !important;
        }}
        .chatbot-history-group {{
            color: #8A8A8A;
            font-size: 11px;
            font-weight: 600;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            padding: 10px 10px 2px 10px;
        }}
        /* The "..." menu button only shows while hovering its chat row (or
           while that row's menu is open). */
        .st-key-chatbot_history [class*="st-key-chatbot_dots_"] {{
            opacity: 0;
            transition: opacity 0.15s ease;
        }}
        .st-key-chatbot_history [class*="st-key-chatbot_row_"]:hover [class*="st-key-chatbot_dots_"],
        .st-key-chatbot_history [class*="st-key-chatbot_dots_open_"] {{
            opacity: 1;
        }}
        .st-key-chatbot_history [class*="st-key-chatbot_row_"] [data-testid="stHorizontalBlock"] {{
            gap: 2px !important;
            align-items: center !important;
            flex-wrap: nowrap !important;
        }}
        .st-key-chatbot_history input {{
            background: #1A1A1A !important;
            color: #FFFFFF !important;
            border: 1px solid #333333 !important;
            font-size: 13px !important;
        }}
        .st-key-chatbot_history [class*="st-key-chatbot_menu_delete_"] button {{
            color: #F87171 !important;
        }}
        .chatbot-header {{
            padding-left: {title_pad_left};
            display: flex;
            align-items: center;
            justify-content: space-between;
            font-weight: 700;
            color: #FFFFFF !important;
            padding-bottom: 8px;
            border-bottom: 1px solid #333333;
            margin-bottom: 8px;
            flex-shrink: 0;
        }}
        .st-key-chatbot_panel > [data-testid="stLayoutWrapper"]:has(.st-key-chatbot_sb_dismiss) {{
            height: 0 !important;
            margin-bottom: -1rem !important;
            overflow: visible !important;
        }}
        /* Small-panel only: the history sidebar is a drawer over the chat, so a
           transparent layer over the rest of the panel turns "click outside the
           sidebar" into "close it". It sits under the header buttons (z-index 10)
           and over the messages/input. */
        .st-key-chatbot_panel .st-key-chatbot_sb_dismiss {{
            position: absolute !important;
            top: 0 !important;
            bottom: 0 !important;
            left: {SIDEBAR_WIDTH}px !important;
            right: 0 !important;
            width: auto !important;
            z-index: 4 !important;
        }}
        /* Streamlit's element wrapper is position:relative with ~0 size, which
           the button would otherwise size itself against. */
        .st-key-chatbot_panel .st-key-chatbot_sb_dismiss [data-testid="stElementContainer"],
        .st-key-chatbot_panel .st-key-chatbot_sb_dismiss [data-testid="stButton"] {{
            position: static !important;
        }}
        .st-key-chatbot_panel .st-key-chatbot_sb_dismiss button {{
            position: absolute !important;
            inset: 0 !important;
            width: 100% !important;
            height: 100% !important;
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
            cursor: default !important;
        }}
        .st-key-chatbot_panel > [data-testid="stLayoutWrapper"]:has(.st-key-chatbot_header_left) {{
            height: 0 !important;
            margin-bottom: -1rem !important;
            overflow: visible !important;
        }}
        .st-key-chatbot_panel .st-key-chatbot_header_left {{
            width: auto !important;
            position: absolute !important;
            top: 10px !important;
            left: 10px !important;
            z-index: 10 !important;
        }}
        .st-key-chatbot_panel .st-key-chatbot_header_left button {{
            padding: 0 !important;
            width: 32px !important;
            height: 32px !important;
            min-height: 32px !important;
            line-height: 32px !important;
            font-size: 14px !important;
            border-radius: 0 !important;
            border: none !important;
        }}
        .st-key-chatbot_panel .st-key-chatbot_header_btns {{
            width: auto !important;
            position: absolute !important;
            top: 10px !important;
            right: 10px !important;
            z-index: 10 !important;
        }}
        .st-key-chatbot_panel .st-key-chatbot_header_btns [data-testid="stHorizontalBlock"] {{
            gap: 6px !important;
        }}
        .st-key-chatbot_panel .st-key-chatbot_header_btns button {{
            padding: 0 !important;
            width: 32px !important;
            height: 32px !important;
            min-height: 32px !important;
            line-height: 32px !important;
            font-size: 14px !important;
            border-radius: 0 !important;
            border: none !important;
        }}
        .st-key-chatbot_panel .st-key-chatbot_history_btn button,
        .st-key-chatbot_panel .st-key-chatbot_fullscreen_btn button {{
            background: #000000 !important;
            color: #FFFFFF !important;
            border: 1px solid #555555 !important;
        }}
        .st-key-chatbot_panel .st-key-chatbot_close_btn button {{
            background: #DC2626 !important;
            color: #FFFFFF !important;
        }}
        /* Streamlit wraps every element (including st.container(key=...)) in
           its own stLayoutWrapper div that only auto-sizes to content by
           default — so flex:1 on the messages container itself did nothing
           until its own wrapper was told to grow too. Without this, the input
           bar sat right under the empty message list instead of pinned to the
           panel's bottom edge, leaving a dead gap below it. */
        .st-key-chatbot_panel > [data-testid="stLayoutWrapper"]:has(.st-key-chatbot_messages_container) {{
            flex: 1 1 auto !important;
            min-height: 0 !important;
            overflow: hidden !important;
        }}
        .st-key-chatbot_messages_container {{
            flex: 1 1 auto !important;
            min-height: 0 !important;
            overflow-y: auto !important;
            display: flex !important;
            flex-direction: column !important;
            gap: 36px !important;
            padding-right: 4px !important;
            margin-bottom: 8px !important;
            max-width: {content_max_width} !important;
            width: 100% !important;
            margin-left: auto !important;
            margin-right: auto !important;
        }}
        /* Each message is its own st.container(key=...) inside the flex column
           above — real st.markdown() renders its text so bold text and bullets
           work, and a tiny marker div (see Python below) lets this CSS tell a
           user bubble from a bot bubble via :has(), the same marker-div +
           :has() technique already used for the sticky stats row higher up
           this file. (A literal asterisk-slash earlier in this comment once
           closed it prematurely and silently dropped the next CSS rule —
           watch for that if editing this comment again.) */
        /* User's own messages get a blue bubble (per reference screenshot);
           the assistant's replies stay plain text, right/left aligned. */
        .st-key-chatbot_messages_container [data-testid="stVerticalBlockBorderWrapper"]:has(.chatbot-msg-user-marker),
        .st-key-chatbot_messages_container [data-testid="stVerticalBlock"]:has(> div .chatbot-msg-user-marker) {{
            align-self: flex-end !important;
            max-width: 92% !important;
            width: fit-content !important;
            gap: 0 !important;
        }}
        .st-key-chatbot_messages_container [data-testid="stVerticalBlockBorderWrapper"]:has(.chatbot-msg-bot-marker),
        .st-key-chatbot_messages_container [data-testid="stVerticalBlock"]:has(> div .chatbot-msg-bot-marker) {{
            align-self: flex-start !important;
            max-width: 92% !important;
            width: fit-content !important;
            gap: 0 !important;
        }}
        /* Each message's text sits inside Streamlit's own stMarkdown wrapper,
           which renders as display:flex + align-items:center — that
           combination was collapsing the wrapper's own height down to a few
           px (its one child's real height, ~21px, never got passed up),
           clipping the bottom half of every message's text. Forcing it back
           to plain block flow sizes it to its content normally, like any
           other div. */
        .st-key-chatbot_messages_container [data-testid="stMarkdown"] > div {{
            display: block !important;
        }}
        /* Streamlit gives every stMarkdownContainer a built-in -16px bottom
           margin, normally cancelled out by the default gap Streamlit puts
           between elements in a vertical block — but the message bubbles
           above set that gap to 0 for tight spacing, so nothing was left to
           cancel it. With a bot reply made of several separate st.markdown()
           calls (the summary text, then a "KPPP (12)" heading, then its
           table, then "GeM (8)", then its table), that negative margin
           pulled each one 16px up into the one before it - visibly
           overlapping text once a short heading followed a multi-line
           paragraph. Overriding it to a small positive value keeps replies
           compact without the overlap. */
        .st-key-chatbot_messages_container [data-testid="stMarkdownContainer"] {{
            margin-bottom: 6px !important;
        }}
        /* Tender tables (Tender Number/Title/Department/Start Date/Deadline)
           keep wrapping text inside each cell exactly as before - just
           scrollable sideways (mouse drag, touchpad or finger swipe) on top
           of that, for whenever the table's still wider than the chat panel
           even with wrapped cells. */
        .st-key-chatbot_messages_container [data-testid="stMarkdownContainer"] table {{
            display: block !important;
            overflow-x: auto !important;
            -webkit-overflow-scrolling: touch !important;
        }}
        /* The bubble's background + rounded corners go on stMarkdownContainer
           (one level deeper than the div the display:block fix above targets)
           rather than on that div itself. That div's own display just changed
           (flex -> block, via the fix above) — painting a border-radius
           background on an element whose size changes post-mount hit a real
           Chromium rendering bug where the rounded background stayed painted
           at the old, collapsed size while the text sat partly outside it.
           stMarkdownContainer's size was always correct from its first paint
           (its display never changes), so painting the bubble chrome here
           instead sidesteps the bug entirely. Confirmed live in devtools:
           moving just these three properties down one level fixed it, nothing
           else needed to change. */
        .st-key-chatbot_messages_container [data-testid="stVerticalBlock"]:has(> div .chatbot-msg-user-marker) [data-testid="stMarkdownContainer"]:not(:has(.chatbot-msg-user-marker)) {{
            background: #2A4D8F !important;
            color: #FFFFFF !important;
            padding: 12px 20px !important;
            /* A fixed radius rather than 999px (full pill) — a pill radius
               scales with the bubble's own height, so once a message wraps
               onto several lines the corners curve in far enough to clip the
               text itself. */
            border-radius: 20px !important;
            line-height: 1.5 !important;
        }}
        .st-key-chatbot_messages_container [data-testid="stVerticalBlockBorderWrapper"]:has(.chatbot-msg-user-marker) p,
        .st-key-chatbot_messages_container [data-testid="stVerticalBlock"]:has(> div .chatbot-msg-user-marker) p {{
            color: #FFFFFF !important;
            margin: 0 !important;
            font-size: {msg_font_size} !important;
            line-height: 1.5 !important;
            text-align: left !important;
        }}
        .st-key-chatbot_messages_container [data-testid="stVerticalBlockBorderWrapper"]:has(.chatbot-msg-bot-marker) p,
        .st-key-chatbot_messages_container [data-testid="stVerticalBlock"]:has(> div .chatbot-msg-bot-marker) p,
        .st-key-chatbot_messages_container [data-testid="stVerticalBlockBorderWrapper"]:has(.chatbot-msg-bot-marker) li,
        .st-key-chatbot_messages_container [data-testid="stVerticalBlock"]:has(> div .chatbot-msg-bot-marker) li {{
            color: #FFFFFF !important;
            font-size: {msg_font_size} !important;
            margin: 2px 0 !important;
        }}
        /* Markdown headings in replies defaulted to the app's dark text colour
           (invisible on this black panel) and were far too large for a chat
           bubble — make them white and just slightly bigger than body text. */
        .st-key-chatbot_messages_container [data-testid="stVerticalBlock"]:has(> div .chatbot-msg-bot-marker) h1,
        .st-key-chatbot_messages_container [data-testid="stVerticalBlock"]:has(> div .chatbot-msg-bot-marker) h2,
        .st-key-chatbot_messages_container [data-testid="stVerticalBlock"]:has(> div .chatbot-msg-bot-marker) h3,
        .st-key-chatbot_messages_container [data-testid="stVerticalBlock"]:has(> div .chatbot-msg-bot-marker) h4,
        .st-key-chatbot_messages_container [data-testid="stVerticalBlock"]:has(> div .chatbot-msg-bot-marker) h5,
        .st-key-chatbot_messages_container [data-testid="stVerticalBlock"]:has(> div .chatbot-msg-bot-marker) h6 {{
            color: #FFFFFF !important;
            font-size: calc({msg_font_size} + 3px) !important;
            font-weight: 700 !important;
            line-height: 1.35 !important;
            margin: 10px 0 4px 0 !important;
            padding: 0 !important;
        }}
        .st-key-chatbot_messages_container [data-testid="stVerticalBlock"]:has(> div .chatbot-msg-bot-marker) a {{
            color: #93C5FD !important;
        }}
        .st-key-chatbot_messages_container [data-testid="stVerticalBlock"]:has(> div .chatbot-msg-bot-marker) th,
        .st-key-chatbot_messages_container [data-testid="stVerticalBlock"]:has(> div .chatbot-msg-bot-marker) td {{
            color: #FFFFFF !important;
            border-color: #333333 !important;
            font-size: {msg_font_size} !important;
        }}
        /* Bid numbers come back from the model wrapped in markdown backticks
           (`GEM/2026/B/...`), rendered as <code> — Streamlit's default code
           styling is a dim green on dark gray, which all but disappears
           against this panel's black background. */
        .st-key-chatbot_messages_container [data-testid="stVerticalBlock"]:has(> div .chatbot-msg-bot-marker) code {{
            background: #2D2D2D !important;
            color: #93C5FD !important;
            padding: 2px 6px !important;
            border-radius: 4px !important;
            font-size: {msg_code_font_size} !important;
        }}
        .chatbot-empty-hint {{
            color: #94A3B8;
            font-size: 13px;
            text-align: center;
            margin-top: 20px;
        }}
        /* "View all N tenders" / "Hide tenders" expand button under a bot
           reply — a small outlined pill rather than Streamlit's default
           full-width button, so it reads as a secondary action under the
           message instead of competing with it. */
        .st-key-chatbot_messages_container div[data-testid="stButton"] button {{
            background: transparent !important;
            color: #93C5FD !important;
            border: 1px solid #3A3A3A !important;
            border-radius: 999px !important;
            padding: 2px 14px !important;
            font-size: 12px !important;
            width: auto !important;
            min-height: 0 !important;
            margin: 4px 0 2px 0 !important;
        }}
        .st-key-chatbot_messages_container div[data-testid="stButton"] button:hover {{
            background: #2D2D2D !important;
            border-color: #525252 !important;
        }}
        .st-key-chatbot_messages_container [data-testid="stVerticalBlock"]:has(> div .chatbot-msg-bot-marker) {{
            align-items: flex-start !important;
        }}
        /* ChatGPT-style pill input bar — st.form doesn't get a CSS key class
           of its own (only widgets like st.container do), so this targets the
           one stForm inside the panel directly via data-testid instead. */
        .st-key-chatbot_panel [data-testid="stForm"] {{
            background: #414141 !important;
            border-radius: 28px !important;
            padding: 4px 6px 4px 18px !important;
            border: none !important;
            flex-shrink: 0 !important;
            max-width: {content_max_width} !important;
            width: 100% !important;
            margin-left: auto !important;
            margin-right: auto !important;
        }}
        .st-key-chatbot_panel [data-testid="stForm"] [data-testid="stHorizontalBlock"] {{
            align-items: center !important;
            gap: 4px !important;
        }}
        /* The 1px auto-scroll helper iframe paints a thin white line on the
           black panel; keep it running but invisible. */
        .st-key-chatbot_panel iframe {{
            opacity: 0 !important;
            pointer-events: none !important;
        }}
        .st-key-chatbot_panel .chatbot-mic-btn {{
            background: transparent;
            border: none;
            color: #D1D5DB;
            width: 36px;
            height: 36px;
            min-width: 36px;
            margin-right: 6px;
            border-radius: 50%;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            padding: 0;
        }}
        .st-key-chatbot_panel .chatbot-mic-btn:hover {{
            background: #525252;
            color: #FFFFFF;
        }}
        .st-key-chatbot_panel .chatbot-mic-btn.listening {{
            background: #B91C1C;
            color: #FFFFFF;
            animation: chatbot-mic-pulse 1.2s ease-in-out infinite;
        }}
        @keyframes chatbot-mic-pulse {{
            0%, 100% {{ opacity: 1; }}
            50% {{ opacity: 0.6; }}
        }}
        /* Keep the send button flush with the right end of the input bar: the
           input column takes all spare width, the button column shrinks to it. */
        .st-key-chatbot_panel [data-testid="stForm"] [data-testid="stColumn"]:first-child {{
            flex: 1 1 0 !important;
            min-width: 0 !important;
        }}
        .st-key-chatbot_panel [data-testid="stForm"] [data-testid="stColumn"]:last-child {{
            flex: 0 0 auto !important;
            width: auto !important;
            min-width: 0 !important;
            display: flex !important;
            justify-content: flex-end !important;
        }}
        .st-key-chatbot_panel [data-testid="stTextInputRootElement"],
        .st-key-chatbot_panel [data-testid="stTextAreaRootElement"] {{
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
            min-height: unset !important;
            height: auto !important;
        }}
        /* Streamlit's own text_area chrome enforces a tall minimum height
           (built for a standalone multi-line box); collapse all of that back
           down to a single compact line so the pill only grows when the
           user's text actually wraps, same as it did as a text_input. */
        .st-key-chatbot_panel [data-testid="stTextArea"] {{
            min-height: unset !important;
        }}
        .st-key-chatbot_panel [data-testid="stTextInput"] input,
        .st-key-chatbot_panel [data-testid="stTextArea"] textarea {{
            background: transparent !important;
            border: none !important;
            color: #FFFFFF !important;
            box-shadow: none !important;
            font-size: 14px !important;
        }}
        .st-key-chatbot_panel [data-testid="stTextArea"] textarea {{
            resize: none !important;
            overflow-y: auto !important;
            line-height: 1.4 !important;
            padding-top: 8px !important;
            padding-bottom: 8px !important;
            min-height: 24px !important;
            height: 24px !important; /* starting height only — the script above grows this as you type */
            max-height: 150px !important;
        }}
        .st-key-chatbot_panel [data-testid="stTextInput"] input::placeholder,
        .st-key-chatbot_panel [data-testid="stTextArea"] textarea::placeholder {{
            color: #9CA3AF !important;
        }}
        /* Streamlit names a form submit button's key class from the form's own
           key plus the button label — "Send" here. */
        .st-key-chatbot_panel [class*="st-key-FormSubmitter-chatbot_form"] {{
            width: auto !important;
        }}
        /* Round blue send button with a white bold arrow (ChatGPT style). The
           label text is hidden and the arrow is drawn as an SVG background. */
        .st-key-chatbot_panel [class*="st-key-FormSubmitter-chatbot_form"] button,
        .st-key-chatbot_panel [data-testid="stFormSubmitButton"] button {{
            background-color: #4A67C8 !important;
            background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='white' stroke-width='2.6' stroke-linecap='round' stroke-linejoin='round'><line x1='12' y1='19' x2='12' y2='5'/><polyline points='5 12 12 5 19 12'/></svg>") !important;
            background-repeat: no-repeat !important;
            background-position: center !important;
            background-size: 20px 20px !important;
            color: transparent !important;
            border-radius: 50% !important;
            width: 36px !important;
            height: 36px !important;
            min-width: 36px !important;
            min-height: 36px !important;
            padding: 0 !important;
            border: none !important;
            box-shadow: none !important;
            font-size: 0 !important;
        }}
        .st-key-chatbot_panel [data-testid="stFormSubmitButton"] button:hover:not(:disabled) {{
            background-color: #5B78D8 !important;
        }}
        .st-key-chatbot_panel [data-testid="stFormSubmitButton"] button:disabled {{
            opacity: 0.45 !important;
        }}
        .st-key-chatbot_panel [data-testid="stFormSubmitButton"] button p {{
            margin: 0 !important;
            font-size: 0 !important;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )

    if not st.session_state.chatbot_open:
        with st.container(key="chatbot_toggle_btn"):
            if st.button("💬 Chat here", key="chatbot_open_btn"):
                st.session_state.chatbot_open = True
                st.rerun(scope="fragment")
    else:
        with st.container(key="chatbot_overlay"):
            if st.button("", key="chatbot_overlay_btn"):
                st.session_state.chatbot_open = False
                st.session_state.chatbot_fullscreen = False
                st.session_state.chatbot_sidebar_open = False
                st.rerun(scope="fragment")

        from database.db import (
            create_chat_session, list_chat_sessions, get_chat_messages,
            add_chat_message, rename_chat_session, delete_chat_session,
        )
        _chat_user = st.session_state.get("username", "")

        def _reset_to_new_chat():
            st.session_state.chatbot_session_id = None
            st.session_state.chatbot_messages = []
            st.session_state.chatbot_pending = False
            st.session_state.chatbot_menu_for = None

        with st.container(key="chatbot_panel"):
            # Chat history sidebar — always rendered (so the CSS slide
            # transition has an element to animate); hidden off-canvas until
            # opened with the ☰ button.
            with st.container(key="chatbot_history"):
                if st.button("＋  New chat", key="chatbot_new_chat", use_container_width=True):
                    _reset_to_new_chat()
                    # Small panel: the sidebar is a drawer over the chat, so
                    # picking something closes it. Fullscreen keeps it open.
                    if not st.session_state.chatbot_fullscreen:
                        st.session_state.chatbot_sidebar_open = False
                    st.rerun(scope="fragment")

                from datetime import date as _date, datetime as _dt
                _today = _date.today()

                def _bucket(updated_at):
                    try:
                        d = _dt.strptime((updated_at or "")[:10], "%Y-%m-%d").date()
                    except ValueError:
                        return "Older"
                    age = (_today - d).days
                    if age <= 0:
                        return "Today"
                    if age == 1:
                        return "Yesterday"
                    if age <= 7:
                        return "Previous 7 days"
                    return "Older"

                _sessions = list_chat_sessions(_chat_user) if _chat_user else []
                if not _sessions:
                    st.markdown('<div class="chatbot-history-group">No chats yet</div>', unsafe_allow_html=True)
                _grouped = {}
                for _s in _sessions:
                    _grouped.setdefault(_bucket(_s["updated_at"]), []).append(_s)
                for _group in ("Today", "Yesterday", "Previous 7 days", "Older"):
                    if _group not in _grouped:
                        continue
                    st.markdown(f'<div class="chatbot-history-group">{_group}</div>', unsafe_allow_html=True)
                    for _s in _grouped[_group]:
                        _sid = _s["id"]
                        _title = _s["title"] or "New chat"
                        _menu_open = st.session_state.chatbot_menu_for == _sid
                        with st.container(key=f"chatbot_row_{_sid}"):
                            _c_title, _c_dots = st.columns([8, 1.4], vertical_alignment="center")
                            with _c_title:
                                _prefix = "chatbot_active_" if _sid == st.session_state.chatbot_session_id else "chatbot_chat_"
                                if st.button(_title, key=f"{_prefix}{_sid}", use_container_width=True):
                                    st.session_state.chatbot_session_id = _sid
                                    st.session_state.chatbot_messages = get_chat_messages(_sid, _chat_user)
                                    st.session_state.chatbot_pending = False
                                    st.session_state.chatbot_menu_for = None
                                    if not st.session_state.chatbot_fullscreen:
                                        st.session_state.chatbot_sidebar_open = False
                                    st.rerun(scope="fragment")
                            with _c_dots:
                                _dots_key = f"chatbot_dots_open_{_sid}" if _menu_open else f"chatbot_dots_{_sid}"
                                if st.button("⋯", key=_dots_key, help="Rename or delete"):
                                    st.session_state.chatbot_menu_for = None if _menu_open else _sid
                                    st.rerun(scope="fragment")
                        if _menu_open:
                            _new_title = st.text_input(
                                "Rename chat", value=_title, key=f"chatbot_rename_{_sid}",
                                label_visibility="collapsed",
                            )
                            _b_save, _b_del = st.columns(2)
                            with _b_save:
                                if st.button("Save", key=f"chatbot_menu_save_{_sid}", use_container_width=True):
                                    if _new_title.strip():
                                        rename_chat_session(_sid, _chat_user, _new_title.strip()[:60])
                                    st.session_state.chatbot_menu_for = None
                                    st.rerun(scope="fragment")
                            with _b_del:
                                if st.button("Delete", key=f"chatbot_menu_delete_{_sid}", use_container_width=True):
                                    delete_chat_session(_sid, _chat_user)
                                    if _sid == st.session_state.chatbot_session_id:
                                        _reset_to_new_chat()
                                    st.session_state.chatbot_menu_for = None
                                    st.rerun(scope="fragment")

            # Small panel only: clicking anywhere outside the open sidebar
            # (i.e. on the chat area) closes it. Fullscreen has room for the
            # sidebar next to the chat, so it stays open there.
            if st.session_state.chatbot_sidebar_open and not st.session_state.chatbot_fullscreen:
                with st.container(key="chatbot_sb_dismiss"):
                    if st.button("", key="chatbot_sb_dismiss_btn"):
                        st.session_state.chatbot_sidebar_open = False
                        st.rerun(scope="fragment")

            # ☰ history toggle, pinned to the panel's top-left corner (above the
            # sidebar when it's open, so it stays reachable to close it again).
            with st.container(key="chatbot_header_left"):
                if st.button("☰", key="chatbot_history_btn", help="Chat history"):
                    st.session_state.chatbot_sidebar_open = not st.session_state.chatbot_sidebar_open
                    st.rerun(scope="fragment")

            header_title, header_btns = st.columns([4, 1])
            with header_title:
                st.markdown('<div class="chatbot-header">🤖 Tender Assistant</div>', unsafe_allow_html=True)
            with header_btns:
                with st.container(key="chatbot_header_btns"):
                    btn_fullscreen, btn_close = st.columns(2)
                    with btn_fullscreen:
                        fullscreen_icon = "⤡" if st.session_state.chatbot_fullscreen else "⤢"
                        if st.button(fullscreen_icon, key="chatbot_fullscreen_btn", help="Toggle fullscreen"):
                            st.session_state.chatbot_fullscreen = not st.session_state.chatbot_fullscreen
                            # Fullscreen has room for the history sidebar (open
                            # by default there); the small panel starts without it.
                            st.session_state.chatbot_sidebar_open = st.session_state.chatbot_fullscreen
                            st.rerun(scope="fragment")
                    with btn_close:
                        if st.button("✕", key="chatbot_close_btn", help="Close"):
                            st.session_state.chatbot_open = False
                            st.session_state.chatbot_fullscreen = False
                            st.session_state.chatbot_sidebar_open = False
                            st.rerun(scope="fragment")

            with st.container(key="chatbot_messages_container"):
                if not st.session_state.chatbot_messages:
                    st.markdown(
                        '<div class="chatbot-empty-hint">Ask me about tenders — e.g. "what came in today?" or "any tenders related to RTMS?"</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    for i, m in enumerate(st.session_state.chatbot_messages):
                        marker = "chatbot-msg-user-marker" if m["role"] == "user" else "chatbot-msg-bot-marker"
                        with st.container(key=f"chatbot_msg_{i}"):
                            st.markdown(f'<div class="{marker}"></div>', unsafe_allow_html=True)
                            st.markdown(m["content"])
                            # The real full tender list the tool actually
                            # fetched (see reply_tenders below, where this is
                            # set) — shown in full immediately, every time,
                            # as one table per portal. No "View all"/"Hide"
                            # toggle: the CEO wants the complete list on
                            # screen as soon as the answer appears, not
                            # behind an extra click, however many there are.
                            tenders = m.get("tenders")
                            if tenders:
                                by_portal = {}
                                for t in tenders:
                                    by_portal.setdefault(t.get("portal") or "Other", []).append(t)
                                for portal, rows in by_portal.items():
                                    st.markdown(f"**{portal} ({len(rows)})**")
                                    def _cell(v):
                                        return str(v or "—").replace("|", "/").replace("\n", " ")
                                    # KPPP tenders don't carry a quantity on
                                    # their listing page (GeM does); GeM's
                                    # listing has no site/location field the
                                    # way KPPP's does - so each portal gets
                                    # its own extra column instead of a
                                    # shared one that's always blank on one
                                    # side.
                                    extra_col = "Location" if portal == "KPPP" else "Quantity"
                                    extra_key = "location" if portal == "KPPP" else "quantity"
                                    table_lines = [
                                        f"| SL No | Tender Number | Title | Department | {extra_col} | Start Date | Deadline |",
                                        "|---|---|---|---|---|---|---|",
                                    ]
                                    for sl, t in enumerate(rows, start=1):
                                        # A real nav link ("?open_tender=<id>")
                                        # rather than a Streamlit button - a
                                        # button per row inside a markdown
                                        # table isn't possible, but a plain
                                        # link is, and the query-param reader
                                        # near the top of the script turns it
                                        # into the same viewing_tender_id the
                                        # "View" button on the main table
                                        # sets, opening the exact same detail
                                        # page. Falls back to plain text if
                                        # this tender has no id (shouldn't
                                        # normally happen).
                                        tender_number_cell = _cell(t.get("tender_number"))
                                        if t.get("id") is not None:
                                            tender_number_cell = f"[{tender_number_cell}](?open_tender={t['id']})"
                                        table_lines.append(
                                            f"| {sl} "
                                            f"| {tender_number_cell} "
                                            f"| {_cell((t.get('title') or '')[:90])} "
                                            f"| {_cell(t.get('department'))} "
                                            f"| {_cell(t.get(extra_key))} "
                                            f"| {_cell(t.get('start_date'))} "
                                            f"| {_cell(t.get('deadline'))} |"
                                        )
                                    st.markdown("\n".join(table_lines))
                # The user's own message is appended to chatbot_messages and
                # rerun immediately on submit — BEFORE calling the (slow) AI
                # — so it renders here, by itself, the instant Send is hit.
                # This "thinking" placeholder then renders right after it, in
                # the same rerun, and only THEN does the blocking AI call
                # happen further down — so the message and the "thinking" cue
                # are both already on screen while that call is running,
                # instead of both the question and the answer popping in
                # together only once the AI finishes.
                if st.session_state.chatbot_pending:
                    with st.container(key="chatbot_thinking"):
                        st.markdown('<div class="chatbot-msg-bot-marker"></div>', unsafe_allow_html=True)
                        st.markdown("🤖 _Thinking..._")

            # Auto-scroll the message list to the latest message on every
            # render. st.markdown can't run <script> tags itself, so this goes
            # through st.iframe reaching back into window.parent.document, the
            # same technique used for the sticky-stats-row sync and the
            # scroll-to-table-anchor jump earlier in this file. The nonce
            # forces the browser to treat this as fresh content and actually
            # re-run it — otherwise Streamlit skips remounting an iframe whose
            # rendered output is byte-for-byte identical to last time.
            import time as _time
            st.iframe(f"""
            <!-- nonce:{_time.time()} -->
            <script>
            (function() {{
                const doc = window.parent.document;
                const el = doc.querySelector('.st-key-chatbot_messages_container');
                if (el) el.scrollTop = el.scrollHeight;
            }})();
            </script>
            """, height=1)

            # Voice-dictation mic button next to Send. It is pure browser JS
            # (Web Speech API) so clicking it never triggers a Streamlit rerun.
            # The logic is copied into the PARENT page as a <script> (once) so
            # it keeps working after this helper iframe is re-mounted on every
            # render; a MutationObserver re-adds the button if Streamlit
            # re-renders the form.
            st.iframe("""
            <script>
            (function() {
                function install() {
                    if (window.__tenderMicInstalled) return;
                    window.__tenderMicInstalled = true;
                    var MIC = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><line x1="12" y1="18" x2="12" y2="22"/></svg>';
                    var rec = null, listening = false, watchdog = null, lastActivity = 0, base = '';

                    function input() {
                        return document.querySelector('.st-key-chatbot_panel [data-testid="stTextArea"] textarea');
                    }
                    function setValue(v) {
                        var el = input();
                        if (!el) return;
                        var setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                        setter.call(el, v);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                    function clearWatchdog() {
                        if (watchdog) { clearInterval(watchdog); watchdog = null; }
                    }
                    function stop(btn) {
                        listening = false;
                        clearWatchdog();
                        if (btn) btn.classList.remove('listening');
                        if (rec) { try { rec.onend = null; rec.stop(); } catch (e) {} }
                    }
                    // Starts one recognizer session. Called again, on a fresh
                    // SpeechRecognition instance each time, whenever a
                    // session ends (onend) or the watchdog below decides the
                    // current one has silently stalled.
                    function startSession(btn) {
                        var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
                        rec = new SR();
                        // Pin to Indian English explicitly rather than
                        // trusting navigator.language, which may report a
                        // generic/different locale and make the browser's
                        // speech engine mis-hear accented words.
                        rec.lang = 'en-IN';
                        rec.interimResults = true;
                        // continuous keeps the mic open through brief pauses
                        // in speech instead of the browser auto-stopping it.
                        rec.continuous = true;
                        rec.onresult = function(e) {
                            lastActivity = Date.now();
                            var t = '';
                            for (var i = 0; i < e.results.length; i++) t += e.results[i][0].transcript;
                            setValue(base + t);
                        };
                        rec.onstart = function() { lastActivity = Date.now(); };
                        // Some browsers still end the session on a long pause
                        // even with continuous=true. If the user hasn't
                        // clicked stop, treat that as a hiccup and restart
                        // rather than actually stopping.
                        rec.onend = function() {
                            if (listening) {
                                // Carry forward whatever is in the box now,
                                // since this new session's results start
                                // counting from zero again.
                                var el2 = input();
                                base = el2 && el2.value ? el2.value.trim() + ' ' : '';
                                try { startSession(btn); return; } catch (e) {}
                            }
                            clearWatchdog();
                            listening = false;
                            btn.classList.remove('listening');
                        };
                        rec.onerror = function(e) {
                            if (e.error === 'no-speech' || e.error === 'aborted') return;
                            clearWatchdog();
                            listening = false;
                            btn.classList.remove('listening');
                        };
                        try { rec.start(); } catch (e) {}
                    }
                    function toggle(btn) {
                        var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
                        if (!SR) { alert('Voice input is not supported in this browser. Please use Chrome or Edge.'); return; }
                        if (listening) { stop(btn); return; }
                        var el = input();
                        if (!el || el.disabled) return;
                        base = el.value ? el.value.trim() + ' ' : '';
                        listening = true;
                        lastActivity = Date.now();
                        btn.classList.add('listening');
                        startSession(btn);
                        // Chrome occasionally goes silently stuck — the mic
                        // stays "listening" (and the button stays red) but
                        // no onresult/onend/onerror ever fires again, so
                        // nothing above notices. Force a hard restart (brand
                        // new recognizer) if nothing has happened for a
                        // while, rather than leaving it looking alive but
                        // deaf.
                        clearWatchdog();
                        watchdog = setInterval(function() {
                            if (!listening) { clearWatchdog(); return; }
                            if (Date.now() - lastActivity > 7000) {
                                lastActivity = Date.now();
                                var el2 = input();
                                base = el2 && el2.value ? el2.value.trim() + ' ' : '';
                                if (rec) { try { rec.onend = null; rec.abort(); } catch (e) {} }
                                startSession(btn);
                            }
                        }, 3000);
                    }
                    function ensure() {
                        var form = document.querySelector('.st-key-chatbot_panel [data-testid="stForm"]');
                        if (!form || form.querySelector('.chatbot-mic-btn')) return;
                        var cols = form.querySelectorAll('[data-testid="stColumn"]');
                        var col = cols[cols.length - 1];
                        if (!col) return;
                        var btn = document.createElement('button');
                        btn.type = 'button';
                        btn.className = 'chatbot-mic-btn';
                        btn.title = 'Speak';
                        btn.setAttribute('aria-label', 'Voice input');
                        btn.innerHTML = MIC;
                        btn.addEventListener('click', function(ev) { ev.preventDefault(); toggle(btn); });
                        col.insertBefore(btn, col.firstChild);
                    }
                    new MutationObserver(ensure).observe(document.body, { childList: true, subtree: true });
                    ensure();
                }
                var s = window.parent.document.createElement('script');
                s.textContent = '(' + install.toString() + ')();';
                window.parent.document.head.appendChild(s);
            })();
            </script>
            """, height=1)

            with st.form(key="chatbot_form", clear_on_submit=True, border=False):
                fcol1, fcol2 = st.columns([8, 1], vertical_alignment="center")
                with fcol1:
                    user_input = st.text_area(
                        "Message", placeholder="Ask anything",
                        label_visibility="collapsed", key="chatbot_input",
                        disabled=st.session_state.chatbot_pending,
                        height=52,
                    )
                with fcol2:
                    sent = st.form_submit_button("↑", disabled=st.session_state.chatbot_pending)

            # text_area wraps long text onto new lines (a text_input can't),
            # but its default Enter key just inserts a newline instead of
            # submitting. Make Enter submit (Shift+Enter still inserts a
            # newline) and let the box grow with its content, both ChatGPT-
            # style behaviours that Streamlit's widget doesn't give for free.
            st.iframe("""
            <script>
            (function() {
                function install() {
                    if (window.__tenderChatAreaInstalled) return;
                    window.__tenderChatAreaInstalled = true;
                    function ta() {
                        return document.querySelector('.st-key-chatbot_panel [data-testid="stTextArea"] textarea');
                    }
                    function autoGrow(el) {
                        if (!el.value) {
                            // Back to empty (e.g. backspaced out) — drop the
                            // inline height so the compact CSS height applies
                            // again, instead of measuring an empty box's own
                            // padding as if it were content.
                            el.style.removeProperty('height');
                            return;
                        }
                        // setProperty(...,'important') is needed because the
                        // panel's own CSS pins this box to 24px with
                        // !important (so it starts compact, like a normal
                        // one-line pill) — a plain el.style.height assignment
                        // can't win against that, so it would never grow.
                        el.style.setProperty('height', 'auto', 'important');
                        var h = Math.min(el.scrollHeight, 150);
                        el.style.setProperty('height', Math.max(h, 24) + 'px', 'important');
                    }
                    function wire(el) {
                        if (el.__tenderWired) return;
                        el.__tenderWired = true;
                        // Only measure/grow once there's actual text — an
                        // empty textarea's own padding still reports a
                        // scrollHeight above 24px, which would start the box
                        // taller than the compact CSS height even with
                        // nothing typed in it.
                        if (el.value) autoGrow(el);
                        el.addEventListener('input', function() { autoGrow(el); });
                        el.addEventListener('keydown', function(e) {
                            if (e.key === 'Enter' && !e.shiftKey) {
                                e.preventDefault();
                                var form = el.closest('[data-testid="stForm"]');
                                var btn = form && form.querySelector('[data-testid="stFormSubmitButton"] button');
                                if (btn && !btn.disabled) btn.click();
                            }
                        });
                        // Sending clears the text_area's VALUE, but Streamlit
                        // reuses this same <textarea> node rather than
                        // remounting it, and setting .value that way doesn't
                        // fire an 'input' event — so autoGrow() above never
                        // reruns and the box stayed at whatever height it had
                        // grown to. Collapse it back down the instant Send is
                        // actually clicked (covers both a direct click and
                        // the Enter-key handler above, since that also just
                        // calls btn.click()).
                        var form = el.closest('[data-testid="stForm"]');
                        var sendBtn = form && form.querySelector('[data-testid="stFormSubmitButton"] button');
                        if (sendBtn && !sendBtn.__tenderWired) {
                            sendBtn.__tenderWired = true;
                            sendBtn.addEventListener('click', function() {
                                el.style.removeProperty('height');
                            });
                        }
                    }
                    new MutationObserver(function() {
                        var el = ta();
                        if (el) wire(el);
                    }).observe(document.body, { childList: true, subtree: true });
                    var el0 = ta();
                    if (el0) wire(el0);
                }
                var s = window.parent.document.createElement('script');
                s.textContent = '(' + install.toString() + ')();';
                window.parent.document.head.appendChild(s);
            })();
            </script>
            """, height=1)

            if sent and user_input.strip() and not st.session_state.chatbot_pending:
                _text = user_input.strip()
                # First message of a fresh chat creates the saved conversation,
                # titled from that message.
                if st.session_state.chatbot_session_id is None and _chat_user:
                    _title = _text if len(_text) <= 40 else _text[:40].rstrip() + "…"
                    st.session_state.chatbot_session_id = create_chat_session(_chat_user, _title)
                if st.session_state.chatbot_session_id is not None:
                    add_chat_message(st.session_state.chatbot_session_id, _chat_user, "user", _text)
                st.session_state.chatbot_messages.append({"role": "user", "content": _text})
                st.session_state.chatbot_pending = True
                st.rerun(scope="fragment")

            if st.session_state.chatbot_pending:
                from agent.chatbot import ask as chatbot_ask
                last_user_msg = st.session_state.chatbot_messages[-1]["content"]
                history = st.session_state.chatbot_messages[:-1]
                reply, reply_tenders = chatbot_ask(history, last_user_msg, st.session_state.get("user_role", "regular"))
                if st.session_state.chatbot_session_id is not None:
                    add_chat_message(st.session_state.chatbot_session_id, _chat_user, "assistant", reply)
                # reply_tenders (the full tender list behind this answer, for
                # the "View all N tenders" button below) is UI-only state —
                # it isn't saved to the chat_messages table, so it's only
                # available for messages from this session, not ones loaded
                # back from a past saved chat.
                st.session_state.chatbot_messages.append({"role": "assistant", "content": reply, "tenders": reply_tenders})
                st.session_state.chatbot_pending = False
                st.rerun(scope="fragment")


_chatbot_widget()
