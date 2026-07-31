from __future__ import annotations

import hashlib
import logging
import os

import psycopg

from .chunk_buffer import ChunkBuffer
from .chunking import detect_language, split_markdown
from .config import Settings
from .db import delete_chunks_by_key, insert_chunk, set_cursor
from .embedding import Embedder
from .git_utils import _git
from .github_auth import TokenProvider
from .sync_utils import _clean_text

log = logging.getLogger(__name__)


# ── Fetch (git pull only) ─────────────────────────────────────────────────


def fetch_docs(
    settings: Settings,
    conn: psycopg.Connection,
    repo: str,
    provider: TokenProvider,
) -> str | None:
    """Fetch docs: git pull only (refresh clone).

    Does NOT read or index any files — only ensures the clone is up-to-date.
    Returns HEAD SHA, or None if clone refresh failed.
    """
    from .refresh import refresh_clone

    try:
        head = refresh_clone(repo, provider, settings)
        return head
    except Exception:
        log.exception("fetch_docs: clone refresh failed for %s", repo)
        return None


# ── Index (walk filesystem, chunk + embed) ────────────────────────────────


def index_docs(
    settings: Settings,
    conn: psycopg.Connection,
    embedder: Embedder,
    repo: str,
    buffer: ChunkBuffer | None = None,
) -> int:
    """Index docs: walk repo directory, chunk + embed changed files.

    Reads the clone on disk (must be fetched first via fetch_docs or
    refresh_clone).  Compares sha256 against doc_files to detect changes.
    Returns the number of updated (changed + removed) files.
    """
    repo_dir = settings.repo_dir(repo)

    if not os.path.isdir(repo_dir):
        log.warning("index_docs: clone not found for %s (fetch_docs not run?)", repo)
        return 0

    # Diff current file set against existing index
    current: dict[str, str] = {}
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            if not f.lower().endswith((".md", ".mdx", ".markdown")):
                continue
            abspath = os.path.join(root, f)
            rel = os.path.relpath(abspath, repo_dir)
            with open(abspath, "rb") as fp:
                current[rel] = hashlib.sha256(fp.read()).hexdigest()

    with conn.cursor() as cur:
        cur.execute("SELECT path, content_sha FROM doc_files WHERE repo = %s", (repo,))
        indexed = dict(cur.fetchall())

    removed = set(indexed) - set(current)
    changed = [p for p, sha in current.items() if indexed.get(p) != sha]

    for path in removed:
        delete_chunks_by_key(conn, f"doc:{repo}:{path}")
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM doc_files WHERE repo = %s AND path = %s", (repo, path)
            )

    # Determine default branch for URL generation
    try:
        default_branch = _git(
            ["rev-parse", "--abbrev-ref", "origin/HEAD"], cwd=repo_dir
        ).split("/")[-1]
    except Exception:
        default_branch = "main"

    for path in changed:
        with open(os.path.join(repo_dir, path), encoding="utf-8", errors="replace") as fp:
            text = fp.read()
        text = _clean_text(text)  # Remove NUL etc. (issue #111)
        language = detect_language(text)
        chunks = split_markdown(text, settings.chunk_max_chars)
        chunk_key = f"doc:{repo}:{path}"
        delete_chunks_by_key(conn, chunk_key)
        if chunks:
            if buffer is not None:
                for c in chunks:
                    anchor = ""
                    if c.heading_path:
                        last = c.heading_path.split(" > ")[-1]
                        anchor = "#" + last.lower().replace(" ", "-")
                    buffer.add(
                        chunk_key=chunk_key,
                        chunk_index=c.chunk_index,
                        source_type="doc",
                        repo=repo,
                        path=path,
                        language=language,
                        heading_path=c.heading_path,
                        content=c.content,
                        url=f"https://github.com/{repo}/blob/{default_branch}/{path}{anchor}",
                    )
            else:
                vectors = embedder.embed_passages([c.content for c in chunks])
                for c, v in zip(chunks, vectors):
                    anchor = ""
                    if c.heading_path:
                        last = c.heading_path.split(" > ")[-1]
                        anchor = "#" + last.lower().replace(" ", "-")
                    insert_chunk(
                        conn,
                        chunk_key=chunk_key,
                        chunk_index=c.chunk_index,
                        source_type="doc",
                        repo=repo,
                        path=path,
                        language=language,
                        heading_path=c.heading_path,
                        content=c.content,
                        embedding=v,
                        url=f"https://github.com/{repo}/blob/{default_branch}/{path}{anchor}",
                    )
        with conn.cursor() as cur:
            cur.execute(
                """ INSERT INTO doc_files (repo, path, content_sha, language)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (repo, path) DO UPDATE
                SET content_sha = EXCLUDED.content_sha, language = EXCLUDED.language
                """,
                (repo, path, current[path], language),
            )
        if buffer is None:
            conn.commit()
        log.debug("indexed doc %s (%d chunks)", path, len(chunks))

    if buffer is None:
        conn.commit()

    # Update docs cursor to trigger HEAD
    try:
        head = _git(["rev-parse", "HEAD"], cwd=repo_dir)
        set_cursor(conn, repo, "docs", head)
    except Exception:
        pass

    return len(changed) + len(removed)


