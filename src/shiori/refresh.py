"""Clone refresh (Phase 1: cheap). git fetch + reset --hard origin/main only.

Extracted from github_sync.py sync_docs for pull-type on-demand sync (#236).
Phase 1 is cheap (sub-second), idempotent, and must not depend on Phase 2.
"""

from __future__ import annotations

import logging
import os

from .config import Settings
from .github_auth import TokenProvider

log = logging.getLogger(__name__)


def refresh_clone(
    repo: str,
    provider: TokenProvider,
    settings: Settings,
) -> str:
    """Ensure clone exists and HEAD is at origin/main. Returns HEAD SHA.

    This is the "cheap path" (Phase 1): git fetch + reset --hard origin/HEAD only.
    No embedding, no DB writes (except optionally recording clone_head in
    repo_index_state).  Called inline by tools that read clone files directly
    (shiori_read_file, shiori_grep) and as a precondition for search tools
    (shiori_search, shiori_keyword_search) that read the index.
    """

    from .github_sync import _authed_url, _git

    repo_dir = settings.repo_dir(repo)
    remote = f"https://github.com/{repo}.git"
    authed_remote = _authed_url(remote, provider)

    if os.path.isdir(os.path.join(repo_dir, ".git")):
        _git(["remote", "set-url", "origin", authed_remote], cwd=repo_dir)
        try:
            _git(["fetch", "--depth=1", "origin"], cwd=repo_dir)
        finally:
            _git(["remote", "set-url", "origin", remote], cwd=repo_dir)
        _git(["reset", "--hard", "origin/HEAD"], cwd=repo_dir)
    else:
        os.makedirs(os.path.dirname(repo_dir), exist_ok=True)
        _git(["clone", "--depth=1", authed_remote, repo_dir])
        _git(["remote", "set-url", "origin", remote], cwd=repo_dir)

    head = _git(["rev-parse", "HEAD"], cwd=repo_dir)
    return head
