"""shiori configuration.

すべて環境変数から読む。docker compose の `.env` 経由で渡す想定。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _repos_from_env() -> list[str]:
    raw = os.environ.get("SHIORI_REPOS", "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def _index_bot_logins_from_env() -> set[str]:
    """SHIORI_INDEX_BOT_LOGINS を小文字の set で返す。

    カンマ区切り。例: ``mcp-launcher-masuda[bot]``
    ここに列挙された bot login は is_bot=true でも索引対象にする（allowlist）。
    """
    raw = os.environ.get("SHIORI_INDEX_BOT_LOGINS", "")
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def _code_extensions_from_env() -> set[str]:
    """SHIORI_CODE_EXTENSIONS を小文字の set で返す。

    カンマ区切り（ドット付き）。例: ``.py,.js,.ts,.go,.rs``
    未設定なら None（= 全コード拡張子が対象）。
    """
    raw = os.environ.get("SHIORI_CODE_EXTENSIONS", "")
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def _code_exclude_globs_from_env() -> list[str]:
    """SHIORI_CODE_EXCLUDE_GLOBS をリストで返す。

    カンマ区切り。glob パターン（例: ``**/test_*, **/migrations/*``）。
    """
    raw = os.environ.get("SHIORI_CODE_EXCLUDE_GLOBS", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


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
    # GitHub App 認証（短期トークン。詳細設計/09）。
    # 3 つ揃えば App を優先、無ければ github_token、どちらも無ければ匿名。
    github_app_id: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_APP_ID") or None
    )
    github_app_installation_id: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_APP_INSTALLATION_ID") or None
    )
    # 秘密鍵は PATH 優先、無ければ本文（PEM）を直接。読み込みは github_app_private_key()。
    github_app_private_key_path: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH") or None
    )
    github_app_private_key_pem: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_APP_PRIVATE_KEY") or None
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
    # bot でも索引する login の allowlist（カンマ区切り。issue #25）
    # GitHub App 経由の投稿は [bot] 扱いになるが、ユーザーの代理として
    # 価値のある投稿を行う bot をここに列挙することで索引対象にできる。
    index_bot_logins: set[str] = field(default_factory=_index_bot_logins_from_env)
    # --- ソースコード索引（詳細設計/10 決定 7） ---
    # マスタスイッチ。off だとコード索引をスキップする。
    index_code: bool = field(
        default_factory=lambda: os.environ.get("SHIORI_INDEX_CODE", "").lower()
        in ("1", "true", "yes")
    )
    # コードとして扱う拡張子（小文字）。空/未設定 = 全コード拡張子が対象。
    code_extensions: set[str] = field(default_factory=_code_extensions_from_env)
    # 除外 glob パターン（カンマ区切り）。例: "**/test_*, **/migrations/*"
    code_exclude_globs: list[str] = field(
        default_factory=_code_exclude_globs_from_env
    )

    def repo_dir(self, repo: str) -> str:
        owner, name = repo.split("/", 1)
        return os.path.join(self.data_dir, "repos", f"{owner}__{name}")

    def github_app_private_key(self) -> str | None:
        """GitHub App の秘密鍵 PEM を返す。PATH 優先、無ければ本文、どちらも無ければ None。"""
        if self.github_app_private_key_path:
            with open(self.github_app_private_key_path, encoding="utf-8") as fp:
                return fp.read()
        return self.github_app_private_key_pem


def load_settings() -> Settings:
    return Settings()
