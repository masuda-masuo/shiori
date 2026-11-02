# Changelog

All notable changes to shiori are documented here. This file starts at
v0.8.0 — the first tagged release; earlier history lives in the issues and
pull requests.

## [Unreleased]

### Operations / Infrastructure

- Automatic mint socket mount reconciliation for WSL2 / Linux (#461):
  - Add host stdlib reconciliation CLI `scripts/reconcile_mint_mount.py` invoked periodically (~120s) via systemd user timer `shiori-mint-mount.timer` (`shiori-mint-mount.service`). Checks filesystem metadata only; does not mint tokens.
  - Resolves WSL tmpfs runtime directory replacement (`st_dev` / `st_ino` change) by force-recreating the app container alone (`docker compose up -d --force-recreate --no-deps app`), while adopting external container ID changes without container mutation or downtime when filesystem generation is unchanged.
  - Implements atomic state tracking (`${XDG_STATE_HOME:-$HOME/.local/state}/shiori/mint-mount.json`), generation-keyed 120s retry cooldown, non-blocking flock serialization, postcheck verification, and fail-closed handling for unverified opaque VM paths.
  - Integrate units into `scripts/install-systemd.sh` with host `/usr/bin/python3` prerequisite.
  - Document mount footguns, root-owned source poisoning, systemd unit-state boundaries, and the pending WSL human acceptance test procedure in `docs/design/15_token_supply_path.md` and `docs/guides/setup.md`.

### Core / MCP Tools

- **BREAKING CHANGE**: Require unambiguous omitted repository resolution across tools (#459):
  - When `repo` is omitted on targeted tools, resolution order is now strictly:
    1. If exactly one repository is configured, resolve to it immediately without inspecting working directory.
    2. If the current working directory (`cwd`) matches a configured repository remote, resolve to it.
    3. Otherwise, raise an error naming the ambiguity, configured repository count, and remedy (`repo="owner/name"` or unique short name; or `repo="*"` for `shiori_grep`).
  - Silent fallback to the first configured repository (`settings.repos[0]`) has been removed; callers relying on omitted `repo` with multiple configured repositories outside a matching working directory now fail with a clear error.
- Expose tool-path Phase 1 refresh failures cleanly across tools (#457, #458, #460):
  - `_ensure_phase1` returns structured `Phase1Outcome(status, head, error)` with `refreshed`, `debounced`, or `failed` status.
  - Clone-reading tools (`shiori_read_file`, `shiori_report`) continue serving the on-disk clone on Phase 1 refresh failures and expose `stale: true` and `refresh_error` metadata (omitted on success/debounce).
  - `shiori_grep` tracks refresh outcomes per repository: stale clones are searched normally and reported in `stale_repos` with top-level `stale: true` and `refresh_error`; missing clones are reported in `skipped_repos` as `{"repo": ..., "reason": ...}` with `partial: true`.
  - **BREAKING CHANGE**: `shiori_list_tree` now always returns a mapping envelope `{"entries": [...]}` instead of bare `list[dict]` (before: bare `[...]` on success/debounced/doc-only reads; after: `{"entries": [...]}` for all success/debounced/doc-only/empty cases). On clone refresh failure, outage fields `stale: true` and `refresh_error: str` are attached to the mapping envelope (omitted otherwise).
  - **BREAKING CHANGE**: `shiori_grep.skipped_repos` changes from `["owner/repo"]` to `[{"repo": "owner/repo", "reason": "..."}]`; callers iterating entries must read `entry["repo"]`, while emptiness checks remain compatible.
  - `shiori_status` surfaces an independent `tool_refresh_consecutive_failures` counter and `tool_refresh_last_error` stored in process memory. Cleared on successful tool-path refresh and independent of background sync failures. The fleet summary adds `unhealthy_counts.tool_refresh_failing` counting repositories with active tool refresh failures.

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
