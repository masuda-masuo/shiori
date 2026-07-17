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


def sync_docs(
    settings: Settings,
    conn: psycopg.Connection,
    embedder: Embedder,
    repo: str,
    provider: TokenProvider,
    buffer: ChunkBuffer | None = None,
) -> int:
    """Sync repo Markdown; index only changed files. Returns update count.
    When buffer specified (bulk path), uses ChunkBuffer for batch embedding."""
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
        log.info("indexed doc %s (%d chunks)", path, len(chunks))

    if buffer is None:
        conn.commit()
    set_cursor(conn, repo, "docs", head)
    return len(changed) + len(removed)
