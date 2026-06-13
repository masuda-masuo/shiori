"""データ取り込みと同期（詳細設計/01）。

決定事項:
- docs は git clone / pull。ファイル単位の content ハッシュを doc_files に保持し、
  変化したファイルだけ再チャンク・再埋め込みする。削除されたファイルの索引も消す。
- issue/PR は REST API の repo 横断エンドポイント＋ `since`（updated_at カーソル）で差分同期:
    - GET /repos/{o}/{r}/issues            (PR を含む。本文)
    - GET /repos/{o}/{r}/issues/comments    (issue/PR コメント)
    - GET /repos/{o}/{r}/pulls/comments     (レビューコメント。path/line/diff_hunk 付き)
- bot コメント（user.type == "Bot" または login が "[bot]" で終わる）は索引から除外する。
  ただし SHIORI_INDEX_BOT_LOGINS に列挙された login は allowlist として索引対象にする（issue #25）。
  生データは issue_items に is_bot=true で保持する（read_issue では表示する）。
- PR の diff 自体は索引しない。レビューコメントには diff_hunk を文脈として付与する。
- code は sync_docs と同一クローンを共有し、sha デルタで変化ファイルのみ再索引する（issue #33）。
認証は TokenProvider 抽象経由（詳細設計/09）。git は http.extraHeader でトークンを注入し、
API は httpx の Auth フックでリクエスト毎に注入する。
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import logging
import os
import re
import subprocess

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
from .db import delete_chunks_by_key, get_cursor, insert_chunk, set_cursor
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
    """bot でも allowlist に含まれていれば索引対象とする（issue #25）。"""
    if not is_bot:
        return True
    if author and author.lower() in settings.index_bot_logins:
        return True
    return False


# ---------------------------------------------------------------------------
# docs (git)
# ---------------------------------------------------------------------------


def _redact(text: str) -> str:
    """URL に埋め込まれた認証情報（x-access-token:...@ 等）をマスクする。"""
    return re.sub(r"https://[^@\s/]+@", "https://", text)


def _git(args: list[str], cwd: str | None = None) -> str:
    # cwd が指定されている場合、安全のため safe.directory を明示的に設定する。
    # app/ingest（root）と runner（非root）が /data/repos を共有する構成で
    # git の dubious ownership エラーを防ぐ（issue #48）。
    cmd = ["git"]
    if cwd:
        cmd += ["-c", f"safe.directory={cwd}"]
    cmd += args
    out = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if out.returncode != 0:
        err = _redact(out.stderr.strip())
        hint = ""
        if "Authentication failed" in err or "could not read Username" in err:
            hint = ("（private リポジトリには GITHUB_TOKEN が必要です。"
                    "公開リポジトリの場合はリポジトリ名を確認してください）")
        raise RuntimeError(
            f"git {args[0]} failed (exit {out.returncode}): {err}{hint}"
        )
    return out.stdout.strip()


def _auth_args(provider: TokenProvider) -> list[str]:
    """git の認証ヘッダを `-c http.extraHeader=...` 引数として返す。

    トークンを clone URL に埋め込むと `.git/config` に平文で永続化され、短期トークンでは
    次回 fetch 時に失効済みトークンが残る。毎回ヘッダで注入することでこれを避ける。
    認証不要（匿名）なら空リストを返す。
    """
    token = provider.get_token()
    if not token:
        return []
    b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {b64}"]


class _GitHubAuth(httpx.Auth):
    """httpx の Auth フック。リクエストごとに provider からトークンを得て注入する。

    長時間の ingest でも、ページネーション途中で provider が自動再発行するため失効しない。
    """

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
) -> int:
    """リポジトリの Markdown を同期し、変化分だけ索引する。返り値は更新ファイル数。"""
    repo_dir = settings.repo_dir(repo)
    remote = f"https://github.com/{repo}.git"
    auth = _auth_args(provider)
    if os.path.isdir(os.path.join(repo_dir, ".git")):
        # 旧方式でトークン入り URL が .git/config に残っていても上書きする（冪等）。
        _git(["remote", "set-url", "origin", remote], cwd=repo_dir)
        _git(auth + ["fetch", "--depth=1", "origin"], cwd=repo_dir)
        _git(["reset", "--hard", "origin/HEAD"], cwd=repo_dir)
    else:
        os.makedirs(os.path.dirname(repo_dir), exist_ok=True)
        _git(auth + ["clone", "--depth=1", remote, repo_dir])
    head = _git(["rev-parse", "HEAD"], cwd=repo_dir)

    # 現在のファイル集合と既存索引を突き合わせる
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
        language = detect_language(text)
        chunks = split_markdown(text, settings.chunk_max_chars)
        chunk_key = f"doc:{repo}:{path}"
        delete_chunks_by_key(conn, chunk_key)
        if chunks:
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
        conn.commit()
        log.info("indexed doc %s (%d chunks)", path, len(chunks))

    conn.commit()
    set_cursor(conn, repo, "docs", head)
    return len(changed) + len(removed)


