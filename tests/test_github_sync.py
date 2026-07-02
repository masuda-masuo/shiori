"""Unit tests for github_sync (issue #25, #54, #72, #73, #81).

Verifies _should_index allowlist logic, _sync_pr_changes head_sha comparison
skip/re-fetch logic, ChunkBuffer batch accumulation and flush,
_clean_text control character removal, _git_fetch_ref / _git_delete_ref.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from shiori.config import Settings
from shiori.github_sync import (
    ChunkBuffer,
    _clean_text,
    _git,
    _git_delete_ref,
    _git_fetch_ref,
    _is_bot,
    _propagate_issue_state,
    _should_index,
    _sync_pr_changes,
)


# ===================================================================
# ChunkBuffer（issue #72）
# ===================================================================


class TestChunkBuffer:
    """ChunkBuffer: chunk accumulate → batch embed → bulk insert."""

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
        """Accumulate with add, then flush does batch embed + bulk insert + commit."""
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
            # bulk_insert_chunks が 1 回呼ばれる
            mock_bulk.assert_called_once()
            # commit が 1 回呼ばれる
            conn.commit.assert_called_once()

    def test_auto_flush_when_batch_size_reached(self):
        """Auto-flush when batch_size is reached."""
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
        """Flushing an empty buffer does nothing."""
        conn = self._mock_conn()
        embedder = MagicMock()

        buf = ChunkBuffer(conn, embedder)

        n = buf.flush()

        assert n == 0
        embedder.embed_passages.assert_not_called()
        conn.commit.assert_not_called()

    def test_flush_returns_inserted_count(self):
        """flush return value is the inserted count."""
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
    """_should_index allowlist logic (issue #25)."""

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
        """Bot with author=None is excluded regardless of allowlist."""
        settings = Settings()
        settings.index_bot_logins = {"any[bot]"}
        assert _should_index(True, None, settings) is False

    def test_case_insensitive(self):
        """allowlist check is case-insensitive."""
        settings = Settings()
        settings.index_bot_logins = {"my-bot[bot]"}
        assert _should_index(True, "MY-BOT[bot]", settings) is True


# ===================================================================
# _is_bot
# ===================================================================


class TestIsBot:
    """_is_bot determination logic."""

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
    """Behavior of _propagate_issue_state."""

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
        """rowcount=0 does not raise an exception."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = 0
        conn.cursor.return_value.__enter__.return_value = cursor

        _propagate_issue_state(conn, "o/r", 42, "open")


# ===================================================================
# _sync_pr_changes（issue #54）
# ===================================================================


class TestSyncPrChanges:
    """Behavior of _sync_pr_changes."""

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
    """_git safe.directory assignment logic (issue #48)."""

    @patch("shiori.github_sync.subprocess.run")
    def test_safe_directory_added_when_cwd_given(self, mock_run):
        """When cwd is specified, -c safe.directory=<cwd> is passed."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        _git(["status"], cwd="/data/repos/foo")
        called_cmd = mock_run.call_args[0][0]
        assert called_cmd[:3] == ["git", "-c", "safe.directory=/data/repos/foo"]

    @patch("shiori.github_sync.subprocess.run")
    def test_no_safe_directory_when_cwd_none(self, mock_run):
        """When cwd=None (e.g. clone), safe.directory is not assigned."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        _git(["clone", "url", "dest"])
        called_cmd = mock_run.call_args[0][0]
        assert "-c" not in called_cmd


# ---------------------------------------------------------------------------
# _clean_text（issue #73）
# ---------------------------------------------------------------------------


class TestCleanText:
    """_clean_text: control character removal (preserves newline, tab)."""

    def test_remove_nul(self):
        """NUL (0x00) is removed."""
        assert _clean_text("hello\x00world") == "helloworld"

    def test_remove_control_chars(self):
        """Control chars except newline/tab (0x01-0x08, 0x0B-0x1F) are removed."""
        assert _clean_text("a\x01b\x08c\x0Bd\x1Fe") == "abcde"

    def test_preserve_newline(self):
        """Newline (\n, 0x0A) is preserved."""
        assert _clean_text("line1\nline2\nline3") == "line1\nline2\nline3"

    def test_preserve_tab(self):
        """Tab (\t, 0x09) is preserved."""
        assert _clean_text("col1\tcol2\tcol3") == "col1\tcol2\tcol3"

    def test_preserve_normal_text(self):
        """Normal text is unchanged."""
        text = "Hello, 世界! This is a test \U0001f60a"
        assert _clean_text(text) == text

    def test_none_returns_empty(self):
        """None becomes empty string."""
        assert _clean_text(None) == ""

    def test_empty_string(self):
        """Empty string is unchanged."""
        assert _clean_text("") == ""

    def test_mixed_control_and_valid(self):
        """Mix of control chars, newline, tab, and normal text."""
        mixed = "a\x00b\x01c\nd\te\x1Ff\x09g\nh"
        expected = "abc\nd\tef\tg\nh"
        assert _clean_text(mixed) == expected

    def test_only_control_chars(self):
        """String with only control chars becomes empty."""
        assert _clean_text("\x00\x01\x08\x0B\x1F") == ""


# ---------------------------------------------------------------------------
# _git_fetch_ref / _git_delete_ref（issue #81）
# ---------------------------------------------------------------------------


class TestGitFetchRef:
    """_git_fetch_ref / _git_delete_ref: PR head fetch common primitives."""

    @patch("shiori.github_sync._git")
    def test_auto_generates_tmp_ref(self, mock_git):
        """Auto-generates refs/shiori/tmp-{uuid} when tmp_ref is unset."""
        with patch("shiori.github_sync.uuid.uuid4") as mock_uuid:
            mock_uuid.return_value.hex = "deadbeef1234"
            result = _git_fetch_ref("pull/42/head", cwd="/data/repos/o/r")

        assert result == "refs/shiori/tmp-deadbeef1234"
        # fetch が呼ばれたこと
        called_args = mock_git.call_args[0][0]
        assert called_args == [
            "fetch", "origin", "pull/42/head:refs/shiori/tmp-deadbeef1234", "--depth=1",
        ]

    @patch("shiori.github_sync._git")
    def test_uses_custom_tmp_ref(self, mock_git):
        """Uses the provided tmp_ref value when specified."""
        result = _git_fetch_ref(
            "pull/42/head",
            cwd="/data/repos/o/r",
            tmp_ref="refs/shiori/my-temp",
        )

        assert result == "refs/shiori/my-temp"
        called_args = mock_git.call_args[0][0]
        assert called_args == [
            "fetch", "origin", "pull/42/head:refs/shiori/my-temp", "--depth=1",
        ]

    @patch("shiori.github_sync._auth_args")
    @patch("shiori.github_sync._git")
    def test_forwards_provider_auth(self, mock_git, mock_auth):
        """When provider is given, includes _auth_args result in git args."""
        mock_auth.return_value = ["-c", "http.extraHeader=Authorization: Basic xxx"]
        provider = MagicMock()

        with patch("shiori.github_sync.uuid.uuid4") as mock_uuid:
            mock_uuid.return_value.hex = "abc"
            _git_fetch_ref("pull/1/head", cwd="/r", provider=provider)

        called_args = mock_git.call_args[0][0]
        assert called_args[:2] == ["-c", "http.extraHeader=Authorization: Basic xxx"]
        assert "fetch" in called_args
        mock_auth.assert_called_once_with(provider)

    @patch("shiori.github_sync._auth_args")
    @patch("shiori.github_sync._git")
    def test_no_auth_when_provider_none(self, mock_git, mock_auth):
        """No auth args when provider=None."""
        mock_auth.return_value = []

        with patch("shiori.github_sync.uuid.uuid4") as mock_uuid:
            mock_uuid.return_value.hex = "abc"
            _git_fetch_ref("pull/1/head", cwd="/r", provider=None)

        called_args = mock_git.call_args[0][0]
        assert called_args[0] == "fetch"
        # _auth_args が呼ばれないことを確認
        mock_auth.assert_not_called()

    @patch("shiori.github_sync._git")
    def test_fetch_failure_propagated(self, mock_git):
        """git fetch failure is propagated as-is."""
        mock_git.side_effect = RuntimeError("fetch failed")

        with pytest.raises(RuntimeError, match="fetch failed"):
            _git_fetch_ref("pull/999/head", cwd="/r")

    @patch("shiori.github_sync._git")
    def test_delete_ref_exists(self, mock_git):
        """_git_delete_ref calls update-ref -d."""
        _git_delete_ref("refs/shiori/tmp-abc", cwd="/r")

        mock_git.assert_called_once_with(
            ["update-ref", "-d", "refs/shiori/tmp-abc"],
            cwd="/r",
        )

    @patch("shiori.github_sync._git")
    def test_delete_ref_nonexistent_ignored(self, mock_git):
        """Deleting a non-existent ref silently swallows RuntimeError."""
        mock_git.side_effect = RuntimeError("fatal: ...")

        # 例外を出さない
        _git_delete_ref("refs/shiori/nonexistent", cwd="/r")
