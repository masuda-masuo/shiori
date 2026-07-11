"""Unit tests for _do_sync allowlist validation and ingest rebuild guard (issue #63)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from shiori.mcp_server import _do_sync, ingest


# ===================================================================
# _do_sync: allowlist validation
# ===================================================================


class TestDoSyncAllowlist:
    """_do_sync repo argument allowlist validation.

    Allowlist validation runs before lock acquisition, so only settings mock is needed.
    """

    def test_valid_repo_passes_validation(self):
        """repo in settings.repos passes validation (failure after lock does not raise ValueError)."""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._sync_lock") as mock_lock,
        ):
            mock_settings.repos = ["owner/repo", "owner2/repo2"]
            mock_lock.acquire.return_value = False  # lock acquisition failed → skipped

            result = _do_sync(repos=["owner/repo"])
            assert result["status"] == "skipped"
            assert result["reason"] == "sync already running"

    def test_invalid_repo_raises_value_error(self):
        """repo not in settings.repos raises ValueError."""
        with patch("shiori.mcp_server.settings") as mock_settings:
            mock_settings.repos = ["owner/repo"]

            with pytest.raises(ValueError, match="SHIORI_REPOS"):
                _do_sync(repos=["evil/repo"])

    def test_partially_invalid_raises(self):
        """Raises ValueError even if only some repos are invalid."""
        with patch("shiori.mcp_server.settings") as mock_settings:
            mock_settings.repos = ["owner/repo"]

            with pytest.raises(ValueError, match="SHIORI_REPOS"):
                _do_sync(repos=["owner/repo", "evil/repo"])

    def test_repos_none_skips_validation(self):
        """repos=None skips validation (uses settings.repos)."""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._sync_lock") as mock_lock,
        ):
            mock_settings.repos = ["owner/repo"]
            mock_lock.acquire.return_value = False

            result = _do_sync(repos=None)
            assert result["status"] == "skipped"  # validation passed, skipped by lock

    def test_error_message_includes_invalid_repo(self):
        """エラーメッセージに無効な repo 名が含まれる。"""
        with patch("shiori.mcp_server.settings") as mock_settings:
            mock_settings.repos = ["owner/repo"]

            with pytest.raises(ValueError, match="evil/repo"):
                _do_sync(repos=["evil/repo"])

    def test_multiple_invalid_in_error_message(self):
        """複数の無効な repo がエラーメッセージに含まれる。"""
        with patch("shiori.mcp_server.settings") as mock_settings:
            mock_settings.repos = ["owner/repo"]

            with pytest.raises(ValueError) as exc_info:
                _do_sync(repos=["evil1/repo", "evil2/repo"])
            msg = str(exc_info.value)
            assert "evil1/repo" in msg
            assert "evil2/repo" in msg

    def test_empty_repos_list_is_valid(self):
        """空リストは settings.repos の部分集合なので検証通過。"""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._sync_lock") as mock_lock,
        ):
            mock_settings.repos = ["owner/repo"]
            mock_lock.acquire.return_value = False

            result = _do_sync(repos=[])
            assert result["status"] == "skipped"


# ===================================================================
# ingest: rebuild ガード
# ===================================================================


class TestIngestRebuildGuard:
    """shiori_ingest の rebuild=True ガード。

    ガードは _do_sync 呼び出し前に行われるため、settings と _do_sync のモックでテスト可能。
    """

    def test_rebuild_true_blocked_by_default(self):
        """rebuild=True raises ValueError when allow_rebuild=False (default)."""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._do_sync") as mock_do_sync,
        ):
            mock_settings.allow_rebuild = False
            mock_settings.repos = ["test/repo"]
            mock_do_sync.return_value = {"status": "ok", "repos": {}}

            with pytest.raises(ValueError, match="rebuild=True"):
                ingest(rebuild=True, repo="test/repo")

            mock_do_sync.assert_not_called()

    def test_rebuild_true_allowed_when_env_set(self):
        """allow_rebuild=True では rebuild=True が許可される。"""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._do_sync") as mock_do_sync,
        ):
            mock_settings.allow_rebuild = True
            mock_settings.repos = ["test/repo"]
            mock_do_sync.return_value = {"status": "ok", "repos": {}}

            result = ingest(rebuild=True, repo="test/repo")
            assert result == {"status": "ok", "repos": {}}
            mock_do_sync.assert_called_once_with(
                repos=["test/repo"], rebuild=True, route="mcp"
            )

    def test_rebuild_false_always_allowed(self):
        """rebuild=False は allow_rebuild の値に関わらず許可される。"""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._do_sync") as mock_do_sync,
        ):
            mock_settings.allow_rebuild = False
            mock_settings.repos = ["test/repo"]
            mock_do_sync.return_value = {"status": "ok", "repos": {}}

            result = ingest(rebuild=False, repo="test/repo")
            assert result == {"status": "ok", "repos": {}}
            mock_do_sync.assert_called_once_with(
                repos=["test/repo"], rebuild=False, route="mcp"
            )

    def test_rebuild_false_with_repo_none(self):
        """rebuild=False, repo=None も許可される。"""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._do_sync") as mock_do_sync,
        ):
            mock_settings.allow_rebuild = False
            mock_settings.repos = ["test/repo"]
            mock_do_sync.return_value = {"status": "ok", "repos": {}}

            result = ingest(rebuild=False)
            assert result == {"status": "ok", "repos": {}}
            mock_do_sync.assert_called_once_with(
                repos=None, rebuild=False, route="mcp"
            )

    def test_error_message_mentions_env_var(self):
        """エラーメッセージが環境変数名と CLI 代替手段を示している。"""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._do_sync"),
        ):
            mock_settings.allow_rebuild = False
            mock_settings.repos = ["test/repo"]

            with pytest.raises(ValueError) as exc_info:
                ingest(rebuild=True)
            msg = str(exc_info.value)
            assert "SHIORI_ALLOW_REBUILD" in msg
            assert "python -m shiori ingest --rebuild" in msg


# ===================================================================
# run_ingest (ingest.py): sync attempt recording (issue #194)
# ===================================================================


class TestRunIngestSyncAttemptRecording:
    """run_ingest() の per-repo ループが record_sync_attempt を呼ぶこと(issue #194).

    PR #191 は mcp_server.py の _do_sync() にのみ試行記録を追加していたため、
    CLI/compose 経由の run_ingest() 経路では成功/失敗のいずれでも
    record_sync_attempt が呼ばれず、consecutive_failures 系の警告が
    ingest 経路の失敗に対して永遠に発火しない問題があった。
    """

    def _mock_conn(self):
        """advisory lock 取得(fetchone -> (True,))と chunks 集計(fetchall -> [])を
        返すダミーの conn/cursor を用意する。"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (True,)
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        return mock_conn

    def test_success_records_sync_attempt_success(self):
        """全フェーズ成功時、record_sync_attempt(success=True) が呼ばれる。

        これが consecutive_failures をリセットする経路であり、record_sync_run だけでは
        リセットされない(issue #194 (b): 古い失敗警告が消えない実害の直接の修正対象)。
        """
        from shiori.ingest import run_ingest

        mock_conn = self._mock_conn()
        mock_settings = MagicMock()
        mock_settings.repos = ["owner/repo"]

        with (
            patch("shiori.ingest.db.connect", return_value=mock_conn),
            patch("shiori.ingest.db.migrate"),
            patch("shiori.ingest.db.migrate_light"),
            patch("shiori.ingest._is_bulk_path", return_value=False),
            patch("shiori.ingest.build_token_provider", return_value=MagicMock()),
            patch("shiori.ingest.Embedder", return_value=MagicMock()),
            patch("shiori.ingest.sync_docs", return_value=1),
            patch("shiori.ingest.sync_issues", return_value=2),
            patch("shiori.ingest.sync_code", return_value=3),
            patch(
                "shiori.ingest.db.record_sync_run",
                return_value=MagicMock(isoformat=lambda: "2026-01-01T00:00:00+00:00"),
            ),
            patch("shiori.ingest.db.record_sync_attempt") as mock_record_attempt,
        ):
            run_ingest(settings=mock_settings)

        mock_record_attempt.assert_called_once_with(
            mock_conn, "owner/repo", success=True
        )

    def test_failure_rolls_back_records_attempt_and_reraises(self):
        """フェーズが例外を送出したとき、rollback -> record_sync_attempt(success=False,
        error=...) -> re-raise される(issue #194 (a): 失敗が不可視だった実害の直接の修正対象)。

        _do_sync (mcp_server.py) と同じ流儀: 例外は握りつぶさず、記録してから
        呼び出し元へ伝播させる。
        """
        from shiori.ingest import run_ingest

        mock_conn = self._mock_conn()
        mock_settings = MagicMock()
        mock_settings.repos = ["owner/repo"]

        with (
            patch("shiori.ingest.db.connect", return_value=mock_conn),
            patch("shiori.ingest.db.migrate"),
            patch("shiori.ingest.db.migrate_light"),
            patch("shiori.ingest._is_bulk_path", return_value=False),
            patch("shiori.ingest.build_token_provider", return_value=MagicMock()),
            patch("shiori.ingest.Embedder", return_value=MagicMock()),
            patch("shiori.ingest.sync_docs", side_effect=RuntimeError("sync failed")),
            patch("shiori.ingest.db.record_sync_attempt") as mock_record_attempt,
        ):
            with pytest.raises(RuntimeError, match="sync failed"):
                run_ingest(settings=mock_settings)

        mock_conn.rollback.assert_called_once()
        mock_record_attempt.assert_called_once_with(
            mock_conn, "owner/repo", success=False, error="sync failed"
        )

    def test_failure_in_second_phase_still_records_attempt(self):
        """docs フェーズは成功し issues フェーズで失敗する場合でも記録される。"""
        from shiori.ingest import run_ingest

        mock_conn = self._mock_conn()
        mock_settings = MagicMock()
        mock_settings.repos = ["owner/repo"]

        with (
            patch("shiori.ingest.db.connect", return_value=mock_conn),
            patch("shiori.ingest.db.migrate"),
            patch("shiori.ingest.db.migrate_light"),
            patch("shiori.ingest._is_bulk_path", return_value=False),
            patch("shiori.ingest.build_token_provider", return_value=MagicMock()),
            patch("shiori.ingest.Embedder", return_value=MagicMock()),
            patch("shiori.ingest.sync_docs", return_value=1),
            patch("shiori.ingest.sync_issues", side_effect=RuntimeError("issues failed")),
            patch("shiori.ingest.db.record_sync_attempt") as mock_record_attempt,
        ):
            with pytest.raises(RuntimeError, match="issues failed"):
                run_ingest(settings=mock_settings)

        mock_record_attempt.assert_called_once_with(
            mock_conn, "owner/repo", success=False, error="issues failed"
        )


# ===================================================================
# _do_sync: pre-loop failure recording (issue #196, exposed by #195)
# ===================================================================


class TestDoSyncPreLoopFailureRecording:
    """_do_sync() が per-repo ループに入る前の失敗(embedder 生成失敗、
    token provider 構築失敗)でも record_sync_attempt を記録すること(issue #196)。

    #195(app イメージに sentence-transformers 不在)がこの穴を実機で顕在化させた:
    _get_embedder() が ModuleNotFoundError を投げると、per-repo ループの
    try/except に一度も入らないため、以前は sync_runs に一切記録されなかった。
    """

    def _mock_conn_cm(self):
        """`with _conn() as conn:` として振る舞うダミーの conn を用意する。"""
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = False
        return mock_conn

    def test_embedder_import_failure_recorded_for_all_targets(self):
        """_get_embedder() の ModuleNotFoundError が全対象 repo に記録される(#195 の直接再現)。"""
        mock_conn = self._mock_conn_cm()

        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._sync_lock") as mock_lock,
            patch("shiori.mcp_server.build_token_provider", return_value=MagicMock()),
            patch(
                "shiori.mcp_server._get_embedder",
                side_effect=ModuleNotFoundError(
                    "No module named 'sentence_transformers'"
                ),
            ),
            patch("shiori.mcp_server._conn", return_value=mock_conn),
            patch("shiori.mcp_server.db.record_sync_attempt") as mock_record_attempt,
        ):
            mock_settings.repos = ["owner/repo1", "owner/repo2"]
            mock_lock.acquire.return_value = True

            with pytest.raises(ModuleNotFoundError, match="sentence_transformers"):
                _do_sync()

        assert mock_record_attempt.call_count == 2
        mock_record_attempt.assert_any_call(
            mock_conn,
            "owner/repo1",
            success=False,
            error="No module named 'sentence_transformers'",
        )
        mock_record_attempt.assert_any_call(
            mock_conn,
            "owner/repo2",
            success=False,
            error="No module named 'sentence_transformers'",
        )

    def test_token_provider_failure_recorded_before_loop(self):
        """build_token_provider() の ValueError(#193 と同根)も記録される。"""
        mock_conn = self._mock_conn_cm()

        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._sync_lock") as mock_lock,
            patch(
                "shiori.mcp_server.build_token_provider",
                side_effect=ValueError("GitHub App configuration is incomplete"),
            ),
            patch("shiori.mcp_server._conn", return_value=mock_conn),
            patch("shiori.mcp_server.db.record_sync_attempt") as mock_record_attempt,
        ):
            mock_settings.repos = ["owner/repo"]
            mock_lock.acquire.return_value = True

            with pytest.raises(ValueError, match="GitHub App configuration"):
                _do_sync()

        mock_record_attempt.assert_called_once_with(
            mock_conn,
            "owner/repo",
            success=False,
            error="GitHub App configuration is incomplete",
        )

    def test_lock_released_after_pre_loop_failure(self):
        """事前失敗後も _sync_lock が release されること(既存の finally 経路は健在)。"""
        mock_conn = self._mock_conn_cm()

        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._sync_lock") as mock_lock,
            patch(
                "shiori.mcp_server.build_token_provider",
                side_effect=ValueError("boom"),
            ),
            patch("shiori.mcp_server._conn", return_value=mock_conn),
            patch("shiori.mcp_server.db.record_sync_attempt"),
        ):
            mock_settings.repos = ["owner/repo"]
            mock_lock.acquire.return_value = True

            with pytest.raises(ValueError, match="boom"):
                _do_sync()

        mock_lock.release.assert_called_once()

    def test_pre_loop_recording_failure_does_not_mask_original_exception(self):
        """記録用の _conn() 自体が失敗しても、元の例外がそのまま伝播する(DB不達ケース)。"""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._sync_lock") as mock_lock,
            patch(
                "shiori.mcp_server.build_token_provider",
                side_effect=ValueError("original failure"),
            ),
            patch(
                "shiori.mcp_server._conn",
                side_effect=RuntimeError("database unreachable"),
            ),
        ):
            mock_settings.repos = ["owner/repo"]
            mock_lock.acquire.return_value = True

            with pytest.raises(ValueError, match="original failure"):
                _do_sync()


