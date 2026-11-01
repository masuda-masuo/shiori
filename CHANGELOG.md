# Changelog

All notable changes to shiori are documented here. This file starts at
v0.8.0 — the first tagged release; earlier history lives in the issues and
pull requests.

## [Unreleased]

### Core / MCP Tools

- Expose tool-path Phase 1 refresh failures cleanly across tools (#457, #458, #460):
  - `_ensure_phase1` returns structured `Phase1Outcome(status, head, error)` with `refreshed`, `debounced`, or `failed` status.
  - Clone-reading tools (`shiori_read_file`, `shiori_report`) continue serving the on-disk clone on Phase 1 refresh failures and expose `stale: true` and `refresh_error` metadata (omitted on success/debounce).
  - `shiori_grep` tracks refresh outcomes per repository: stale clones are searched normally and reported in `stale_repos` with top-level `stale: true` and `refresh_error`; missing clones are reported in `skipped_repos` as `{"repo": ..., "reason": ...}` with `partial: true`.
  - `shiori_list_tree` returns bare `list[dict]` on successful/debounced refresh and doc-only reads, and returns `{"entries": [...], "stale": true, "refresh_error": ...}` on clone refresh failure.
  - `shiori_status` surfaces an independent `tool_refresh_consecutive_failures` counter and `tool_refresh_last_error` stored in process memory. Cleared on successful tool-path refresh and independent of background sync failures.

## [0.8.0] - 2026-08-02

First tagged release. The state of the system at this point:

### Core

- Unified, cross-lingual (ja/en) index over GitHub repository knowledge —
  Markdown docs, source code, and issue/PR discussions — on Postgres +
  pgvector + pgroonga, served as 13 MCP tools over streamable HTTP.
- MCP Python SDK v2 (2026-07-28 spec); in-memory client round-trip test
  guards the served contract (#369).
- Tool descriptions reorganized as Map (server `instructions`) / Contract
  (per-tool docstring with a mandatory `Data sources:` line) (#270).
- Decision-record comments (the `## 設計判断` heading convention) get a
  bounded, sort-key-only ranking boost; signal chosen by measurement on
  citation-labeled data (#404).

### Ingest

- Role-scoped steady sync driven by host timers (dev ~15 min / ref daily)
  (#347); per-run log files (#372).
- Connection discipline: phase-scoped connections (#373), bounded runs with
  visible remaining work (#377), a global PR-review connection ceiling
  (#375), per-repo failure isolation (#370), lane-scoped circuit-breaker
  backoff (#371).
- Work-aware bulk indexing path (#376); GPU auto-detection for ingest lanes
  (#383); ONNX INT8 embedder on the CPU/serve paths with explicit opt-out on
  GPU lanes (#353).

### Operations

- Pull-based deploy from GHCR: CI pushes an image per image-relevant commit;
  `scripts/update.sh` pulls the image for the newest image-relevant commit
  and fails loud when CI has not pushed it yet (#402, #403).
- Config env parsing hardened (#386, #397); compose `environment:`
  reachability guarded by tests (#396).
