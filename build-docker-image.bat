@echo off
set DIR=%~dp0
cd /d "%DIR%"

echo Building shiori-app:local from docker/app/Dockerfile (local build; repo is
echo private so GHCR pull is not authorized yet -- see issue #119)...
docker build -f "%DIR%docker/app/Dockerfile" -t shiori-app:local "%DIR%"

echo.
echo Done. Image: shiori-app:local
