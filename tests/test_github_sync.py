"""Unit tests for github_sync (issue #25, #54, #72, #73, #81).

_should_index allowlist logic, _sync_pr_changes head_sha comparison for
 skip/re-fetch logic, ChunkBuffer batch accumulation/flush,
_clean_text control character removal, _git_fetch_ref / _git_delete_ref.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from shiori.config import Settings
from shiori.github_sync import (
    ChunkBuffer,
    _authed_url,
    _clean_text,
    _git,
    _git_delete_ref,
    _git_fetch_ref,
    _is_bot,
    _is_excluded_dir,
    _looks_minified,
    _propagate_issue_state,
    _should_index,
    _sync_pr_changes,
    _sync_pr_reviews,
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

        with patch("shiori.github_sync.bulk_insert_chunks") as mock_bulk:
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

        with patch("shiori.github_sync.bulk_insert_chunks"):
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


# ===================================================================
# _sync_pr_changes（issue #54）
# ===================================================================


class TestSyncPrChanges:
    """_sync_pr_changes behavior."""

    def test_skips_when_head_sha_unchanged(self):
        client = MagicMock()
        conn = MagicMock()

        # PR detail response
        client.get.return_value.raise_for_status.return_value = None
        client.get.return_value.json.return_value = {
            "head": {"sha": "abc1234"},
        }

        # mock get_pr_head_sha to return the same SHA
        with patch("shiori.github_sync.get_pr_head_sha", return_value="abc1234"):
            with patch("shiori.github_sync.upsert_pr_changes") as mock_upsert:
                _sync_pr_changes(client, conn, "o/r", 42)

        # files not fetched, upsert not called
        mock_upsert.assert_not_called()

    def test_fetches_files_when_head_sha_changed(self):
        client = MagicMock()
        conn = MagicMock()

        # PR detail
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
    """_git safe.directory injection logic (issue #48)."""

    @patch("shiori.github_sync.subprocess.run")
    def test_safe_directory_added_when_cwd_given(self, mock_run):
        """Ensures -c safe.directory=<cwd> is passed when cwd is set."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        _git(["status"], cwd="/data/repos/foo")
        called_cmd = mock_run.call_args[0][0]
        assert called_cmd[:3] == ["git", "-c", "safe.directory=/data/repos/foo"]

    @patch("shiori.github_sync.subprocess.run")
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

    @patch("shiori.github_sync._git")
    def test_auto_generates_tmp_ref(self, mock_git):
        """tmp_ref 未指定時は refs/shiori/tmp-{uuid} を自動生成する。"""
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

    @patch("shiori.github_sync._git")
    def test_forwards_provider_auth(self, mock_git):
        fake_remote = "https://github.com/o/r.git"
        fake_authed = "https://x-access-token:tok@github.com/o/r.git"
        mock_git.side_effect = [fake_remote, None, None, None]
        provider = MagicMock()
        provider.get_token.return_value = "tok"

        with patch("shiori.github_sync.uuid.uuid4") as mock_uuid:
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

    @patch("shiori.github_sync._git")
    def test_no_auth_when_provider_none(self, mock_git):
        with patch("shiori.github_sync.uuid.uuid4") as mock_uuid:
            mock_uuid.return_value.hex = "abc"
            _git_fetch_ref("pull/1/head", cwd="/r", provider=None)

        mock_git.assert_called_once()
        called_args = mock_git.call_args[0][0]
        assert called_args[0] == "fetch"

    @patch("shiori.github_sync._git")
    def test_fetch_failure_propagated(self, mock_git):
        """git fetch の失敗はそのまま伝播する。"""
        mock_git.side_effect = RuntimeError("fetch failed")

        with pytest.raises(RuntimeError, match="fetch failed"):
            _git_fetch_ref("pull/999/head", cwd="/r")

    @patch("shiori.github_sync._git")
    def test_delete_ref_exists(self, mock_git):
        """_git_delete_ref は update-ref -d を呼ぶ。"""
        _git_delete_ref("refs/shiori/tmp-abc", cwd="/r")

        mock_git.assert_called_once_with(
            ["update-ref", "-d", "refs/shiori/tmp-abc"],
            cwd="/r",
        )

    @patch("shiori.github_sync._git")
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
            patch("shiori.github_sync._upsert_issue_item") as mock_upsert,
            patch("shiori.github_sync._should_index", return_value=True),
            patch("shiori.github_sync._issue_title_state_kind", return_value=("Title", "open", "pr")),
            patch("shiori.github_sync._index_item"),
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
            patch("shiori.github_sync._upsert_issue_item"),
            patch("shiori.github_sync._should_index", return_value=True),
            patch("shiori.github_sync._issue_title_state_kind", return_value=("T", "open", "pr")),
            patch("shiori.github_sync._index_item") as mock_index,
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
            patch("shiori.github_sync._upsert_issue_item") as mock_upsert,
            patch("shiori.github_sync._index_item") as mock_index,
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
            patch("shiori.github_sync._upsert_issue_item") as mock_upsert,
            patch("shiori.github_sync._index_item") as mock_index,
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
            patch("shiori.github_sync._upsert_issue_item") as mock_upsert,
            patch("shiori.github_sync._should_index", return_value=False),
            patch("shiori.github_sync._index_item") as mock_index,
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
            patch("shiori.github_sync._upsert_issue_item"),
            patch("shiori.github_sync._should_index", return_value=True),
            patch("shiori.github_sync._issue_title_state_kind", return_value=("T", "open", "pr")),
            patch("shiori.github_sync._index_item") as mock_index,
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
            patch("shiori.github_sync._upsert_issue_item") as mock_upsert,
            patch("shiori.github_sync._should_index") as mock_should,
            patch("shiori.github_sync._index_item") as mock_index,
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
            patch("shiori.github_sync._upsert_issue_item") as mock_upsert,
            patch("shiori.github_sync._should_index") as mock_should,
            patch("shiori.github_sync._index_item") as mock_index,
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
            patch("shiori.github_sync._upsert_issue_item"),
            patch("shiori.github_sync._should_index", return_value=True),
            patch("shiori.github_sync._issue_title_state_kind", return_value=("T", "open", "pr")),
            patch("shiori.github_sync._index_item") as mock_index,
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
            patch("shiori.github_sync._upsert_issue_item") as mock_upsert,
            patch("shiori.github_sync._should_index", return_value=True),
            patch("shiori.github_sync._issue_title_state_kind", return_value=("T", "open", "pr")),
            patch("shiori.github_sync._index_item"),
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
