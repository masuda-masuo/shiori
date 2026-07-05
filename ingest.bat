@echo off
set DIR=%~dp0
call "%DIR%env.bat"
"%DIR%.venv\Scripts\python.exe" -m shiori ingest %*
