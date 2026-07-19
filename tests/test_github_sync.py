"""Unit tests for github_sync (issue #25, #72, #73, #81).

_should_index allowlist logic, ChunkBuffer batch accumulation/flush,
_clean_text control character removal, _git_fetch_ref / _git_delete_ref.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from shiori.config import Settings
from shiori.github_sync import (
    ChunkBuffer,
    _api_pages_gen,
    _authed_url,
    _clean_text,
    _git,
    _git_delete_ref,
    _git_fetch_ref,
    _is_bot,
    _propagate_issue_state,
    _should_index,
    _sync_pr_reviews,
    sync_issues,
    index_issues,
)
from shiori.walk_utils import (
    _is_excluded_dir,
    _looks_minified,
)


# ===================================================================
# ChunkBuffer（issue #72）
# ===================================================================


class TestChunkBuffer:
    """ChunkBuffer: chunk accumulation → batch embed → bulk insert."""

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
        """add accumulates, flush does batch embed + bulk insert + commit."""
        conn = self._mock_conn()
        embedder = MagicMock()
        embedder.embed_passages.return_value = [[0.1, 0.2], [0.3, 0.4]]

        with patch("shiori.chunk_buffer.bulk_insert_chunks") as mock_bulk:
            buf = ChunkBuffer(conn, embedder, batch_size=500)

            buf.add(**self._chunk_kwargs(content="chunk1"))
            buf.add(**self._chunk_kwargs(chunk_key="doc:o/r:test2.md", content="chunk2"))

            n = buf.flush()

            assert n == 2
            # embed_passages called once (both texts batched)
            embedder.embed_passages.assert_called_once()
            # bulk_insert_chunks called once
            mock_bulk.assert_called_once()
            # commit called once
            conn.commit.assert_called_once()

    def test_auto_flush_when_batch_size_reached(self):
        """Auto-flushes when batch_size is reached."""
        conn = self._mock_conn()
        embedder = MagicMock()
        embedder.embed_passages.return_value = [[0.1, 0.2], [0.3, 0.4]]

        with patch("shiori.chunk_buffer.bulk_insert_chunks"):
            buf = ChunkBuffer(conn, embedder, batch_size=2)

            buf.add(**self._chunk_kwargs(content="a"))
            buf.add(**self._chunk_kwargs(chunk_key="k2", content="b"))
            # batch_size=2 reached → auto flush
            assert embedder.embed_passages.call_count == 1
            assert conn.commit.call_count == 1

            # buffer should be empty
            buf.add(**self._chunk_kwargs(chunk_key="k3", content="c"))
            assert len(buf._items) == 1  # not flushed yet

    def test_flush_empty_buffer_noops(self):
        """flush on empty buffer is a no-op."""
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

        with patch("shiori.chunk_buffer.bulk_insert_chunks"):
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
        """allowlist matching is case-insensitive."""
        settings = Settings()
        settings.index_bot_logins = {"my-bot[bot]"}
        assert _should_index(True, "MY-BOT[bot]", settings) is True


# ===================================================================
# _is_bot
# ===================================================================


class TestIsBot:
    """_is_bot detection logic."""

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
    """_propagate_issue_state behavior."""

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
        """No exception when rowcount=0."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = 0
        conn.cursor.return_value.__enter__.return_value = cursor

        _propagate_issue_state(conn, "o/r", 42, "open")



# ---------------------------------------------------------------------------
# _git (safe.directory)
# ---------------------------------------------------------------------------


class TestGit:
    """_git safe.directory injection logic (issue #48)."""

    @patch("shiori.git_utils.subprocess.run")
    def test_safe_directory_added_when_cwd_given(self, mock_run):
        """Ensures -c safe.directory=<cwd> is passed when cwd is set."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        _git(["status"], cwd="/data/repos/foo")
        called_cmd = mock_run.call_args[0][0]
        assert called_cmd[:3] == ["git", "-c", "safe.directory=/data/repos/foo"]

    @patch("shiori.git_utils.subprocess.run")
    def test_no_safe_directory_when_cwd_none(self, mock_run):
        """safe.directory is not injected when cwd=None (clone etc)."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        _git(["clone", "url", "dest"])
        called_cmd = mock_run.call_args[0][0]
        assert "-c" not in called_cmd


# ---------------------------------------------------------------------------
# _clean_text（issue #73）
# ---------------------------------------------------------------------------


class TestCleanText:
    """_clean_text: control character removal (newline/tab preserved)."""

    def test_remove_nul(self):
        """NUL (0x00) is removed."""
        assert _clean_text("hello\x00world") == "helloworld"

    def test_remove_control_chars(self):
        """改行・タブ以外の制御文字 (0x01-0x08, 0x0B-0x1F) が除去される。"""
        assert _clean_text("a\x01b\x08c\x0Bd\x1Fe") == "abcde"

    def test_preserve_newline(self):
        """改行 (\n, 0x0A) は保持される。"""
        assert _clean_text("line1\nline2\nline3") == "line1\nline2\nline3"

    def test_preserve_tab(self):
        """タブ (\t, 0x09) は保持される。"""
        assert _clean_text("col1\tcol2\tcol3") == "col1\tcol2\tcol3"

    def test_preserve_normal_text(self):
        """通常のテキストはそのまま。"""
        text = "Hello, 世界! This is a test \U0001f60a"
        assert _clean_text(text) == text

    def test_none_returns_empty(self):
        """None は空文字列になる。"""
        assert _clean_text(None) == ""

    def test_empty_string(self):
        """空文字列はそのまま。"""
        assert _clean_text("") == ""

    def test_mixed_control_and_valid(self):
        """制御文字・改行・タブ・通常テキストの混合。"""
        mixed = "a\x00b\x01c\nd\te\x1Ff\x09g\nh"
        expected = "abc\nd\tef\tg\nh"
        assert _clean_text(mixed) == expected

    def test_only_control_chars(self):
        """制御文字だけの文字列は空になる。"""
        assert _clean_text("\x00\x01\x08\x0B\x1F") == ""


