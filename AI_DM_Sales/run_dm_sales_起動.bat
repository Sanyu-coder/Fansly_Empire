@echo off
cd /d C:\Fansly_Empire\AI_DM_Sales

:: 1. 前回開いていた「AI_DM_Sales専用Chromeウィンドウ」および旧「run_dm_sales.py」プロセスだけをピンポイントで閉じる（メインのChromeやAI_Factory・ComfyUIは閉じない）
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'chrome.exe' -and $_.CommandLine -like '*Fansly_Empire*AI_DM_Sales*.chrome_profile*') -or ($_.Name -eq 'python.exe' -and $_.CommandLine -like '*AI_DM_Sales*run_dm_sales.py*') -or ($_.Name -eq 'chrome-headless-shell.exe' -and $_.CommandLine -like '*AI_DM_Sales*profiles*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 1 /nobreak >nul

:: 2. サーバーを先にバックグラウンドで起動（ポート 5051）
start "" /B python C:\Fansly_Empire\AI_DM_Sales\run_dm_sales.py

:: サーバーの立ち上がりを2秒待つ
timeout /t 2 /nobreak >nul

:: 3. タブが増えない「独立アプリウィンドウ」でコンソールを開く
start "" chrome.exe --app=http://127.0.0.1:5051 --user-data-dir="C:\Fansly_Empire\AI_DM_Sales\.chrome_profile" --window-size=1440,900