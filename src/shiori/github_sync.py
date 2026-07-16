'''Data fetching and sync (detailed design/01).

Decisions:
- docs: git clone/pull; changed files re-chunked/re-embedded; deleted files removed from index.
- issue/PR: REST API cross-repo endpoints + `since` (updated_at cursor) incremental sync.
- Bot comments excluded; allowlist via SHIORI_INDEX_BOT_LOGINS (issue #25).
- PR diffs not indexed; review comments include diff_hunk as context.
- code: shares same clone; sha-delta re-indexes only changed files (issue #33).
- PR change file maps: metadata only; content delegated to GitHub MCP (issue #54).
- Bulk path: ChunkBuffer batches across files, bulk-inserts chunks, coarsens commits (issue #72).
- _git_fetch_ref / _git_delete_ref: PR head file primitives (issue #81).
Auth via TokenProvider (detailed design/09); git via http.extraHeader; API via httpx Auth hook.
'''

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import re
import subprocess
from collections.abc import Iterator
from datetime import datetime, timezone
import uuid

import httpx
import psycopg

from .chunking import (
    _detect_prog_lang,
    detect_language,
    split_code,
    split_issue_text,
    split_markdown,
)
from .config import Settings
from .db import (
    bulk_insert_chunks,
    delete_chunks_by_key,
    get_cursor,
    get_pr_head_sha,
    insert_chunk,
    set_cursor,
    upsert_pr_changes,
)
from .embedding import Embedder
from .github_auth import TokenProvider

log = logging.getLogger(__name__)

API = "https://api.github.com"


def _is_bot(user: dict | None) -> bool:
    if not user:
        return False
    login = (user.get("login") or "").lower()
    return user.get("type") == "Bot" or login.endswith("[bot]")


def _should_index(is_bot: bool, author: str | None, settings: Settings) -> bool:
    """Allow indexing even for bot comments if login is in allowlist (issue #25)."""
    if not is_bot:
        return True
    if author and author.lower() in settings.index_bot_logins:
        return True
    return False


# Regex for control char removal: matches control chars (0x00-0x1F) except newline (\n=0x0A) and tab (\t=0x09)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0B-\x1F]")


def _clean_text(s: str | None) -> str:
    """Normalize control characters from GitHub API text (issue #73).
"""
    if not s:
        return ""
    return _CONTROL_CHARS_RE.sub("", s)


# ---------------------------------------------------------------------------
# ChunkBuffer: batch embed/insert buffer for bulk path (issue #72)
# ---------------------------------------------------------------------------

class ChunkBuffer:
    """Accumulate chunks, batch-embed, bulk-insert, coarse-commit for high throughput.
    Incremental path unused; initial/rebuild only (detailed design/01, 02, 10).
"""

    def __init__(
        self,
        conn: psycopg.Connection,
        embedder: Embedder,
        batch_size: int = 500,
    ):
        self._conn = conn
        self._embedder = embedder
        self._batch_size = batch_size
        self._items: list[dict] = []
        self._texts: list[str] = []

    def add(self, *, chunk_key: str, chunk_index: int, source_type: str,
            repo: str, content: str,
            path: str | None = None, issue_no: int | None = None,
            comment_id: int | None = None, kind: str | None = None,
            language: str | None = None, heading_path: str | None = None,
            state: str | None = None, author: str | None = None,
            line: int | None = None,
            end_line: int | None = None, commit_sha: str | None = None,
            prog_lang: str | None = None, symbols: str | None = None,
            created_at=None, updated_at=None, url: str | None = None,
    ) -> None:
        """Add chunk to buffer. Auto-flushes at batch_size."""
        self._items.append({
            "chunk_key": chunk_key, "chunk_index": chunk_index,
            "source_type": source_type, "repo": repo, "content": content,
            "path": path, "issue_no": issue_no, "comment_id": comment_id,
            "kind": kind,
            "language": language, "heading_path": heading_path,
            "state": state, "author": author, "line": line,
            "end_line": end_line, "commit_sha": commit_sha,
            "prog_lang": prog_lang, "symbols": symbols,
            "created_at": created_at, "updated_at": updated_at,
            "url": url,
            # Embedding placeholder (filled at flush time)
            "embedding": None,
        })
        self._texts.append(content)
        if len(self._items) >= self._batch_size:
            self.flush()

    def flush(self) -> int:
        """Flush buffer: batch embed → bulk insert → commit. Returns insert count."""
        if not self._items:
            return 0
        n = len(self._items)
        vectors = self._embedder.embed_passages(
            self._texts, batch_size=min(len(self._texts), 256),
        )
        for item, vec in zip(self._items, vectors):
            item["embedding"] = vec
        bulk_insert_chunks(self._conn, self._items)
        self._conn.commit()
        log.info("ChunkBuffer flushed: %d chunks", n)
        self._items.clear()
        self._texts.clear()
        return n


