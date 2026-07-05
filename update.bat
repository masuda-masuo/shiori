@echo off
set DIR=%~dp0
cd /d "%DIR%"
git pull
"%DIR%.venv\Scripts\pip.exe" install torch --index-url https://download.pytorch.org/whl/cpu
"%DIR%.venv\Scripts\pip.exe" install -e .[dev]

echo.
echo Update done. Run run-shiori.bat to start.
echo (Docker image is not rebuilt here -- run build-docker-image.bat separately when needed.)
