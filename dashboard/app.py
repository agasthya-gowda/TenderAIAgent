"""
Tender Agent Dashboard
"""

import streamlit as st
import os
import json
import html
import pandas as pd
import altair as alt
from rapidfuzz import fuzz
from st_keyup import st_keyup
from database.db import (
    get_all_tenders, get_stats, update_tender_status,
    update_tender_pdfs, get_all_ceo_searches, get_learned_patterns,
    delete_tender, delete_pattern,
    get_department_counts, get_daily_trend,
)
from agent.orchestrator import (
    start_agent, stop_agent, get_state,
    get_log, trigger_manual_search
)
from analysis.analyzer import analyze_tender
from portals.data.kppp_options import (
    CATEGORIES as KPPP_CATEGORIES,
    PROCURING_ENTITIES as KPPP_ENTITIES,
)

PORTAL_FILTER_OPTIONS = ["🔵 GeM", "🟠 KPPP (KA)"]
PORTAL_FILTER_DEFAULT = "🔵 GeM"
PORTAL_FILTER_MAP = {
    "🔵 GeM": "GeM",
    "🟠 KPPP (KA)": "KPPP",
}


def _sync_portal_from_pills():
    """Keep the narrow-screen dropdown in sync when the pill control changes."""
    label = st.session_state.get("portal_filter_display", PORTAL_FILTER_DEFAULT)
    st.session_state.portal_filter_value = PORTAL_FILTER_MAP.get(label, "GeM")
    st.session_state.portal_filter_dropdown = label


def _sync_portal_from_dropdown():
    """Keep the wide-screen pill control in sync when the dropdown changes."""
    label = st.session_state.get("portal_filter_dropdown", PORTAL_FILTER_DEFAULT)
    st.session_state.portal_filter_value = PORTAL_FILTER_MAP.get(label, "GeM")
    st.session_state.portal_filter_display = label


def fuzzy_filter_tenders(tenders, query, threshold=80):
    """Filter tenders by a fuzzy/substring match over the visible table columns.

    Scores each field separately (not one concatenated string) and takes the
    best per-tender score — concatenating first let a short query "get lucky"
    against the combined length of every field (e.g. a long department name),
    surfacing unrelated tenders as fuzzy matches.
    """
    query = (query or "").strip().lower()
    if not query:
        return tenders
    fields = ("tender_number", "title", "department", "quantity", "published_date", "deadline")
    out = []
    for t in tenders:
        values = [str(t.get(f, "")).lower() for f in fields]
        if any(query in v for v in values) or \
                max(fuzz.partial_ratio(query, v) for v in values) >= threshold:
            out.append(t)
    return out

