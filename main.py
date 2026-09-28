"""
Main Entry Point
Starts the Tender Agent system:
1. Starts the agent in background
2. Launches Streamlit dashboard
"""

import os
import sys
import subprocess
import webbrowser
import time
import threading



def main():
    print("=" * 50)
    print("  🔍 TENDER AI AGENT")
    print("  Starting up...")
    print("=" * 50)

    # Make sure downloads folder exists
    download_path = os.getenv(
        "DOWNLOAD_PATH",
        os.path.join(os.path.dirname(__file__), "downloads")
    )
    os.makedirs(download_path, exist_ok=True)

    # Get path to streamlit app
    dashboard_path = os.path.join(
        os.path.dirname(__file__),
        "dashboard",
        "app.py"
    )

    print("\n✅ Starting dashboard...")
    print("📌 Open browser at: http://localhost:8501")
    print("📌 Press Ctrl+C to stop\n")

    # Streamlit opens browser automatically, no need to do it here

    # Start Streamlit
    subprocess.run([
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

if __name__ == "__main__":
    main()