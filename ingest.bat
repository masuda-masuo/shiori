@echo off
cd /d "C:\work\mcp\shiori"
call env.bat
.venv\Scripts\python.exe -m shiori ingest %*
