"""github_sync のユニットテスト（issue #25, #54, #72）。

_should_index の allowlist 判定ロジック、_sync_pr_changes の head_sha 比較による
スキップ／再取得ロジック、ChunkBuffer のバッチ蓄積・フラッシュを検証する。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from shiori.config import Settings
from shiori.github_sync import (
    ChunkBuffer,
    _git,
    _is_bot,
    _propagate_issue_state,
    _should_index,
    _sync_pr_changes,
)


# ===================================================================
# ChunkBuffer（issue #72）
# ===================================================================


class TestChunkBuffer:
    """ChunkBuffer: チャンク蓄積→一括埋め込み→バルク挿入。"""

    def _mock_conn(self):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = MagicMock()
        return conn

    def _chunk_kwargs(self, **overrides):
        defaults = {
            "chunk_key": "doc:o/r:test.md",
            "chunk_index": 0,
            "source_type": "doc",
            "repo": "o/r",
            "content": "hello world",
            "path": "test.md",
            "issue_no": None,
            "comment_id": None,
            "language": "en",
            "heading_path": None,
            "state": None,
            "author": None,
            "line": None,
            "end_line": None,
            "commit_sha": None,
            "prog_lang": None,
            "symbols": None,
            "created_at": None,
            "updated_at": None,
            "url": None,
        }
        defaults.update(overrides)
        return defaults

    def test_add_then_flush_batches_embedding_and_insert(self):
        """add で蓄積し、flush で一括埋め込み＋バルク挿入＋commit。"""
        conn = self._mock_conn()
        embedder = MagicMock()
        embedder.embed_passages.return_value = [[0.1, 0.2], [0.3, 0.4]]

        with patch("shiori.github_sync.bulk_insert_chunks") as mock_bulk:
            buf = ChunkBuffer(conn, embedder, batch_size=500)

            buf.add(**self._chunk_kwargs(content="chunk1"))
            buf.add(**self._chunk_kwargs(chunk_key="doc:o/r:test2.md", content="chunk2"))

            n = buf.flush()

            assert n == 2
            # embed_passages が 1 回だけ呼ばれる（2 テキストをまとめて）
            embedder.embed_passages.assert_called_once()
            texts_arg = embedder.embed_passages.call_args[0][0]
            assert texts_arg == ["chunk1", "chunk2"]
            # bulk_insert_chunks が 1 回呼ばれる
            mock_bulk.assert_called_once()
            # commit が 1 回呼ばれる
            conn.commit.assert_called_once()

    def test_auto_flush_when_batch_size_reached(self):
        """batch_size に達すると自動で flush される。"""
        conn = self._mock_conn()
        embedder = MagicMock()
        embedder.embed_passages.return_value = [[0.1, 0.2], [0.3, 0.4]]

        with patch("shiori.github_sync.bulk_insert_chunks"):
            buf = ChunkBuffer(conn, embedder, batch_size=2)

            buf.add(**self._chunk_kwargs(content="a"))
            buf.add(**self._chunk_kwargs(chunk_key="k2", content="b"))
            # batch_size=2 到達 → 自動 flush
            assert embedder.embed_passages.call_count == 1
            assert conn.commit.call_count == 1

            # バッファは空のはず
            buf.add(**self._chunk_kwargs(chunk_key="k3", content="c"))
            assert len(buf._items) == 1  # まだ flush されていない

    def test_flush_empty_buffer_noops(self):
        """空バッファの flush は何もしない。"""
        conn = self._mock_conn()
        embedder = MagicMock()

        buf = ChunkBuffer(conn, embedder)

        n = buf.flush()

        assert n == 0
        embedder.embed_passages.assert_not_called()
        conn.commit.assert_not_called()

    def test_flush_returns_inserted_count(self):
        """flush の返り値は挿入件数。"""
        conn = self._mock_conn()
        embedder = MagicMock()
        embedder.embed_passages.return_value = [
            [0.1, 0.2], [0.3, 0.4], [0.5, 0.6],
        ]

        with patch("shiori.github_sync.bulk_insert_chunks"):
            buf = ChunkBuffer(conn, embedder, batch_size=500)

            for i in range(3):
                buf.add(**self._chunk_kwargs(
                    chunk_key=f"doc:o/r:file{i}.md",
                    chunk_index=i,
                    content=f"content {i}",
                ))

            n = buf.flush()
            assert n == 3


# ===================================================================
# _should_index
# ===================================================================


class TestShouldIndex:
    """_should_index の allowlist 判定（issue #25）。"""

    def test_non_bot_always_indexed(self):
        settings = Settings()
        assert _should_index(False, "alice", settings) is True

    def test_bot_without_allowlist_excluded(self):
        settings = Settings()
        assert _should_index(True, "dependabot[bot]", settings) is False

    def test_bot_in_allowlist_included(self):
        settings = Settings()
        settings.index_bot_logins = {"mcp-launcher-masuda[bot]"}
        assert _should_index(True, "mcp-launcher-masuda[bot]", settings) is True

    def test_bot_not_in_allowlist_excluded(self):
        settings = Settings()
        settings.index_bot_logins = {"mcp-launcher-masuda[bot]"}
        assert _should_index(True, "other[bot]", settings) is False

    def test_author_none_with_allowlist(self):
        """author が None の bot は allowlist にかかわらず除外。"""
        settings = Settings()
        settings.index_bot_logins = {"any[bot]"}
        assert _should_index(True, None, settings) is False

    def test_case_insensitive(self):
        """allowlist 判定は大文字小文字を無視する。"""
        settings = Settings()
        settings.index_bot_logins = {"my-bot[bot]"}
        assert _should_index(True, "MY-BOT[bot]", settings) is True