st.set_page_config(
    page_title="Tender Agent",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded"
)

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
    section[data-testid="stSidebar"] * {
        color: #E2E8F0 !important;
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
    .block-container {
        padding-top: 1rem !important;
        padding-left: 2rem !important;
        padding-right: 2rem !important;
        max-width: 100% !important;
    }
    div[data-testid="stVerticalBlock"] div:has(> div.stats-sticky) {
        position: sticky;
        top: 0;
        z-index: 999;
        background-color: #F8FAFC;
        padding-bottom: 8px;
    }
    .stats-sticky {
        background-color: #F8FAFC;
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
    .st-key-btn_patterns button {
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
    .st-key-btn_patterns button[kind="secondary"]:hover {
        border-color: #CBD5E1 !important;
    }
    .st-key-btn_all button[kind="primary"],
    .st-key-btn_new button[kind="primary"],
    .st-key-btn_saved button[kind="primary"],
    .st-key-btn_searches button[kind="primary"],
    .st-key-btn_patterns button[kind="primary"] {
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
    .st-key-btn_patterns button[kind="primary"] {
        color: #BE185D !important;
        border: 1.5px solid #EC4899 !important;
        box-shadow: 0 0 0 3px rgba(236, 72, 153, 0.12) !important;
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
    div[data-testid="stColumn"]:has(.col-marker-portal) {
        flex: 0 0 auto !important;
        min-width: 190px !important;
        max-width: none !important;
    }
    /* Wide screens: show the pill control, hide the dropdown fallback */
    div[data-testid="stColumn"]:has(.col-marker-portal) div[data-testid="stElementContainer"]:has(div[data-testid="stSelectbox"]) {
        display: none !important;
    }
    @media (max-width: 900px) {
        /* Narrow screens: the portal column only needs dropdown-sized width */
        div[data-testid="stColumn"]:has(.col-marker-portal) {
            flex: 0 0 200px !important;
            min-width: 200px !important;
            max-width: 220px !important;
        }
        div[data-testid="stColumn"]:has(.col-marker-portal) div[data-testid="stElementContainer"]:has(div[data-testid="stButtonGroup"]) {
            display: none !important;
        }
        div[data-testid="stColumn"]:has(.col-marker-portal) div[data-testid="stElementContainer"]:has(div[data-testid="stSelectbox"]) {
            display: block !important;
        }
    }
    div[data-testid="stColumn"]:has(.col-marker-search) {
        flex: 1 1 0% !important;
        min-width: 180px !important;
        max-width: none !important;
    }
    div[data-testid="stColumn"]:has(.col-marker-sort) {
        flex: 0 0 200px !important;
        min-width: 200px !important;
        max-width: 220px !important;
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
    div[data-testid="stElementContainer"]:has(div[data-testid="stButtonGroup"]) {
        width: 100% !important;
    }
    div[data-testid="stButtonGroup"] {
        width: 100% !important;
    }
    div[data-testid="stButtonGroup"] div[role="radiogroup"] {
        flex-wrap: nowrap !important;
        overflow-x: auto;
        justify-content: flex-start !important;
        gap: 6px !important;
        width: 100% !important;
        max-width: 100% !important;
    }
    div[data-testid="stButtonGroup"] div[role="radiogroup"] button {
        white-space: nowrap;
        flex-shrink: 0;
        border-radius: 8px !important;
        font-weight: 600 !important;
        color: #475569 !important;
        padding: 6px 12px !important;
        font-size: 12px !important;
        background: #FFFFFF !important;
        border: 1px solid #E2E8F0 !important;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    }
    div[data-testid="stButtonGroup"] div[role="radiogroup"] {
        background: transparent;
        border: none;
        border-radius: 0;
        padding: 0;
        box-shadow: none;
    }
    /* Portal pills — each option gets its own tint when selected, matching
       the reference's GeM (blue) / KPPP (amber). */
    div[data-testid="stButtonGroup"] div[role="radiogroup"] button:nth-of-type(1)[aria-checked="true"] {
        background: #EFF6FF !important;
        color: #1D4ED8 !important;
        border: 1px solid #BFDBFE !important;
    }
    div[data-testid="stButtonGroup"] div[role="radiogroup"] button:nth-of-type(2)[aria-checked="true"] {
        background: #FFFBEB !important;
        color: #92400E !important;
        border: 1px solid #FDE68A !important;
    }
    div[data-testid="stButton"] button:has-text("🗑️") {
        background-color: #E74C3C !important;
        color: white !important;
        border: none !important;
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


# ── SIDEBAR ───────────────────────────────────────────────
with st.sidebar:
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

    with st.expander("🔍 GeM Search Filters", expanded=True):
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
    with st.expander("🏛️ KPPP Search Filters", expanded=True):
        kppp_category = st.selectbox(
            "Category", KPPP_CATEGORIES, key="kppp_category"
        )
        kppp_entity = st.selectbox(
            "Procuring Entity (optional)", ["-- Any --"] + KPPP_ENTITIES, key="kppp_entity"
        )
        if st.button("Search KPPP", icon=":material/search:", use_container_width=True, type="primary", key="kppp_search_btn"):
            from agent.orchestrator import on_kppp_search
            on_kppp_search({
                "category": kppp_category,
                "procuring_entity": "" if kppp_entity == "-- Any --" else kppp_entity,
            })
            st.success("Searching KPPP — this filter will keep running automatically from now on.")

    state = get_state()
    status = state.get("status", "idle")
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

    st.markdown("---")
    

   
# ── FILTER BAR ────────────────────────────────────────────


st.markdown("<br>", unsafe_allow_html=True)
# ── STATS ROW ─────────────────────────────────────────────
# ── STATS ROW ─────────────────────────────────────────────
st.markdown('<div class="stats-sticky">', unsafe_allow_html=True)
stats = get_stats()
active = st.session_state.active_section
c1, c2, c3, c4, c5 = st.columns(5)

with c1:
    if st.button(
        f"📋  {stats['total_tenders']}\nTotal Tenders",
        key="btn_all", use_container_width=True,
        type="primary" if active == "all" else "secondary"
    ):
        st.session_state.active_section = "all"
        st.session_state.page_num = 1
        st.rerun()

with c2:
    if st.button(
        f"🟢  {stats['new']}\nNew Tenders",
        key="btn_new", use_container_width=True,
        type="primary" if active == "new" else "secondary"
    ):
        st.session_state.active_section = "new"
        st.session_state.page_num = 1
        st.rerun()

with c3:
    if st.button(
        f"💾  {stats['saved']}\nSaved",
        key="btn_saved", use_container_width=True,
        type="primary" if active == "saved" else "secondary"
    ):
        st.session_state.active_section = "saved"
        st.session_state.page_num = 1
        st.rerun()

with c4:
    if st.button(
        f"🔎  {stats['ceo_searches']}\nSearch History",
        key="btn_searches", use_container_width=True,
        type="primary" if active == "searches" else "secondary"
    ):
        st.session_state.active_section = "searches"
        st.session_state.page_num = 1
        st.rerun()

with c5:
    if st.button(
        f"🧠  {stats['patterns_learned']}\nPatterns Learned",
        key="btn_patterns", use_container_width=True,
        type="primary" if active == "patterns" else "secondary"
    ):
        st.session_state.active_section = "patterns"
        st.session_state.page_num = 1
        st.rerun()
st.markdown('</div>', unsafe_allow_html=True)

# ── OVERVIEW CHARTS — real data from the tenders table, one GeM/KPPP
# toggle per chart (defaults to GeM). Purely additive/visual: no existing
# query, filter, or button behavior above/below this block is touched. ──
DEPARTMENT_BAR_COLORS = ["#4F46E5", "#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#94A3B8"]

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
    label = st.segmented_control(
        "Portal", ["GeM", "KPPP (KA)"], default="GeM",
        key=key, label_visibility="collapsed",
    )
    return "KPPP" if label and "KPPP" in label else "GeM"


chart_col1, chart_col2 = st.columns(2)

with chart_col1:
    with st.container(border=True):
        dept_title_col, dept_toggle_col = st.columns([2, 2])
        with dept_title_col:
            st.markdown("**Tenders by Department**")
        with dept_toggle_col:
            dept_portal = _chart_portal_toggle("chart_dept_portal")
        dept_data = get_department_counts(dept_portal)
        if dept_data:
            dept_df = pd.DataFrame(dept_data, columns=["Department", "Count"])
            total_dept = int(dept_df["Count"].sum())
            dept_df["Pct"] = (dept_df["Count"] / total_dept * 100).round().astype(int) if total_dept else 0
            dept_df["idx"] = range(len(dept_df))
            dept_df["CountLabel"] = dept_df["Count"].astype(str) + " (" + dept_df["Pct"].astype(str) + "%)"
            dept_color_domain = dept_df["Department"].tolist()

            donut_col, legend_col = st.columns([1, 1])

            with donut_col:
                donut = alt.Chart(dept_df).mark_arc(innerRadius=52, outerRadius=82).encode(
                    theta=alt.Theta("Count:Q", stack=True),
                    color=alt.Color(
                        "Department:N",
                        scale=alt.Scale(domain=dept_color_domain, range=DEPARTMENT_BAR_COLORS),
                        legend=None,
                    ),
                    order=alt.Order("Count:Q", sort="descending"),
                    tooltip=[alt.Tooltip("Department:N"), alt.Tooltip("Count:Q"), alt.Tooltip("Pct:Q", title="Percent")],
                )
                center_total = alt.Chart(pd.DataFrame({"v": [str(total_dept)]})).mark_text(
                    size=24, fontWeight="bold", color="#0F172A", dy=-5,
                ).encode(text="v:N")
                center_label = alt.Chart(pd.DataFrame({"v": ["TOTAL"]})).mark_text(
                    size=10, color="#64748B", dy=13,
                ).encode(text="v:N")
                donut_chart = (donut + center_total + center_label).properties(height=190, width="container")
                st.altair_chart(donut_chart, width="stretch")

            with legend_col:
                legend_rows = ""
                for i, row in dept_df.iterrows():
                    color = DEPARTMENT_BAR_COLORS[i % len(DEPARTMENT_BAR_COLORS)]
                    name_esc = html.escape(row["Department"])
                    tooltip = html.escape(f"{row['Department']} — {int(row['Count'])} ({int(row['Pct'])}%)")
                    legend_rows += (
                        f'<div class="dept-legend-row" title="{tooltip}" '
                        f'style="display:flex;align-items:center;gap:8px;padding:5px 0;cursor:default;">'
                        f'<span style="width:9px;height:9px;border-radius:50%;background:{color};flex-shrink:0;"></span>'
                        f'<span style="flex:1;font-size:12.5px;color:#334155;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{name_esc}</span>'
                        f'<span style="font-size:12.5px;color:#64748B;flex-shrink:0;">{int(row["Count"])} ({int(row["Pct"])}%)</span>'
                        f'</div>'
                    )
                st.markdown(
                    "<style>.dept-legend-row:hover{background:#F1F5F9;border-radius:6px;}</style>"
                    f"<div style='padding-top:6px;'>{legend_rows}</div>",
                    unsafe_allow_html=True,
                )
        else:
            st.info(f"No {dept_portal} tenders yet.")

with chart_col2:
    with st.container(border=True):
        trend_title_col, trend_toggle_col, trend_range_col = st.columns([2, 2, 2])
        with trend_title_col:
            st.markdown("**Tenders Trend**")
        with trend_toggle_col:
            trend_portal = _chart_portal_toggle("chart_trend_portal")
        with trend_range_col:
            trend_range = st.selectbox(
                "Range", ["Last 7 Days", "Last 14 Days", "Last 30 Days"],
                key="chart_trend_range", label_visibility="collapsed", filter_mode=None,
            )
        days = {"Last 7 Days": 7, "Last 14 Days": 14, "Last 30 Days": 30}[trend_range]
        trend_data = get_daily_trend(trend_portal, days=days)
        trend_df = pd.DataFrame(trend_data, columns=["Date", "Count"])
        trend_df["Label"] = pd.to_datetime(trend_df["Date"]).dt.strftime("%d %b")
        line = alt.Chart(trend_df).mark_line(point=True, color="#4F46E5").encode(
            x=alt.X("Label:N", sort=list(trend_df["Label"]), title=None),
            y=alt.Y("Count:Q", title="No. of Tenders"),
            tooltip=[alt.Tooltip("Label:N", title="Date"), alt.Tooltip("Count:Q")],
        ).properties(height=190)
        st.altair_chart(line, width='stretch')

# Auto-refresh while agent is actively searching
if state.get("status") in ("searching", "expanding", "learning"):
    st.caption("🔄 Agent is searching... dashboard will refresh automatically")
    import time
    time.sleep(40)
    st.rerun()
# ── SECTION CONTENT ───────────────────────────────────────
section = st.session_state.active_section

# ── CEO SEARCHES ──────────────────────────────────────────
if section == "searches":
    searches = get_all_ceo_searches()
    if searches:
        for s in searches:
            st.markdown(f"""
            <div class='tender-card'>
                <div class='tender-title'>🔍 {s['keyword']}</div>
                <div class='tender-meta'>Portal: {s['portal']} | Searched {s['frequency']} times | Last: {s['searched_at'][:16]}</div>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.info("No searches yet.")

# ── PATTERNS ──────────────────────────────────────────────
elif section == "patterns":
    patterns = get_learned_patterns()
    if patterns:
        for p in patterns:
            try:
                kws = json.loads(p['keywords'])
            except:
                kws = []
            col_p, col_d = st.columns([4, 1])
            with col_p:
                st.markdown(f"""
                <div class='tender-card'>
                    <div class='tender-title'>📂 {p['category']}</div>
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
    if section == "new":
        tenders = get_all_tenders("new")
        section_title = "🟢 New Tenders"
    elif section == "saved":
        tenders = get_all_tenders("saved")
        section_title = "💾 Saved Tenders"
    else:
        tenders = get_all_tenders()
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

                    st.markdown(
                        f'<a href="app/static/{static_name}" target="_blank" style="text-decoration:none;">'
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
        col_portal, col_search, col_sort = st.columns(
            [0.2, 0.58, 0.22], vertical_alignment="center"
        )
        with col_portal:
            st.markdown('<div class="col-marker-portal"></div>', unsafe_allow_html=True)
            st.segmented_control(
                "Portal",
                PORTAL_FILTER_OPTIONS,
                default=PORTAL_FILTER_DEFAULT,
                key="portal_filter_display",
                label_visibility="collapsed",
                on_change=_sync_portal_from_pills,
            )
            st.selectbox(
                "Portal",
                PORTAL_FILTER_OPTIONS,
                key="portal_filter_dropdown",
                label_visibility="collapsed",
                on_change=_sync_portal_from_dropdown,
                filter_mode=None,
            )
        with col_search:
            st.markdown('<div class="col-marker-search"></div>', unsafe_allow_html=True)
            search_query = st_keyup(
                "Search tenders",
                key="table_search_query",
                placeholder="🔍 Search by bid number, item name, department...",
                debounce=300,
                on_change=lambda: st.session_state.update(page_num=1),
                label_visibility="collapsed",
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
        tenders = fuzzy_filter_tenders(tenders, search_query)

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

            # Table header
            st.markdown('<div class="table-card-marker"></div>', unsafe_allow_html=True)
            h1, h2, h3, h4, h5, h6, h7, h8, h9, h10 = st.columns([0.5, 1.5, 2.5, 1.7, 0.8, 1.2, 1.2, 0.8, 0.8, 0.8])
            h1.markdown('<span class="th-cell">Sl No</span>', unsafe_allow_html=True)
            h2.markdown('<span class="th-cell">Bid Number</span>', unsafe_allow_html=True)
            h3.markdown('<span class="th-cell">Item Name</span>', unsafe_allow_html=True)
            h4.markdown('<span class="th-cell">Department</span>', unsafe_allow_html=True)
            h5.markdown('<span class="th-cell">Qty</span>', unsafe_allow_html=True)
            h6.markdown('<span class="th-cell">Start Date</span>', unsafe_allow_html=True)
            h7.markdown('<span class="th-cell">End Date</span>', unsafe_allow_html=True)
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
                quantity = tender.get("quantity", "N/A")
                start_date = tender.get("published_date", "N/A")
                end_date = tender.get("deadline", "N/A")
                portal = tender.get("portal", "")
                portal_chip_class = "portal-chip-kppp" if portal.upper() == "KPPP" else "portal-chip-gem"

                r1, r2, r3, r4, r5, r6, r7, r8, r9, r10 = st.columns([0.5, 1.5, 2.5, 1.7, 0.8, 1.2, 1.2, 0.8, 0.8, 0.8])
                r1.markdown(f'<span class="td-slno">{sl_no}</span>', unsafe_allow_html=True)
                r2.markdown(
                    f'<div class="mono td-bid">{bid_number}</div>'
                    f'<span class="portal-chip {portal_chip_class}">{portal or "N/A"}</span>',
                    unsafe_allow_html=True,
                )
                r3.markdown(f"{title[:60]}")
                r4.markdown(f'<span class="td-dept">{department[:30] if department else "N/A"}</span>', unsafe_allow_html=True)
                r5.markdown(f'<span class="mono td-qty">{quantity}</span>', unsafe_allow_html=True)
                r6.markdown(f'<span class="mono td-date">{start_date}</span>', unsafe_allow_html=True)
                r7.markdown(f'<span class="mono enddate-chip">{end_date}</span>', unsafe_allow_html=True)
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
                if st.button("◀ Prev", disabled=st.session_state.page_num <= 1, key="prev_btn2"):
                    st.session_state.page_num -= 1
                    st.rerun()
            with col_page2:
                st.markdown(f"**{st.session_state.page_num}/{total_pages}**")
            with col_next2:
                if st.button("Next ▶", disabled=st.session_state.page_num >= total_pages, key="next_btn2"):
                    st.session_state.page_num += 1
                    st.rerun()