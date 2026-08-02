from __future__ import annotations

import os
import subprocess
from typing import Any

from .registry import mcp
from .common import _resolve_repos
from ..pipeline import _ensure_phase1, settings


@mcp.tool(name="shiori_grep")
def grep_search(
    pattern: str,
    repo: str | None = None,
    path: str | None = None,
    regex: bool = True,
    ignore_case: bool = True,
    max_results: int = 200,
) -> dict[str, Any]:
    """Grep clone files with ripgrep. Returns line-level matches.

    Data sources: clone on disk (_ensure_phase1 refreshes each repo before grepping).

    Each match includes a "repo" field identifying the source repository.

    pattern: search pattern (regex or fixed string)
    path: optional file/subdir path within repo to scope the search
    regex: True (default) for regex search, False for fixed-string search.
          Patterns containing literal ``[...]`` (character classes) should use
          ``regex=False`` to avoid silent misinterpretation.
    ignore_case: case-insensitive search (default True)
    max_results: maximum matches to return (default 200)
    """
    targets = _resolve_repos(repo)

    # Phase 1: refresh clones inline before searching (#236)
    for target in targets:
        _ensure_phase1(target)

    all_matches: list[dict[str, Any]] = []
    total = 0
    skipped_repos: list[str] = []

    for target in targets:
        base = os.path.realpath(settings.repo_dir(target))

        if not os.path.isdir(base):
            skipped_repos.append(target)
            continue

        if path:
            search_path = os.path.join(base, path)
            resolved = os.path.realpath(search_path)
            if not resolved.startswith(os.path.realpath(base) + os.sep) and resolved != os.path.realpath(base):
                raise ValueError("path must be inside the repository")
        else:
            resolved = base

        cmd = ["rg", "-n", "--no-heading", "--color", "never", "--with-filename"]
        if ignore_case:
            cmd.append("-i")
        if not regex:
            cmd.append("--fixed-strings")
        cmd.extend(["-e", pattern])
        cmd.append(resolved)

        try:
            rg_result = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError:
            raise RuntimeError("ripgrep (rg) is not installed in this container")

        if rg_result.returncode not in (0, 1):
            msg = f"rg failed (exit {rg_result.returncode}): {rg_result.stderr.strip()}"
            if rg_result.returncode == 2:
                msg += " (regex parse error. If you intended a literal search, retry with regex=False)"
            raise RuntimeError(msg)

        if rg_result.stdout:
            for line in rg_result.stdout.splitlines():
                if not line.strip() or line.startswith("--"):
                    continue
                parts = line.split(":", 2)
                if len(parts) >= 2:
                    try:
                        line_num = int(parts[1])
                        total += 1
                        text = parts[2] if len(parts) > 2 else ""
                        if len(all_matches) < max_results:
                            rel_path = parts[0]
                            if rel_path.startswith(base + "/"):
                                rel_path = rel_path[len(base) + 1:]
                            all_matches.append({
                                "repo": target,
                                "path": rel_path,
                                "line": line_num,
                                "text": text,
                            })
                        continue
                    except ValueError:
                        pass

    truncated = total > max_results

    return {
        "pattern": pattern,
        "path": path or "",
        "total_matches": total,
        "truncated": truncated,
        "matches": all_matches,
        "skipped_repos": skipped_repos,
    }