# ---------------------------------------------------------------------------
# _authed_url（PR #177）
# ---------------------------------------------------------------------------


class TestAuthedUrl:
    """_authed_url: URL-embedded token for git auth (issue #174, PR #177)."""

    def test_embeds_token(self):
        """Token is embedded into URL via x-access-token scheme."""
        provider = MagicMock()
        provider.get_token.return_value = "ghs_token123"
        url = _authed_url("https://github.com/o/r.git", provider)
        assert url == "https://x-access-token:ghs_token123@github.com/o/r.git"

    def test_none_token_returns_original(self):
        """When provider returns None, original URL is returned."""
        provider = MagicMock()
        provider.get_token.return_value = None
        url = _authed_url("https://github.com/o/r.git", provider)
        assert url == "https://github.com/o/r.git"

    def test_empty_token_returns_original(self):
        """When provider returns empty string, original URL is returned."""
        provider = MagicMock()
        provider.get_token.return_value = ""
        url = _authed_url("https://github.com/o/r.git", provider)
        assert url == "https://github.com/o/r.git"

    def test_only_replaces_first_https(self):
        """Only the first https:// is replaced (unlikely edge case)."""
        provider = MagicMock()
        provider.get_token.return_value = "tok"
        url = _authed_url("https://github.com/https://path.git", provider)
        assert url == "https://x-access-token:tok@github.com/https://path.git"

    def test_token_with_url_unsafe_chars(self):
        """URL-unsafe chars in token are embedded as-is (git handles them)."""
        provider = MagicMock()
        provider.get_token.return_value = "tok/+="
        url = _authed_url("https://github.com/o/r.git", provider)
        assert url == "https://x-access-token:tok/+=@github.com/o/r.git"


# ---------------------------------------------------------------------------
# _git_fetch_ref / _git_delete_ref（issue #81）
# ---------------------------------------------------------------------------


class TestGitFetchRef:
    """_git_fetch_ref / _git_delete_ref: PR head 取得の共通プリミティブ。"""

    @patch("shiori.git_utils._git")
    def test_auto_generates_tmp_ref(self, mock_git):
        """tmp_ref 未指定時は refs/shiori/tmp-{uuid} を自動生成する。"""
        with patch("shiori.git_utils.uuid.uuid4") as mock_uuid:
            mock_uuid.return_value.hex = "deadbeef1234"
            result = _git_fetch_ref("pull/42/head", cwd="/data/repos/o/r")

        assert result == "refs/shiori/tmp-deadbeef1234"
        # fetch が呼ばれたこと
        called_args = mock_git.call_args[0][0]
        assert called_args == [
            "fetch", "origin", "pull/42/head:refs/shiori/tmp-deadbeef1234", "--depth=1",
        ]

    @patch("shiori.git_utils._git")
    def test_uses_custom_tmp_ref(self, mock_git):
        """tmp_ref 指定時はその値を使う。"""
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

    @patch("shiori.git_utils._git")
    def test_forwards_provider_auth(self, mock_git):
        fake_remote = "https://github.com/o/r.git"
        fake_authed = "https://x-access-token:tok@github.com/o/r.git"
        mock_git.side_effect = [fake_remote, None, None, None]
        provider = MagicMock()
        provider.get_token.return_value = "tok"

        with patch("shiori.git_utils.uuid.uuid4") as mock_uuid:
            mock_uuid.return_value.hex = "abc"
            _git_fetch_ref("pull/1/head", cwd="/r", provider=provider)

        assert mock_git.call_count == 4
        assert mock_git.call_args_list[0][0][0] == ["remote", "get-url", "origin"]
        assert mock_git.call_args_list[1][0][0] == ["remote", "set-url", "origin", fake_authed]
        assert mock_git.call_args_list[2][0][0] == [
            "fetch", "origin", "pull/1/head:refs/shiori/tmp-abc", "--depth=1",
        ]
        assert mock_git.call_args_list[3][0][0] == ["remote", "set-url", "origin", fake_remote]
        provider.get_token.assert_called_once()

    @patch("shiori.git_utils._git")
    def test_no_auth_when_provider_none(self, mock_git):
        with patch("shiori.git_utils.uuid.uuid4") as mock_uuid:
            mock_uuid.return_value.hex = "abc"
            _git_fetch_ref("pull/1/head", cwd="/r", provider=None)

        mock_git.assert_called_once()
        called_args = mock_git.call_args[0][0]
        assert called_args[0] == "fetch"

    @patch("shiori.git_utils._git")
    def test_fetch_failure_propagated(self, mock_git):
        """git fetch の失敗はそのまま伝播する。"""
        mock_git.side_effect = RuntimeError("fetch failed")

        with pytest.raises(RuntimeError, match="fetch failed"):
            _git_fetch_ref("pull/999/head", cwd="/r")

    @patch("shiori.git_utils._git")
    def test_delete_ref_exists(self, mock_git):
        """_git_delete_ref は update-ref -d を呼ぶ。"""
        _git_delete_ref("refs/shiori/tmp-abc", cwd="/r")

        mock_git.assert_called_once_with(
            ["update-ref", "-d", "refs/shiori/tmp-abc"],
            cwd="/r",
        )

    @patch("shiori.git_utils._git")
    def test_delete_ref_nonexistent_ignored(self, mock_git):
        """Silently ignores RuntimeError when deleting a non-existent ref."""
        mock_git.side_effect = RuntimeError("fatal: ...")

        # 例外を出さない
        _git_delete_ref("refs/shiori/nonexistent", cwd="/r")


# ===================================================================
# _sync_pr_reviews (issue #103)
# ===================================================================


