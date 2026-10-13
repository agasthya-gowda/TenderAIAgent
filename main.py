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

    # Start Streamlit. Theme now lives in .streamlit/config.toml instead of
    # being passed here as flags — that way it applies no matter how
    # Streamlit gets launched (this local launcher, or the server's own
    # `streamlit run dashboard/app.py` directly), instead of only here.
    subprocess.run([
        sys.executable, "-m", "streamlit", "run",
        dashboard_path,
        "--server.port", "8501",
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ])

if __name__ == "__main__":
    main()