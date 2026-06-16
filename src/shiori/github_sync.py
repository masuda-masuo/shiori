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
- PR 変更ファイルマップはメタデータのみ同期・保持し、コンテンツ（patch）は GitHub MCP に委譲する（issue #54）。
- 初回／rebuild のバルク経路では、ChunkBuffer により埋め込みをファイル横断でバッチ化し、
  チャンク挿入をバルク化＋commit を粗くする（issue #72）。
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
    """bot でも allowlist に含まれていれば索引対象とする（issue #25）。"""
    if not is_bot:
        return True
    if author and author.lower() in settings.index_bot_logins:
        return True
    return False


# 制御文字除去用の正規表現: 改行(\n=0x0A)とタブ(\t=0x09)以外の制御文字(0x00-0x1F)にマッチ
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0B-\x1F]")


def _clean_text(s: str | None) -> str:
    """GitHub API から取得したテキストの制御文字を正規化する（issue #73）。

    PostgreSQL の text 型は NUL (0x00) を格納できず、埋め込みモデルの
    入力としても制御文字はノイズとなるため、改行・タブ以外の制御文字を除去する。
    """
    if not s:
        return ""
    return _CONTROL_CHARS_RE.sub("", s)


# ---------------------------------------------------------------------------
# ChunkBuffer: バルク経路用の埋め込み・挿入バッファ（issue #72）
# ---------------------------------------------------------------------------