class TestSyncPrReviews:
    """_sync_pr_reviews: PR review submissions from pulls/reviews."""

    @staticmethod
    def _review(
        rid: int,
        body: str = "looks good",
        state: str = "COMMENTED",
        login: str = "alice",
        bot: bool = False,
        submitted_at: str = "2024-01-01T00:00:00Z",
    ) -> dict:
        return {
            "id": rid,
            "user": {"login": login, "type": "Bot" if bot else "User"},
            "state": state,
            "body": body,
            "submitted_at": submitted_at,
            "html_url": f"https://github.com/o/r/pull/42#pullrequestreview-{rid}",
        }

    def _setup_api_pages(self, client, reviews):
        """Configure client mock so _api_pages returns the given reviews."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = reviews
        mock_resp.links = {}
        client.get.return_value = mock_resp

    def test_stores_review_with_negative_comment_id(self):
        client = MagicMock()
        conn = MagicMock()
        embedder = MagicMock()
        settings = Settings()

        self._setup_api_pages(client, [
            self._review(111, body="Looks good", state="APPROVED"),
            self._review(222, body="Needs changes", state="CHANGES_REQUESTED"),
        ])

        with (
            patch("shiori.sync_issues._upsert_issue_item") as mock_upsert,
            patch("shiori.sync_issues._should_index", return_value=True),
            patch("shiori.sync_issues._issue_title_state_kind", return_value=("Title", "open", "pr")),
            patch("shiori.sync_issues._index_item"),
        ):
            _sync_pr_reviews(client, conn, embedder, settings, "o/r", 42)

        assert mock_upsert.call_count == 2
        c1 = mock_upsert.call_args_list[0][0][1]
        assert c1["comment_id"] == -111
        assert c1["kind"] == "pr_review"
        assert c1["state"] == "APPROVED"
        assert c1["body"] == "Looks good"

        c2 = mock_upsert.call_args_list[1][0][1]
        assert c2["comment_id"] == -222
        assert c2["state"] == "CHANGES_REQUESTED"
        assert c2["body"] == "Needs changes"

    def test_indexes_body_with_state_prefix(self):
        client = MagicMock()
        conn = MagicMock()
        embedder = MagicMock()
        settings = Settings()

        self._setup_api_pages(client, [
            self._review(333, body="LGTM", state="APPROVED"),
        ])

        with (
            patch("shiori.sync_issues._upsert_issue_item"),
            patch("shiori.sync_issues._should_index", return_value=True),
            patch("shiori.sync_issues._issue_title_state_kind", return_value=("T", "open", "pr")),
            patch("shiori.sync_issues._index_item") as mock_index,
        ):
            _sync_pr_reviews(client, conn, embedder, settings, "o/r", 42)

        mock_index.assert_called_once()
        kwargs = mock_index.call_args[1]
        assert "[APPROVED]" in kwargs["body"]
        assert "LGTM" in kwargs["body"]

    def test_no_reviews_returns_early(self):
        client = MagicMock()
        conn = MagicMock()
        embedder = MagicMock()
        settings = Settings()

        self._setup_api_pages(client, [])

        with (
            patch("shiori.sync_issues._upsert_issue_item") as mock_upsert,
            patch("shiori.sync_issues._index_item") as mock_index,
        ):
            _sync_pr_reviews(client, conn, embedder, settings, "o/r", 42)

        mock_upsert.assert_not_called()
        mock_index.assert_not_called()

    def test_api_error_caught_and_returns(self):
        client = MagicMock()
        conn = MagicMock()
        embedder = MagicMock()
        settings = Settings()

        def _raise(*args, **kwargs):
            raise httpx.HTTPError("500")
        client.get = _raise

        with (
            patch("shiori.sync_issues._upsert_issue_item") as mock_upsert,
            patch("shiori.sync_issues._index_item") as mock_index,
        ):
            _sync_pr_reviews(client, conn, embedder, settings, "o/r", 42)

        mock_upsert.assert_not_called()
        mock_index.assert_not_called()

    def test_bot_review_upserted_but_not_indexed(self):
        """Bot review outside allowlist: upsert happens, _should_index=False -> no index."""
        client = MagicMock()
        conn = MagicMock()
        embedder = MagicMock()
        settings = Settings()

        self._setup_api_pages(client, [
            self._review(444, body="auto-review", state="COMMENTED",
                         login="dependabot[bot]", bot=True),
        ])

        with (
            patch("shiori.sync_issues._upsert_issue_item") as mock_upsert,
            patch("shiori.sync_issues._should_index", return_value=False),
            patch("shiori.sync_issues._index_item") as mock_index,
        ):
            _sync_pr_reviews(client, conn, embedder, settings, "o/r", 42)

        assert mock_upsert.call_count == 1
        mock_index.assert_not_called()

    def test_bot_in_allowlist_is_indexed(self):
        client = MagicMock()
        conn = MagicMock()
        embedder = MagicMock()
        settings = Settings()
        settings.index_bot_logins = {"my-bot[bot]"}

        self._setup_api_pages(client, [
            self._review(555, body="approved", state="APPROVED",
                         login="my-bot[bot]", bot=True),
        ])

        with (
            patch("shiori.sync_issues._upsert_issue_item"),
            patch("shiori.sync_issues._should_index", return_value=True),
            patch("shiori.sync_issues._issue_title_state_kind", return_value=("T", "open", "pr")),
            patch("shiori.sync_issues._index_item") as mock_index,
        ):
            _sync_pr_reviews(client, conn, embedder, settings, "o/r", 42)

        mock_index.assert_called_once()

    def test_empty_body_upserted_but_not_indexed(self):
        client = MagicMock()
        conn = MagicMock()
        embedder = MagicMock()
        settings = Settings()

        self._setup_api_pages(client, [
            self._review(666, body="", state="COMMENTED"),
        ])

        with (
            patch("shiori.sync_issues._upsert_issue_item") as mock_upsert,
            patch("shiori.sync_issues._should_index") as mock_should,
            patch("shiori.sync_issues._index_item") as mock_index,
        ):
            _sync_pr_reviews(client, conn, embedder, settings, "o/r", 42)

        assert mock_upsert.call_count == 1
        c1 = mock_upsert.call_args_list[0][0][1]
        assert c1["body"] == ""
        mock_index.assert_not_called()
        mock_should.assert_not_called()

    def test_none_body_cleaned_and_not_indexed(self):
        client = MagicMock()
        conn = MagicMock()
        embedder = MagicMock()
        settings = Settings()

        self._setup_api_pages(client, [
            self._review(777, body=None, state="APPROVED"),
        ])

        with (
            patch("shiori.sync_issues._upsert_issue_item") as mock_upsert,
            patch("shiori.sync_issues._should_index") as mock_should,
            patch("shiori.sync_issues._index_item") as mock_index,
        ):
            _sync_pr_reviews(client, conn, embedder, settings, "o/r", 42)

        assert mock_upsert.call_count == 1
        c1 = mock_upsert.call_args_list[0][0][1]
        assert c1["body"] == ""
        mock_index.assert_not_called()
        mock_should.assert_not_called()

    def test_pass_through_chunk_buffer(self):
        """When buffer is provided, _index_item receives it."""
        client = MagicMock()
        conn = MagicMock()
        embedder = MagicMock()
        settings = Settings()

        self._setup_api_pages(client, [
            self._review(888, body="via buffer", state="COMMENTED"),
        ])

        buffer = MagicMock()

        with (
            patch("shiori.sync_issues._upsert_issue_item"),
            patch("shiori.sync_issues._should_index", return_value=True),
            patch("shiori.sync_issues._issue_title_state_kind", return_value=("T", "open", "pr")),
            patch("shiori.sync_issues._index_item") as mock_index,
        ):
            _sync_pr_reviews(client, conn, embedder, settings, "o/r", 42, buffer=buffer)

        mock_index.assert_called_once()
        kwargs = mock_index.call_args[1]
        assert kwargs["buffer"] is buffer

    def test_negative_comment_id_avoids_collision(self):
        """All review comment_ids must be negative to avoid collision with inline comments."""
        client = MagicMock()
        conn = MagicMock()
        embedder = MagicMock()
        settings = Settings()

        self._setup_api_pages(client, [
            self._review(1, body="a"),
            self._review(2, body="b"),
            self._review(999999, body="c"),
        ])

        with (
            patch("shiori.sync_issues._upsert_issue_item") as mock_upsert,
            patch("shiori.sync_issues._should_index", return_value=True),
            patch("shiori.sync_issues._issue_title_state_kind", return_value=("T", "open", "pr")),
            patch("shiori.sync_issues._index_item"),
        ):
            _sync_pr_reviews(client, conn, embedder, settings, "o/r", 42)

        for call_args in mock_upsert.call_args_list:
            comment_id = call_args[0][1]["comment_id"]
            assert comment_id < 0, f"comment_id {comment_id} must be negative"


# ===================================================================
# _is_excluded_dir / _looks_minified (issue #235)
# ===================================================================


class TestIsExcludedDir:
    """_is_excluded_dir: 完全一致ディレクトリ名 + ビルド成果物サフィックスの除外。"""

    def test_exact_match(self):
        assert _is_excluded_dir("node_modules") is True
        assert _is_excluded_dir("dist") is True
        assert _is_excluded_dir(".git") is True

    def test_dist_suffix(self):
        """dashboard_dist のような完全一致しないビルド成果物ディレクトリも除外。"""
        assert _is_excluded_dir("dashboard_dist") is True
        assert _is_excluded_dir("web-dist") is True

    def test_unrelated_dir_kept(self):
        assert _is_excluded_dir("src") is False
        assert _is_excluded_dir("distutils_helpers") is False


class TestLooksMinified:
    """_looks_minified: 長い1行の有無によるバンドル検知ヒューリスティック。"""

    def test_normal_source_not_flagged(self):
        content = b"def foo():\n    return 1\n\n\ndef bar():\n    return 2\n"
        assert _looks_minified(content) is False

    def test_single_long_line_flagged(self):
        content = ("var x=1;" * 200).encode("utf-8")
        assert _looks_minified(content) is True

    def test_empty_content_not_flagged(self):
        assert _looks_minified(b"") is False

    def test_binary_content_does_not_raise(self):
        assert _looks_minified(b"\xff\xfe\x00\x01" * 100) is False

    def test_exactly_500_chars_not_minified(self):
        """500 chars exactly (threshold boundary, safe side)."""
        content = (b"x" * 500)
        assert _looks_minified(content) is False

    def test_501_chars_flagged_minified(self):
        """501 chars (threshold + 1, should be flagged)."""
        content = (b"x" * 501)
        assert _looks_minified(content) is True


# ===================================================================
# _api_pages_gen (issue #250)
# ===================================================================


class TestApiPagesGen:
    """_api_pages_gen: page-at-a-time generator."""

    def test_single_page(self):
        client = MagicMock()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = [{"id": 1}, {"id": 2}]
        resp.links = {}
        client.get.return_value = resp

        pages = list(_api_pages_gen(client, "https://api.github.com/repos/o/r/issues", {"per_page": 100}))
        assert pages == [[{"id": 1}, {"id": 2}]]

    def test_multi_page(self):
        client = MagicMock()
        resp1 = MagicMock()
        resp1.raise_for_status.return_value = None
        resp1.json.return_value = [{"id": 1}]
        resp1.links = {"next": {"url": "https://api.github.com/repos/o/r/issues?page=2"}}

        resp2 = MagicMock()
        resp2.raise_for_status.return_value = None
        resp2.json.return_value = [{"id": 2}]
        resp2.links = {}

        client.get.side_effect = [resp1, resp2]

        pages = list(_api_pages_gen(client, "https://api.github.com/repos/o/r/issues", {"per_page": 100}))
        assert pages == [[{"id": 1}], [{"id": 2}]]

    def test_empty_page(self):
        client = MagicMock()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = []
        resp.links = {}
        client.get.return_value = resp

        pages = list(_api_pages_gen(client, "https://api.github.com/repos/o/r/issues", {"per_page": 100}))
        assert pages == [[]]

    def test_http_error_propagated(self):
        client = MagicMock()
        client.get.side_effect = httpx.HTTPError("500")

        with pytest.raises(httpx.HTTPError):
            list(_api_pages_gen(client, "https://api.github.com/repos/o/r/issues", {"per_page": 100}))

    def test_not_found_ok_returns_empty(self):
        client = MagicMock()
        resp = MagicMock()
        resp.status_code = 404
        resp.raise_for_status.side_effect = httpx.HTTPStatusError("404", request=MagicMock(), response=resp)
        client.get.return_value = resp

        pages = list(_api_pages_gen(client, "https://api.github.com/repos/o/r/issues/comments", {}, not_found_ok=True))
        assert pages == []

    def test_not_found_ok_transport_error(self):
        client = MagicMock()
        client.get.side_effect = httpx.HTTPError("500")

        with pytest.raises(httpx.HTTPError):
            list(_api_pages_gen(client, "https://api.github.com/repos/o/r/issues/comments", {}, not_found_ok=True))

    def test_not_found_ok_false_raises_on_404(self):
        client = MagicMock()
        resp = MagicMock()
        resp.status_code = 404
        resp.raise_for_status.side_effect = httpx.HTTPStatusError("404", request=MagicMock(), response=resp)
        client.get.return_value = resp

        with pytest.raises(httpx.HTTPStatusError):
            list(_api_pages_gen(client, "https://api.github.com/repos/o/r/issues", {}, not_found_ok=False))

    def test_link_header_preserves_query_params(self):
        """Next URL's query params should be used as-is (next_params=None)."""
        client = MagicMock()
        resp1 = MagicMock()
        resp1.raise_for_status.return_value = None
        resp1.json.return_value = [{"id": 1}]
        resp1.links = {"next": {"url": "https://api.github.com/repos/o/r/issues?page=2&per_page=50"}}

        resp2 = MagicMock()
        resp2.raise_for_status.return_value = None
        resp2.json.return_value = [{"id": 2}]
        resp2.links = {}

        client.get.side_effect = [resp1, resp2]

        pages = list(_api_pages_gen(client, "https://api.github.com/repos/o/r/issues", {"per_page": 100}))
        assert pages == [[{"id": 1}], [{"id": 2}]]
        # Second call uses the next URL as-is (no params dict)
        assert client.get.call_args_list[1][0][0] == "https://api.github.com/repos/o/r/issues?page=2&per_page=50"
        assert client.get.call_args_list[1][1]["params"] is None

