"""shiori configuration.

すべて環境変数から読む。docker compose の `.env` 経由で渡す想定。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _repos_from_env() -> list[str]:
    raw = os.environ.get("SHIORI_REPOS", "")
    return [r.strip() for r in raw.split(",") if r.strip()]


@dataclass
class Settings:
    # Postgres (pgvector + pgroonga)
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "DATABASE_URL", "postgresql://shiori:shiori@db:5432/shiori"
        )
    )
    # 非公開リポジトリ用。公開リポジトリのみなら未設定でも動く（レート制限は厳しくなる）
    github_token: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_TOKEN") or None
    )
    # 対象リポジトリ。"owner/name" をカンマ区切りで複数指定可
    repos: list[str] = field(default_factory=_repos_from_env)
    # 埋め込みモデル。変更したら再索引（ingest --rebuild）が必要
    embedding_model: str = field(
        default_factory=lambda: os.environ.get(
            "EMBEDDING_MODEL", "intfloat/multilingual-e5-small"
        )
    )
    embedding_dim: int = field(
        default_factory=lambda: int(os.environ.get("EMBEDDING_DIM", "384"))
    )
    # クローン先・作業データ
    data_dir: str = field(
        default_factory=lambda: os.environ.get("SHIORI_DATA_DIR", "/data")
    )
    # チャンクの最大文字数（文字基準。詳細設計/02 の決定を参照）
    chunk_max_chars: int = field(
        default_factory=lambda: int(os.environ.get("SHIORI_CHUNK_MAX_CHARS", "1200"))
    )
    # 検索結果の既定値（詳細設計/05・06 の決定を参照）
    default_top_k: int = field(
        default_factory=lambda: int(os.environ.get("SHIORI_TOP_K", "8"))
    )
    snippet_chars: int = field(
        default_factory=lambda: int(os.environ.get("SHIORI_SNIPPET_CHARS", "400"))
    )
    # serve プロセス内のバックグラウンド自動同期間隔（秒）。0 で無効（既定）。
    # 差分同期は数秒で終わるため、この値が「索引の古さの上限」になる。
    sync_interval_seconds: int = field(
        default_factory=lambda: int(
            os.environ.get("SHIORI_SYNC_INTERVAL_SECONDS", "0")
        )
    )
    # MCP サーバー (streamable HTTP)
    mcp_host: str = field(
        default_factory=lambda: os.environ.get("SHIORI_MCP_HOST", "0.0.0.0")
    )
    mcp_port: int = field(
        default_factory=lambda: int(os.environ.get("SHIORI_MCP_PORT", "8765"))
    )

    def repo_dir(self, repo: str) -> str:
        owner, name = repo.split("/", 1)
        return os.path.join(self.data_dir, "repos", f"{owner}__{name}")


def load_settings() -> Settings:
    return Settings()