# ===================================================================
# _do_sync: mid-stage failure recording (issue #196)
# DB接続後・per-repoループ前(bulk判定 / migrate / advisory lock 取得)
# ===================================================================


class TestDoSyncMidStageFailureRecording:
    """_do_sync() の DB接続後・per-repoループ前の失敗記録(issue #196)。

    pre-loop(provider/embedder 構築、DB接続前)とは別の中段の try/except:
    bulk-path 検出・migrate・advisory lock 取得の失敗は、既に生きている conn を
    使って rollback → 全対象 repo へ record_sync_attempt(success=False) → 再raise
    される。_record_pre_loop_sync_failure のような使い捨て接続は開かない。
    """

    def _mock_conn_cm(self, cursor_execute_side_effect=None):
        """`with _conn() as conn:` として振る舞うダミー conn。

        conn.cursor() はコンテキストマネージャとして cursor を返す。
        cursor_execute_side_effect を渡すと cursor.execute に仕込む
        (advisory lock 取得失敗の模擬用)。
        """
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = False
        mock_cursor = MagicMock()
        if cursor_execute_side_effect is not None:
            mock_cursor.execute.side_effect = cursor_execute_side_effect
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        return mock_conn

    def test_migrate_failure_recorded_for_all_targets_with_live_conn(self):
        """db.migrate() の失敗で、生きている conn を使って全対象 repo に記録される。"""
        mock_conn = self._mock_conn_cm()

        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._sync_lock") as mock_lock,
            patch("shiori.mcp_server.build_token_provider", return_value=MagicMock()),
            patch("shiori.mcp_server._get_embedder", return_value=MagicMock()),
            patch("shiori.mcp_server._conn", return_value=mock_conn) as mock_conn_factory,
            patch("shiori.mcp_server._is_bulk_path", return_value=False),
            patch(
                "shiori.mcp_server.db.migrate",
                side_effect=RuntimeError("migrate failed"),
            ),
            patch("shiori.mcp_server.db.record_sync_attempt") as mock_record_attempt,
        ):
            mock_settings.repos = ["owner/repo1", "owner/repo2"]
            mock_lock.acquire.return_value = True

            with pytest.raises(RuntimeError, match="migrate failed"):
                _do_sync()

        # rollback happens before recording; recording targets every repo and
        # uses the already-open conn (no throwaway connection: _conn called once).
        mock_conn.rollback.assert_called_once()
        assert mock_conn_factory.call_count == 1
        assert mock_record_attempt.call_count == 2
        mock_record_attempt.assert_any_call(
            mock_conn, "owner/repo1", success=False, error="migrate failed"
        )
        mock_record_attempt.assert_any_call(
            mock_conn, "owner/repo2", success=False, error="migrate failed"
        )

    def test_advisory_lock_acquisition_failure_recorded(self):
        """advisory lock 取得の execute 失敗でも記録され、元例外が伝播する。"""

        def explode_on_lock_query(query, *args, **kwargs):
            if "pg_try_advisory_lock" in query:
                raise RuntimeError("lock query failed")

        mock_conn = self._mock_conn_cm(
            cursor_execute_side_effect=explode_on_lock_query
        )

        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._sync_lock") as mock_lock,
            patch("shiori.mcp_server.build_token_provider", return_value=MagicMock()),
            patch("shiori.mcp_server._get_embedder", return_value=MagicMock()),
            patch("shiori.mcp_server._conn", return_value=mock_conn),
            patch("shiori.mcp_server._is_bulk_path", return_value=False),
            patch("shiori.mcp_server.db.migrate"),
            patch("shiori.mcp_server.db.record_sync_attempt") as mock_record_attempt,
        ):
            mock_settings.repos = ["owner/repo"]
            mock_lock.acquire.return_value = True

            with pytest.raises(RuntimeError, match="lock query failed"):
                _do_sync()

        mock_conn.rollback.assert_called_once()
        mock_record_attempt.assert_called_once_with(
            mock_conn, "owner/repo", success=False, error="lock query failed"
        )

    def test_bulk_path_detection_failure_recorded(self):
        """_is_bulk_path() の失敗(中段の最初のステップ)でも記録される。"""
        mock_conn = self._mock_conn_cm()

        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._sync_lock") as mock_lock,
            patch("shiori.mcp_server.build_token_provider", return_value=MagicMock()),
            patch("shiori.mcp_server._get_embedder", return_value=MagicMock()),
            patch("shiori.mcp_server._conn", return_value=mock_conn),
            patch(
                "shiori.mcp_server._is_bulk_path",
                side_effect=RuntimeError("bulk detection failed"),
            ),
            patch("shiori.mcp_server.db.record_sync_attempt") as mock_record_attempt,
        ):
            mock_settings.repos = ["owner/repo"]
            mock_lock.acquire.return_value = True

            with pytest.raises(RuntimeError, match="bulk detection failed"):
                _do_sync()

        mock_record_attempt.assert_called_once_with(
            mock_conn, "owner/repo", success=False, error="bulk detection failed"
        )

    def test_sync_lock_released_after_mid_stage_failure(self):
        """中段失敗後も _sync_lock が release されること(外側の finally 経路)。"""
        mock_conn = self._mock_conn_cm()

        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._sync_lock") as mock_lock,
            patch("shiori.mcp_server.build_token_provider", return_value=MagicMock()),
            patch("shiori.mcp_server._get_embedder", return_value=MagicMock()),
            patch("shiori.mcp_server._conn", return_value=mock_conn),
            patch("shiori.mcp_server._is_bulk_path", return_value=False),
            patch(
                "shiori.mcp_server.db.migrate",
                side_effect=RuntimeError("migrate failed"),
            ),
            patch("shiori.mcp_server.db.record_sync_attempt"),
        ):
            mock_settings.repos = ["owner/repo"]
            mock_lock.acquire.return_value = True

            with pytest.raises(RuntimeError, match="migrate failed"):
                _do_sync()

        mock_lock.release.assert_called_once()


