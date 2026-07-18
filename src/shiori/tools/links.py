from __future__ import annotations

import httpx
from typing import Any

from .registry import mcp
from .common import _resolve_repo
from ..pipeline import _github_client, _conn
from ..github_sync import API, _api_pages
from ..links import merge_outbound_refs
from .. import db


@mcp.tool(name="shiori_issue_links")
def issue_links(number: int, repo: str | None = None) -> dict[str, Any]:
    """Return issue/PR cross-references (inbound/outbound) (issue #97).

    Extracts #N references from body text and comments, classifying
    them as closes/duplicate/refs/mention. Includes target title and
    state. Inbound lists other issues/PRs that reference this issue.

    Useful for duplicate detection, epic construction, and regression tracking.

    repo: "owner/name", or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit for the default configured repo.
    """
    target = _resolve_repo(repo)

    with _github_client() as client:
        try:
            resp = client.get(f"{API}/repos/{target}/issues/{number}")
            resp.raise_for_status()
            issue = resp.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError(f"#{number} not found on GitHub")
            raise

        bodies = [{"body": issue.get("body") or ""}]
        try:
            comments = _api_pages(
                client,
                f"{API}/repos/{target}/issues/{number}/comments",
                {"per_page": 100},
            )
            for c in comments:
                if c.get("body"):
                    bodies.append({"body": c["body"]})
        except httpx.HTTPError:
            pass

    outbound_refs = merge_outbound_refs(bodies, number)

    outbound_nos = list(outbound_refs)
    with _conn() as conn:
        outbound_details = db.get_issues_by_numbers(conn, target, outbound_nos)
        inbound = db.find_inbound_refs(conn, target, number)

    outbound = []
    for n, ref in outbound_refs.items():
        detail = outbound_details.get(n, {})
        outbound.append({
            "issue_no": n,
            "type": ref["type"],
            "title": detail.get("title"),
            "state": detail.get("state"),
            "kind": detail.get("kind"),
            "url": detail.get("url"),
        })

    return {
        "repo": target,
        "number": number,
        "outbound": outbound,
        "inbound": inbound,
    }