# ===================================================================
# sync_issues cursor fallback (PR #275)
# ===================================================================


class TestSyncIssuesCursor:
    """sync_issues: cursor is set even on empty API response (PR #275)."""

    def test_empty_issues_sets_cursor_fallback(self):
        settings = Settings()
        conn = MagicMock()
        embedder = MagicMock()
        provider = MagicMock()

        with (
            patch("shiori.sync_issues._api_pages_gen", return_value=iter([[]])),
            patch("shiori.sync_issues.get_cursor", return_value=None),
            patch("shiori.sync_issues.set_cursor") as mock_set,
        ):
            n = sync_issues(settings, conn, embedder, "o/r", provider)

        assert n == 0
        # Each of the 3 categories should get a fallback cursor
        assert mock_set.call_count == 3
        kinds = [call[0][2] for call in mock_set.call_args_list]
        assert kinds == ["issues", "issue_comments", "pr_review_comments"]
        # Each cursor value should be a Z-suffixed ISO timestamp
        for call in mock_set.call_args_list:
            cursor = call[0][3]
            assert cursor.endswith("Z"), f"Expected Z suffix, got: {cursor}"
            assert "T" in cursor, f"Expected ISO 8601 format, got: {cursor}"

    def test_existing_cursor_skips_fallback(self):
        settings = Settings()
        conn = MagicMock()
        embedder = MagicMock()
        provider = MagicMock()
        existing = "2024-01-01T00:00:00Z"

        def get_cursor_side(conn, repo, kind):
            return existing

        with (
            patch("shiori.sync_issues._api_pages_gen", side_effect=lambda *a, **kw: iter([[]])),
            patch("shiori.sync_issues.get_cursor", side_effect=get_cursor_side),
            patch("shiori.sync_issues.set_cursor") as mock_set,
        ):
            n = sync_issues(settings, conn, embedder, "o/r", provider)

        assert n == 0
        # No fallback cursor should be set (existing cursor means earlier sync ran)
        mock_set.assert_not_called()

    def test_with_data_uses_api_cursor(self):
        settings = Settings()
        conn = MagicMock()
        embedder = MagicMock()
        provider = MagicMock()

        page = [{
            "number": 1,
            "user": {"login": "alice", "type": "User"},
            "title": "Test Issue",
            "body": "Test body",
            "state": "open",
            "html_url": "https://github.com/o/r/issues/1",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z",
        }]

        with (
            patch("shiori.sync_issues._api_pages_gen", return_value=iter([page])),
            patch("shiori.sync_issues.get_cursor", return_value=None),
            patch("shiori.sync_issues._upsert_issue_item"),
            patch("shiori.sync_issues._propagate_issue_state"),
            patch("shiori.sync_issues._should_index", return_value=False),
            patch("shiori.sync_issues.set_cursor") as mock_set,
        ):
            n = sync_issues(settings, conn, embedder, "o/r", provider)

        assert n == 0  # _should_index=False
        # set_cursor should be called with API timestamps (not fallback)
        issues_calls = [
            c for c in mock_set.call_args_list
            if c[0][2] == "issues"
        ]
        assert len(issues_calls) >= 1
        # The cursor should be the API timestamp, not a fallback Z timestamp
        api_cursor = issues_calls[0][0][3]
        assert api_cursor == "2024-01-02T00:00:00Z"


