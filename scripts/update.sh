#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

git pull

# Pull the CI-built image for exactly this commit: build-app.yml pushes
# :latest and :<sha> to ghcr.io on every push to main. Pulling the sha tag
# instead of :latest fails loud when the build-app workflow has not finished
# pushing yet, instead of silently restarting on the previous image.
#
# Empty DOCKER_CONFIG: the package is public (anonymous pull), and this
# sidesteps the Docker Desktop credential helper, which dies under WSL
# interop while Desktop is stopped -- even for anonymous pulls.
IMAGE=ghcr.io/masuda-masuo/shiori/app
SHA="$(git rev-parse HEAD)"
if ! DOCKER_CONFIG="$(mktemp -d)" docker pull "${IMAGE}:${SHA}"; then
  echo "" >&2
  echo "image for ${SHA} is not on ghcr.io (build-app workflow still running," >&2
  echo "or the push failed). Check: gh run list -w build-app -b main -L 1" >&2
  echo "Fallback for an offline/urgent update: docker compose build app" >&2
  exit 1
fi
docker tag "${IMAGE}:${SHA}" "${IMAGE}:latest"

if systemctl --user is-active --quiet shiori.service 2>/dev/null; then
  systemctl --user restart shiori.service
else
  docker compose up -d app
fi

echo ""
echo "Update done. app container restarted with ${IMAGE}:${SHA}."
