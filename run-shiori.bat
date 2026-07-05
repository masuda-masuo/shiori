@echo off
set DIR=%~dp0
call "%DIR%stop-shiori.bat"
call "%DIR%env.bat"
"%DIR%.venv\Scripts\python.exe" -m shiori serve %*
