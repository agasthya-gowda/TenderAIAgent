"""
Browser Activity Watcher
Monitors CEO's Chrome/Edge browser history every 5 seconds
and extracts search keywords from GeM and KPPP portals.
"""

import time
import threading
import os
import shutil
import sqlite3
from urllib.parse import urlparse, parse_qs, unquote_plus
from database.db import save_ceo_search

# URL patterns to detect searches on each portal
PORTAL_PATTERNS = {
    "GeM": [
        {"domain": "bidplus.gem.gov.in", "params": ["search_keyword", "q", "search", "keyword"]},
        {"domain": "gem.gov.in", "params": ["search_keyword", "q", "search", "keyword"]},
    ],
    "KPPP": [
        {"domain": "kppp.karnataka.gov.in", "params": ["search", "keyword", "q"]},
    ]
}

# Browser history file locations on Windows
BROWSER_HISTORY_PATHS = {
    "Chrome": os.path.join(
        os.path.expanduser("~"), "AppData", "Local",
        "Google", "Chrome", "User Data", "Default", "History"
    ),
    "Edge": os.path.join(
        os.path.expanduser("~"), "AppData", "Local",
        "Microsoft", "Edge", "User Data", "Default", "History"
    )
}

_callbacks = []
_watching = False
_watch_thread = None
_seen_urls = set()
_last_visit_times = {}

def register_callback(fn):
    """Register function to call when CEO makes a search."""
    _callbacks.append(fn)

def _notify(keyword, portal):
    """Notify all registered callbacks."""
    for fn in _callbacks:
        try:
            fn(keyword, portal)
        except Exception:
            pass

def extract_keyword_from_url(url: str):
    """Extract search keyword and portal name from a URL."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        params = parse_qs(parsed.query)

        for portal_name, patterns in PORTAL_PATTERNS.items():
            for p in patterns:
                if p["domain"] in domain:
                    for param in p["params"]:
                        keyword = params.get(param, [None])[0]
                        if keyword and len(keyword.strip()) > 1:
                            return unquote_plus(keyword).strip(), portal_name
    except Exception:
        pass
    return None, None

def _watch_loop():
    """Main watching loop - checks browser history every 5 seconds."""
    while _watching:
        for browser, history_path in BROWSER_HISTORY_PATHS.items():
            if not os.path.exists(history_path):
                continue
            try:
                # Copy DB because browser locks it
                temp_path = history_path + "_tender_copy"
                shutil.copy2(history_path, temp_path)

                conn = sqlite3.connect(temp_path)
                conn.row_factory = sqlite3.Row

                last_time = _last_visit_times.get(browser, 0)

                rows = conn.execute("""
                    SELECT url, last_visit_time
                    FROM urls
                    WHERE last_visit_time > ?
                    ORDER BY last_visit_time DESC
                    LIMIT 30
                """, (last_time,)).fetchall()

                for row in rows:
                    url = row["url"]
                    visit_time = row["last_visit_time"]

                    if url in _seen_urls:
                        continue
                    _seen_urls.add(url)

                    keyword, portal = extract_keyword_from_url(url)
                    if keyword and portal:
                        print(f"[Watcher] CEO searched: '{keyword}' on {portal}")
                        save_ceo_search(keyword, portal)
                        _notify(keyword, portal)

                    if visit_time > _last_visit_times.get(browser, 0):
                        _last_visit_times[browser] = visit_time

                conn.close()
                os.remove(temp_path)

            except Exception as e:
                pass

        time.sleep(5)

def start_watching():
    """Start watching CEO browser in background thread."""
    global _watching, _watch_thread
    if _watching:
        return
    _watching = True
    _watch_thread = threading.Thread(target=_watch_loop, daemon=True)
    _watch_thread.start()
    print("[Watcher] Started watching CEO browser activity...")

def stop_watching():
    """Stop the watcher."""
    global _watching
    _watching = False
    print("[Watcher] Stopped.")

def get_status():
    return {
        "watching": _watching,
        "searches_detected": len(_seen_urls)
    }