@echo off
REM compose の ingest サービス（app と同一イメージ）でワンショット同期する。
cd /d "%~dp0.."
call scripts\refresh-token.bat
if errorlevel 1 exit /b 1
docker compose run --build --rm ingest python -m shiori ingest %*