class TestSyncIssuesPrReviewGuard:
    """sync_issues の PR review 同期ガード条件のテスト（issue #289）。"""

    @classmethod
    def _page_with_pr(cls) -> list[dict]:
        return [{
            "number": 42,
            "pull_request": {},  # PR であることを示す
            "user": {"login": "alice", "type": "User"},
            "title": "Test PR",
            "body": "PR body",
            "state": "open",
            "html_url": "https://github.com/o/r/pull/42",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z",
        }]

    def _mock_api_pages(self, *args, **kwargs):
        """Return PR page for /issues endpoints, empty for comments endpoints."""
        url = args[1] if len(args) >= 2 else ""
        if "/issues/comments" in url or "/pulls/comments" in url:
            return iter([[]])  # empty page
        return iter([self._page_with_pr()])

    @patch("shiori.sync_issues.db.connect", return_value=MagicMock())
    @patch("shiori.sync_issues.get_cursor", return_value=None)
    @patch("shiori.sync_issues._api_pages_gen")
    @patch("shiori.sync_issues._upsert_issue_item")
    @patch("shiori.sync_issues._propagate_issue_state")
    @patch("shiori.sync_issues._should_index", return_value=False)
    @patch("shiori.sync_issues._sync_pr_reviews")
    @patch("shiori.sync_issues.set_cursor")
    def test_dev_repo_calls_reviews(
        self, mock_set, mock_reviews, mock_should,
        mock_prop, mock_upsert, mock_pages, mock_cursor, mock_db_connect,
    ):
        """dev repo の diff sync では _sync_pr_reviews が呼ばれる。"""
        settings = Settings()
        settings.dev_repos = {"o/r"}
        mock_pages.side_effect = self._mock_api_pages
        sync_issues(settings, MagicMock(), MagicMock(), "o/r", MagicMock(), buffer=None)
        mock_reviews.assert_called_once()

    @patch("shiori.sync_issues.get_cursor", return_value=None)
    @patch("shiori.sync_issues._api_pages_gen")
    @patch("shiori.sync_issues._upsert_issue_item")
    @patch("shiori.sync_issues._propagate_issue_state")
    @patch("shiori.sync_issues._should_index", return_value=False)
    @patch("shiori.sync_issues._sync_pr_reviews")
    @patch("shiori.sync_issues.set_cursor")
    def test_ref_repo_skips_reviews_on_diff_sync(
        self, mock_set, mock_reviews, mock_should,
        mock_prop, mock_upsert, mock_pages, mock_cursor,
    ):
        """ref repo の diff sync では _sync_pr_reviews は呼ばれない。"""
        settings = Settings()
        settings.dev_repos = ["other/repo"]  # o/r は dev ではない
        mock_pages.side_effect = self._mock_api_pages
        sync_issues(settings, MagicMock(), MagicMock(), "o/r", MagicMock(), buffer=None)
        mock_reviews.assert_not_called()

    @patch("shiori.sync_issues.db.connect", return_value=MagicMock())
    @patch("shiori.sync_issues.get_cursor", return_value=None)
    @patch("shiori.sync_issues._api_pages_gen")
    @patch("shiori.sync_issues._upsert_issue_item")
    @patch("shiori.sync_issues._propagate_issue_state")
    @patch("shiori.sync_issues._should_index", return_value=False)
    @patch("shiori.sync_issues._sync_pr_reviews")
    @patch("shiori.sync_issues.set_cursor")
    def test_ref_repo_calls_reviews_on_bulk(
        self, mock_set, mock_reviews, mock_should,
        mock_prop, mock_upsert, mock_pages, mock_cursor, mock_db_connect,
    ):
        """ref repo でも bulk 時は _sync_pr_reviews が呼ばれる。"""
        settings = Settings()
        settings.dev_repos = ["other/repo"]
        mock_pages.side_effect = self._mock_api_pages
        sync_issues(settings, MagicMock(), MagicMock(), "o/r", MagicMock(), buffer=MagicMock())
        mock_reviews.assert_called_once()


