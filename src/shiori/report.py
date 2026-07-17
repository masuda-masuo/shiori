"""Report generation module (issue #279).

Extracted from mcp_server.py to separate report concerns
from MCP server infrastructure.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
from typing import Any

from . import db
from .config import Settings, load_settings

log = logging.getLogger(__name__)

settings: Settings = load_settings()


def _conn():
    return db.connect(settings)


_REPORT_TEMPLATES: dict[str, str] = {
    "stats": "Language statistics via tokei (files / code / comments / blanks).",
    "symbol_index": "Symbol index via universal-ctags (name / kind / visibility / location).",
    "module_tree": "Mermaid mindmap of repository structure (directory → file → class → function).",
    "api_reference": "API reference showing classes, functions and docstrings.",
}


def _run_ctags(target_path: str, base: str) -> list[dict[str, Any]]:
    """Run universal-ctags and return list of parsed symbol dicts.

    Shared helper used by symbol_index and module_tree (issue #155) templates.
    Paths in the result are relative to *base*.
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["ctags", "-R", "--output-format=json", "--fields=+naZ", "-f", "-", target_path],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "universal-ctags is not installed in this container. "
            "Add universal-ctags to the Dockerfile apt-get install line."
        )

    if result.returncode != 0:
        raise RuntimeError(
            f"ctags failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    symbols: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        path = entry.get("path", "")
        if path.startswith(base + "/"):
            path = path[len(base) + 1:]

        symbols.append({
            "name": entry.get("name", ""),
            "path": path,
            "line": entry.get("line", 0),
            "kind": entry.get("kind", ""),
            "access": entry.get("access", ""),
            "scope": entry.get("scope", ""),
            "scope_kind": entry.get("scopeKind", ""),
        })

    return symbols


def _extract_python_api_from_file(file_path: str, base_path: str) -> list[dict]:
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        tree = ast.parse(content, filename=file_path)
    except Exception:
        return []

    rel_path = os.path.relpath(file_path, base_path)

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.current_class = None
            self.docs = []

        def visit_ClassDef(self, node):
            doc = ast.get_docstring(node) or ""
            sig = f"class {node.name}"
            bases_names = []
            for b in node.bases:
                if isinstance(b, ast.Name):
                    bases_names.append(b.id)
                elif isinstance(b, ast.Attribute):
                    bases_names.append(b.attr)
            if bases_names:
                sig += f"({', '.join(bases_names)})"

            self.docs.append({
                "type": "class",
                "name": node.name,
                "signature": sig,
                "docstring": doc,
                "line": node.lineno,
                "parent": self.current_class
            })

            prev_class = self.current_class
            self.current_class = node.name
            self.generic_visit(node)
            self.current_class = prev_class

        def _visit_func(self, node, is_async=False):
            doc = ast.get_docstring(node) or ""
            args = []
            for arg in node.args.args:
                args.append(arg.arg)
            prefix = "async def" if is_async else "def"
            sig = f"{prefix} {node.name}({', '.join(args)})"

            self.docs.append({
                "type": "method" if self.current_class else "function",
                "name": node.name,
                "signature": sig,
                "docstring": doc,
                "line": node.lineno,
                "parent": self.current_class
            })
            self.generic_visit(node)

        def visit_FunctionDef(self, node):
            self._visit_func(node, is_async=False)

        def visit_AsyncFunctionDef(self, node):
            self._visit_func(node, is_async=True)

    visitor = Visitor()
    visitor.visit(tree)

    filtered = []
    for doc in visitor.docs:
        name = doc["name"]
        if name.startswith("_") and not (name.startswith("__") and name.endswith("__")):
            continue
        filtered.append({
            "path": rel_path,
            "line": doc["line"],
            "type": doc["type"],
            "name": name,
            "signature": doc["signature"],
            "docstring": doc["docstring"],
            "parent": doc["parent"]
        })

    return filtered


def _api_reference_data(
    target_repo: str,
    path_prefix: str | None = None,
    prog_lang: str | None = None,
) -> dict[str, Any]:
    """Fetch and structure API reference data from code chunks (issue #156).

    Returns dict with "entries" (list of dicts with path/line/end_line/
    content/prog_lang) and "columns" metadata.
    Module gap chunks are filtered out.  The structured data can be
    rendered to Markdown via _api_reference_to_markdown() or consumed
    directly by dashboards.
    """
    from .config import load_settings
    settings = load_settings()
    base = os.path.realpath(settings.repo_dir(target_repo))

    search_dir = os.path.join(base, path_prefix) if path_prefix else base
    py_files = []
    if os.path.isdir(search_dir):
        for root, _, files in os.walk(search_dir):
            for file in files:
                if file.endswith(".py"):
                    py_files.append(os.path.join(root, file))
    elif os.path.isfile(search_dir) and search_dir.endswith(".py"):
        py_files.append(search_dir)

    entries: list[dict[str, Any]] = []
    if py_files and (prog_lang is None or prog_lang.lower() == "python"):
        for py_file in sorted(py_files):
            entries.extend(_extract_python_api_from_file(py_file, base))

    if not entries:
        with _conn() as conn:
            chunks = db.get_code_chunks(
                conn,
                repo=target_repo,
                prog_lang=prog_lang,
                path_prefix=path_prefix,
            )

        for chunk in chunks:
            content = chunk["content"]
            first_line = content.split("\n")[0] if content else ""
            if first_line.startswith("[") and first_line.endswith("(module)"):
                continue
            entries.append({
                "path": chunk["path"],
                "line": chunk["line"],
                "end_line": chunk["end_line"],
                "type": "raw_chunk",
                "name": os.path.basename(chunk["path"]),
                "signature": f"Chunk L{chunk['line']}-L{chunk['end_line']}",
                "docstring": content,
                "prog_lang": chunk.get("prog_lang") or "",
                "parent": None,
            })

    return {
        "columns": ["path", "line", "type", "name", "signature", "docstring", "parent"],
        "entries": entries,
    }


def _api_reference_to_markdown(
    data: dict[str, Any],
    max_chars: int = 50000,
) -> dict[str, Any]:
    """Render structured API reference data as Markdown.

    Returns dict with "markdown" (str) and "truncated" (bool).
    """
    markdown_lines: list[str] = []
    current_length = 0
    truncated = False
    current_path = None

    for entry in data["entries"]:
        path = entry["path"]
        path_header = ""
        if path != current_path:
            path_header = f"## {path}\n\n"

        if entry["type"] == "raw_chunk":
            line_range = f"L{entry['line']}-L{entry['end_line']}"
            lang = entry["prog_lang"]
            chunk_md = f"{line_range}\n```{lang}\n{entry['docstring']}\n```\n\n"
            added_md = path_header + chunk_md
        else:
            docstring_fmt = ""
            if entry["docstring"]:
                # Indent docstring to look like blockquote
                docstring_lines = entry["docstring"].strip().split("\n")
                indented_doc = "\n".join(f"> {line}" for line in docstring_lines)
                docstring_fmt = f"{indented_doc}\n\n"

            if entry["type"] == "class":
                item_md = f"### class `{entry['signature']}`\n\n{docstring_fmt}"
            else:
                if entry["parent"]:
                    item_md = f"  - **`{entry['signature']}`**\n\n{docstring_fmt}"
                else:
                    item_md = f"### `{entry['signature']}`\n\n{docstring_fmt}"

            added_md = path_header + item_md

        if current_length + len(added_md) > max_chars:
            truncated = True
            break

        markdown_lines.append(added_md)
        current_length += len(added_md)
        current_path = path

    return {
        "markdown": "".join(markdown_lines).strip(),
        "truncated": truncated,
    }


def _report_api_reference(
    target_repo: str,
    path_prefix: str | None = None,
    prog_lang: str | None = None,
    max_chars: int = 50000,
) -> dict[str, Any]:
    """Generate API reference report (issue #156)."""
    data = _api_reference_data(
        target_repo, path_prefix=path_prefix, prog_lang=prog_lang,
    )
    return _api_reference_to_markdown(data, max_chars=max_chars)


def _stats_data(target_path: str) -> dict[str, Any]:
    """Run tokei and return structured statistics data.

    Returns dict with "rows" (list of dicts with language/files/code/comments/blanks),
    "total" (dict with files/code/comments/blanks), and "columns" metadata.
    The structured data can be consumed directly by dashboards or rendered
    to Markdown via _stats_to_markdown().
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["tokei", "--output", "json", target_path],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "tokei is not installed in this container. "
            "Add tokei to the Dockerfile apt-get install line."
        )

    if result.returncode != 0:
        raise RuntimeError(
            f"tokei failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    data = json.loads(result.stdout)

    sorted_langs = sorted(
        (lang for lang in data if lang != "Total"),
        key=lambda k: k.lower(),
    )

    rows: list[dict[str, Any]] = []
    total_files = 0

    for lang in sorted_langs:
        info = data[lang]
        reports = info.get("reports", [])
        # Simplify reports to only name and stats to keep payload small
        simplified_reports = []
        for r in reports:
            # path is relative to target_path
            rel_path = os.path.relpath(r["name"], target_path)
            stats = r.get("stats", {})
            simplified_reports.append({
                "name": rel_path,
                "code": stats.get("code", 0),
                "comments": stats.get("comments", 0),
                "blanks": stats.get("blanks", 0),
            })

        n_files = len(reports)
        code = info.get("code", 0)
        comments = info.get("comments", 0)
        blanks = info.get("blanks", 0)
        total_files += n_files
        rows.append({
            "language": lang,
            "files": n_files,
            "code": code,
            "comments": comments,
            "blanks": blanks,
            "reports": simplified_reports,
        })

    total_info = data.get("Total", {})
    return {
        "columns": ["language", "files", "code", "comments", "blanks"],
        "rows": rows,
        "total": {
            "files": total_files,
            "code": total_info.get("code", 0),
            "comments": total_info.get("comments", 0),
            "blanks": total_info.get("blanks", 0),
        },
    }


def _stats_to_markdown(stats: dict[str, Any]) -> str:
    """Render structured stats data as a Markdown table."""
    md_rows: list[str] = []
    for r in stats["rows"]:
        md_rows.append(
            f"| {r['language']} | {r['files']} | {r['code']} | {r['comments']} | {r['blanks']} |"
        )

    t = stats["total"]
    header = "| Language | Files | Code | Comments | Blanks |"
    sep = "| --- | --- | --- | --- | --- |"
    total_row = (
        f"| **Total** | **{t['files']}** | "
        f"**{t['code']}** | **{t['comments']}** | "
        f"**{t['blanks']}** |"
    )

    return "\n".join([header, sep] + md_rows + [total_row])


def _report_stats(target_path: str) -> str:
    """Run tokei and format output as a Markdown table."""
    return _stats_to_markdown(_stats_data(target_path))


def _symbol_index_data(
    target_path: str,
    base: str,
    kind: str | None = None,
    public_only: bool = False,
    max_results: int = 500,
) -> dict[str, Any]:
    """Run universal-ctags and return structured symbol data.

    Returns dict with "columns", "rows" (list of dicts with name/kind/
    access/path/line kept separate for downstream linking), "truncated",
    and "total".
    """
    symbols = _run_ctags(target_path, base)

    if kind:
        symbols = [s for s in symbols if s["kind"] == kind]

    if public_only:
        symbols = [
            s for s in symbols
            if s["access"] not in ("private", "protected")
        ]

    symbols.sort(key=lambda s: (s["path"], s["line"]))

    total = len(symbols)
    truncated = total > max_results
    shown = symbols[:max_results]

    rows: list[dict[str, Any]] = []
    for s in shown:
        rows.append({
            "name": s["name"],
            "kind": s["kind"],
            "access": s["access"] if s["access"] else "",
            "path": s["path"],
            "line": s["line"],
        })

    return {
        "columns": ["name", "kind", "access", "path", "line"],
        "rows": rows,
        "truncated": truncated,
        "total": total,
    }


def _symbol_index_to_markdown(data: dict[str, Any]) -> str:
    """Render structured symbol index data as a Markdown table."""
    parts: list[str] = [
        "| symbol | kind | visibility | location |",
        "| --- | --- | --- | --- |",
    ]
    for r in data["rows"]:
        parts.append(
            f"| {r['name']} | {r['kind']} | {r['access']} | {r['path']}:{r['line']} |"
        )
    if data["truncated"]:
        shown = len(data["rows"])
        parts.append("")
        parts.append(f"*Truncated: showing {shown} of {data['total']} symbols.*")

    return "\n".join(parts)


def _report_symbol_index(
    target_path: str,
    base: str,
    kind: str | None = None,
    public_only: bool = False,
    max_results: int = 500,
) -> dict[str, Any]:
    """Run universal-ctags and format output as a Markdown table.

    Returns dict with "markdown" (str) and "truncated" (bool).
    """
    data = _symbol_index_data(
        target_path, base, kind=kind, public_only=public_only,
        max_results=max_results,
    )
    return {
        "markdown": _symbol_index_to_markdown(data),
        "truncated": data["truncated"],
        "data": data,
    }


_MODULE_TREE_MAX_NODES = 300

_TREE_DIR = "d"
_TREE_FILE = "f"
_TREE_SYM = "s"


def _module_tree_data(
    target_path: str,
    base: str,
    max_nodes: int = _MODULE_TREE_MAX_NODES,
) -> dict[str, Any]:
    """Build a hierarchical tree of the repo structure from ctags data.

    Returns dict with "tree" (list of nested dicts with name/children/t),
    "root_name", "truncated", and "total_nodes".  The tree can be rendered
    to Mermaid via _module_tree_to_markdown() or consumed directly by
    dashboards for alternative visualisation.
    """
    symbols = _run_ctags(target_path, base)

    file_paths: set[str] = set()
    for s in symbols:
        file_paths.add(s["path"])

    all_dirs: set[str] = set()
    for fp in sorted(file_paths):
        d = os.path.dirname(fp)
        while d and d != ".":
            all_dirs.add(d)
            d = os.path.dirname(d)

    def _count_nodes(nodes: list[dict]) -> int:
        n = len(nodes)
        for x in nodes:
            n += _count_nodes(x.get("children", []))
        return n

    def _build_tree(dir_path: str) -> list[dict]:
        children: list[dict] = []
        for d in sorted(x for x in all_dirs if os.path.dirname(x) == dir_path):
            sub = _build_tree(d)
            children.append(dict(name=os.path.basename(d), children=sub, t=_TREE_DIR))
        for fp in sorted(x for x in file_paths if os.path.dirname(x) == dir_path):
            file_syms = [s for s in symbols if s["path"] == fp]
            sym_children = _build_symbol_children(file_syms)
            children.append(dict(name=os.path.basename(fp), children=sym_children, t=_TREE_FILE))
        return children

    def _build_symbol_children(file_syms: list[dict]) -> list[dict]:
        top = [s for s in file_syms if not s.get("scope")]
        children: list[dict] = []
        for s in top:
            if not s["name"]:
                continue
            subs = [x for x in file_syms if x.get("scope") == s["name"]]
            sub_c = []
            for ss in subs:
                if ss["name"]:
                    sub_c.append(dict(name=ss["name"], children=[], t=_TREE_SYM))
            children.append(dict(name=s["name"], children=sub_c, t=_TREE_SYM))
        return children

    tree = _build_tree("")
    total_nodes = _count_nodes(tree)
    truncated = total_nodes > max_nodes

    if truncated:
        def _strip_symbols(nodes: list[dict]) -> list[dict]:
            result: list[dict] = []
            for n in nodes:
                if n.get("t") == _TREE_SYM:
                    continue
                kids = _strip_symbols(n.get("children", []))
                result.append(dict(name=n["name"], children=kids, t=n.get("t")))
            return result
        tree = _strip_symbols(tree)
        total_nodes = _count_nodes(tree)

    root_name = os.path.basename(target_path.rstrip("/")) or os.path.basename(base)

    return {
        "tree": tree,
        "root_name": root_name,
        "truncated": truncated,
        "total_nodes": total_nodes,
    }


def _module_tree_to_markdown(data: dict[str, Any]) -> str:
    """Render hierarchical tree data as a Mermaid mindmap."""
    safe_root = data['root_name'].replace('"', '&quot;')
    lines = ["```mermaid", "mindmap", f"  root[\"{safe_root}\"]"]

    node_counter = [0]

    def _render(nodes: list[dict], indent: int = 2) -> list[str]:
        result: list[str] = []
        for n in nodes:
            node_counter[0] += 1
            nid = f"n{node_counter[0]}"
            safe_name = n['name'].replace('"', '&quot;')
            result.append(f"{'  ' * indent}{nid}[\"{safe_name}\"]")
            if n.get("children"):
                result.extend(_render(n["children"], indent + 1))
        return result

    lines.extend(_render(data["tree"], indent=2))
    lines.append("```")
    if data["truncated"]:
        lines.append(f"*Truncated: showing {data['total_nodes']} directory nodes (symbol level omitted)*")

    return '\n'.join(lines)


def _report_module_tree(
    target_path: str,
    base: str,
    max_nodes: int = _MODULE_TREE_MAX_NODES,
) -> dict[str, Any]:
    """Build a Mermaid mindmap of the repo structure from ctags data.

    Returns dict with "markdown" and "truncated".
    """
    data = _module_tree_data(target_path, base, max_nodes=max_nodes)
    return {
        "markdown": _module_tree_to_markdown(data),
        "truncated": data["truncated"],
    }
