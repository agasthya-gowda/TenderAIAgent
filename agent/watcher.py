"""
Browser Activity Watcher
Runs silently in background.
CEO uses dashboard search box to trigger searches.
"""

import threading
import time
import re
from urllib.parse import urlparse, parse_qs, unquote_plus
from database.db import save_ceo_search

_callbacks = []
_watching = False
_watch_thread = None
_seen_keywords = set()

def register_callback(fn):
    _callbacks.append(fn)

def _notify(keyword, portal):
    for fn in _callbacks:
        try:
            fn(keyword, portal)
        except Exception:
            pass

def _watch_loop():
    print("[Watcher] Agent watching silently in background...")
    while _watching:
        time.sleep(1)

def start_watching():
    global _watching, _watch_thread
    if _watching:
        return
    _watching = True
    _watch_thread = threading.Thread(target=_watch_loop, daemon=True)
    _watch_thread.start()
    print("[Watcher] Started silently")

def stop_watching():
    global _watching
    _watching = False
    print("[Watcher] Stopped.")

def get_status():
    return {
        "watching": _watching,
        "searches_detected": len(_seen_keywords)
    }