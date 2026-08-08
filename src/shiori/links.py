"""Issue/PR cross-reference extraction module (issue #282 / #97).

Extracted from mcp_server.py to separate reference extraction logic
from MCP server infrastructure. Pure functions — no settings, conn,
httpx, or db dependencies.
"""

from __future__ import annotations

import re

# Type precedence for cross-reference merging (closes > duplicate > refs > mention)
_TYPE_PRECEDENCE: dict[str, int] = {
    "closes": 0,
    "duplicate": 1,
    "refs": 2,
    "mention": 3,
}

# Patterns for issue reference classification (issue #97)
_CLOSES_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(?:#([0-9]+)|https://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/([0-9]+))",
    re.IGNORECASE,
)
_DUPLICATE_RE = re.compile(
    r"\bduplicate\s+(?:of\s+)?(?:#([0-9]+)|https://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/([0-9]+))",
    re.IGNORECASE,
)
_REFS_RE = re.compile(
    r"\b(?:refs?|see|related(?:\s+to)?)\s+(?:#([0-9]+)|https://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/([0-9]+))",
    re.IGNORECASE,
)
_MENTION_RE = re.compile(
    r"(?<!\w)#([0-9]+)"
)


def _extract_refs(text: str | None) -> list[dict]:
    """Extract classified cross-references from body text (issue #97)."""
    if not text:
        return []
    seen: dict[int, str] = {}
    for m in _CLOSES_RE.finditer(text):
        n = int(m.group(1) or m.group(2))
        if n not in seen:
            seen[n] = "closes"
    for m in _DUPLICATE_RE.finditer(text):
        n = int(m.group(1) or m.group(2))
        if n not in seen:
            seen[n] = "duplicate"
    for m in _REFS_RE.finditer(text):
        n = int(m.group(1) or m.group(2))
        if n not in seen:
            seen[n] = "refs"
    for m in _MENTION_RE.finditer(text):
        n = int(m.group(1))
        if n not in seen:
            seen[n] = "mention"
    return [{"issue_no": no, "type": typ} for no, typ in seen.items()]


def merge_outbound_refs(
    bodies: list[dict], self_number: int
) -> dict[int, dict]:
    """Merge outbound refs from multiple bodies, excluding self references.

    Applies _TYPE_PRECEDENCE priority: closes > duplicate > refs > mention.
    The first-encountered lower-priority ref for a given issue_no is
    retained unless a higher-priority one appears later.
    """
    outbound: dict[int, dict] = {}
    for b in bodies:
        for ref in _extract_refs(b["body"]):
            n = ref["issue_no"]
            if n == self_number:
                continue
            if n not in outbound or _TYPE_PRECEDENCE.get(
                ref["type"], 99
            ) < _TYPE_PRECEDENCE.get(outbound[n]["type"], 99):
                outbound[n] = ref
    return outbound
