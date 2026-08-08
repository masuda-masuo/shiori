from __future__ import annotations

import os
from typing import Any

import httpx

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
from ..pipeline import _ensure_phase1, settings
from .common import _github_client, _resolve_repo
from .registry import mcp

_LARGE_FILE_THRESHOLD = 500


@mcp.tool(name="shiori_read_file")
def read_file(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """Read full file (or range) from clone (not index).
    Data sources: clone on disk (_ensure_phase1 refreshes it before reading)."""
    target = _resolve_repo(repo)
    _ensure_phase1(target)
    base = os.path.realpath(settings.repo_dir(target))
    full = os.path.realpath(os.path.join(base, path))
    if not full.startswith(base + os.sep):
        raise ValueError("cannot read path outside repository")
    if not os.path.isfile(full):
        raise FileNotFoundError(f"{path} does not exist in clone (sync may be needed)")
    with open(full, encoding="utf-8", errors="replace") as fp:
        lines = fp.read().splitlines()
    total = len(lines)
    s = max((start_line or 1) - 1, 0)
    e = min(end_line or total, total)
    body = "\n".join(lines[s:e])

    hints: list[str] = []
    if end_line is None and total > _LARGE_FILE_THRESHOLD:
        hints.append(
            f"File is large ({total} lines). "
            "Use start_line/end_line for range-based reading."
        )

    result: dict[str, Any] = {
        "repo": target,
        "path": path,
        "start_line": s + 1,
        "end_line": e,
        "total_lines": total,
        "content": body,
    }
    if hints:
        result["hints"] = hints
    return result


def _read_issue_single(target: str, number: int, exclude_noise_bots: bool) -> dict[str, Any]:
    """Fetch single issue/PR via GitHub API.
    Raises ValueError if not found on GitHub.
    """
    with _github_client() as client:
        try:
            resp = client.get(f"{API}/repos/{target}/issues/{number}")
            resp.raise_for_status()
            issue = resp.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError(f"#{number} not found on GitHub")
            raise

        is_pr = "pull_request" in issue
        kind = "pr" if is_pr else "issue"
        title = issue.get("title") or ""
        state = issue.get("state")
        url = issue.get("html_url")

        items: list[dict[str, Any]] = [{
            "author": (issue.get("user") or {}).get("login"),
            "is_bot": _is_bot(issue.get("user")),
            "kind": kind,
            "state": state,
            "created_at": issue.get("created_at"),
            "body": _clean_text(issue.get("body") or ""),
            "url": url,
        }]

        # Issue/PR comments
        comments = _api_pages(
            client,
            f"{API}/repos/{target}/issues/{number}/comments",
            {"per_page": 100},
        )
        for c in comments:
            items.append({
                "author": (c.get("user") or {}).get("login"),
                "is_bot": _is_bot(c.get("user")),
                "kind": "comment",
                "state": state,
                "created_at": c.get("created_at"),
                "body": _clean_text(c.get("body") or ""),
                "url": c.get("html_url"),
            })

        if is_pr:
            # PR review submissions
            try:
                reviews = _api_pages(
                    client,
                    f"{API}/repos/{target}/pulls/{number}/reviews",
                    {"per_page": 100},
                )
                for r in reviews:
                    rbody = _clean_text(r.get("body") or "")
                    if not rbody:
                        continue
                    items.append({
                        "author": (r.get("user") or {}).get("login"),
                        "is_bot": _is_bot(r.get("user")),
                        "kind": "pr_review",
                        "state": r.get("state"),
                        "created_at": r.get("submitted_at"),
                        "body": rbody,
                        "url": r.get("html_url"),
                    })
            except httpx.HTTPError:
                pass

            # PR inline review comments
            try:
                review_comments = _api_pages(
                    client,
                    f"{API}/repos/{target}/pulls/{number}/comments",
                    {"per_page": 100},
                )
                for rc in review_comments:
                    rc_line = rc.get("line") or rc.get("original_line")
                    diff_hunk = rc.get("diff_hunk")
                    rc_body = _clean_text(rc.get("body") or "")
                    if diff_hunk:
                        rc_body = f"{rc_body}\n\n```diff\n{_clean_text(diff_hunk)}\n```"
                    item: dict[str, Any] = {
                        "author": (rc.get("user") or {}).get("login"),
                        "is_bot": _is_bot(rc.get("user")),
                        "kind": "pr_review_comment",
                        "state": state,
                        "created_at": rc.get("created_at"),
                        "body": rc_body,
                        "url": rc.get("html_url"),
                    }
                    if rc.get("path"):
                        item["path"] = rc["path"]
                        item["line"] = rc_line
                    items.append(item)
            except httpx.HTTPError:
                pass

    # Sort: body first, then by created_at
    body_item = items[0]
    rest = sorted(items[1:], key=lambda x: x.get("created_at") or "")
    items = [body_item] + rest

    # Exclude bots outside the allowlist (issue #44)
    if exclude_noise_bots:
        allowlist = settings.index_bot_logins
        items = [
            item for item in items
            if not item["is_bot"] or (item["author"] and item["author"].lower() in allowlist)
        ]
        if not items:
            raise ValueError(f"#{number}: all items are bots outside the allowlist")

    return {
        "repo": target,
        "number": number,
        "kind": kind,
        "title": title,
        "state": state,
        "url": url,
        "labels": [label["name"] for label in issue.get("labels", [])],
        "items": items,
    }


@mcp.tool(name="shiori_read_issue")
def read_issue(
    number: int | None = None,
    repo: str | None = None,
    exclude_noise_bots: bool = False,
    numbers: list[int] | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Fetch full issue/PR thread chronologically (body + comments + review).
    Data sources: GitHub API (live); no clone/index read. Does not call _ensure_phase1.
    Bot comments included (identifiable via is_bot).
    Each item has a state field: for kind='pr_review' it is the review
    submission state (APPROVED/COMMENTED/CHANGES_REQUESTED); for other
    kinds it is the overall issue state (open/closed).
    Items have a kind field: 'issue', 'pr', 'comment', 'pr_review', or
    'pr_review_comment'.
    An unresolvable repo raises immediately with the indexed-repo list."""
    if number is not None and numbers is not None:
        raise ValueError("number and numbers cannot be specified together")
    target = _resolve_repo(repo)
    if numbers is not None:
        if len(numbers) > 50:
            raise ValueError(f"numbers supports up to 50 items ({len(numbers)} specified)")
        results: list[dict[str, Any]] = []
        for n in numbers:
            try:
                result = _read_issue_single(target, n, exclude_noise_bots)
                result["status"] = "ok"
                results.append(result)
            except ValueError as e:
                results.append({
                    "repo": target,
                    "number": n,
                    "status": "error",
                    "error": str(e),
                })
        return results
    if number is None:
        raise ValueError("specify number or numbers")
    return _read_issue_single(target, number, exclude_noise_bots)


@mcp.tool(name="shiori_read_pr_file")
def read_pr_file(
    number: int,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """Read PR head file content (or range).
    Data sources: own git fetch of the PR head ref against the clone; not _ensure_phase1."""
    target = _resolve_repo(repo)
    base = os.path.realpath(settings.repo_dir(target))

    if not os.path.isdir(os.path.join(base, ".git")):
        raise FileNotFoundError(
            f"Clone for {target} does not exist. Run python -m shiori ingest first."
        )

    ref = f"pull/{number}/head"
    tmp_ref = None
    try:
        provider = build_token_provider(settings)
        tmp_ref = _git_fetch_ref(ref, cwd=base, provider=provider)

        # Get file content via git show
        try:
            content = _git(["show", f"{tmp_ref}:{path}"], cwd=base)
        except RuntimeError as exc:
            raise FileNotFoundError(
                f"PR #{number}: {path} not found: {exc}"
            )

        lines = content.splitlines()
        total = len(lines)
        s = max((start_line or 1) - 1, 0)
        e = min(end_line or total, total)
        body = "\n".join(lines[s:e])

        hints: list[str] = []
        if end_line is None and total > _LARGE_FILE_THRESHOLD:
            hints.append(
                f"File is large ({total} lines). "
                "Use start_line/end_line for range-based reading."
            )

        result: dict[str, Any] = {
            "repo": target,
            "number": number,
            "path": path,
            "start_line": s + 1,
            "end_line": e,
            "total_lines": total,
            "content": body,
        }
        if hints:
            result["hints"] = hints
        return result
    finally:
        if tmp_ref:
            _git_delete_ref(tmp_ref, cwd=base)
