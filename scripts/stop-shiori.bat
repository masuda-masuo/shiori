@echo off
cd /d "%~dp0.."
REM トークン更新ループを止める
taskkill /f /t /fi "WINDOWTITLE eq shiori-token-refresher*" >nul 2>&1
REM 旧運用（ホスト venv serve）が残っていれば止める
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object CommandLine -match 'shiori serve' | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
docker compose stop app 2>nul
exit /b 0