# ---------------------------------------------------------------------------
# docs (git)
# ---------------------------------------------------------------------------


def _redact(text: str) -> str:
    """Mask auth credentials embedded in URLs (x-access-token:...@ etc.)."""
    return re.sub(r"https://[^@\s/]+@", "https://", text)


def _git(args: list[str], cwd: str | None = None) -> str:
    # When cwd is specified, explicitly set safe.directory for security.
    # app/ingest (root) runs in the container where /data/repos is mounted;
    # prevents git dubious ownership errors (issue #48).
    cmd = ["git"]
    if cwd:
        cmd += ["-c", f"safe.directory={cwd}"]
    cmd += args
    out = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if out.returncode != 0:
        err = _redact(out.stderr.strip())
        hint = ""
        if "Authentication failed" in err or "could not read Username" in err:
            hint = ("Private repos require GITHUB_TOKEN."
                    "For public repos, verify the repository name.")
        raise RuntimeError(
            f"git {args[0]} failed (exit {out.returncode}): {err}{hint}"
        )
    return out.stdout.strip()


def _auth_args(provider: TokenProvider) -> list[str]:
    """Return git auth header as `-c http.extraHeader=...` args.
    Token clipped from clone URL.

    NOTE: http.extraHeader is not used in practice because it fails in some
    git versions. Use _authed_url() instead for clone/fetch operations.
    Kept for compatibility."""
    return []


def _authed_url(remote: str, provider: TokenProvider) -> str:
    """Return remote URL with token embedded (x-access-token scheme).
    Falls back to the original URL if no token is available.
    The token is redacted from logs by _redact()."""
    token = provider.get_token()
    if not token:
        return remote
    return remote.replace("https://", f"https://x-access-token:{token}@", 1)


def _git_fetch_ref(
    ref: str,
    cwd: str | None = None,
    provider: TokenProvider | None = None,
    tmp_ref: str | None = None,
) -> str:
    """Shallow-fetch a ref and return a temp ref name (issue #81).
    tmp_ref=None skips fetch. Returns SHA of fetched ref."""
    resolved = tmp_ref or f"refs/shiori/tmp-{uuid.uuid4().hex}"
    if provider:
        remote = _git(["remote", "get-url", "origin"], cwd=cwd)
        authed = _authed_url(remote, provider)
        _git(["remote", "set-url", "origin", authed], cwd=cwd)
        try:
            _git(["fetch", "origin", f"{ref}:{resolved}", "--depth=1"], cwd=cwd)
        finally:
            _git(["remote", "set-url", "origin", remote], cwd=cwd)
    else:
        _git(["fetch", "origin", f"{ref}:{resolved}", "--depth=1"], cwd=cwd)
    return resolved


def _git_delete_ref(tmp_ref: str, cwd: str | None = None) -> None:
    """Delete a temporary ref (issue #81). No-op if not found."""
    try:
        _git(["update-ref", "-d", tmp_ref], cwd=cwd)
    except RuntimeError:
        pass  # Already deleted etc.


