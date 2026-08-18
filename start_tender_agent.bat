@echo off
cd "C:\Users\Kishan Gowda\OneDrive\Desktop\tender-agent"
taskkill /f /im python.exe >nul 2>&1
taskkill /f /im pythonw.exe >nul 2>&1
timeout /t 2 >nul
start "" "C:\Users\Kishan Gowda\OneDrive\Desktop\tender-agent\venv\Scripts\pythonw.exe" run_agent.pyw