# ===================================================================
# fetch_issues backfill_since seeding + one-time state=open pass (issue #315)
# ===================================================================


class TestFetchIssuesBackfillSince:
    """fetch_issues backfill_since: seed cursors for new repos only."""

    def test_backfill_since_seeds_when_cursor_none(self):
        """When cursor is None and backfill_since is set, seed all 3 cursors."""
        settings = Settings()
        conn = MagicMock()
        provider = MagicMock()

        # Stateful cursor: starts None, remembers set_cursor values
        cursor_store: dict[str, str | None] = {}
        def get_cursor_side(conn, repo, kind):
            return cursor_store.get(kind)
        def set_cursor_side(conn, repo, kind, cursor):
            cursor_store[kind] = cursor

        with (
            patch("shiori.sync_issues._api_pages_gen", return_value=iter([[]])),
            patch("shiori.sync_issues.get_cursor", side_effect=get_cursor_side),
            patch("shiori.sync_issues.set_cursor", side_effect=set_cursor_side) as mock_set,
            patch("shiori.sync_issues._fetch_dormant_open_bodies") as mock_dormant,
        ):
            from shiori.sync_issues import fetch_issues
            fetch_issues(settings, conn, "o/r", provider, backfill_since="2024-06-01T00:00:00Z")

        # set_cursor should have been called with seed date for all 3 kinds
        seed_calls = [
            c for c in mock_set.call_args_list
            if c[0][3] == "2024-06-01T00:00:00Z"
        ]
        assert len(seed_calls) == 3
        seed_kinds = [c[0][2] for c in seed_calls]
        assert "issues" in seed_kinds
        assert "issue_comments" in seed_kinds
        assert "pr_review_comments" in seed_kinds
        # Dormant open pass should have been called
        mock_dormant.assert_called_once()

    def test_backfill_since_skips_when_cursor_exists(self):
        """When cursor already exists, backfill_since does NOT seed."""
        settings = Settings()
        conn = MagicMock()
        provider = MagicMock()
        existing = "2024-01-01T00:00:00Z"

        def get_cursor_side(conn, repo, kind):
            return existing  # all cursors exist

        with (
            patch("shiori.sync_issues._api_pages_gen", return_value=iter([[]])),
            patch("shiori.sync_issues.get_cursor", side_effect=get_cursor_side),
            patch("shiori.sync_issues.set_cursor") as mock_set,
            patch("shiori.sync_issues._fetch_dormant_open_bodies") as mock_dormant,
        ):
            from shiori.sync_issues import fetch_issues
            fetch_issues(settings, conn, "o/r", provider, backfill_since="2024-06-01T00:00:00Z")

        # set_cursor should NOT be called with backfill_since
        # (it may be called by the normal cursor-advancement logic, but not with the seed)
        for call in mock_set.call_args_list:
            assert call[0][3] != "2024-06-01T00:00:00Z", "Seed date must not override existing cursor"
        # Dormant open pass should NOT have been called
        mock_dormant.assert_not_called()

    def test_backfill_since_none_no_seeding(self):
        """When backfill_since is None, no seeding occurs."""
        settings = Settings()
        conn = MagicMock()
        provider = MagicMock()

        def get_cursor_side(conn, repo, kind):
            return None

        with (
            patch("shiori.sync_issues._api_pages_gen", return_value=iter([[]])),
            patch("shiori.sync_issues.get_cursor", side_effect=get_cursor_side),
            patch("shiori.sync_issues.set_cursor"),
            patch("shiori.sync_issues._fetch_dormant_open_bodies") as mock_dormant,
        ):
            from shiori.sync_issues import fetch_issues
            fetch_issues(settings, conn, "o/r", provider, backfill_since=None)

        # Dormant open pass should NOT have been called
        mock_dormant.assert_not_called()


