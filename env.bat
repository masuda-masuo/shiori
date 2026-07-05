@echo off
for /f "usebackq delims=" %%t in (`C:\work\mcp\mcp-token.exe github`) do set GITHUB_TOKEN=%%t
set SHIORI_REPOS=masuda-masuo/shiori,masuda-masuo/code-sandbox-mcp,masuda-masuo/mcp-launcher
set SHIORI_INDEX_CODE=true
set SHIORI_ALLOW_REBUILD=false
set DATABASE_URL=postgresql://shiori:shiori@127.0.0.1:5432/shiori
set SHIORI_INDEX_BOT_LOGINS=mcp-launcher-masuda[bot],github-app[bot],mcp-launcher-masuda,code-sandbox-mcp[bot]
set SHIORI_SYNC_INTERVAL_SECONDS=10