class _GitHubAuth(httpx.Auth):
    """httpx Auth hook. Gets token from provider per request.
    Refreshes token near expiry to survive long ingests."""

    def __init__(self, provider: TokenProvider) -> None:
        self._provider = provider

    def auth_flow(self, request):
        token = self._provider.get_token()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request


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
                """
                INSERT INTO doc_files (repo, path, content_sha, language)
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


# ---------------------------------------------------------------------------
# code (shares same git clone, sha delta)
# ---------------------------------------------------------------------------

# Directory names to skip in os.walk
_EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    ".venv", "venv",
    "dist", "build",
    "__pycache__",
    ".tox", ".eggs",
    ".next",
    "target",
    ".cache",
}

# Directory name suffixes to skip in os.walk (build-output dirs that don't
# match _EXCLUDE_DIRS exactly, e.g. "dashboard_dist"; issue #235)
_EXCLUDE_DIR_SUFFIXES = ("_dist", "-dist")

# Longest line (chars) a hand-written source file is expected to have.
# Minified/bundled JS routinely puts an entire file on one line; filename-based
# detection (".min.js") misses non-suffixed bundles like Vite's "index-*.js".
_MINIFIED_LINE_THRESHOLD = 500


def _looks_minified(content: bytes) -> bool:
    """Heuristic: a single very long line strongly suggests a minified bundle."""
    try:
        sample = content[:8192].decode("utf-8", errors="ignore")
    except Exception:
        return False
    return any(len(line) > _MINIFIED_LINE_THRESHOLD for line in sample.splitlines())


def _is_excluded_dir(name: str) -> bool:
    """Directory should be pruned from os.walk (issue #235)."""
    return name in _EXCLUDE_DIRS or name.endswith(_EXCLUDE_DIR_SUFFIXES)

# File extensions excluded from code indexing (binary/asset etc.)
_EXCLUDE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pyc", ".pyo",
    ".so", ".dylib", ".dll", ".wasm",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".pdf",
    ".lock",
    ".min.js", ".min.css",
}


def _is_code_file(filename: str, settings: Settings) -> bool:
    """Determine if the relative path is a code file that should be indexed.
"""
    lower = filename.lower()
    if lower.endswith((".md", ".mdx", ".markdown")):
        return False
    if any(lower.endswith(ext) for ext in _EXCLUDE_EXTENSIONS):
        return False
    if settings.code_extensions:
        return any(lower.endswith(ext) for ext in settings.code_extensions)
    return True


def _is_excluded_by_glob(rel_path: str, settings: Settings) -> bool:
    """Check if path matches excluded glob patterns."""
    for pattern in settings.code_exclude_globs:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
    return False


def sync_code(
    settings: Settings,
    conn: psycopg.Connection,
    embedder: Embedder,
    repo: str,
    provider: TokenProvider,
    buffer: ChunkBuffer | None = None,
) -> int:
    """Sync source code; index only changed files (detailed design/10 Step 3).
    Shares clone with sync_docs."""
    if not settings.index_code:
        return 0

    repo_dir = settings.repo_dir(repo)

    if not os.path.isdir(repo_dir):
        log.warning("sync_code: clone not found (sync_docs not run?)")
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
            log.warning("sync_code: skip %s (%s)", path, exc)
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
                """
                INSERT INTO doc_files (repo, path, content_sha, language, kind)
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


# ---------------------------------------------------------------------------
# issues / PR (GitHub API)
# ---------------------------------------------------------------------------

def _api_pages(client: httpx.Client, url: str, params: dict) -> "list[dict]":
    """Paginate all pages via Link header."""
    items: list[dict] = []
    next_params: dict | None = params
    next_url: str | None = url
    while next_url:
        resp = client.get(next_url, params=next_params)
        resp.raise_for_status()
        items.extend(resp.json())
        next_url = resp.links.get("next", {}).get("url")  # type: ignore[assignment]
        next_params = None   # None not {}; preserves next URL query params as-is
    return items


def _api_pages_gen(client: httpx.Client, url: str, params: dict) -> "Iterator[list[dict]]":
    """Yield one page at a time via Link header.
    Page-at-a-time processing avoids idle-in-transaction timeout on large repos
    and enables per-page cursor updates for resume on interruption (issue #250).
    """
    next_params: dict | None = params
    next_url: str | None = url
    while next_url:
        resp = client.get(next_url, params=next_params)
        resp.raise_for_status()
        yield resp.json()
        next_url = resp.links.get("next", {}).get("url")
        next_params = None


def _upsert_issue_item(conn: psycopg.Connection, row: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO issue_items (
                repo, issue_no, comment_id, kind, title, author, is_bot,
                state, path, line, body, url, created_at, updated_at
            ) VALUES (
                %(repo)s, %(issue_no)s, %(comment_id)s, %(kind)s, %(title)s,
                %(author)s, %(is_bot)s, %(state)s, %(path)s, %(line)s,
                %(body)s, %(url)s, %(created_at)s, %(updated_at)s
            )
            ON CONFLICT (repo, issue_no, comment_id) DO UPDATE SET
                kind = EXCLUDED.kind, title = EXCLUDED.title,
                author = EXCLUDED.author, is_bot = EXCLUDED.is_bot,
                state = EXCLUDED.state, path = EXCLUDED.path,
                line = EXCLUDED.line, body = EXCLUDED.body,
                url = EXCLUDED.url, created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at
            """,
            row,
        )


