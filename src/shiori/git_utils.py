from __future__ import annotations

import logging
import os
import re
import subprocess
import uuid

from .github_auth import TokenProvider

log = logging.getLogger(__name__)


def _redact(text: str) -> str:
    """Mask auth credentials embedded in URLs (x-access-token:...@ etc.)."""
    return re.sub(r"https://[^@\s/]+@", "https://", text)


def _git(args: list[str], cwd: str | None = None) -> str:
    # When cwd is specified, explicitly set safe.directory for security.
    # app/ingest (root) runs in the container where /data/repos is mounted;
    # prevents git dubious ownership errors (issue #48).
    cmd = ["git"]
    if cwd:
        cmd += ["-c", f"safe.directory={cwd}"]
    cmd += args
    out = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if out.returncode != 0:
        err = _redact(out.stderr.strip())
        hint = ""
        if "Authentication failed" in err or "could not read Username" in err:
            hint = ("Private repos require GITHUB_TOKEN."
                    "For public repos, verify the repository name.")
        raise RuntimeError(
            f"git {args[0]} failed (exit {out.returncode}): {err}{hint}"
        )
    return out.stdout.strip()


def _auth_args(provider: TokenProvider) -> list[str]:
    """Return git auth header as `-c http.extraHeader=...` args.
    Token clipped from clone URL.

    NOTE: http.extraHeader is not used in practice because it fails in some
    git versions. Use _authed_url() instead for clone/fetch operations.
    Kept for compatibility.
    """
    return []


def _authed_url(remote: str, provider: TokenProvider) -> str:
    """Return remote URL with token embedded (x-access-token scheme).
    Falls back to the original URL if no token is available.
    The token is redacted from logs by _redact().
    """
    token = provider.get_token()
    if not token:
        return remote
    return remote.replace("https://", f"https://x-access-token:{token}@", 1)


def _git_fetch_ref(
    ref: str,
    cwd: str | None = None,
    provider: TokenProvider | None = None,
    tmp_ref: str | None = None,
) -> str:
    """Shallow-fetch a ref and return a temp ref name (issue #81).
    tmp_ref=None skips fetch. Returns SHA of fetched ref.
    """
    resolved = tmp_ref or f"refs/shiori/tmp-{uuid.uuid4().hex}"
    if provider:
        remote = _git(["remote", "get-url", "origin"], cwd=cwd)
        authed = _authed_url(remote, provider)
        _git(["remote", "set-url", "origin", authed], cwd=cwd)
        try:
            _git(["fetch", "origin", f"{ref}:{resolved}", "--depth=1"], cwd=cwd)
        finally:
            _git(["remote", "set-url", "origin", remote], cwd=cwd)
    else:
        _git(["fetch", "origin", f"{ref}:{resolved}", "--depth=1"], cwd=cwd)
    return resolved


def _git_delete_ref(tmp_ref: str, cwd: str | None = None) -> None:
    """Delete a temporary ref (issue #81). No-op if not found."""
    try:
        _git(["update-ref", "-d", tmp_ref], cwd=cwd)
    except RuntimeError:
        pass  # Already deleted etc.
