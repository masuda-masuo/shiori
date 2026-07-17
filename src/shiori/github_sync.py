'''Data fetching and sync (detailed design/01).

Decisions:
- docs: git clone/pull; changed files re-chunked/re-embedded; deleted files removed from index.
- issue/PR: REST API cross-repo endpoints + `since` (updated_at cursor) incremental sync.
- Bot comments excluded; allowlist via SHIORI_INDEX_BOT_LOGINS (issue #25).
- PR diffs not indexed; review comments include diff_hunk as context.
- code: shares same clone; sha-delta re-indexes only changed files (issue #33).
- PR change file maps: metadata only; content delegated to GitHub MCP (issue #54).
- Bulk path: ChunkBuffer batches across files, bulk-inserts chunks, coarsens commits (issue #72).
- _git_fetch_ref / _git_delete_ref: PR head file primitives (issue #81).
Auth via TokenProvider (detailed design/09); git via http.extraHeader; API via httpx Auth hook.
'''

from __future__ import annotations

from .api_utils import API, _GitHubAuth, _api_pages, _api_pages_gen
from .chunk_buffer import ChunkBuffer
from .git_utils import _auth_args, _authed_url, _git, _git_delete_ref, _git_fetch_ref, _redact
from .sync_code import sync_code
from .sync_docs import sync_docs
from .sync_issues import (
    _index_item,
    _issue_title_state_kind,
    _propagate_issue_state,
    _sync_pr_reviews,
    _upsert_issue_item,
    sync_issues,
)
from .sync_utils import _clean_text, _CONTROL_CHARS_RE, _is_bot, _should_index

__all__ = [  # re-export (ruff F401 avoidance)
    "API", "_GitHubAuth", "_api_pages", "_api_pages_gen", "ChunkBuffer",
    "_CONTROL_CHARS_RE", "_is_bot", "_should_index", "_clean_text",
    "_redact", "_git", "_auth_args", "_authed_url", "_git_fetch_ref", "_git_delete_ref",
    "sync_docs", "sync_code", "sync_issues",
    "_upsert_issue_item", "_issue_title_state_kind", "_propagate_issue_state",
    "_index_item", "_sync_pr_reviews",
]
