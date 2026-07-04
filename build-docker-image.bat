@echo off
cd /d "C:\work\mcp\shiori"

echo Building shiori-app:local from docker/app/Dockerfile (local build; repo is
echo private so GHCR pull is not authorized yet -- see issue #119)...
docker build -f docker/app/Dockerfile -t shiori-app:local .

echo.
echo Done. Image: shiori-app:local
