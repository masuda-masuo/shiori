"""shiori configuration.

Everything reads from environment variables. Passed via docker compose `.env`."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _repos_from_env() -> list[str]:
    raw = os.environ.get("SHIORI_REPOS", "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def _index_bot_logins_from_env() -> set[str]:
    """Return SHIORI_INDEX_BOT_LOGINS as a lowercase set."""
    raw = os.environ.get("SHIORI_INDEX_BOT_LOGINS", "")
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def _code_extensions_from_env() -> set[str]:
    """Return SHIORI_CODE_EXTENSIONS as a lowercase set."""
    raw = os.environ.get("SHIORI_CODE_EXTENSIONS", "")
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def _code_exclude_globs_from_env() -> list[str]:
    """Return SHIORI_CODE_EXCLUDE_GLOBS as a list."""
    raw = os.environ.get("SHIORI_CODE_EXCLUDE_GLOBS", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _allow_rebuild_from_env() -> bool:
    """Return SHIORI_ALLOW_REBUILD as a bool."""
    return os.environ.get("SHIORI_ALLOW_REBUILD", "").lower() in (
        "1", "true", "yes"
    )


@dataclass
class Settings:
    # Postgres (pgvector + pgroonga)
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "DATABASE_URL", "postgresql://shiori:shiori@db:5432/shiori"
        )
    )
    # For private repos. Works unset with public repos only (stricter rate limits).
    github_token: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_TOKEN") or None
    )
    # GitHub App authentication (short-lived token. See detailed design/09).
    # App preferred if all 3 present; fall back to github_token; then anonymous.
    github_app_id: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_APP_ID") or None
    )
    github_app_installation_id: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_APP_INSTALLATION_ID") or None
    )
    # Private key: PATH first, then inline PEM. Read via github_app_private_key().
    github_app_private_key_path: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH") or None
    )
    github_app_private_key_pem: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_APP_PRIVATE_KEY") or None
    )
    # Target repos. "owner/name" comma-separated, multiple allowed.
    repos: list[str] = field(default_factory=_repos_from_env)
    # Embedding model. Re-index (ingest --rebuild) required after change.
    embedding_model: str = field(
        default_factory=lambda: os.environ.get(
            "EMBEDDING_MODEL", "intfloat/multilingual-e5-small"
        )
    )
    embedding_dim: int = field(
        default_factory=lambda: int(os.environ.get("EMBEDDING_DIM", "384"))
    )
    # Clone destination and working data.
    data_dir: str = field(
        default_factory=lambda: os.environ.get("SHIORI_DATA_DIR", "/data")
    )
    # Max chunk characters (character-based. See detailed design/02 decisions).
    chunk_max_chars: int = field(
        default_factory=lambda: int(os.environ.get("SHIORI_CHUNK_MAX_CHARS", "1200"))
    )
    # Search result defaults (see detailed design/05 and 06 decisions).
    default_top_k: int = field(
        default_factory=lambda: int(os.environ.get("SHIORI_TOP_K", "8"))
    )
    snippet_chars: int = field(
        default_factory=lambda: int(os.environ.get("SHIORI_SNIPPET_CHARS", "400"))
    )
    # Background auto-sync interval in serve process (seconds). 0 disables (default).
    # Diff sync finishes in seconds, so this value caps index staleness.
    sync_interval_seconds: int = field(
        default_factory=lambda: int(
            os.environ.get("SHIORI_SYNC_INTERVAL_SECONDS", "0")
        )
    )
    # MCP server (streamable HTTP)
    mcp_host: str = field(
        default_factory=lambda: os.environ.get("SHIORI_MCP_HOST", "0.0.0.0")
    )
    mcp_port: int = field(
        default_factory=lambda: int(os.environ.get("SHIORI_MCP_PORT", "8765"))
    )
    # Allowlist for indexing bot logins (comma-separated. Issue #25).
    # Posts via GitHub App get [bot] suffix, but bots acting on
    # behalf of users can be allowlisted here for indexing.
    index_bot_logins: set[str] = field(default_factory=_index_bot_logins_from_env)
    # --- Source code indexing (detailed design/10, decision 7) ---
    # Master switch. Off skips code indexing entirely.
    index_code: bool = field(
        default_factory=lambda: os.environ.get("SHIORI_INDEX_CODE", "").lower()
        in ("1", "true", "yes")
    )
    # Code file extensions (lowercase). Empty/unset = all code extensions.
    code_extensions: set[str] = field(default_factory=_code_extensions_from_env)
    # Exclude glob patterns (comma-separated). E.g. "**/test_*, **/migrations/*"
    code_exclude_globs: list[str] = field(
        default_factory=_code_exclude_globs_from_env
    )
    # Allow rebuild=True from MCP tool shiori_ingest (full TRUNCATE). Issue #63.
    # Default false. Set true only when operationally required.
    # CLI path (python -m shiori ingest --rebuild) always allowed regardless.
    allow_rebuild: bool = field(default_factory=_allow_rebuild_from_env)

    def repo_dir(self, repo: str) -> str:
        owner, name = repo.split("/", 1)
        return os.path.join(self.data_dir, "repos", f"{owner}__{name}")

    def github_app_private_key(self) -> str | None:
        """Return the GitHub App private key PEM. PATH preferred, then inline text, else None."""
        if self.github_app_private_key_path:
            with open(self.github_app_private_key_path, encoding="utf-8") as fp:
                return fp.read()
        return self.github_app_private_key_pem


def load_settings() -> Settings:
    return Settings()
