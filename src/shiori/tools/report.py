from __future__ import annotations

import os
from typing import Any

from ..pipeline import _conn, _ensure_phase1, settings
from ..report import (
    _REPORT_TEMPLATES,
    _report_api_reference,
    _report_module_tree,
    _report_symbol_index,
    _stats_data,
    _stats_to_markdown,
)
from .common import _resolve_repo, _resolve_repos  # noqa: F401 — re-export for tests
from .registry import mcp


@mcp.tool(name="shiori_report")
def report(
    template: str,
    repo: str | None = None,
    path: str | None = None,
    kind: str | None = None,
    public_only: bool = False,
    max_results: int = 500,
    prog_lang: str | None = None,
    max_chars: int = 50000,
) -> dict[str, Any]:
    """Generate a structured report (stats, module_tree, symbol_index, api_reference).

    Data sources: clone on disk (_ensure_phase1 refreshes it first; on refresh failure,
    serves stale clone and reports stale=True with refresh_error); api_reference
    template additionally reads the search index for cross-linking.

    template: one of the registered report templates (see error message for the list).
    """
    if template not in _REPORT_TEMPLATES:
        raise ValueError(
            f"Unknown template: '{template}'. "
            f"Valid templates: {', '.join(sorted(_REPORT_TEMPLATES))}"
        )

    target = _resolve_repo(repo)
    outcome = _ensure_phase1(target)
    base = os.path.realpath(settings.repo_dir(target))

    if not os.path.isdir(base):
        raise FileNotFoundError(
            f"Clone for {target} does not exist. Run python -m shiori ingest first."
        )

    if path:
        resolved = os.path.realpath(os.path.join(base, path))
        if not resolved.startswith(base + os.sep):
            raise ValueError("path must be inside the repository")
        target_path = resolved
    else:
        target_path = base

    out_dict: dict[str, Any]
    if template == "stats":
        data = _stats_data(target_path)
        markdown = _stats_to_markdown(data)
        out_dict = {
            "repo": target,
            "template": template,
            "markdown": markdown,
            "data": data,
        }
    elif template == "module_tree":
        result = _report_module_tree(
            target_path=target_path,
            base=base,
            max_nodes=max_results,
        )
        out_dict = {
            "repo": target,
            "template": template,
            "markdown": result["markdown"],
            "truncated": result["truncated"],
        }
    elif template == "symbol_index":
        result = _report_symbol_index(
            target_path=target_path,
            base=base,
            kind=kind,
            public_only=public_only,
            max_results=max_results,
        )
        markdown = result["markdown"]
        truncated = result["truncated"]
        out_dict = {
            "repo": target,
            "template": template,
            "markdown": markdown,
            "truncated": truncated,
            "data": result.get("data"),
        }
    elif template == "api_reference":
        result = _report_api_reference(
            base=base,
            target_repo=target,
            path_prefix=path,
            prog_lang=prog_lang,
            max_chars=max_chars,
            conn_factory=_conn,
        )
        out_dict = {
            "repo": target,
            "template": template,
            "markdown": result["markdown"],
            "truncated": result["truncated"],
        }
    else:
        raise AssertionError("Unreachable template code path")

    if getattr(outcome, "status", None) == "failed":
        out_dict["stale"] = True
        out_dict["refresh_error"] = outcome.error
    return out_dict
