"""
System Tray Application
Runs agent silently in background.
Shows icon in Windows system tray.
CEO right clicks to open dashboard.
Shows notifications when new tenders found.
Auto starts with Windows.
"""

import os
import sys
import threading
import subprocess
import webbrowser
import time
from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as item
from dotenv import load_dotenv

load_dotenv()

# ── STATE ─────────────────────────────────────────────────
_state = {
    "dashboard_running": False,
    "agent_running": False,
    "last_tender_count": 0
}

def create_icon_image():
    """Create a simple purple circle icon for system tray."""
    img = Image.new("RGB", (64, 64), color=(142, 68, 173))
    draw = ImageDraw.Draw(img)
    # Draw white magnifying glass
    draw.ellipse([15, 10, 42, 37], outline="white", width=4)
    draw.line([38, 34, 52, 48], fill="white", width=4)
    return img

def open_dashboard():
    """Open dashboard in browser."""
    webbrowser.open("http://localhost:8501")

def start_dashboard_process():
    """Start Streamlit dashboard in background."""
    if _state["dashboard_running"]:
        open_dashboard()
        return

    dashboard_path = os.path.join(
        os.path.dirname(__file__),
        "dashboard",
        "app.py"
    )

    subprocess.Popen([
        sys.executable, "-m", "streamlit", "run",
        dashboard_path,
        "--server.port", "8501",
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
        "--theme.primaryColor", "#8E44AD",
        "--theme.backgroundColor", "#F8F9FA",
        "--theme.secondaryBackgroundColor", "#FFFFFF",
        "--theme.textColor", "#2C3E50"
    ])

    _state["dashboard_running"] = True
    time.sleep(3)
    open_dashboard()

def start_agent_background():
    """Start the tender agent in background."""
    if _state["agent_running"]:
        return

    try:
        sys.path.insert(0, os.path.dirname(__file__))
        from agent.orchestrator import start_agent
        start_agent()
        _state["agent_running"] = True
        print("[Tray] Agent started in background")
    except Exception as e:
        print(f"[Tray] Agent start error: {e}")

def check_new_tenders(icon):
    """
    Check for new tenders every 5 minutes.
    Show notification if new ones found.
    """
    while True:
        try:
            time.sleep(300)  # Check every 5 minutes
            from database.db import get_stats
            stats = get_stats()
            current_count = stats.get("new", 0)

            if current_count > _state["last_tender_count"]:
                new_count = current_count - _state["last_tender_count"]
                _state["last_tender_count"] = current_count
                # Show notification
                icon.notify(
                    f"🔔 {new_count} new tender(s) found!",
                    "Tender Agent"
                )
        except Exception:
            pass

def setup_autostart():
    """Add to Windows startup so agent starts with Windows."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE
        )
        tray_path = os.path.abspath(__file__)
        python_path = sys.executable
        winreg.SetValueEx(
            key,
            "TenderAgent",
            0,
            winreg.REG_SZ,
            f'"{python_path}" "{tray_path}"'
        )
        winreg.CloseKey(key)
        print("[Tray] Added to Windows startup")
        return True
    except Exception as e:
        print(f"[Tray] Autostart setup error: {e}")
        return False

def remove_autostart():
    """Remove from Windows startup."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE
        )
        winreg.DeleteValue(key, "TenderAgent")
        winreg.CloseKey(key)
        print("[Tray] Removed from Windows startup")
        return True
    except Exception as e:
        print(f"[Tray] Remove autostart error: {e}")
        return False

def on_open_dashboard(icon, item):
    """Menu: Open Dashboard."""
    threading.Thread(
        target=start_dashboard_process,
        daemon=True
    ).start()

def on_search_now(icon, item):
    """Menu: Trigger manual search."""
    try:
        from agent.orchestrator import trigger_manual_search
        trigger_manual_search()
        icon.notify("🔍 Manual search started!", "Tender Agent")
    except Exception as e:
        print(f"[Tray] Search error: {e}")

def on_enable_autostart(icon, item):
    """Menu: Enable auto start with Windows."""
    if setup_autostart():
        icon.notify(
            "✅ Tender Agent will now start with Windows",
            "Tender Agent"
        )

def on_disable_autostart(icon, item):
    """Menu: Disable auto start."""
    if remove_autostart():
        icon.notify(
            "❌ Auto start disabled",
            "Tender Agent"
        )

def on_quit(icon, item):
    """Menu: Quit."""
    icon.stop()
    os._exit(0)

def main():
    """Start the tray application."""
    print("[Tray] Starting Tender Agent...")

    # Start agent in background
    threading.Thread(
        target=start_agent_background,
        daemon=True
    ).start()

    # Start dashboard
    threading.Thread(
        target=start_dashboard_process,
        daemon=True
    ).start()

    # Create tray icon
    icon_image = create_icon_image()

    # Build menu
    menu = pystray.Menu(
        item("🔍 Open Dashboard", on_open_dashboard, default=True),
        item("🔄 Search Now", on_search_now),
        pystray.Menu.SEPARATOR,
        item("▶️ Enable Auto Start", on_enable_autostart),
        item("⏹️ Disable Auto Start", on_disable_autostart),
        pystray.Menu.SEPARATOR,
        item("❌ Quit", on_quit)
    )

    # Create tray icon
    icon = pystray.Icon(
        "TenderAgent",
        icon_image,
        "Tender Agent — Running",
        menu
    )

    # Start notification checker
    threading.Thread(
        target=check_new_tenders,
        args=(icon,),
        daemon=True
    ).start()

    print("[Tray] Tender Agent is running in system tray")
    print("[Tray] Right click the tray icon to open dashboard")

    # Run tray icon (blocks until quit)
    icon.run()

if __name__ == "__main__":
    main()