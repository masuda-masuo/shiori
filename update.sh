#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

git pull
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -e .

echo ""
echo "Update done. Run ./run-shiori.sh to start."
echo "(Docker image is not rebuilt here -- run ./build-docker-image.sh separately when needed.)"
