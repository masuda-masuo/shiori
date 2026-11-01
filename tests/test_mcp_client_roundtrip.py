"""In-memory MCP v2 client round-trip test (shiori#369).

The v1 FastMCP server module was removed in mcp 2.x; shiori now builds its
server from ``mcp.server.mcpserver.MCPServer``. v2 also inserted
``title``/``description``
positional params before ``instructions`` on the constructor, so a naive
`MCPServer("shiori", instructions=...)` written positionally would silently
land the text in ``title`` and never deliver it to clients.

This test is the regression net for that trap: it drives the real shiori server
object through an in-memory v2 ``mcp.Client`` (no transport) and asserts the
wire-visible contract -- exactly 13 tools, non-empty descriptions, and the
``instructions`` text actually delivered in the initialize result.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

from mcp import Client

import shiori.mcp_server  # noqa: F401 -- side effect: registers every @mcp.tool
from shiori.pipeline import Phase1Outcome
from shiori.tools.registry import mcp

EXPECTED_TOOL_NAMES = [
    "shiori_search",
    "shiori_keyword_search",
    "shiori_list_tree",
    "shiori_read_file",
    "shiori_read_issue",
    "shiori_read_pr_file",
    "shiori_pr_changes",
    "shiori_pr_diff",
    "shiori_pr_review_comments",
    "shiori_grep",
    "shiori_report",
    "shiori_issue_links",
    "shiori_status",
]


def _served_tools():
    """Return the tools/list result from an in-memory v2 client session."""

    async def _run():
        async with Client(mcp) as client:
            return await client.list_tools()

    return asyncio.run(_run())


def test_client_sees_exactly_the_13_expected_tools() -> None:
    result = _served_tools()
    names = [t.name for t in result.tools]
    # Order is not part of the contract (the server's iteration order is
    # deterministic-in-process but not guaranteed) -- assert the exact set.
    assert set(names) == set(EXPECTED_TOOL_NAMES), (
        f"expected exactly {sorted(EXPECTED_TOOL_NAMES)}, "
        f"got {sorted(names)} (missing: {sorted(set(EXPECTED_TOOL_NAMES) - set(names))}, "
        f"extra: {sorted(set(names) - set(EXPECTED_TOOL_NAMES))})"
    )


def test_every_tool_has_a_non_empty_description() -> None:
    result = _served_tools()
    empty = [t.name for t in result.tools if not (t.description or "").strip()]
    assert not empty, f"tools served with an empty description: {empty}"


def test_instructions_are_delivered_to_the_client() -> None:
    """The v2 positional-args trap: instructions= must land in `instructions`,
    not in the new `title` positional slot, or clients would never see it.
    """

    async def fetch():
        async with Client(mcp) as client:
            return client.instructions

    instructions = asyncio.run(fetch())
    assert instructions, "initialize result delivered empty instructions"
    assert len(instructions) > 100, "instructions present but suspiciously short"


def test_list_tree_roundtrip_doc_only_success() -> None:
    """shiori_list_tree returns {entries: [...]} envelope over live MCP client."""

    async def run():
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = False
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [("docs/manual.md",)]
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_conn.cursor.return_value.__exit__.return_value = False

        with patch("shiori.tools.list_tree._conn", return_value=mock_conn):
            async with Client(mcp) as client:
                res = await client.call_tool("shiori_list_tree", {"repo": "owner/roundtrip", "source_type": "doc"})
                return res

    res = asyncio.run(run())
    assert res.is_error is False
    assert len(res.content) == 1
    assert res.content[0].type == "text"
    data = json.loads(res.content[0].text)
    assert isinstance(data, dict)
    assert data["entries"] == [{"path": "docs/manual.md", "source": "doc"}]
    assert "stale" not in data
    assert "refresh_error" not in data


def test_list_tree_roundtrip_failed_refresh_wire_shape() -> None:
    """Wire transport returns identical shape {entries: [...], stale: True, refresh_error: ...} on failure."""

    async def run():
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = False
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_conn.cursor.return_value.__exit__.return_value = False

        outcome = Phase1Outcome(status="failed", head=None, error="mock fetch failure")
        with (
            patch("shiori.tools.list_tree._ensure_phase1", return_value=outcome),
            patch("shiori.tools.list_tree._conn", return_value=mock_conn),
        ):
            async with Client(mcp) as client:
                res = await client.call_tool("shiori_list_tree", {"repo": "owner/roundtrip", "source_type": "code"})
                return res

    res = asyncio.run(run())
    assert res.is_error is False
    assert len(res.content) == 1
    assert res.content[0].type == "text"
    data = json.loads(res.content[0].text)
    assert isinstance(data, dict)
    assert data["entries"] == []
    assert data["stale"] is True
    assert data["refresh_error"] == "mock fetch failure"


def test_omitted_repo_tool_error(monkeypatch) -> None:
    """Omitted repo with multiple configured repos reaches client as isError tool
    result with helpful message, and session remains usable (no crash).
    """
    monkeypatch.setattr(
        "shiori.tools.common.settings.repos",
        ["owner/repo-a", "owner/repo-b"],
    )
    monkeypatch.setattr(
        "shiori.mcp_server.settings.repos",
        ["owner/repo-a", "owner/repo-b"],
    )

    async def run():
        with patch("shiori.tools.common._infer_repo_from_cwd", return_value=None):
            async with Client(mcp) as client:
                res = await client.call_tool("shiori_grep", {"pattern": "test"})
                assert res.is_error is True
                assert len(res.content) == 1
                assert res.content[0].type == "text"
                msg = res.content[0].text
                assert "required" in msg.lower()
                assert "2" in msg
                assert "cwd" in msg.lower()
                assert 'repo="*"' in msg
                assert "owner/name" in msg
                assert "short name" in msg.lower()

                # Session remains usable after tool error (no crash)
                tools = await client.list_tools()
                assert len(tools.tools) == len(EXPECTED_TOOL_NAMES)

    asyncio.run(run())

