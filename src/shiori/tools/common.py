from __future__ import annotations

import contextlib
import logging
import subprocess
import threading
from typing import Any, Iterator

import httpx

from ..api_utils import _GitHubAuth
from ..github_auth import build_token_provider
from ..pipeline import settings

log = logging.getLogger(__name__)

# Cached token provider (built once, reused across _github_client calls)
_token_provider: Any | None = None
_token_provider_lock = threading.Lock()


def _get_token_provider():
    global _token_provider
    with _token_provider_lock:
        if _token_provider is None:
            _token_provider = build_token_provider(settings)
    return _token_provider


@contextlib.contextmanager
def _github_client() -> Iterator[httpx.Client]:
    provider = _get_token_provider()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(headers=headers, auth=_GitHubAuth(provider), timeout=30.0, follow_redirects=True) as client:
        yield client


def _infer_repo_from_cwd() -> str | None:
    """Infer repo from git remote of current working directory."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],  # noqa: S607
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        remote_url = result.stdout.strip()
        if "github.com" not in remote_url:
            return None
        path_part = remote_url.split("github.com")[-1].lstrip("/:")
        candidate = path_part.replace(".git", "").strip()
        if candidate.count("/") == 1:
            return candidate if candidate in settings.repos else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def _validate_repo_name(repo: str) -> str:
    """Validate/resolve an explicit ``repo`` argument (issue #189).

    Before this, any non-empty ``repo`` string was passed through
    unchanged, so an unresolvable repo name and a resolvable-but-not-yet-
    indexed repo both surfaced downstream as the *same* "not indexed"
    error -- which points the caller at a useless ``ingest`` retry when
    the real problem is the argument itself.  This separates the two:

    - Exact ``"owner/name"`` match in ``settings.repos`` -> returned as-is.
    - A short name (no ``/``) that uniquely matches one configured repo's
      ``name`` component -> resolved to the full ``"owner/name"`` form
      (e.g. ``"shiori"`` -> ``"masuda-masuo/shiori"``).
    - A short name matching more than one configured repo -> ``ValueError``
      listing the ambiguous candidates.
    - Anything else (unresolvable full name or short name) -> ``ValueError``
      with the full indexed-repo list, so callers can tell "unknown repo"
      apart from "known repo, not indexed yet".

    When ``SHIORI_REPOS`` is unset (``settings.repos`` empty) there is
    nothing configured to validate against, so *repo* is returned
    unchanged (matches pre-#189 behavior for that case).
    """
    if not settings.repos:
        return repo
    if repo in settings.repos:
        return repo
    if "/" not in repo:
        matches = [r for r in settings.repos if r.split("/", 1)[-1] == repo]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(
                f'ambiguous repo "{repo}". Candidates: {", ".join(matches)}. '
                'Specify the full "owner/repo" form.'
            )
    raise ValueError(
        f'unknown repo "{repo}". Indexed repos: {", ".join(settings.repos)}'
    )


def _resolve_repo(repo: str | None) -> str:
    if repo:
        return _validate_repo_name(repo)
    if not settings.repos:
        raise ValueError("SHIORI_REPOS not set")
    inferred = _infer_repo_from_cwd()
    if inferred:
        return inferred
    log.info(
        "repo not specified and could not be inferred from cwd; "
        "falling back to %s (configured: %s)",
        settings.repos[0],
        ", ".join(settings.repos),
    )
    return settings.repos[0]


def _resolve_repo_filter(repo: str | None) -> str | None:
    """Resolve an optional ``repo`` *search filter* (issue #189).

    Unlike :func:`_resolve_repo`, ``None`` here means "no filter -- search
    across every configured repo", not "fall back to the default repo",
    so ``None`` passes through unchanged.  A given value is still
    validated / short-name-resolved via :func:`_validate_repo_name`.
    """
    if repo is None:
        return None
    return _validate_repo_name(repo)


def _resolve_repos(repo: str | None) -> list[str]:
    """Resolve repo parameter to a list of target repos.

    repo="*" returns all configured repos (cross-repo search).
    repo="owner/name" returns that single repo.
    repo=None returns the default single repo via _resolve_repo (backward compat).
    """
    if repo == "*":
        if not settings.repos:
            raise ValueError("SHIORI_REPOS not set")
        return list(settings.repos)
    return [_resolve_repo(repo)]


def _make_filters(
    source_type: str | None,
    language: str | None,
    state: str | None,
    repo: str | None,
    path_prefix: str | None,
    updated_after: str | None,
    prog_lang: str | None = None,
    kind: str | None = None,
    labels: list[str] | None = None,
) -> dict:
    return {
        "source_type": source_type,
        "language": language,
        "state": state,
        "repo": repo,
        "path_prefix": path_prefix,
        "updated_after": updated_after,
        "prog_lang": prog_lang,
        "kind": kind,
        "labels": labels,
    }
