@echo off
setlocal enabledelayedexpansion
set DIR=%~dp0
set DIR=%DIR:~0,-1%

if not exist "%DIR%\.venv" (
    echo Creating Python virtual environment at %DIR%\.venv...
    python -m venv "%DIR%\.venv"
)

if exist "%DIR%\.env" (
    for /f "usebackq delims=" %%a in ("%DIR%\.env") do set "%%a"
)

set GITHUB_TOKEN_COMMAND=%GITHUB_TOKEN_COMMAND%
if not defined GITHUB_TOKEN_COMMAND set GITHUB_TOKEN_COMMAND=C:\work\mcp\mcp-token.exe github
set SHIORI_REPOS=%SHIORI_REPOS%
if not defined SHIORI_REPOS set SHIORI_REPOS=masuda-masuo/shiori,masuda-masuo/code-sandbox-mcp,masuda-masuo/mcp-launcher
set SHIORI_INDEX_CODE=%SHIORI_INDEX_CODE%
if not defined SHIORI_INDEX_CODE set SHIORI_INDEX_CODE=true
set SHIORI_ALLOW_REBUILD=%SHIORI_ALLOW_REBUILD%
if not defined SHIORI_ALLOW_REBUILD set SHIORI_ALLOW_REBUILD=false
set DATABASE_URL=%DATABASE_URL%
if not defined DATABASE_URL set DATABASE_URL=postgresql://shiori:shiori@127.0.0.1:5432/shiori
set SHIORI_INDEX_BOT_LOGINS=%SHIORI_INDEX_BOT_LOGINS%
if not defined SHIORI_INDEX_BOT_LOGINS set SHIORI_INDEX_BOT_LOGINS=mcp-launcher-masuda[bot],github-app[bot],mcp-launcher-masuda,code-sandbox-mcp[bot]
set SHIORI_SYNC_INTERVAL_SECONDS=%SHIORI_SYNC_INTERVAL_SECONDS%
if not defined SHIORI_SYNC_INTERVAL_SECONDS set SHIORI_SYNC_INTERVAL_SECONDS=10
endlocal & (
    set GITHUB_TOKEN_COMMAND=%GITHUB_TOKEN_COMMAND%
    set SHIORI_REPOS=%SHIORI_REPOS%
    set SHIORI_INDEX_CODE=%SHIORI_INDEX_CODE%
    set SHIORI_ALLOW_REBUILD=%SHIORI_ALLOW_REBUILD%
    set DATABASE_URL=%DATABASE_URL%
    set SHIORI_INDEX_BOT_LOGINS=%SHIORI_INDEX_BOT_LOGINS%
    set SHIORI_SYNC_INTERVAL_SECONDS=%SHIORI_SYNC_INTERVAL_SECONDS%
)
