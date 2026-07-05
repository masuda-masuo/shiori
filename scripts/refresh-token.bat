@echo off
REM 短命 GitHub トークンを runtime\github-token に発行する（mcp-token モデル）。
REM app コンテナは GITHUB_TOKEN_COMMAND=cat /run/shiori/github-token で読む。
REM TokenCommandProvider は読んだトークンを最長 50 分使い回すため、失効（60 分）
REM 以内に収めるにはファイルは常に 10 分未満の鮮度が必要。--loop の間隔は
REM 300 秒（5 分）から増やさないこと。
cd /d "%~dp0.."
if not defined MCP_TOKEN_EXE set "MCP_TOKEN_EXE=C:\work\mcp\mcp-token.exe"
if not exist runtime mkdir runtime

if "%~1"=="--loop" goto loop

call :refresh
exit /b %errorlevel%

:loop
call :refresh
timeout /t 300 /nobreak >nul
goto loop

:refresh
"%MCP_TOKEN_EXE%" github > runtime\github-token.tmp
if errorlevel 1 (
  echo refresh-token: mcp-token failed 1>&2
  del runtime\github-token.tmp >nul 2>&1
  exit /b 1
)
move /y runtime\github-token.tmp runtime\github-token >nul
exit /b 0