# ===================================================================
# _do_sync: per-repo continue on failure, aggregate raise (issue #199)
# ===================================================================


class TestDoSyncPerRepoContinueOnFailure:
    """diff sync (is_bulk=False) の per-repo ループは1リポジトリの失敗で

    後続リポジトリの試行を止めない。全リポジトリ処理後、失敗が1件でも
    あれば集約例外を raise する(issue #199)。bulk path (初回一括) は
    従来どおり最初の失敗で即中断する(issue #199 論点)。
    """

    def _mock_conn_cm(self):
        """`with _conn() as conn:` として振る舞うダミーの conn を用意する。"""
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = False
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        return mock_conn

    def test_second_repo_still_syncs_after_first_fails(self):
        """1つ目の repo が失敗しても2つ目は同期され、両方の attempt が記録され、

        最後に集約例外が上がる。
        """
        mock_conn = self._mock_conn_cm()

        def fake_sync_docs(settings, conn, embedder, repo, provider, buffer=None):
            if repo == "owner/repo1":
                raise RuntimeError("boom")
            return 1

        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._sync_lock") as mock_lock,
            patch("shiori.mcp_server.build_token_provider", return_value=MagicMock()),
            patch("shiori.mcp_server._get_embedder", return_value=MagicMock()),
            patch("shiori.mcp_server._conn", return_value=mock_conn),
            patch("shiori.mcp_server._is_bulk_path", return_value=False),
            patch("shiori.mcp_server.db.migrate"),
            patch("shiori.mcp_server.sync_docs", side_effect=fake_sync_docs),
            patch("shiori.mcp_server.sync_issues", return_value=2),
            patch("shiori.mcp_server.sync_code", return_value=3),
            patch(
                "shiori.mcp_server.db.record_sync_run",
                return_value=MagicMock(isoformat=lambda: "2026-01-01T00:00:00+00:00"),
            ),
            patch("shiori.mcp_server.db.record_sync_attempt") as mock_record_attempt,
        ):
            mock_settings.repos = ["owner/repo1", "owner/repo2"]
            mock_lock.acquire.return_value = True

            with pytest.raises(RuntimeError, match="owner/repo1"):
                _do_sync()

        # Both repos got a recorded attempt: repo1 failed, repo2 succeeded --
        # repo2 was not skipped just because repo1 failed first.
        mock_record_attempt.assert_any_call(
            mock_conn, "owner/repo1", success=False, error="boom"
        )
        mock_record_attempt.assert_any_call(mock_conn, "owner/repo2", success=True)
        assert mock_record_attempt.call_count == 2
        # Only the failing repo triggered a rollback; repo2's own commits
        # (via record_sync_run / record_sync_attempt) are untouched.
        mock_conn.rollback.assert_called_once()

    def test_all_success_no_exception(self):
        """全 repo 成功時は例外なく result が返る。"""
        mock_conn = self._mock_conn_cm()

        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._sync_lock") as mock_lock,
            patch("shiori.mcp_server.build_token_provider", return_value=MagicMock()),
            patch("shiori.mcp_server._get_embedder", return_value=MagicMock()),
            patch("shiori.mcp_server._conn", return_value=mock_conn),
            patch("shiori.mcp_server._is_bulk_path", return_value=False),
            patch("shiori.mcp_server.db.migrate"),
            patch("shiori.mcp_server.sync_docs", return_value=1),
            patch("shiori.mcp_server.sync_issues", return_value=2),
            patch("shiori.mcp_server.sync_code", return_value=3),
            patch(
                "shiori.mcp_server.db.record_sync_run",
                return_value=MagicMock(isoformat=lambda: "2026-01-01T00:00:00+00:00"),
            ),
            patch("shiori.mcp_server.db.record_sync_attempt") as mock_record_attempt,
        ):
            mock_settings.repos = ["owner/repo1", "owner/repo2"]
            mock_lock.acquire.return_value = True

            result = _do_sync()

        assert result["status"] == "ok"
        assert set(result["repos"]) == {"owner/repo1", "owner/repo2"}
        mock_record_attempt.assert_any_call(mock_conn, "owner/repo1", success=True)
        mock_record_attempt.assert_any_call(mock_conn, "owner/repo2", success=True)
        mock_conn.rollback.assert_not_called()

    def test_bulk_path_still_aborts_immediately_on_first_failure(self):
        """bulk path (初回一括) は #199 の対象外で、従来どおり即中断する。"""
        mock_conn = self._mock_conn_cm()

        def fake_sync_docs(settings, conn, embedder, repo, provider, buffer=None):
            if repo == "owner/repo1":
                raise RuntimeError("boom")
            return 1

        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._sync_lock") as mock_lock,
            patch("shiori.mcp_server.build_token_provider", return_value=MagicMock()),
            patch("shiori.mcp_server._get_embedder", return_value=MagicMock()),
            patch("shiori.mcp_server._conn", return_value=mock_conn),
            patch("shiori.mcp_server._is_bulk_path", return_value=True),
            patch("shiori.mcp_server.db.migrate_light"),
            patch("shiori.mcp_server.db.drop_heavy_indexes"),
            patch("shiori.mcp_server.ChunkBuffer", return_value=MagicMock()),
            patch("shiori.mcp_server.sync_docs", side_effect=fake_sync_docs),
            patch("shiori.mcp_server.sync_issues", return_value=2),
            patch("shiori.mcp_server.sync_code", return_value=3),
            patch("shiori.mcp_server.db.record_sync_attempt") as mock_record_attempt,
        ):
            mock_settings.repos = ["owner/repo1", "owner/repo2"]
            mock_lock.acquire.return_value = True

            with pytest.raises(RuntimeError, match="boom"):
                _do_sync(rebuild=True)

        # Only the first (failing) repo was attempted; repo2 never ran.
        mock_record_attempt.assert_called_once_with(
            mock_conn, "owner/repo1", success=False, error="boom"
        )


