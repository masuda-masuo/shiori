# Changelog

All notable changes to shiori are documented here. This file starts at
v0.8.0 — the first tagged release; earlier history lives in the issues and
pull requests.

## [Unreleased]

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
