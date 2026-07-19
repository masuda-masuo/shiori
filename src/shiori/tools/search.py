from __future__ import annotations

from typing import Any

from .registry import mcp
from .common import _make_filters, _resolve_repo, _resolve_repo_filter, _resolve_repos  # noqa: F401 — re-export for tests
from ..pipeline import _conn, _ensure_phase1, _get_embedder, settings, _trigger_phase2
from .. import search


@mcp.tool(name="shiori_search")
def semantic_search(
    query: str,
    source_type: str | None = None,
    language: str | None = None,
    state: str | None = None,
    repo: str | None = None,
    path_prefix: str | None = None,
    updated_after: str | None = None,
    prog_lang: str | None = None,
    kind: str | None = None,
    labels: list[str] | None = None,
    top_k: int | None = None,
    sort_by: str = "score",
    sort_order: str = "desc",
) -> list[dict[str, Any]]:
    """Semantic search (entry). Strong for paraphrasing, concept, cross-lingual queries.
    Hybrid with keyword search internally.
    labels: filter by GitHub issue labels (match-any semantics). Only affects
            issue/pr_review source_types; doc/code results are excluded when
            labels filter is active (issue #165).
    kind: 'issue' | 'pr' — further filter source_type='issue'/'pr_review' results
          by thread type. No effect on doc/code results (issue #98).
    repo: "owner/name" filter, or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit to search across all indexed repos."""
    # Phase 1/2 for cross-repo search: refresh all repos when no filter (#236)
    resolved_repo = _resolve_repo_filter(repo)
    if resolved_repo:
        _ensure_phase1(resolved_repo)
        _trigger_phase2(resolved_repo)
    else:
        for r in _resolve_repos("*"):
            _ensure_phase1(r)
            _trigger_phase2(r)
    with _conn() as conn:
        return search.semantic_search(
            settings, conn, _get_embedder(), query,
            _make_filters(source_type, language, state, _resolve_repo_filter(repo), path_prefix, updated_after, prog_lang, kind, labels=labels),
            top_k,
            sort_by,
            sort_order,
        )


@mcp.tool(name="shiori_keyword_search")
def keyword_search(
    query: str,
    source_type: str | None = None,
    language: str | None = None,
    state: str | None = None,
    repo: str | None = None,
    path_prefix: str | None = None,
    updated_after: str | None = None,
    prog_lang: str | None = None,
    kind: str | None = None,
    labels: list[str] | None = None,
    top_k: int | None = None,
    sort_by: str = "score",
    sort_order: str = "desc",
    match_all: bool = False,
) -> list[dict[str, Any]]:
    """Keyword search (Japanese tokenize). Strong for exact matches: function names, API names, error codes, config keys.
    Multi-token queries use OR matching by default (any token can match); tokens that match more/strongly rank higher.
    Pass match_all=True for AND behavior (all tokens must match the same chunk).
    labels: filter by GitHub issue labels (match-any semantics). Only affects
            issue/pr_review source_types; doc/code results are excluded when
            labels filter is active (issue #165).
    kind: 'issue' | 'pr' — further filter source_type='issue'/'pr_review' results
          by thread type. No effect on doc/code results (issue #98).
    repo: "owner/name" filter, or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit to search across all indexed repos."""
    # Phase 1/2 for cross-repo search: refresh all repos when no filter (#236)
    resolved_repo = _resolve_repo_filter(repo)
    if resolved_repo:
        _ensure_phase1(resolved_repo)
        _trigger_phase2(resolved_repo)
    else:
        for r in _resolve_repos("*"):
            _ensure_phase1(r)
            _trigger_phase2(r)
    with _conn() as conn:
        return search.keyword_search(
            settings, conn, query,
            _make_filters(source_type, language, state, _resolve_repo_filter(repo), path_prefix, updated_after, prog_lang, kind, labels=labels),
            top_k,
            sort_by,
            sort_order,
            match_all=match_all,
        )