# ---------------------------------------------------------------------------
# code（git 同一クローン共有、sha デルタ）
# ---------------------------------------------------------------------------

# os.walk でスキップするディレクトリ名
_EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    ".venv", "venv",
    "dist", "build",
    "__pycache__",
    ".tox", ".eggs",
    ".next",
    "target",
}

# コード索引から除外するファイル拡張子（バイナリ・アセット等）
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
    """コード索引対象のファイルか判定する。

    - ドキュメント拡張子（.md / .mdx / .markdown）は除外
    - バイナリ・アセット拡張子は除外
    - SHIORI_CODE_EXTENSIONS が設定されていれば、その拡張子のみ対象
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
    """除外 glob パターンにマッチするか。"""
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
) -> int:
    """リポジトリのソースコードを同期し、変化分だけ索引する（詳細設計/10 Step 3）。

    sync_docs と同一クローンを共有するため、git pull は sync_docs 側で完了済み。
    sha デルタで変化したファイルのみ再チャンク・再埋め込みする。

    Returns
    -------
    int
        更新（追加・変更・削除）したファイル数。
    """
    if not settings.index_code:
        return 0

    repo_dir = settings.repo_dir(repo)

    if not os.path.isdir(repo_dir):
        log.warning("sync_code: クローンが存在しません（sync_docs 未実行?）")
        return 0

    head = _git(["rev-parse", "HEAD"], cwd=repo_dir)

    # 現在のコードファイル集合（sha 付き）
    current: dict[str, str] = {}
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS]
        for f in files:
            abspath = os.path.join(root, f)
            rel = os.path.relpath(abspath, repo_dir)
            if not _is_code_file(f, settings):
                continue
            if _is_excluded_by_glob(rel, settings):
                continue
            with open(abspath, "rb") as fp:
                current[rel] = hashlib.sha256(fp.read()).hexdigest()

    # 既存索引（kind='code' の doc_files 行）
    with conn.cursor() as cur:
        cur.execute(
            "SELECT path, content_sha FROM doc_files WHERE repo = %s AND kind = 'code'",
            (repo,),
        )
        indexed = dict(cur.fetchall())

    removed = set(indexed) - set(current)
    changed = [p for p, sha in current.items() if indexed.get(p) != sha]

    # 削除
    for path in removed:
        delete_chunks_by_key(conn, f"code:{repo}:{path}")
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM doc_files WHERE repo = %s AND path = %s AND kind = 'code'",
                (repo, path),
            )
        log.info("removed code %s", path)

    default_branch = _git(
        ["rev-parse", "--abbrev-ref", "origin/HEAD"], cwd=repo_dir
    ).split("/")[-1]

    # 変更・追加ファイルの再索引
    for path in changed:
        abspath = os.path.join(repo_dir, path)
        try:
            with open(abspath, encoding="utf-8", errors="replace") as fp:
                text = fp.read()
        except Exception as exc:
            log.warning("sync_code: skip %s (%s)", path, exc)
            continue

        chunks = split_code(path, text, settings.chunk_max_chars)
        chunk_key = f"code:{repo}:{path}"
        delete_chunks_by_key(conn, chunk_key)

        if chunks:
            prog_lang = _detect_prog_lang(path)
            vectors = embedder.embed_passages([c.content for c in chunks])
            for c, v in zip(chunks, vectors):
                # URL は行範囲付き
                url = (
                    f"https://github.com/{repo}/blob/{default_branch}/{path}"
                    f"#L{c.start_line}-L{c.end_line}"
                    if c.start_line and c.end_line
                    else f"https://github.com/{repo}/blob/{default_branch}/{path}"
                )
                insert_chunk(
                    conn,
                    chunk_key=chunk_key,
                    chunk_index=c.chunk_index,
                    source_type="code",
                    repo=repo,
                    path=path,
                    language=None,  # code は language=NULL（決定4）
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

        # doc_files に kind='code' で記録
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
        conn.commit()
        log.info("indexed code %s (%d chunks, %s)", path, len(chunks), prog_lang or "?")

    conn.commit()
    return len(changed) + len(removed)


# ---------------------------------------------------------------------------
# issues / PR (GitHub API)
# ---------------------------------------------------------------------------


def _api_pages(
    client: httpx.Client, url: str, params: dict
) -> "list[dict]":
    """Link ヘッダに従って全ページを集める。"""
    items: list[dict] = []
    while url:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        items.extend(resp.json())
        url = resp.links.get("next", {}).get("url")
        params = {}  # next URL に含まれる
    return items


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


def _issue_title_state(
    conn: psycopg.Connection, repo: str, issue_no: int
) -> tuple[str | None, str | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT title, state FROM issue_items "
            "WHERE repo = %s AND issue_no = %s AND comment_id = 0",
            (repo, issue_no),
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


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
    title: str | None,
    body: str,
    state: str | None,
    author: str | None,
    path: str | None,
    line: int | None,
    created_at,
    updated_at,
    url: str | None,
) -> None:
    chunks = split_issue_text(title, body, settings.chunk_max_chars)
    delete_chunks_by_key(conn, chunk_key)
    if not chunks:
        return
    language = detect_language((title or "") + "\n" + (body or ""))
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


def sync_issues(
    settings: Settings,
    conn: psycopg.Connection,
    embedder: Embedder,
    repo: str,
    provider: TokenProvider,
) -> int:
    """issue / PR / コメント / レビューコメントを差分同期し索引する。"""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    n_indexed = 0

    with httpx.Client(
        headers=headers, auth=_GitHubAuth(provider), timeout=30.0
    ) as client:
        # --- 本文 (issues endpoint は PR も含む) ---
        since = get_cursor(conn, repo, "issues")
        params = {
            "state": "all",
            "sort": "updated",
            "direction": "asc",
            "per_page": 100,
        }
        if since:
            params["since"] = since
        items = _api_pages(client, f"{API}/repos/{repo}/issues", params)
        for it in items:
            no = it["number"]
            kind = "pr" if "pull_request" in it else "issue"
            author = (it.get("user") or {}).get("login")
            row = {
                "repo": repo,
                "issue_no": no,
                "comment_id": 0,
                "kind": kind,
                "title": it.get("title"),
                "author": author,
                "is_bot": _is_bot(it.get("user")),
                "state": it.get("state"),
                "path": None,
                "line": None,
                "body": it.get("body") or "",
                "url": it.get("html_url"),
                "created_at": it.get("created_at"),
                "updated_at": it.get("updated_at"),
            }
            _upsert_issue_item(conn, row)
            if _should_index(row["is_bot"], author, settings):
                _index_item(
                    settings, conn, embedder,
                    chunk_key=f"issue:{repo}:{no}:body",
                    source_type="issue",
                    repo=repo, issue_no=no, comment_id=None,
                    title=it.get("title"), body=it.get("body") or "",
                    state=it.get("state"), author=author,
                    path=None, line=None,
                    created_at=it.get("created_at"),
                    updated_at=it.get("updated_at"),
                    url=it.get("html_url"),
                )
                n_indexed += 1
            conn.commit()
        if items:
            set_cursor(conn, repo, "issues", items[-1]["updated_at"])

        # --- issue/PR コメント ---
        since = get_cursor(conn, repo, "issue_comments")
        params = {"sort": "updated", "direction": "asc", "per_page": 100}
        if since:
            params["since"] = since
        comments = _api_pages(client, f"{API}/repos/{repo}/issues/comments", params)
        for c in comments:
            no = int(c["issue_url"].rstrip("/").rsplit("/", 1)[-1])
            title, state = _issue_title_state(conn, repo, no)
            author = (c.get("user") or {}).get("login")
            is_bot = _is_bot(c.get("user"))
            _upsert_issue_item(conn, {
                "repo": repo, "issue_no": no, "comment_id": c["id"],
                "kind": "comment", "title": None, "author": author,
                "is_bot": is_bot, "state": state, "path": None, "line": None,
                "body": c.get("body") or "", "url": c.get("html_url"),
                "created_at": c.get("created_at"), "updated_at": c.get("updated_at"),
            })
            if _should_index(is_bot, author, settings):
                _index_item(
                    settings, conn, embedder,
                    chunk_key=f"issue:{repo}:{no}:c{c['id']}",
                    source_type="issue",
                    repo=repo, issue_no=no, comment_id=c["id"],
                    title=title, body=c.get("body") or "",
                    state=state, author=author, path=None, line=None,
                    created_at=c.get("created_at"), updated_at=c.get("updated_at"),
                    url=c.get("html_url"),
                )
                n_indexed += 1
            conn.commit()
        if comments:
            set_cursor(conn, repo, "issue_comments", comments[-1]["updated_at"])

        # --- PR レビューコメント (path/line/diff_hunk 付き) ---
        since = get_cursor(conn, repo, "pr_review_comments")
        params = {"sort": "updated", "direction": "asc", "per_page": 100}
        if since:
            params["since"] = since
        reviews = _api_pages(client, f"{API}/repos/{repo}/pulls/comments", params)
        for c in reviews:
            no = int(c["pull_request_url"].rstrip("/").rsplit("/", 1)[-1])
            title, state = _issue_title_state(conn, repo, no)
            author = (c.get("user") or {}).get("login")
            is_bot = _is_bot(c.get("user"))
            line = c.get("line") or c.get("original_line")
            # diff_hunk を文脈として本文に付与する（diff 自体は索引しない決定の範囲内）
            body = c.get("body") or ""
            if c.get("diff_hunk"):
                body = f"{body}\n\n```diff\n{c['diff_hunk']}\n```"
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
                    title=title, body=body,
                    state=state, author=author,
                    path=c.get("path"), line=line,
                    created_at=c.get("created_at"), updated_at=c.get("updated_at"),
                    url=c.get("html_url"),
                )
                n_indexed += 1
            conn.commit()
        if reviews:
            set_cursor(conn, repo, "pr_review_comments", reviews[-1]["updated_at"])

    return n_indexed