# ===================================================================
# run_ingest (ingest.py): per-repo continue on failure, aggregate raise
# (issue #199 -- mirrors _do_sync in mcp_server.py)
# ===================================================================


class TestRunIngestPerRepoContinueOnFailure:
    """diff sync (is_bulk=False) の per-repo ループが1リポジトリの失敗で

    後続リポジトリを止めない、run_ingest 版(issue #199)。
    """

    def _mock_conn(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (True,)
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        return mock_conn

    def test_second_repo_still_syncs_after_first_fails(self):
        from shiori.ingest import run_ingest

        mock_conn = self._mock_conn()
        mock_settings = MagicMock()
        mock_settings.repos = ["owner/repo1", "owner/repo2"]

        def fake_sync_docs(settings, conn, embedder, repo, provider, buffer=None):
            if repo == "owner/repo1":
                raise RuntimeError("boom")
            return 1

        with (
            patch("shiori.ingest.db.connect", return_value=mock_conn),
            patch("shiori.ingest.db.migrate"),
            patch("shiori.ingest.db.migrate_light"),
            patch("shiori.ingest._is_bulk_path", return_value=False),
            patch("shiori.ingest.build_token_provider", return_value=MagicMock()),
            patch("shiori.ingest.Embedder", return_value=MagicMock()),
            patch("shiori.ingest.sync_docs", side_effect=fake_sync_docs),
            patch("shiori.ingest.sync_issues", return_value=2),
            patch("shiori.ingest.sync_code", return_value=3),
            patch(
                "shiori.ingest.db.record_sync_run",
                return_value=MagicMock(isoformat=lambda: "2026-01-01T00:00:00+00:00"),
            ),
            patch("shiori.ingest.db.record_sync_attempt") as mock_record_attempt,
        ):
            with pytest.raises(RuntimeError, match="owner/repo1"):
                run_ingest(settings=mock_settings)

        mock_record_attempt.assert_any_call(
            mock_conn, "owner/repo1", success=False, error="boom"
        )
        mock_record_attempt.assert_any_call(mock_conn, "owner/repo2", success=True)
        assert mock_record_attempt.call_count == 2
        mock_conn.rollback.assert_called_once()

    def test_all_success_no_exception(self):
        from shiori.ingest import run_ingest

        mock_conn = self._mock_conn()
        mock_settings = MagicMock()
        mock_settings.repos = ["owner/repo1", "owner/repo2"]

        with (
            patch("shiori.ingest.db.connect", return_value=mock_conn),
            patch("shiori.ingest.db.migrate"),
            patch("shiori.ingest.db.migrate_light"),
            patch("shiori.ingest._is_bulk_path", return_value=False),
            patch("shiori.ingest.build_token_provider", return_value=MagicMock()),
            patch("shiori.ingest.Embedder", return_value=MagicMock()),
            patch("shiori.ingest.sync_docs", return_value=1),
            patch("shiori.ingest.sync_issues", return_value=2),
            patch("shiori.ingest.sync_code", return_value=3),
            patch(
                "shiori.ingest.db.record_sync_run",
                return_value=MagicMock(isoformat=lambda: "2026-01-01T00:00:00+00:00"),
            ),
            patch("shiori.ingest.db.record_sync_attempt") as mock_record_attempt,
        ):
            run_ingest(settings=mock_settings)  # must not raise

        mock_record_attempt.assert_any_call(mock_conn, "owner/repo1", success=True)
        mock_record_attempt.assert_any_call(mock_conn, "owner/repo2", success=True)
        mock_conn.rollback.assert_not_called()

    def test_bulk_path_still_aborts_immediately_on_first_failure(self):
        from shiori.ingest import run_ingest

        mock_conn = self._mock_conn()
        mock_settings = MagicMock()
        mock_settings.repos = ["owner/repo1", "owner/repo2"]

        def fake_sync_docs(settings, conn, embedder, repo, provider, buffer=None):
            if repo == "owner/repo1":
                raise RuntimeError("boom")
            return 1

        with (
            patch("shiori.ingest.db.connect", return_value=mock_conn),
            patch("shiori.ingest.db.migrate"),
            patch("shiori.ingest.db.migrate_light"),
            patch("shiori.ingest._is_bulk_path", return_value=True),
            patch("shiori.ingest.db.drop_heavy_indexes"),
            patch("shiori.ingest.ChunkBuffer", return_value=MagicMock()),
            patch("shiori.ingest.build_token_provider", return_value=MagicMock()),
            patch("shiori.ingest.Embedder", return_value=MagicMock()),
            patch("shiori.ingest.sync_docs", side_effect=fake_sync_docs),
            patch("shiori.ingest.sync_issues", return_value=2),
            patch("shiori.ingest.sync_code", return_value=3),
            patch("shiori.ingest.db.record_sync_attempt") as mock_record_attempt,
        ):
            with pytest.raises(RuntimeError, match="boom"):
                run_ingest(settings=mock_settings, rebuild=True)

        mock_record_attempt.assert_called_once_with(
            mock_conn, "owner/repo1", success=False, error="boom"
        )
