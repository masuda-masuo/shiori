"""Guard test for Issue #340: every registered MCP tool documents its data source.

Enumerates tools dynamically from the live FastMCP registry (not a hardcoded
list) so a future tool added without a "Data sources:" line fails this test.
"""

from __future__ import annotations

import asyncio

import shiori.mcp_server as mcp_server


def _registered_tools():
    """Return the live list of registered tools via FastMCP's public API.

    Uses the public async ``FastMCP.list_tools()`` rather than the private
    ``_tool_manager`` so a FastMCP upgrade can't silently change what this
    guard enumerates. Importing shiori.mcp_server already triggers
    registration of every @mcp.tool as a side effect -- see the
    `from .tools import (...)` block there.
    """
    return asyncio.run(mcp_server.mcp.list_tools())


def test_registry_is_not_empty() -> None:
    """Sanity check: if this is ever 0, the enumeration below is vacuously true."""
    tools = _registered_tools()
    assert len(tools) > 0


def test_every_tool_documents_its_data_source() -> None:
    """Every registered tool's visible description has a 'Data sources:' line.

    FastMCP builds `description` from the tool function's docstring, and issue
    #550 established that everything from an 'Args:' section onward is dropped
    from that visible description -- so this also guards against the line
    being added only under 'Args:' where it would silently stop being visible.
    """
    tools = _registered_tools()
    missing = [t.name for t in tools if "Data sources:" not in (t.description or "")]
    assert not missing, (
        f"tools missing a 'Data sources:' line in their visible docstring: {missing}"
    )


def test_data_sources_line_is_reasonably_short() -> None:
    """Each 'Data sources:' line stays well under the ~120 char guidance (issue #340).

    Loose per-line check (not per-sentence) so a still-compact multi-line
    contract isn't penalized; catches an accidental essay-length line.
    """
    tools = _registered_tools()
    offenders: list[str] = []
    for t in tools:
        for line in (t.description or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("Data sources:") and len(stripped) > 160:
                offenders.append(f"{t.name}: {len(stripped)} chars")
    assert not offenders, f"Data sources line too long: {offenders}"