class TestFetchDormantOpenBodies:
    """_fetch_dormant_open_bodies: upserts body rows only, no cursor advance."""

    def test_upserts_open_bodies(self):
        """Fetches state=open issues/PRs and upserts body rows."""
        conn = MagicMock()
        client = MagicMock()

        page = [{
            "number": 1,
            "user": {"login": "alice", "type": "User"},
            "title": "Dormant Issue",
            "body": "Old issue not updated since seed date",
            "state": "open",
            "html_url": "https://github.com/o/r/issues/1",
            "created_at": "2023-01-01T00:00:00Z",
            "updated_at": "2023-01-01T00:00:00Z",
        }, {
            "number": 2,
            "pull_request": {},
            "user": {"login": "bob", "type": "User"},
            "title": "Dormant PR",
            "body": "Old PR not updated since seed date",
            "state": "open",
            "html_url": "https://github.com/o/r/pull/2",
            "created_at": "2023-06-01T00:00:00Z",
            "updated_at": "2023-06-01T00:00:00Z",
        }]

        with (
            patch("shiori.sync_issues._api_pages_gen", return_value=iter([page])),
            patch("shiori.sync_issues._upsert_issue_item") as mock_upsert,
        ):
            from shiori.sync_issues import _fetch_dormant_open_bodies
            n = _fetch_dormant_open_bodies(client, conn, "o/r")

        assert n == 2
        assert mock_upsert.call_count == 2
        # Only body rows (comment_id=0)
        for call in mock_upsert.call_args_list:
            row = call[0][1]
            assert row["comment_id"] == 0
            assert row["state"] == "open"
        conn.commit.assert_called()


# ===================================================================
# index_issues incremental (issue #318)
# ===================================================================


