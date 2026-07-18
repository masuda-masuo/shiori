from __future__ import annotations

import os
from typing import Any

from .registry import mcp
from .common import _resolve_repo
from ..pipeline import _conn, _ensure_phase1, settings
from ..walk_utils import _match_extension, _walk_code_files


# Valid values for list_tree source_type
_VALID_SOURCE_TYPES = {"doc", "code"}


@mcp.tool(name="shiori_list_tree")
def list_tree(
    path: str | None = None,
    source_type: str | None = None,
    extension: str | None = None,
    repo: str | None = None,
) -> list[dict[str, Any]]:
    """List indexed doc/code file paths. Filter by path/source_type/extension.
    Understand repo structure and locate files.
    repo: "owner/name", or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit for the default configured repo."""
    # Validation
    if source_type is not None and source_type not in _VALID_SOURCE_TYPES:
        raise ValueError(
            f"Invalid source_type: '{source_type}'."
            f"Valid values: {', '.join(sorted(_VALID_SOURCE_TYPES))}"
        )

    target = _resolve_repo(repo)
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1. Indexed documents (doc_files table)
    if source_type is None or source_type == "doc":
        with _conn() as conn, conn.cursor() as cur:
            if path:
                prefix = path.rstrip("/")
                cur.execute(
                    "SELECT path FROM doc_files"
                    " WHERE repo = %s AND (path = %s OR path LIKE %s)"
                    " ORDER BY path",
                    (target, prefix, prefix + "/%"),
                )
            else:
                cur.execute(
                    "SELECT path FROM doc_files WHERE repo = %s ORDER BY path",
                    (target,),
                )
            for r in cur.fetchall():
                p = r[0]
                if extension and not _match_extension(p, extension):
                    continue
                if p not in seen:
                    seen.add(p)
                    entries.append({"path": p, "source": "doc"})

    # 2. Code files (clone filesystem)
    if source_type is None or source_type == "code":
        _ensure_phase1(target)  # Phase 1: ensure clone is fresh (#236)
        base = os.path.realpath(settings.repo_dir(target))
        prefix = path.rstrip("/") if path else ""
        code_paths = _walk_code_files(base, prefix, extension=extension)
        # No pre-sort needed on code side; final sort handles it
        for p in code_paths:
            if p not in seen:
                seen.add(p)
                entries.append({"path": p, "source": "code"})

    # Sort by path (when doc and code are interleaved)
    entries.sort(key=lambda e: e["path"])
    return entries
