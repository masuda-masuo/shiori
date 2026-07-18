# Phase 3: PR review fetch を ThreadPoolExecutor で並列化する

**Status:** accepted
**Deciders:** opencode
**Date:** 2026-07-19

## Context

Issue #305 EPIC の Phase 3。PR review 投稿（`/pulls/{n}/reviews`）の N+1 API call 問題に対処する。

Phase 1 (#306) で `fetch_issues` / `index_issues` の分離が完了し、
Phase 2 (#307) でリポジトリ単位ロック＋複数リポジトリの並列 fetch 基盤が整った。

残る問題: `fetch_issues()` 内の PR review 取得ループが PR ごとに逐次 API call を行っており、
大規模 ref repo（cockroachdb クラスで 50K+ PR）ではブロッキングになる。

## Decision Drivers

- PR review データは全リポジトリで取得する必要がある（データ欠損防止）
- 大規模 ref repo でも許容可能な時間で同期を完了する
- 既存のデータフロー（fetch → index 分離）を維持する

## Considered Options

- **Option 1: ThreadPoolExecutor で PR review fetch を並列化** — 各スレッドが独立した httpx.Client + DB 接続を持つ
- **Option 2: 非同期 I/O (asyncio/httpx.AsyncClient)** — より高い並列度が得られるが既存コードとの整合性が低い
- **Option 3: スレッド共有 Client + Connection に threading.Lock** — リソース節約になるが Lock 競合がボトルネックに

## Decision Outcome

選んだ選択肢: **Option 1: ThreadPoolExecutor**

理由:
- 既存の `ingest.py` で同一パターン（`_fetch_one` 内で独自 DB 接続 + httpx.Client）が実績あり
- スレッド並列数は `MAX_PR_REVIEW_WORKERS=10` 固定。GitHub API レート制限を考慮した保守的な値
- `_sync_pr_reviews` の本体内ロジックは変更せず、呼び出し側のループ構造のみ変更
- 各 PR のエラーは個別にログ出力し、全体の fetch は継続する（resilient design）

### Positive Consequences

- N+1 問題が並列 fetch で吸収される — 理論上最大 10 倍の高速化
- エラー耐性: 1 件の PR 失敗が全体の同期を止めない
- 既存コードへの影響が小さい（呼び出し構造のみ変更）

### Negative Consequences

- PR ごとに DB 接続を作成するため、DB 接続数のピークが増加
- ThreadPoolExecutor のオーバーヘッド（少ない PR 数ではコストが割に合わない可能性あり）

## Pros and Cons of the Options

### Option 1: ThreadPoolExecutor + per-thread client/connection

- Good: 既存の並列パターンと一貫性がある
- Good: httpx.Client / psycopg.Connection のスレッド安全性問題を回避
- Good: 個別の PR 失敗が全体に影響しない
- Bad: PR ごとに DB 接続を作成するコスト

### Option 2: asyncio

- Good: より高い並列度と低オーバーヘッド
- Bad: 全 fetch パスを非同期に書き換える必要があり、変更範囲が大きい
- Bad: `_sync_pr_reviews` を async に変更する必要がある

### Option 3: Shared client/connection with lock

- Good: DB 接続数が一定
- Bad: 全 PR の upsert が直列化される (N+1 が書き込み側に移動するだけ)
- Bad: httpx.Client の共有も thread-safe ではない
