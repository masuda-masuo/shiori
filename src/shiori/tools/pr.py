from __future__ import annotations

import contextlib
import os
from typing import Any

import httpx

from .registry import mcp
from .common import _github_client, _resolve_repo
from ..pipeline import settings
from ..github_auth import build_token_provider
from ..github_sync import (
    API,
    _api_pages,
    _clean_text,
    _git,
    _git_delete_ref,
    _git_fetch_ref,
    _is_bot,
)


@mcp.tool(name="shiori_pr_changes")
def pr_changes(
    number: int,
    repo: str | None = None,
    include_diff: bool = False,
) -> dict[str, Any]:
    """PR change file map computed from git clone (issue #259).
    Uses git diff --name-status + --numstat to build the file list.
    blob_url is omitted because it cannot be computed from git alone.
    repo: "owner/name", or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit for the default configured repo."""
    target = _resolve_repo(repo)
    git_dir = os.path.realpath(settings.repo_dir(target))
    if not os.path.isdir(os.path.join(git_dir, ".git")):
        raise FileNotFoundError(
            f"Clone for {target} does not exist. Run python -m shiori ingest first."
        )

    provider = build_token_provider(settings)
    tmp_ref = None
    tmp_base = None
    try:
        tmp_ref = _git_fetch_ref(
            f"pull/{number}/head", cwd=git_dir, provider=provider,
        )
        head_sha = _git(["rev-parse", tmp_ref], cwd=git_dir)

        tmp_base = _git_fetch_ref(
            "HEAD", cwd=git_dir, provider=provider,
        )
        base_sha = _git(["rev-parse", tmp_base], cwd=git_dir)

        name_status_text = _git(
            ["diff", "--name-status", f"{tmp_base}..{tmp_ref}"], cwd=git_dir,
        )
        numstat_text = _git(
            ["diff", "--numstat", f"{tmp_base}..{tmp_ref}"], cwd=git_dir,
        )

        numstat_map: dict[str, tuple[int, int]] = {}
        for line in numstat_text.split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                try:
                    add = int(parts[0])
                    delete = int(parts[1])
                except ValueError:
                    add = 0
                    delete = 0
                numstat_map[parts[-1]] = (add, delete)

        files: list[dict[str, Any]] = []
        for line in name_status_text.split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            status = parts[0]
            path = parts[-1]
            add, delete = numstat_map.get(path, (0, 0))
            files.append({
                "path": path,
                "status": status,
                "additions": add,
                "deletions": delete,
                "changes": add + delete,
            })

        result: dict[str, Any] = {
            "repo": target,
            "number": number,
            "head_sha": head_sha,
            "base_sha": base_sha,
            "files": files,
        }
        if include_diff:
            diff_text, stat_text = _compute_pr_diff(
                number, target, base_sha=None, path=None,
                tmp_ref=tmp_ref, tmp_base=tmp_base,
            )
            result["diff"] = diff_text
            if stat_text:
                result["stats"] = stat_text
        return result
    finally:
        with contextlib.suppress(RuntimeError):
            if tmp_ref:
                _git_delete_ref(tmp_ref, cwd=git_dir)
        with contextlib.suppress(RuntimeError):
            if tmp_base:
                _git_delete_ref(tmp_base, cwd=git_dir)


def _compute_pr_diff(
    number: int,
    target: str,
    base_sha: str | None,
    path: str | None = None,
    *,
    tmp_ref: str | None = None,
    tmp_base: str | None = None,
) -> tuple[str, str]:
    """Fetch PR head and compute unified diff + stat (issue #96, #259).

    When base_sha=None, diffs against remote HEAD (default branch).
    When tmp_ref / tmp_base are provided, skips fetch (avoids duplicate
    fetch when the caller has already fetched).
    Returns (diff_text, stat_text). Raises FileNotFoundError if clone
    is missing.
    """
    git_dir = os.path.realpath(settings.repo_dir(target))
    if not os.path.isdir(os.path.join(git_dir, ".git")):
        raise FileNotFoundError(
            f"Clone for {target} does not exist. Run python -m shiori ingest first."
        )

    ref = f"pull/{number}/head"
    owned_ref: str | None = None
    owned_base: str | None = None
    try:
        if tmp_ref:
            diff_head = tmp_ref
        else:
            provider = build_token_provider(settings)
            owned_ref = _git_fetch_ref(ref, cwd=git_dir, provider=provider)
            diff_head = owned_ref

        if tmp_base:
            diff_base = tmp_base
        elif base_sha:
            provider = build_token_provider(settings)
            owned_base = _git_fetch_ref(
                base_sha, cwd=git_dir, provider=provider,
            )
            diff_base = owned_base
        else:
            provider = build_token_provider(settings)
            owned_base = _git_fetch_ref(
                "HEAD", cwd=git_dir, provider=provider,
            )
            diff_base = owned_base

        args = ["diff", f"{diff_base}..{diff_head}", "--unified=3"]
        if path:
            args.extend(["--", path])
        diff_text = _git(args, cwd=git_dir)
        stat_text = _git(
            ["diff", f"{diff_base}..{diff_head}", "--stat"], cwd=git_dir
        )
        return diff_text, stat_text.strip() if stat_text else ""
    finally:
        with contextlib.suppress(RuntimeError):
            if owned_ref:
                _git_delete_ref(owned_ref, cwd=git_dir)
        with contextlib.suppress(RuntimeError):
            if owned_base:
                _git_delete_ref(owned_base, cwd=git_dir)


@mcp.tool(name="shiori_pr_diff")
def pr_diff(
    number: int,
    path: str | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """Return PR diff (issue #96).

    Computes the unified diff between PR head and base using git diff.
    Returns per-file diffs. When path is given, returns diff for that file only.

    number: PR number
    path: File path to scope the diff (omit for all files)
    repo: "owner/name", or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit for the default configured repo.
    """
    target = _resolve_repo(repo)
    diff_text, stat_text = _compute_pr_diff(number, target, base_sha=None, path=path)
    return {
        "repo": target,
        "number": number,
        "diff": diff_text,
        "stats": stat_text,
    }


@mcp.tool(name="shiori_pr_review_comments")
def pr_review_comments(number: int, repo: str | None = None) -> dict[str, Any]:
    """Return PR review comments (issue #96).

    Fetches from GitHub Pull Request Review Comments API directly.
    Includes file path, line number, body, author, and timestamps.
    Useful for reviewing comment history and understanding other reviewers' feedback.

    repo: "owner/name", or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit for the default configured repo.
    """
    target = _resolve_repo(repo)
    with _github_client() as client:
        try:
            rows = _api_pages(
                client,
                f"{API}/repos/{target}/pulls/{number}/comments",
                {"per_page": 100},
            )
        except httpx.HTTPError as exc:
            raise ValueError(f"PR #{number} not found on GitHub: {exc}")

    comments = []
    for r in rows:
        line = r.get("line") or r.get("original_line")
        comments.append({
            "comment_id": r["id"],
            "author": (r.get("user") or {}).get("login"),
            "is_bot": _is_bot(r.get("user")),
            "path": r.get("path"),
            "line": line,
            "body": _clean_text(r.get("body") or ""),
            "url": r.get("html_url"),
            "created_at": r.get("created_at"),
        })

    return {
        "repo": target,
        "number": number,
        "count": len(comments),
        "comments": comments,
    }
