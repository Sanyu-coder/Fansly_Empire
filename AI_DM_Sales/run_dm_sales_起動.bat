@echo off
cd /d C:\Fansly_Empire\AI_DM_Sales

:: 1. 前回開いていた「AI_DM_Sales専用Chromeウィンドウ」だけをピンポイントで閉じる（メインのChromeやAI_Factoryは閉じない）
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object { $_.CommandLine -like '*Fansly_Empire*AI_DM_Sales*.chrome_profile*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

:: 2. サーバーを先にバックグラウンドで起動（ポート 5051）
start "" /B python C:\Fansly_Empire\AI_DM_Sales\run_dm_sales.py

:: サーバーの立ち上がりを2秒待つ
timeout /t 2 /nobreak >nul

:: 3. タブが増えない「独立アプリウィンドウ」でコンソールを開く
start "" chrome.exe --app=http://127.0.0.1:5051 --user-data-dir="C:\Fansly_Empire\AI_DM_Sales\.chrome_profile" --window-size=1440,900