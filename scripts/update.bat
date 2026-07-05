@echo off
cd /d "%~dp0.."
git pull
docker compose build app
docker compose up -d app

echo.
echo Update done. app container restarted with the new image.
echo (fresh full start: scripts\run-shiori.bat)
