@echo off
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object CommandLine -match 'shiori serve' | ForEach-Object { Write-Host \"Stopping PID $($_.ProcessId)...\"; Stop-Process -Id $_.ProcessId -Force }" 2>nul
