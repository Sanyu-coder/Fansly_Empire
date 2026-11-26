@echo off
cd /d C:\Fansly_Empire\AI_Factory

:: 1. Stop the previous dedicated Chrome window and old background console_app.py
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object { $_.CommandLine -like '*Fansly_Empire*AI_Factory*.chrome_profile*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*console_app.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

:: 2. Start the server in an independent hidden window (survives after this bat exits)
powershell -NoProfile -Command "Start-Process python -ArgumentList 'C:\Fansly_Empire\AI_Factory\console_app.py' -WindowStyle Hidden -WorkingDirectory 'C:\Fansly_Empire\AI_Factory'"

:: Wait 2 seconds for the server to come up
timeout /t 2 /nobreak >nul

:: 3. Open the console in a separate app window (no extra tabs)
start "" chrome.exe --app=http://127.0.0.1:5050 --user-data-dir="C:\Fansly_Empire\AI_Factory\.chrome_profile" --window-size=1440,900