class TestIndexIssuesIncremental:
    """index_issues: incremental filtering, durability invariant, kill-resume, rebuild."""

    def _make_item_row(
        self, issue_no, comment_id=0, kind="issue", title="T", author="alice",
        is_bot=False, state="open", path=None, line=None, body="body",
        url="url", created_at=None, updated_at=None,
    ):
        return (issue_no, comment_id, kind, title, author, is_bot,
                state, path, line, body, url, created_at, updated_at)

    def _make_conn(self, body_rows, filtered_rows):
        """Create a mock connection that returns *body_rows* for the body
        SELECT and *filtered_rows* for the incremental-filtered SELECT."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        def _execute(sql, params=None):
            if 'comment_id = 0' in (sql or '') and 'indexed_at' not in (sql or ''):
                cursor.fetchall.return_value = body_rows
            elif 'indexed_at IS NULL' in (sql or '') or 'updated_at > indexed_at' in (sql or ''):
                cursor.fetchall.return_value = filtered_rows
            else:
                cursor.fetchall.return_value = []

        cursor.execute.side_effect = _execute
        return conn

    def test_all_indexed_skips_embedder(self):
        """When every item has indexed_at set and no item has newer updated_at,
        zero embedder calls are made (acceptance criterion 1)."""
        settings = Settings()
        embedder = MagicMock()
        body = [(1, "issue", "open")]
        filtered = []  # nothing needs indexing
        conn = self._make_conn(body, filtered)

        n = index_issues(settings, conn, embedder, "o/r")

        assert n == 0
        embedder.embed_passages.assert_not_called()

    def test_null_indexed_at_is_indexed(self):
        """Items whose indexed_at IS NULL are (re)indexed (acceptance criterion 2)."""
        settings = Settings()
        embedder = MagicMock()
        embedder.embed_passages.return_value = [[0.1, 0.2]]
        body = [(1, "issue", "open")]
        filtered = [self._make_item_row(1)]
        conn = self._make_conn(body, filtered)

        n = index_issues(settings, conn, embedder, "o/r")

        assert n == 1
        embedder.embed_passages.assert_called_once()

    def test_updated_at_newer_than_indexed_reindexes(self):
        """Items whose updated_at > indexed_at are re-indexed (acceptance criterion 2)."""
        settings = Settings()
        embedder = MagicMock()
        embedder.embed_passages.return_value = [[0.1, 0.2]]
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        future = datetime(2025, 6, 1, tzinfo=timezone.utc)
        body = [(1, "issue", "open")]
        filtered = [self._make_item_row(1, updated_at=future, created_at=past)]
        conn = self._make_conn(body, filtered)

        n = index_issues(settings, conn, embedder, "o/r")

        assert n == 1
        embedder.embed_passages.assert_called_once()

    def test_mixed_old_and_new_partial_index(self):
        """Only unindexed or outdated items are processed; already-indexed
        items are skipped."""
        settings = Settings()
        embedder = MagicMock()
        embedder.embed_passages.return_value = [[0.1, 0.2]]
        body = [(1, "issue", "open"), (2, "pr", "closed")]
        # Only item 2 needs indexing
        filtered = [self._make_item_row(2, kind="pr", author="bob", body="body2")]
        conn = self._make_conn(body, filtered)

        n = index_issues(settings, conn, embedder, "o/r")

        assert n == 1
        embedder.embed_passages.assert_called_once()

    def test_rebuild_all_null_indexed_at(self):
        """After rebuild (all indexed_at = NULL), every item is indexed
        (acceptance criterion 4)."""
        settings = Settings()
        embedder = MagicMock()
        embedder.embed_passages.return_value = [[0.1, 0.2], [0.3, 0.4]]
        body = [(1, "issue", "open"), (2, "pr", "closed")]
        filtered = [
            self._make_item_row(1),
            self._make_item_row(2, kind="pr", author="bob", body="body2"),
        ]
        conn = self._make_conn(body, filtered)

        n = index_issues(settings, conn, embedder, "o/r")

        assert n == 2
        assert embedder.embed_passages.call_count >= 1

    def test_kill_resume(self):
        """Simulate kill-resume: first run indexes everything, second run
        skips already-indexed items (acceptance criterion 3).

        In production, kill-resume means some items have indexed_at committed
        while others do not.  The incremental filter ensures the second run
        only touches remaining items.
        """
        settings = Settings()

        # Phase 1: index all items
        embedder1 = MagicMock()
        embedder1.embed_passages.return_value = [[0.1, 0.2], [0.3, 0.4]]
        body1 = [(1, "issue", "open"), (2, "issue", "open")]
        filtered1 = [
            self._make_item_row(1),
            self._make_item_row(2, body="body2"),
        ]
        conn1 = self._make_conn(body1, filtered1)
        n1 = index_issues(settings, conn1, embedder1, "o/r")
        assert n1 == 2

        # Phase 2: all items now indexed; second run must NOT re-embed
        embedder2 = MagicMock()
        body2 = [(1, "issue", "open"), (2, "issue", "open")]
        filtered2 = []  # everything already indexed
        conn2 = self._make_conn(body2, filtered2)
        n2 = index_issues(settings, conn2, embedder2, "o/r")

        assert n2 == 0
        embedder2.embed_passages.assert_not_called()

    def test_kill_resume_partial_batch(self):
        """Simulate mid-run kill after batch 1 committed: rerun processes
        only remaining items without re-embedding batch 1."""
        settings = Settings()

        # 250 items: batch 1 (200) indexed in first run, batch 2 (50) remaining
        # On rerun, only the 50 remaining items appear in filtered_rows
        embedder = MagicMock()
        embedder.embed_passages.return_value = [[0.5, 0.6]] * 50
        body = [(i, "issue", "open") for i in range(1, 251)]
        remaining = [
            self._make_item_row(i,
                                kind="issue",
                                author="alice",
                                body=f"body{i}",
                                url=f"url{i}",
            )
            for i in range(201, 251)
        ]
        conn = self._make_conn(body, remaining)

        n = index_issues(settings, conn, embedder, "o/r")

        assert n == 50
        # The first 200 items must NOT have been re-embedded
        # (they don't appear in filtered_rows, so they're skipped)
        # embed_passages is called at least once for the 50 remaining items
        assert embedder.embed_passages.called, \
            "embedder must be called for remaining items"

    def test_propagate_state_covers_all_body_rows(self):
        """_propagate_issue_state runs for ALL body rows, not just
        the filtered subset (state propagation unchanged, outcome 6)."""
        settings = Settings()
        embedder = MagicMock()
        embedder.embed_passages.return_value = [[0.1, 0.2]]

        body = [
            (1, "issue", "closed"),  # already indexed but state='closed'
            (2, "issue", "open"),     # new, needs indexing
        ]
        filtered = [self._make_item_row(2, body="new body")]

        with patch("shiori.sync_issues._propagate_issue_state") as mock_prop:
            conn = self._make_conn(body, filtered)
            n = index_issues(settings, conn, embedder, "o/r")

        assert n == 1
        # Both issues must have state propagated
        assert mock_prop.call_count == 2
        issues_propagated = {call[0][2] for call in mock_prop.call_args_list}
        assert 1 in issues_propagated, "issue 1 (body-only, not re-indexed) must get state propagation"
        assert 2 in issues_propagated, "issue 2 (re-indexed) must get state propagation"

    def test_signature_backward_compatible(self):
        """index_issues signature unchanged: callers in pipeline.py and
        ingest.py keep working (outcome 7)."""
        # This verifies the function can be called with all existing params
        settings = Settings()
        conn = MagicMock()
        embedder = MagicMock()
        buffer = MagicMock()

        # Without buffer (non-bulk path)
        result = index_issues(settings, conn, embedder, "o/r")
        assert result == 0  # no rows

        # With buffer (bulk path)
        result = index_issues(settings, conn, embedder, "o/r", buffer=buffer)
        assert result == 0  # no rows