def _issue_title_state_kind(
    conn: psycopg.Connection, repo: str, issue_no: int
) -> tuple[str | None, str | None, str | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT title, state, kind FROM issue_items "
            "WHERE repo = %s AND issue_no = %s AND comment_id = 0",
            (repo, issue_no),
        )
        row = cur.fetchone()
    return (row[0], row[1], row[2]) if row else (None, None, None)


def _propagate_issue_state(
    conn: psycopg.Connection, repo: str, issue_no: int, state: str | None
) -> None:
    """Propagate issue_items state changes to chunks (issue #56).
"""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE chunks SET state = %s WHERE repo = %s AND issue_no = %s",
            (state, repo, issue_no),
        )
        if cur.rowcount:
            log.debug(
                "propagated state=%s to %d chunks (repo=%s, issue_no=%d)",
                state, cur.rowcount, repo, issue_no,
            )


def _index_item(
    settings: Settings,
    conn: psycopg.Connection,
    embedder: Embedder,
    *,
    chunk_key: str,
    source_type: str,
    repo: str,
    issue_no: int,
    comment_id: int | None,
    kind: str | None,
    title: str | None,
    body: str,
    state: str | None,
    author: str | None,
    path: str | None,
    line: int | None,
    created_at,
    updated_at,
    url: str | None,
    buffer: ChunkBuffer | None = None,
) -> None:
    chunks = split_issue_text(title, body, settings.chunk_max_chars)
    delete_chunks_by_key(conn, chunk_key)
    if not chunks:
        return
    language = detect_language((title or "") + "\n" + (body or ""))
    if buffer is not None:
        for c in chunks:
            buffer.add(
                chunk_key=chunk_key,
                chunk_index=c.chunk_index,
                source_type=source_type,
                repo=repo,
                path=path,
                issue_no=issue_no,
                comment_id=comment_id,
                kind=kind,
                language=language,
                content=c.content,
                state=state,
                author=author,
                line=line,
                created_at=created_at,
                updated_at=updated_at,
                url=url,
            )
    else:
        vectors = embedder.embed_passages([c.content for c in chunks])
        for c, v in zip(chunks, vectors):
            insert_chunk(
                conn,
                chunk_key=chunk_key,
                chunk_index=c.chunk_index,
                source_type=source_type,
                repo=repo,
                path=path,
                issue_no=issue_no,
                comment_id=comment_id,
                kind=kind,
                language=language,
                content=c.content,
                embedding=v,
                state=state,
                author=author,
                line=line,
                created_at=created_at,
                updated_at=updated_at,
                url=url,
            )


def _sync_pr_changes(
    client: httpx.Client,
    conn: psycopg.Connection,
    repo: str,
    issue_no: int,
) -> None:
    """Sync PR change file maps (issue #54).
    GET /repos/{repo}/pulls/{issue_number}/files"""
    # 1. Fetch PR details to get head_sha and base_sha
    try:
        resp = client.get(f"{API}/repos/{repo}/pulls/{issue_no}")
        resp.raise_for_status()
        pr_data = resp.json()
        head_sha = pr_data.get("head", {}).get("sha")
        base_sha = pr_data.get("base", {}).get("sha")
        if not head_sha:
            log.debug("PR #%d: could not obtain head_sha", issue_no)
            return
    except httpx.HTTPError as exc:
        log.warning("PR #%d: failed to fetch details: %s", issue_no, exc)
        return

    # 2. Skip if head_sha unchanged
    prev_sha = get_pr_head_sha(conn, repo, issue_no)
    if prev_sha == head_sha:
        log.debug("PR #%d: head_sha unchanged, skipping", issue_no)
        return

    # 3. Fetch file list
    try:
        files = _api_pages(
            client,
            f"{API}/repos/{repo}/pulls/{issue_no}/files",
            {"per_page": 100},
        )
        log.debug("PR #%d: %d files fetched", issue_no, len(files))
    except httpx.HTTPError as exc:
        log.warning("PR #%d: failed to fetch file list: %s", issue_no, exc)
        return

    # 4. upsert
    upsert_pr_changes(conn, repo, issue_no, head_sha, base_sha, files)
    log.info("PR #%d: updated change file map (head_sha=%s, base_sha=%s, %d files)",
             issue_no, head_sha[:7], (base_sha or "?")[:7], len(files))


