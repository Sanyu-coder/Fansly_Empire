@echo off
cd /d C:\Fansly_Empire\AI_Factory

:: 1. 前回開いていた「AI Factory専用Chromeウィンドウ」および旧バックグラウンド console_app.py を停止
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object { $_.CommandLine -like '*Fansly_Empire*AI_Factory*.chrome_profile*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*console_app.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

:: 2. サーバーを先にバックグラウンドで起動
start "" /B python C:\Fansly_Empire\AI_Factory\console_app.py

:: サーバーの立ち上がりを2秒待つ
timeout /t 2 /nobreak >nul

:: 3. タブが増えない「独立アプリウィンドウ」でコンソールを開く
start "" chrome.exe --app=http://127.0.0.1:5050 --user-data-dir="C:\Fansly_Empire\AI_Factory\.chrome_profile" --window-size=1440,900