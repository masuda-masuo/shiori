#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Building shiori-app:local from docker/app/Dockerfile..."
docker build -f "$DIR/docker/app/Dockerfile" -t shiori-app:local "$DIR"

echo ""
echo "Done. Image: shiori-app:local"