def _sync_pr_reviews(
    client: httpx.Client,
    conn: psycopg.Connection,
    embedder: Embedder,
    settings: Settings,
    repo: str,
    issue_no: int,
    buffer: ChunkBuffer | None = None,
) -> None:
    """Sync PR review submissions from pulls/{issue_no}/reviews (issue #103).

    Review submissions (body + state like COMMENTED/APPROVED/CHANGES_REQUESTED)
    are not returned by pulls/comments (which only has inline review comments).
    Store with comment_id = -(review_id) to avoid collision with inline reviews.
    """
    try:
        reviews = _api_pages(
            client,
            f"{API}/repos/{repo}/pulls/{issue_no}/reviews",
            {"per_page": 100},
        )
    except httpx.HTTPError as exc:
        log.info("PR #%d: could not fetch reviews: %s", issue_no, exc)
        return

    if not reviews:
        return

    pr_state = _issue_title_state_kind(conn, repo, issue_no)[1]

    for r in reviews:
        rid = r["id"]
        author = (r.get("user") or {}).get("login")
        is_bot = _is_bot(r.get("user"))
        state = r.get("state")
        body = _clean_text(r.get("body") or "")
        submitted_at = r.get("submitted_at")

        _upsert_issue_item(conn, {
            "repo": repo,
            "issue_no": issue_no,
            "comment_id": -rid,
            "kind": "pr_review",
            "title": None,
            "author": author,
            "is_bot": is_bot,
            "state": state,
            "path": None,
            "line": None,
            "body": body,
            "url": r.get("html_url"),
            "created_at": submitted_at,
            "updated_at": submitted_at,
        })

        if body and _should_index(is_bot, author, settings):
            _index_item(
                settings, conn, embedder,
                chunk_key=f"pr_review_submission:{repo}:{issue_no}:r{rid}",
                source_type="pr_review",
                repo=repo, issue_no=issue_no, comment_id=-rid,
                kind="pr",
                title=None,
                body=f"[{state}] {body}" if state else body,
                state=pr_state, author=author,
                path=None, line=None,
                created_at=submitted_at, updated_at=submitted_at,
                url=r.get("html_url"),
                buffer=buffer,
            )


