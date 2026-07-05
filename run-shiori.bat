@echo off
cd /d "C:\work\mcp\shiori"
call stop-shiori.bat
call env.bat
.venv\Scripts\python.exe -m shiori serve %*
