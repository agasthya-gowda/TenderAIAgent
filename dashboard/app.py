"""
Tender Agent Dashboard
"""

import streamlit as st
import os
import json
import base64
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

st.set_page_config(
    page_title="Tender Agent",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .main { background-color: #F8F9FA; }
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

# ── HEADER ────────────────────────────────────────────────
st.markdown("""
<div class='agent-header'>
    <h1>🔍 Tender Agent</h1>
    <p>AI-powered tender discovery for GeM & KPPP portals</p>
</div>
""", unsafe_allow_html=True)

# ── SIDEBAR ───────────────────────────────────────────────
with st.sidebar:
    st.markdown("### ⚙️ Agent Control")

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
        keyword_input = st.text_input("🔍 Search Tenders", placeholder="Enter keyword e.g. cctv")
        if st.button("🚀 Search", use_container_width=True, type="primary"):
            if keyword_input.strip():
                from agent.orchestrator import on_ceo_search
                on_ceo_search(keyword_input.strip(), "GeM")
                st.success(f"Searching for '{keyword_input}'...")
            else:
                st.warning("Please enter a keyword first")

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

    st.markdown("---")
    st.markdown("### 🔍 Search History")
    searches = get_all_ceo_searches()
    if searches:
        for s in searches[:8]:
            st.markdown(f"**{s['keyword']}** — {s['portal']} ({s['frequency']}x)")
    else:
        st.caption("No searches detected yet.")

    st.markdown("---")
    st.markdown("### 🧠 Learned Patterns")
    patterns = get_learned_patterns()
    if patterns:
        for p in patterns[:5]:
            st.markdown(f"**{p['category']}** — weight: {p['weight']:.1f}")
    else:
        st.caption("Agent is still learning...")

    st.markdown("---")
    if st.button("📡 Live Agent Log", use_container_width=True):
        st.session_state.active_section = "log"
        st.rerun()

# ── STATS ROW ─────────────────────────────────────────────
stats = get_stats()
c1, c2, c3, c4, c5 = st.columns(5)

with c1:
    if st.button(
        f"📋  {stats['total_tenders']}\nTotal Tenders",
        key="btn_all", use_container_width=True
    ):
        st.session_state.active_section = "all"
        st.session_state.page_num = 1
        st.rerun()

with c2:
    if st.button(
        f"🟢  {stats['new']}\nNew Tenders",
        key="btn_new", use_container_width=True
    ):
        st.session_state.active_section = "new"
        st.session_state.page_num = 1
        st.rerun()

with c3:
    if st.button(
        f"💾  {stats['saved']}\nSaved",
        key="btn_saved", use_container_width=True
    ):
        st.session_state.active_section = "saved"
        st.session_state.page_num = 1
        st.rerun()

with c4:
    if st.button(
        f"🔎  {stats['ceo_searches']}\nSearch History",
        key="btn_searches", use_container_width=True
    ):
        st.session_state.active_section = "searches"
        st.session_state.page_num = 1
        st.rerun()

with c5:
    if st.button(
        f"🧠  {stats['patterns_learned']}\nPatterns Learned",
        key="btn_patterns", use_container_width=True
    ):
        st.session_state.active_section = "patterns"
        st.session_state.page_num = 1
        st.rerun()

st.markdown("<br>", unsafe_allow_html=True)

# Auto-refresh while agent is actively searching
if state.get("status") in ("searching", "expanding", "learning"):
    st.caption("🔄 Agent is searching... dashboard will refresh automatically")
    import time
    time.sleep(40)
    st.rerun()

st.markdown("---")

# ── SECTION CONTENT ───────────────────────────────────────
section = st.session_state.active_section

# ── CEO SEARCHES ──────────────────────────────────────────
if section == "searches":
    st.markdown("### 🔍 Search History")
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
    st.markdown("### 🧠 Learned Patterns")
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
else:
    if section == "new":
        tenders = get_all_tenders("new")
        st.markdown("### 🟢 New Tenders")
    elif section == "saved":
        tenders = get_all_tenders("saved")
        st.markdown("### 💾 Saved Tenders")
    else:
        tenders = get_all_tenders()
        st.markdown("### 📋 All Tenders")

    if portal_filter != "All":
        tenders = [t for t in tenders if t["portal"] == portal_filter]

    if sort_by == "Newest First":
        tenders = sorted(tenders, key=lambda x: x["found_at"], reverse=True)
    elif sort_by == "Deadline":
        tenders = sorted(tenders, key=lambda x: x.get("deadline", ""))
    else:
        tenders = sorted(tenders, key=lambda x: x.get("relevance_score", 0), reverse=True)

    if not tenders:
        st.info("🔍 No tenders found yet.")
    else:
        # Pagination
        page_size = 10
        total_pages = max(1, (len(tenders) - 1) // page_size + 1)

        if st.session_state.page_num > total_pages:
            st.session_state.page_num = 1

        st.markdown(f"**{len(tenders)} tenders** — Page {st.session_state.page_num} of {total_pages}")
        st.markdown("---")

        start = (st.session_state.page_num - 1) * page_size
        end = start + page_size
        page_tenders = tenders[start:end]

        for tender in page_tenders:
            tid = tender["id"]
            portal = tender.get("portal", "")
            title = tender.get("title", "Unknown")
            value = tender.get("value", "N/A")
            deadline = tender.get("deadline", "N/A")
            score = tender.get("relevance_score", 0)
            found_via = tender.get("found_via", "")
            status_val = tender.get("status", "new")

            with st.expander(
                f"{'🟢' if status_val == 'new' else '💾' if status_val == 'saved' else '⬜'} "
                f"{title[:80]}  |  {portal}  |  {value}  |  {deadline}  |  {int(score*100)}%"
            ):
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
                    for label, val in details_data:
                        if val and val not in ["", "None", "null"]:
                            st.markdown(
                                f"<span class='detail-label'>{label}:</span> "
                                f"<span class='detail-value'>{val}</span>",
                                unsafe_allow_html=True
                            )

                with col_b:
                    st.markdown("#### 🔗 Links & Downloads")
                    url = tender.get("url", "")
                    if url:
                        st.markdown(
                            f"🔗 <a href='{url}' target='_blank'>Open on {portal} Portal</a>",
                            unsafe_allow_html=True
                        )
                    st.markdown("---")

                    downloaded_files = tender.get("downloaded_files", [])
                    if isinstance(downloaded_files, str):
                        try:
                            downloaded_files = json.loads(downloaded_files)
                        except:
                            downloaded_files = []

                    # Refresh KPPP files from DB
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
                                    key=f"file_{tid}_{filename}_{section}"
                                )
                    else:
                        if portal == "GeM":
                            st.caption("No files downloaded yet")

                    st.markdown("---")

                    analysis_path = tender.get("analysis_pdf_path", "")

                    if analysis_path and os.path.exists(analysis_path):
                        with open(analysis_path, "rb") as f:
                            pdf_bytes = f.read()

                        pdf_b64 = base64.b64encode(pdf_bytes).decode()
                        html_viewer = f"""<!DOCTYPE html><html>
                        <head><title>Tender Analysis</title></head>
                        <body style="margin:0;padding:0;">
                        <embed src="data:application/pdf;base64,{pdf_b64}"
                               width="100%" height="100%"
                               type="application/pdf"
                               style="position:fixed;top:0;left:0;width:100%;height:100%;">
                        </body></html>"""
                        html_b64 = base64.b64encode(html_viewer.encode()).decode()
                        st.markdown(
                            f'<a href="data:text/html;base64,{html_b64}" '
                            f'target="_blank" style="text-decoration:none;">'
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
                            key=f"analysis_{tid}_{section}",
                        )
                    else:
                        if st.session_state.analyzing.get(tid):
                            st.info("⏳ Generating analysis... please wait")
                        else:
                            if st.button(
                                "📊 Get Full Analysis",
                                key=f"analyze_{tid}_{section}_{status_val}",
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
                    if section == "saved":
                        if st.button("🔓 Unsave", key=f"unsave_{tid}_{section}"):
                            update_tender_status(tid, "new")
                            st.rerun()
                    else:
                        if st.button("💾 Save", key=f"save_{tid}_{section}"):
                            update_tender_status(tid, "saved")
                            st.success("Saved!")
                            st.rerun()

                with col_y:
                    if st.button("🗑️ Delete", key=f"delete_{tid}_{section}"):
                        st.session_state[f"confirm_delete_{tid}"] = True

                    if st.session_state.get(f"confirm_delete_{tid}"):
                        st.warning("Are you sure you want to delete?")
                        col_yes, col_no = st.columns(2)
                        with col_yes:
                            if st.button("✅ Yes Delete", key=f"yes_{tid}_{section}"):
                                delete_tender(tid)
                                st.session_state[f"confirm_delete_{tid}"] = False
                                st.rerun()
                        with col_no:
                            if st.button("❌ Cancel", key=f"no_{tid}_{section}"):
                                st.session_state[f"confirm_delete_{tid}"] = False
                                st.rerun()

                with col_z:
                    if status_val == "saved":
                        st.success("✅ Saved")
                    elif status_val == "skipped":
                        st.warning("⬜ Skipped")
                    else:
                        st.info("🟢 New")



                        # Bottom pagination
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
                
                