# ── Combined (fetch + index) — backward compatible ───────────────────────


def sync_docs(
    settings: Settings,
    conn: psycopg.Connection,
    embedder: Embedder,
    repo: str,
    provider: TokenProvider,
    buffer: ChunkBuffer | None = None,
) -> int:
    """Sync repo Markdown; index only changed files. Returns update count.
    When buffer specified (bulk path), uses ChunkBuffer for batch embedding.

    This is the combined path: fetch + index.
    """
    # Fetch (git pull)
    from .refresh import refresh_clone

    head = refresh_clone(repo, provider, settings)
    repo_dir = settings.repo_dir(repo)

    # Diff current file set against existing index
    current: dict[str, str] = {}
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            if not f.lower().endswith((".md", ".mdx", ".markdown")):
                continue
            abspath = os.path.join(root, f)
            rel = os.path.relpath(abspath, repo_dir)
            with open(abspath, "rb") as fp:
                current[rel] = hashlib.sha256(fp.read()).hexdigest()

    with conn.cursor() as cur:
        cur.execute("SELECT path, content_sha FROM doc_files WHERE repo = %s", (repo,))
        indexed = dict(cur.fetchall())

    removed = set(indexed) - set(current)
    changed = [p for p, sha in current.items() if indexed.get(p) != sha]

    for path in removed:
        delete_chunks_by_key(conn, f"doc:{repo}:{path}")
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM doc_files WHERE repo = %s AND path = %s", (repo, path)
            )

    default_branch = _git(
        ["rev-parse", "--abbrev-ref", "origin/HEAD"], cwd=repo_dir
    ).split("/")[-1]

    for path in changed:
        with open(os.path.join(repo_dir, path), encoding="utf-8", errors="replace") as fp:
            text = fp.read()
        text = _clean_text(text)  # Remove NUL etc. (issue #111)
        language = detect_language(text)
        chunks = split_markdown(text, settings.chunk_max_chars)
        chunk_key = f"doc:{repo}:{path}"
        delete_chunks_by_key(conn, chunk_key)
        if chunks:
            if buffer is not None:
                for c in chunks:
                    anchor = ""
                    if c.heading_path:
                        last = c.heading_path.split(" > ")[-1]
                        anchor = "#" + last.lower().replace(" ", "-")
                    buffer.add(
                        chunk_key=chunk_key,
                        chunk_index=c.chunk_index,
                        source_type="doc",
                        repo=repo,
                        path=path,
                        language=language,
                        heading_path=c.heading_path,
                        content=c.content,
                        url=f"https://github.com/{repo}/blob/{default_branch}/{path}{anchor}",
                    )
            else:
                vectors = embedder.embed_passages([c.content for c in chunks])
                for c, v in zip(chunks, vectors):
                    anchor = ""
                    if c.heading_path:
                        last = c.heading_path.split(" > ")[-1]
                        anchor = "#" + last.lower().replace(" ", "-")
                    insert_chunk(
                        conn,
                        chunk_key=chunk_key,
                        chunk_index=c.chunk_index,
                        source_type="doc",
                        repo=repo,
                        path=path,
                        language=language,
                        heading_path=c.heading_path,
                        content=c.content,
                        embedding=v,
                        url=f"https://github.com/{repo}/blob/{default_branch}/{path}{anchor}",
                    )
        with conn.cursor() as cur:
            cur.execute(
                """ INSERT INTO doc_files (repo, path, content_sha, language)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (repo, path) DO UPDATE
                SET content_sha = EXCLUDED.content_sha, language = EXCLUDED.language
                """,
                (repo, path, current[path], language),
            )
        if buffer is None:
            conn.commit()
        log.debug("indexed doc %s (%d chunks)", path, len(chunks))

    if buffer is None:
        conn.commit()
    set_cursor(conn, repo, "docs", head)
    return len(changed) + len(removed)
