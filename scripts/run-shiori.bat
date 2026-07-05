@echo off
REM compose の app コンテナ（ripgrep 同梱）で shiori を serve する。
REM 認証は mcp-token モデル: GitHub App の鍵は OS keystore から出さず、
REM ホスト側で発行した短命トークン（runtime\github-token）だけがコンテナに届く。
cd /d "%~dp0.."
call scripts\stop-shiori.bat
call scripts\refresh-token.bat
if errorlevel 1 (
  echo run-shiori: initial token mint failed; aborting. 1>&2
  exit /b 1
)
start "shiori-token-refresher" /min cmd /c "scripts\refresh-token.bat --loop"
docker compose up -d --build app
echo shiori MCP: http://localhost:8765/mcp
