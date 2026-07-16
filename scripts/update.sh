#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

git pull

# Prefer the prebuilt image from ghcr.io (built by build-app.yml on main),
# falling back to a local build when pull is not possible (#254).
# GITHUB_TOKEN (read:packages) comes from the environment or .env — never
# from the repository itself.
if [[ -z "${GITHUB_TOKEN:-}" && -f .env ]]; then
  GITHUB_TOKEN="$(sed -n 's/^GITHUB_TOKEN=//p' .env | tail -1 | tr -d "\"'" )"
fi

pulled=""
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  login_err=$(mktemp /tmp/docker-login-err.XXXXXX)
  if printf '%s' "$GITHUB_TOKEN" \
      | docker login ghcr.io -u "${GHCR_USER:-masuda-masuo}" --password-stdin >/dev/null 2>"$login_err"; then
    rm -f "$login_err"
    if docker compose pull app; then
      pulled=1
      echo "Pulled prebuilt app image from ghcr.io — skipping local build."
    else
      echo "ghcr.io pull failed — falling back to local build."
    fi
    docker logout ghcr.io >/dev/null 2>&1 || true
  else
    echo "docker login ghcr.io failed — falling back to local build."
    sed 's/^/  | /' "$login_err"
    rm -f "$login_err"
  fi
else
  echo "GITHUB_TOKEN not set — building locally."
fi

if [[ -z "$pulled" ]]; then
  docker compose build app
fi

if systemctl --user is-active --quiet shiori.service 2>/dev/null; then
  systemctl --user restart shiori.service
else
  docker compose up -d app
fi

echo ""
echo "Update done. app container restarted with the new image."
