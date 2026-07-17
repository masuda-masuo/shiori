"""Walk utility functions shared between mcp_server.py and github_sync.py.

Consolidates duplicate constants and functions related to file walking,
exclusion rules, and code file detection.
"""

from __future__ import annotations

import fnmatch
import os

from .config import Settings


# Document extensions (case-insensitive). Excluded from walk; handled by doc_files table
_DOC_EXTENSIONS = {".md", ".mdx", ".markdown"}

# Directory names to skip in os.walk
_EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    ".venv", "venv",
    "dist", "build",
    "__pycache__",
    ".tox", ".eggs",
    ".next",
    "target",
    ".cache",
}

# Directory name suffixes to skip in os.walk (build-output dirs that don't
# match _EXCLUDE_DIRS exactly, e.g. "dashboard_dist"; issue #235)
_EXCLUDE_DIR_SUFFIXES = ("_dist", "-dist")

# File extensions excluded from code indexing (binary/asset etc.)
_EXCLUDE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pyc", ".pyo",
    ".so", ".dylib", ".dll", ".wasm",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".pdf",
    ".lock",
    ".min.js", ".min.css",
}

# Longest line (chars) a hand-written source file is expected to have.
_MINIFIED_LINE_THRESHOLD = 500


def _is_excluded_dir(name: str) -> bool:
    """Directory should be pruned from os.walk (issue #235)."""
    return name in _EXCLUDE_DIRS or name.endswith(_EXCLUDE_DIR_SUFFIXES)


def _is_doc_file(filename: str) -> bool:
    """Check if filename has a document extension (case-insensitive)."""
    return any(filename.lower().endswith(ext) for ext in _DOC_EXTENSIONS)


def _is_excluded_file(filename: str) -> bool:
    """Check if filename has an excluded extension (case-insensitive)."""
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in _EXCLUDE_EXTENSIONS)


def _match_extension(path: str, extension: str) -> bool:
    """Check if extension matches given value (case-insensitive, with/without leading dot)."""
    ext = extension if extension.startswith(".") else "." + extension
    return path.lower().endswith(ext.lower())


def _looks_minified(content: bytes) -> bool:
    """Heuristic: a single very long line strongly suggests a minified bundle."""
    try:
        sample = content[:8192].decode("utf-8", errors="ignore")
    except Exception:
        return False
    return any(len(line) > _MINIFIED_LINE_THRESHOLD for line in sample.splitlines())


def _is_code_file(filename: str, settings: Settings) -> bool:
    """Determine if the relative path is a code file that should be indexed."""
    lower = filename.lower()
    if lower.endswith((".md", ".mdx", ".markdown")):
        return False
    if any(lower.endswith(ext) for ext in _EXCLUDE_EXTENSIONS):
        return False
    if settings.code_extensions:
        return any(lower.endswith(ext) for ext in settings.code_extensions)
    return True


def _is_excluded_by_glob(rel_path: str, settings: Settings) -> bool:
    """Check if path matches excluded glob patterns."""
    for pattern in settings.code_exclude_globs:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
    return False


def _walk_code_files(base: str, prefix: str, extension: str | None = None) -> set[str]:
    """Walk clone and return code file relative paths.
    Skips .git/node_modules/.venv, binary extensions.
    """
    paths: set[str] = set()
    if not os.path.isdir(base):
        return paths
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not _is_excluded_dir(d)]
        rel_dir = os.path.relpath(dirpath, base)
        if rel_dir == ".":
            rel_dir = ""
        for fn in filenames:
            if _is_doc_file(fn) or _is_excluded_file(fn):
                continue
            rel_path = os.path.join(rel_dir, fn) if rel_dir else fn
            if prefix and not (
                rel_path == prefix or rel_path.startswith(prefix + "/")
            ):
                continue
            if extension and not _match_extension(rel_path, extension):
                continue
            paths.add(rel_path)
    return paths
