"""
Tender Agent Dashboard
"""

import streamlit as st
import os
import json
from rapidfuzz import fuzz
from database.db import (
    get_all_tenders, get_stats, update_tender_status,
    update_tender_pdfs, get_all_ceo_searches, get_learned_patterns,
    delete_tender, delete_pattern
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
<style>
    .main { background-color: #F8F9FA; }
    .block-container { padding-top: 1rem !important; }
    div[data-testid="stVerticalBlock"] div:has(> div.stats-sticky) {
        position: sticky;
        top: 0;
        z-index: 999;
        background-color: #F8F9FA;
        padding-bottom: 8px;
    }
    .stats-sticky {
        background-color: #F8F9FA;
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
        padding: 12px 8px;
        white-space: pre-wrap;
    }
    div[data-testid="stButton"] button:has-text("🗑️") {
        background-color: #E74C3C !important;
        color: white !important;
        border: none !important;
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
        if st.button("▶️ Start Agent", use_container_width=True, type="primary"):
            start_agent()
            st.session_state.agent_started = True
            st.success("Agent started!")
            st.rerun()
    else:
        if st.button("⏹️ Stop Agent", use_container_width=True):
            stop_agent()
            st.session_state.agent_started = False
            st.rerun()

    with st.expander("🔍 GeM Search Filters"):
        keyword_input = st.text_input("Search Tenders", placeholder="Enter keyword e.g. cctv")
        if st.button("🚀 Search", use_container_width=True, type="primary"):
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
    with st.expander("🏛️ KPPP Search Filters"):
        kppp_category = st.selectbox(
            "Category", KPPP_CATEGORIES, key="kppp_category"
        )
        kppp_entity = st.selectbox(
            "Procuring Entity (optional)", ["-- Any --"] + KPPP_ENTITIES, key="kppp_entity"
        )
        if st.button("🔎 Search KPPP", use_container_width=True, type="primary", key="kppp_search_btn"):
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
    st.markdown("### 🔽 Filter Tenders")
    portal_filter = st.selectbox("Portal", ["All", "GeM", "KPPP"])
    sort_by = st.selectbox("Sort By", ["Relevance", "Newest First", "Deadline"])
    

   
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

    if portal_filter != "All":
        tenders = [t for t in tenders if t["portal"] == portal_filter]

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
        search_query = st.text_input(
            "🔍 Search tenders",
            key="table_search_query",
            placeholder="Search by bid number, item name, department...",
            on_change=lambda: st.session_state.update(page_num=1),
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
            h1, h2, h3, h4, h5, h6, h7, h8, h9, h10 = st.columns([0.5, 1.5, 2.5, 1.7, 0.8, 1.2, 1.2, 0.8, 0.8, 0.8])
            h1.markdown("**Sl No**")
            h2.markdown("**Bid Number**")
            h3.markdown("**Item Name**")
            h4.markdown("**Department**")
            h5.markdown("**Qty**")
            h6.markdown("**Start Date**")
            h7.markdown("**End Date**")
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

                r1, r2, r3, r4, r5, r6, r7, r8, r9, r10 = st.columns([0.5, 1.5, 2.5, 1.7, 0.8, 1.2, 1.2, 0.8, 0.8, 0.8])
                r1.markdown(f"{sl_no}")
                r2.markdown(f"{bid_number}")
                r3.markdown(f"{title[:60]}")
                r4.markdown(f"{department[:30] if department else 'N/A'}")
                r5.markdown(f"{quantity}")
                r6.markdown(f"{start_date}")
                r7.markdown(f"{end_date}")
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