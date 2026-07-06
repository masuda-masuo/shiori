#!/usr/bin/env bash
# Mint a short-lived GitHub token into runtime/github-token (mcp-token model).
# The app container reads it via GITHUB_TOKEN_COMMAND=cat /run/shiori/github-token.
# TokenCommandProvider reuses the token for up to 50 min, so the file must stay
# fresher than 10 min.  --loop runs every 300 s (5 min) -- do not increase.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHIORI_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$SHIORI_DIR"
mkdir -p runtime

# --- sha256 for pinned mcp-token binaries (masuda-masuo/mcp-launcher mcp-token/v1.1.1) ---
MCP_TOKEN_LINUX_AMD64_SHA256="08d22380f0af932508aaaea80cb114acada1ef46d0a3b32507755c67f5f77bba"
MCP_TOKEN_LINUX_ARM64_SHA256="032ee0942fd4e2184158f111873c67f32d843def0cbefca576df614bfc8d3c64"

resolve_mcp_token() {
  # 1. explicit override
  if [ -n "${MCP_TOKEN_EXE:-}" ] && [ -x "${MCP_TOKEN_EXE}" ]; then
    echo "${MCP_TOKEN_EXE}"
    return 0
  fi

  # 2. in PATH
  if command -v mcp-token >/dev/null 2>&1; then
    command -v mcp-token
    return 0
  fi

  # 3. local cache
  local cached="runtime/mcp-token"
  if [ -x "$cached" ]; then
    echo "$cached"
    return 0
  fi

  # 4. download Linux binary from GitHub
  if download_mcp_token "$cached"; then
    echo "$cached"
    return 0
  fi

  echo "ERROR: cannot find or download mcp-token." >&2
  echo "  Set MCP_TOKEN_EXE=/path/to/mcp-token or ensure network access." >&2
  return 1
}

download_mcp_token() {
  local dest="$1"
  local arch expected_sha
  case "$(uname -m)" in
    x86_64|amd64)
      arch="amd64"
      expected_sha="$MCP_TOKEN_LINUX_AMD64_SHA256"
      ;;
    aarch64|arm64)
      arch="arm64"
      expected_sha="$MCP_TOKEN_LINUX_ARM64_SHA256"
      ;;
    *)
      echo "ERROR: unsupported architecture: $(uname -m)" >&2
      return 1
      ;;
  esac

  local asset="mcp-token-linux-${arch}"
  local tag="mcp-token/v1.1.1"
  local encoded_tag="${tag//\//%2F}"
  local url="https://github.com/masuda-masuo/mcp-launcher/releases/download/${encoded_tag}/${asset}"

  echo "==> Downloading ${asset} ..." >&2

  local token="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
  if command -v curl >/dev/null 2>&1; then
    if [ -n "$token" ]; then
      curl -fsSL -H "Authorization: Bearer ${token}" -o "$dest" "$url" || { echo "ERROR: download failed" >&2; return 1; }
    else
      curl -fsSL -o "$dest" "$url" || { echo "ERROR: download failed" >&2; return 1; }
    fi
  elif command -v wget >/dev/null 2>&1; then
    if [ -n "$token" ]; then
      wget -q --header="Authorization: Bearer ${token}" -O "$dest" "$url" || { echo "ERROR: download failed" >&2; return 1; }
    else
      wget -q -O "$dest" "$url" || { echo "ERROR: download failed" >&2; return 1; }
    fi
  else
    echo "ERROR: neither curl nor wget found" >&2
    return 1
  fi

  # verify checksum
  local actual_sha
  actual_sha=$(sha256sum "$dest" | awk '{print $1}')
  if [ "$actual_sha" != "$expected_sha" ]; then
    echo "ERROR: sha256 mismatch for ${asset}" >&2
    echo "  expected: ${expected_sha}" >&2
    echo "  actual:   ${actual_sha}" >&2
    rm -f "$dest"
    return 1
  fi

  chmod +x "$dest"
  echo "==> mcp-token cached at ${dest}" >&2
}

# --- resolve and use ---
MCP_TOKEN_EXE=$(resolve_mcp_token) || exit 1

refresh() {
  "$MCP_TOKEN_EXE" github > runtime/github-token.tmp
  mv -f runtime/github-token.tmp runtime/github-token
}

if [ "${1:-}" = "--loop" ]; then
  while true; do
    refresh || echo "refresh-token: mint failed; keeping previous token" >&2
    sleep 300
  done
else
  refresh
fi
