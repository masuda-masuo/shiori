from __future__ import annotations

import hashlib
import logging
import os

import psycopg

from .chunk_buffer import ChunkBuffer
from .chunking import _detect_prog_lang, split_code
from .config import Settings
from .db import delete_chunks_by_key, get_cursor, insert_chunk, set_cursor
from .embedding import Embedder
from .git_utils import _git
from .github_auth import TokenProvider
from .sync_utils import _clean_text
from .walk_utils import (
    _is_code_file,
    _is_excluded_by_glob,
    _is_excluded_dir,
    _looks_minified,
)

log = logging.getLogger(__name__)


# ── Index (walk filesystem, chunk + embed) ────────────────────────────────


def index_code(
    settings: Settings,
    conn: psycopg.Connection,
    embedder: Embedder,
    repo: str,
    buffer: ChunkBuffer | None = None,
) -> int:
    """Index source code; only re-index changed files (detailed design/10 Step 3).

    Shares clone with docs.  Only indexes code for dev repos (those in
    SHIORI_DEV_REPOS).  Reference repos are clone-only; use shiori_grep
    for code search.

    Does NOT fetch — the clone must already be up-to-date (via fetch_docs
    or refresh_clone).
    """
    if repo not in settings.dev_repos:
        return 0

    repo_dir = settings.repo_dir(repo)

    if not os.path.isdir(repo_dir):
        log.warning("index_code: clone not found (fetch_docs not run?)")
        return 0

    head = _git(["rev-parse", "HEAD"], cwd=repo_dir)

    # Cursor check: skip walk if HEAD unchanged since last run
    prev_head = get_cursor(conn, repo, "code")
    if prev_head == head:
        return 0

    # Current code file set (with sha)
    current: dict[str, str] = {}
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if not _is_excluded_dir(d)]
        for f in files:
            abspath = os.path.join(root, f)
            rel = os.path.relpath(abspath, repo_dir)
            if not _is_code_file(f, settings):
                continue
            if _is_excluded_by_glob(rel, settings):
                continue
            with open(abspath, "rb") as fp:
                content = fp.read()
            if _looks_minified(content):
                continue
            current[rel] = hashlib.sha256(content).hexdigest()

    # Existing index (doc_files rows with kind='code')
    with conn.cursor() as cur:
        cur.execute(
            "SELECT path, content_sha FROM doc_files WHERE repo = %s AND kind = 'code'",
            (repo,),
        )
        indexed = dict(cur.fetchall())

    removed = set(indexed) - set(current)
    changed = [p for p, sha in current.items() if indexed.get(p) != sha]

    # Delete removed files
    for path in removed:
        delete_chunks_by_key(conn, f"code:{repo}:{path}")
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM doc_files WHERE repo = %s AND path = %s AND kind = 'code'",
                (repo, path),
            )
        log.info("removed code %s", path)

    # Re-index changed/added files
    for path in changed:
        abspath = os.path.join(repo_dir, path)
        try:
            with open(abspath, encoding="utf-8", errors="replace") as fp:
                text = fp.read()
            text = _clean_text(text)  # Remove NUL etc. (issue #111)
        except Exception as exc:
            log.warning("index_code: skip %s (%s)", path, exc)
            continue

        chunks = split_code(path, text, settings.chunk_max_chars)
        chunk_key = f"code:{repo}:{path}"
        delete_chunks_by_key(conn, chunk_key)

        prog_lang = _detect_prog_lang(path)
        if chunks:
            if buffer is not None:
                for c in chunks:
                    # Permalink uses commit_sha (resilient to line drift. Should-fix #5)
                    url = (
                        f"https://github.com/{repo}/blob/{head}/{path}"
                        f"#L{c.start_line}-L{c.end_line}"
                        if c.start_line and c.end_line
                        else f"https://github.com/{repo}/blob/{head}/{path}"
                    )
                    buffer.add(
                        chunk_key=chunk_key,
                        chunk_index=c.chunk_index,
                        source_type="code",
                        repo=repo,
                        path=path,
                        language=None,  # code uses language=NULL (decision 4)
                        heading_path=c.heading_path,
                        content=c.content,
                        line=c.start_line,
                        end_line=c.end_line,
                        commit_sha=head,
                        prog_lang=prog_lang,
                        symbols=c.symbols,
                        url=url,
                    )
            else:
                vectors = embedder.embed_passages([c.content for c in chunks])
                for c, v in zip(chunks, vectors):
                    # Permalink uses commit_sha (resilient to line drift. Should-fix #5)
                    url = (
                        f"https://github.com/{repo}/blob/{head}/{path}"
                        f"#L{c.start_line}-L{c.end_line}"
                        if c.start_line and c.end_line
                        else f"https://github.com/{repo}/blob/{head}/{path}"
                    )
                    insert_chunk(
                        conn,
                        chunk_key=chunk_key,
                        chunk_index=c.chunk_index,
                        source_type="code",
                        repo=repo,
                        path=path,
                        language=None,  # code uses language=NULL (decision 4)
                        heading_path=c.heading_path,
                        content=c.content,
                        embedding=v,
                        line=c.start_line,
                        end_line=c.end_line,
                        commit_sha=head,
                        prog_lang=prog_lang,
                        symbols=c.symbols,
                        url=url,
                    )

        # Record in doc_files with kind='code'
        with conn.cursor() as cur:
            cur.execute(
                """ INSERT INTO doc_files (repo, path, content_sha, language, kind)
                VALUES (%s, %s, %s, NULL, 'code')
                ON CONFLICT (repo, path) DO UPDATE
                SET content_sha = EXCLUDED.content_sha, kind = 'code'
                """,
                (repo, path, current[path]),
            )
        if buffer is None:
            conn.commit()
        log.info("indexed code %s (%d chunks, %s)", path, len(chunks), prog_lang or "?")

    if buffer is None:
        conn.commit()
    set_cursor(conn, repo, "code", head)
    return len(changed) + len(removed)


# ── Combined — backward compatible alias ──────────────────────────────────


def sync_code(
    settings: Settings,
    conn: psycopg.Connection,
    embedder: Embedder,
    repo: str,
    provider: TokenProvider,
    buffer: ChunkBuffer | None = None,
) -> int:
    """Sync source code; index only changed files (detailed design/10 Step 3).

    Backward-compatible wrapper around index_code (provider is unused).
    """
    return index_code(settings, conn, embedder, repo, buffer=buffer)