def sync_issues(
    settings: Settings,
    conn: psycopg.Connection,
    embedder: Embedder,
    repo: str,
    provider: TokenProvider,
    buffer: ChunkBuffer | None = None,
) -> int:
    """Incremental sync of issues/PRs/comments/reviews.
    When buffer specified (bulk path), uses ChunkBuffer for batch embedding."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    n_indexed = 0

    with httpx.Client(
        headers=headers, auth=_GitHubAuth(provider), timeout=30.0
    ) as client:
        # --- Body (issues endpoint includes PRs) ---
        since = get_cursor(conn, repo, "issues")
        params = {
            "state": "all",
            "sort": "updated",
            "direction": "asc",
            "per_page": 100,
        }
        if since:
            params["since"] = since
        for page in _api_pages_gen(client, f"{API}/repos/{repo}/issues", params):
            if not page:
                break
            for it in page:
                no = it["number"]
                kind = "pr" if "pull_request" in it else "issue"
                author = (it.get("user") or {}).get("login")
                title = _clean_text(it.get("title"))
                body = _clean_text(it.get("body") or "")
                row = {
                    "repo": repo,
                    "issue_no": no,
                    "comment_id": 0,
                    "kind": kind,
                    "title": title,
                    "author": author,
                    "is_bot": _is_bot(it.get("user")),
                    "state": it.get("state"),
                    "path": None,
                    "line": None,
                    "body": body,
                    "url": it.get("html_url"),
                    "created_at": it.get("created_at"),
                    "updated_at": it.get("updated_at"),
                }
                _upsert_issue_item(conn, row)
                _propagate_issue_state(conn, repo, no, it.get("state"))
                if _should_index(row["is_bot"], author, settings):
                    _index_item(
                        settings, conn, embedder,
                        chunk_key=f"issue:{repo}:{no}:body",
                        source_type="issue",
                        repo=repo, issue_no=no, comment_id=None,
                        kind=kind,
                        title=title, body=body,
                        state=it.get("state"), author=author,
                        path=None, line=None,
                        created_at=it.get("created_at"),
                        updated_at=it.get("updated_at"),
                        url=it.get("html_url"),
                        buffer=buffer,
                    )
                    n_indexed += 1
                # Sync change file maps and review submissions for PRs
                if kind == "pr":
                    _sync_pr_changes(client, conn, repo, no)
                    _sync_pr_reviews(client, conn, embedder, settings, repo, no, buffer=buffer)
            if buffer is None:
                conn.commit()
            set_cursor(conn, repo, "issues", page[-1]["updated_at"])

        if get_cursor(conn, repo, "issues") is None:
            set_cursor(conn, repo, "issues", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

        # --- Issue/PR comments ---
        since = get_cursor(conn, repo, "issue_comments")
        params = {"sort": "updated", "direction": "asc", "per_page": 100}
        if since:
            params["since"] = since
        for page in _api_pages_gen(client, f"{API}/repos/{repo}/issues/comments", params):
            if not page:
                break
            for c in page:
                no = int(c["issue_url"].rstrip("/").rsplit("/", 1)[-1])
                title, state, issue_kind = _issue_title_state_kind(conn, repo, no)
                author = (c.get("user") or {}).get("login")
                is_bot = _is_bot(c.get("user"))
                body = _clean_text(c.get("body") or "")
                _upsert_issue_item(conn, {
                    "repo": repo, "issue_no": no, "comment_id": c["id"],
                    "kind": "comment", "title": None, "author": author,
                    "is_bot": is_bot, "state": state, "path": None, "line": None,
                    "body": body, "url": c.get("html_url"),
                    "created_at": c.get("created_at"), "updated_at": c.get("updated_at"),
                })
                if _should_index(is_bot, author, settings):
                    _index_item(
                        settings, conn, embedder,
                        chunk_key=f"issue:{repo}:{no}:c{c['id']}",
                        source_type="issue",
                        repo=repo, issue_no=no, comment_id=c["id"],
                        kind=issue_kind,
                        title=title, body=body,
                        state=state, author=author, path=None, line=None,
                        created_at=c.get("created_at"), updated_at=c.get("updated_at"),
                        url=c.get("html_url"),
                        buffer=buffer,
                    )
                    n_indexed += 1
            if buffer is None:
                conn.commit()
            set_cursor(conn, repo, "issue_comments", page[-1]["updated_at"])

        if get_cursor(conn, repo, "issue_comments") is None:
            set_cursor(conn, repo, "issue_comments", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

        # --- PR review comments (with path/line/diff_hunk) ---
        since = get_cursor(conn, repo, "pr_review_comments")
        params = {"sort": "updated", "direction": "asc", "per_page": 100}
        if since:
            params["since"] = since
        for page in _api_pages_gen(client, f"{API}/repos/{repo}/pulls/comments", params):
            if not page:
                break
            for c in page:
                no = int(c["pull_request_url"].rstrip("/").rsplit("/", 1)[-1])
                title, state, _ = _issue_title_state_kind(conn, repo, no)
                author = (c.get("user") or {}).get("login")
                is_bot = _is_bot(c.get("user"))
                line = c.get("line") or c.get("original_line")
                # Append diff_hunk to body as context (within decision not to index diffs themselves)
                body = _clean_text(c.get("body") or "")
                diff_hunk = c.get("diff_hunk")
                if diff_hunk:
                    body = f"{body}\n\n```diff\n{_clean_text(diff_hunk)}\n```"
                _upsert_issue_item(conn, {
                    "repo": repo, "issue_no": no, "comment_id": c["id"],
                    "kind": "pr_review_comment", "title": None, "author": author,
                    "is_bot": is_bot, "state": state,
                    "path": c.get("path"), "line": line,
                    "body": body, "url": c.get("html_url"),
                    "created_at": c.get("created_at"), "updated_at": c.get("updated_at"),
                })
                if _should_index(is_bot, author, settings):
                    _index_item(
                        settings, conn, embedder,
                        chunk_key=f"pr_review:{repo}:{no}:rc{c['id']}",
                        source_type="pr_review",
                        repo=repo, issue_no=no, comment_id=c["id"],
                        kind="pr",
                        title=title, body=body,
                        state=state, author=author,
                        path=c.get("path"), line=line,
                        created_at=c.get("created_at"), updated_at=c.get("updated_at"),
                        url=c.get("html_url"),
                        buffer=buffer,
                    )
                    n_indexed += 1
            if buffer is None:
                conn.commit()
            set_cursor(conn, repo, "pr_review_comments", page[-1]["updated_at"])

        if get_cursor(conn, repo, "pr_review_comments") is None:
            set_cursor(conn, repo, "pr_review_comments", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    return n_indexed