# ===================================================================
# _is_bot
# ===================================================================


class TestIsBot:
    """_is_bot の判定ロジック。"""

    def test_type_bot(self):
        assert _is_bot({"type": "Bot", "login": "x"}) is True
        assert _is_bot({"type": "User", "login": "x"}) is False

    def test_login_ends_with_bot(self):
        assert _is_bot({"login": "my-bot[bot]"}) is True
        assert _is_bot({"login": "alice"}) is False

    def test_none_user(self):
        assert _is_bot(None) is False


# ===================================================================
# _propagate_issue_state（issue #56）
# ===================================================================


class TestPropagateIssueState:
    """_propagate_issue_state の振る舞い。"""

    def test_updates_all_chunks_for_repo_and_issue(self):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = 3
        conn.cursor.return_value.__enter__.return_value = cursor

        _propagate_issue_state(conn, "o/r", 42, "closed")

        cursor.execute.assert_called_once_with(
            "UPDATE chunks SET state = %s WHERE repo = %s AND issue_no = %s",
            ("closed", "o/r", 42),
        )

    def test_zero_rowcount_does_not_log(self):
        """rowcount=0 でも例外は出ない。"""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = 0
        conn.cursor.return_value.__enter__.return_value = cursor

        _propagate_issue_state(conn, "o/r", 42, "open")


# ===================================================================
# _sync_pr_changes（issue #54）
# ===================================================================


class TestSyncPrChanges:
    """_sync_pr_changes の振る舞い。"""

    def test_skips_when_head_sha_unchanged(self):
        client = MagicMock()
        conn = MagicMock()

        # PR 詳細レスポンス
        client.get.return_value.raise_for_status.return_value = None
        client.get.return_value.json.return_value = {
            "head": {"sha": "abc1234"},
        }

        # get_pr_head_sha が同じ SHA を返すようモック
        with patch("shiori.github_sync.get_pr_head_sha", return_value="abc1234"):
            with patch("shiori.github_sync.upsert_pr_changes") as mock_upsert:
                _sync_pr_changes(client, conn, "o/r", 42)

        # files の取得は行われず、upsert も呼ばれない
        mock_upsert.assert_not_called()

    def test_fetches_files_when_head_sha_changed(self):
        client = MagicMock()
        conn = MagicMock()

        # PR 詳細
        client.get.side_effect = [
            MagicMock(
                raise_for_status=lambda: None,
                json=lambda: {"head": {"sha": "newsha"}},
            ),
            # files
            MagicMock(
                raise_for_status=lambda: None,
                json=lambda: [
                    {"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0, "changes": 1, "blob_url": "u"},
                ],
                links={},
            ),
        ]

        with patch("shiori.github_sync.get_pr_head_sha", return_value="oldsha"):
            with patch("shiori.github_sync.upsert_pr_changes") as mock_upsert:
                _sync_pr_changes(client, conn, "o/r", 42)

        mock_upsert.assert_called_once()

    def test_fetches_files_when_no_previous_sha(self):
        client = MagicMock()
        conn = MagicMock()

        client.get.side_effect = [
            MagicMock(
                raise_for_status=lambda: None,
                json=lambda: {"head": {"sha": "abc"}},
            ),
            MagicMock(
                raise_for_status=lambda: None,
                json=lambda: [],
                links={},
            ),
        ]

        with patch("shiori.github_sync.get_pr_head_sha", return_value=None):
            with patch("shiori.github_sync.upsert_pr_changes") as mock_upsert:
                _sync_pr_changes(client, conn, "o/r", 42)

        mock_upsert.assert_called_once()

    def test_returns_early_when_no_head_sha_in_response(self):
        client = MagicMock()
        conn = MagicMock()

        client.get.return_value.raise_for_status.return_value = None
        client.get.return_value.json.return_value = {"head": {}}

        with patch("shiori.github_sync.upsert_pr_changes") as mock_upsert:
            _sync_pr_changes(client, conn, "o/r", 42)

        mock_upsert.assert_not_called()


# ---------------------------------------------------------------------------
# _git (safe.directory)
# ---------------------------------------------------------------------------


class TestGit:
    """_git の safe.directory 付与ロジック（issue #48）。"""

    @patch("shiori.github_sync.subprocess.run")
    def test_safe_directory_added_when_cwd_given(self, mock_run):
        """cwd 指定時に -c safe.directory=<cwd> が渡ること。"""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        _git(["status"], cwd="/data/repos/foo")
        called_cmd = mock_run.call_args[0][0]
        assert called_cmd[:3] == ["git", "-c", "safe.directory=/data/repos/foo"]

    @patch("shiori.github_sync.subprocess.run")
    def test_no_safe_directory_when_cwd_none(self, mock_run):
        """cwd=None（clone 時など）には safe.directory が付与されないこと。"""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        _git(["clone", "url", "dest"])
        called_cmd = mock_run.call_args[0][0]
        assert "-c" not in called_cmd
