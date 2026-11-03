"""Real PostgreSQL acceptance checks for versioned search snapshots and forget."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from shiori.config import Settings
from shiori.schema import forget_repo, migrate_light

# Dedicated test-only lock key; production search and forget locks use other keys.
_EXTENSION_BOOTSTRAP_LOCK = (472, 1)


def _bootstrap_extensions(dsn: str) -> None:
    """Install test prerequisites in public once, outside disposable schemas."""
    with psycopg.connect(dsn) as admin:
        admin.execute("SELECT pg_advisory_xact_lock(%s, %s)", _EXTENSION_BOOTSTRAP_LOCK)
        for extension in ("vector", "pgroonga"):
            admin.execute(
                sql.SQL("CREATE EXTENSION IF NOT EXISTS {} WITH SCHEMA public").format(
                    sql.Identifier(extension)
                )
            )


def _assert_extensions_persist(dsn: str) -> None:
    with psycopg.connect(dsn) as admin:
        rows = admin.execute(
            """SELECT extname, nspname
               FROM pg_extension JOIN pg_namespace ON extnamespace = pg_namespace.oid
               WHERE extname IN (%s, %s) ORDER BY extname""",
            ("pgroonga", "vector"),
        ).fetchall()
        assert rows == [("pgroonga", "public"), ("vector", "public")]
        assert admin.execute("SELECT to_regtype(%s)", ("public.vector",)).fetchone()[0]


@pytest.fixture(scope="module")
def snapshot_db():
    dsn = os.environ.get(
        "SHIORI_TEST_DATABASE_URL",
        "postgresql://shiori_test:shiori_test@127.0.0.1:55432/shiori_test",
    )
    _bootstrap_extensions(dsn)
    schema_name = "shiori_471_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
    options = f"-c search_path={schema_name},public"
    try:
        with psycopg.connect(dsn, options=options) as conn:
            migrate_light(conn, Settings())
        yield dsn, options, schema_name
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )
        _assert_extensions_persist(dsn)


@pytest.fixture
def db(snapshot_db):
    dsn, options, _ = snapshot_db
    with psycopg.connect(dsn, options=options) as conn:
        conn.execute("TRUNCATE search_log, chunks RESTART IDENTITY CASCADE")
        conn.commit()
        yield conn
        conn.rollback()


def _chunk(conn, repo: str, key: str, content: str) -> int:
    row = conn.execute(
        """INSERT INTO chunks
           (chunk_key, chunk_index, source_type, repo, path, content)
           VALUES (%s, 0, 'doc', %s, %s, %s) RETURNING id""",
        (f"{repo}:{key}", repo, key, content),
    ).fetchone()
    assert row is not None
    return row[0]


def _log(
    conn,
    log_id: int,
    results: list[dict],
    *,
    format_version: int | None,
    original_count: int | None,
    removed_count: int | None,
) -> None:
    conn.execute(
        """INSERT INTO search_log
           (id, query, search_type, top_k, filters, results, format_version,
            original_result_count, removed_result_count)
           VALUES (%s, 'dummy query', 'keyword', 10, %s, %s, %s, %s, %s)""",
        (
            log_id,
            Jsonb({"source_type": "doc"}),
            Jsonb(results),
            format_version,
            original_count,
            removed_count,
        ),
    )


def _snapshot(chunk_id: int, repo: str, content: str) -> dict:
    return {
        "id": chunk_id,
        "score": 0.25,
        "vec_score": None,
        "vec_rank": None,
        "kw_score": 0.75,
        "kw_rank": 0,
        "source_type": "doc",
        "repo": repo,
        "chunk_key": f"doc:{repo}:guide.md",
        "chunk_index": 0,
        "path": "guide.md",
        "url": "https://example.invalid/guide",
        "heading_path": "Guide",
        "line": None,
        "end_line": None,
        "commit_sha": None,
        "content_snapshot": content,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def test_light_migration_adds_nullable_metadata_without_rewriting_legacy_rows(db) -> None:
    """Legacy values stay intact and unversioned when the additive migration runs."""
    db.execute(
        """INSERT INTO search_log (id, query, search_type, top_k, filters, results)
           VALUES (700, 'legacy query', 'keyword', 4, %s, %s)""",
        (Jsonb({"repo": "old/repo"}), Jsonb([{"id": 9, "score": 0.5}])),
    )
    db.commit()

    migrate_light(db, Settings())

    row = db.execute(
        """SELECT query, filters, results, format_version, original_result_count,
                  removed_result_count
           FROM search_log WHERE id = 700"""
    ).fetchone()
    assert row == (
        "legacy query",
        {"repo": "old/repo"},
        [{"id": 9, "score": 0.5}],
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    "present_columns",
    [
        ("format_version",),
        ("original_result_count",),
        ("removed_result_count",),
        ("format_version", "original_result_count"),
        ("format_version", "removed_result_count"),
        ("original_result_count", "removed_result_count"),
    ],
)
def test_partial_snapshot_columns(db, present_columns) -> None:
    """Partial v2 metadata must abort before index or historical snapshots are deleted."""
    repo = f"partial/{uuid.uuid4().hex}"
    item = _snapshot(_chunk(db, repo, "guide.md", "indexed body"), repo, "historic body")
    _log(db, 700, [item], format_version=2, original_count=1, removed_count=0)
    db.execute(
        "INSERT INTO repo_index_state (repo, clone_head, indexed_head) VALUES (%s, 'head', 'head')",
        (repo,),
    )
    db.commit()

    all_columns = {
        "format_version",
        "original_result_count",
        "removed_result_count",
    }
    for column in sorted(all_columns - set(present_columns)):
        db.execute(f"ALTER TABLE search_log DROP COLUMN {column}")

    with pytest.raises(RuntimeError, match="partial search_log snapshot metadata"):
        forget_repo(db, repo)

    db.rollback()
    assert db.execute(
        "SELECT repo, clone_head, indexed_head FROM repo_index_state WHERE repo = %s",
        (repo,),
    ).fetchone() == (repo, "head", "head")
    assert db.execute(
        """SELECT results, format_version, original_result_count, removed_result_count
           FROM search_log WHERE id = 700"""
    ).fetchone() == ([item], 2, 1, 0)


def test_forget_purges_low_id_v2_and_preserves_legacy_rows(db) -> None:
    """Purge every v2 ID, leave low/high legacy logs intact, and count only new removals."""
    target_id = _chunk(db, "gone/repo", "guide.md", "target text")
    keep_id = _chunk(db, "keep/repo", "guide.md", "keep text")
    low = _snapshot(target_id, "gone/repo", "low v2")
    low_with_prior_removal = _snapshot(target_id, "gone/repo", "low v2 with prior removal")
    at_old_cutoff = _snapshot(target_id, "gone/repo", "v2 at old cutoff")
    legacy_low = _snapshot(target_id, "gone/repo", "low legacy stays")
    legacy_high = _snapshot(target_id, "gone/repo", "high legacy stays")
    current = _snapshot(target_id, "gone/repo", "target text")
    mixed = [current, _snapshot(keep_id, "keep/repo", "keep text")]
    emptied = _snapshot(target_id, "gone/repo", "target only")

    _log(db, 1, [low], format_version=2, original_count=1, removed_count=0)
    _log(db, 2, [legacy_low], format_version=None, original_count=None, removed_count=None)
    _log(db, 43, [low_with_prior_removal], format_version=2, original_count=3, removed_count=2)
    _log(db, 585, [at_old_cutoff], format_version=2, original_count=1, removed_count=0)
    _log(db, 586, [legacy_high], format_version=None, original_count=None, removed_count=None)
    _log(db, 600, mixed, format_version=2, original_count=2, removed_count=0)
    _log(db, 601, [emptied], format_version=2, original_count=1, removed_count=0)
    _log(db, 700, [legacy_high], format_version=None, original_count=None, removed_count=None)
    db.commit()

    deleted = forget_repo(db, "gone/repo")
    db.commit()

    assert deleted["search_log_results"] == 5
    rows = db.execute(
        """SELECT id, results, original_result_count, removed_result_count
           FROM search_log ORDER BY id"""
    ).fetchall()
    assert rows == [
        (1, [], 1, 1),
        (2, [legacy_low], None, None),
        (43, [], 3, 3),
        (585, [], 1, 1),
        (586, [legacy_high], None, None),
        (600, [mixed[1]], 2, 1),
        (601, [], 1, 1),
        (700, [legacy_high], None, None),
    ]
    assert db.execute("SELECT count(*) FROM chunks WHERE repo = 'gone/repo'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM chunks WHERE repo = 'keep/repo'").fetchone()[0] == 1

    deleted_again = forget_repo(db, "gone/repo")
    db.commit()
    assert deleted_again["search_log_results"] == 0
    assert db.execute(
        "SELECT results, original_result_count, removed_result_count FROM search_log WHERE id = 43"
    ).fetchone() == ([], 3, 3)
    assert db.execute(
        "SELECT results, original_result_count, removed_result_count FROM search_log WHERE id = 600"
    ).fetchone() == ([mixed[1]], 2, 1)


def test_export_uses_historic_snapshot_when_chunk_is_missing_or_changed(db, tmp_path: Path) -> None:
    """A valid v2 export row comes from the logged snapshot, independent of chunks."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.search_log_export import export_search_logs

    chunk_id = _chunk(db, "gone/repo", "guide.md", "mutable current body")
    original = "historic content remains exact\n"
    item = _snapshot(chunk_id, "gone/repo", original)
    _log(db, 600, [item], format_version=2, original_count=1, removed_count=0)
    db.execute("UPDATE chunks SET content = 'changed current body' WHERE id = %s", (chunk_id,))
    db.commit()

    calibration = tmp_path / "calibration"
    output = calibration / "snapshot.jsonl"
    summary = export_search_logs(db, output, calibration_root=calibration)
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

    assert summary["searches_exported"] == 1
    assert summary["results_exported"] == 1
    assert records[0]["snippet"] == original
    assert records[0]["score"] == 0.25
    assert records[0]["vec_score"] is None
    assert records[0]["kw_score"] == 0.75

def test_keyword_search_logs_exact_retrieved_snapshots_for_all_sources_and_zero_hits(db) -> None:
    """New logs carry exact chunk metadata/hash; empty queries still get v2 counts."""
    from shiori.schema import create_heavy_indexes
    from shiori.search import keyword_search

    settings = Settings()
    settings.snippet_chars = 8
    create_heavy_indexes(db, settings)
    rows = [
        ("doc", "one/repo", "docs/a.md", None, None, None, "Heading", 5, 8, "snapshotneedle doc body"),
        ("code", "two/repo", "src/a.py", None, None, "python", "function", 11, 19, "snapshotneedle code body"),
        ("issue", "three/repo", None, 314, 0, None, "Issue title", None, None, "snapshotneedle issue body"),
        ("pr_review", "four/repo", "src/review.py", 315, 77, "python", "Review title", 21, 22, "snapshotneedle review body"),
    ]
    expected_by_content = {}
    for source_type, repo, path, issue_no, comment_id, prog_lang, heading, line, end_line, content in rows:
        chunk_key = f"{source_type}:{repo}:{path or issue_no}:{comment_id}"
        db.execute(
            """INSERT INTO chunks
               (chunk_key, chunk_index, source_type, repo, path, issue_no, comment_id,
                kind, language, heading_path, content, line, end_line, commit_sha,
                prog_lang, url)
               VALUES (%s, 0, %s, %s, %s, %s, %s, %s, 'en', %s, %s, %s, %s,
                       'deadbeef', %s, %s)
               RETURNING id""",
            (
                chunk_key, source_type, repo, path, issue_no, comment_id,
                "issue" if source_type == "issue" else None, heading, content,
                line, end_line, prog_lang, f"https://example.invalid/{source_type}",
            ),
        )
        chunk_id = db.execute(
            "SELECT id FROM chunks WHERE chunk_key = %s", (chunk_key,)
        ).fetchone()[0]
        expected_by_content[content] = (chunk_id, source_type, repo, chunk_key)
    db.commit()

    hits = keyword_search(settings, db, "snapshotneedle", top_k=10)
    assert len(hits) == 4
    logged = db.execute(
        """SELECT format_version, original_result_count, removed_result_count, results
           FROM search_log ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    assert logged is not None
    version, original_count, removed_count, results = logged
    assert (version, original_count, removed_count) == (2, 4, 0)
    assert len(results) == 4
    for item in results:
        content = item["content_snapshot"]
        chunk_id, source_type, repo, chunk_key = expected_by_content[content]
        assert item["id"] == chunk_id
        assert item["source_type"] == source_type
        assert item["repo"] == repo
        assert item["chunk_key"] == chunk_key
        assert item["chunk_index"] == 0
        assert item["content_sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert "snippet" not in item
        if len(content) > settings.snippet_chars:
            matching_hit = next(
                hit for hit in hits
                if hit["source_type"] == source_type
                and hit["repo"] == repo
                and hit.get("path") == item.get("path")
                and hit.get("issue_no") == item.get("issue_no")
            )
            assert item["display_snippet"] == matching_hit["snippet"]

    short_chunk = _chunk(db, "short/repo", "short.md", "tiny")
    db.commit()
    short_hits = keyword_search(settings, db, "tiny", top_k=3)
    short_log = db.execute(
        "SELECT results FROM search_log ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    short_item = next(item for item in short_log if item["repo"] == "short/repo")
    assert short_item["id"] == short_chunk
    assert short_item["content_snapshot"] == "tiny"
    assert "display_snippet" not in short_item
    assert any(hit["repo"] == "short/repo" and hit["snippet"] == "tiny" for hit in short_hits)

    keyword_search(settings, db, "needlewithnoresult", top_k=3)
    empty = db.execute(
        """SELECT format_version, original_result_count, removed_result_count, results
           FROM search_log ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    assert empty == (2, 0, 0, [])


@pytest.fixture
def race_db(snapshot_db):
    """Private schema with metadata prerequisites for isolating transaction races."""
    from shiori.schema import create_heavy_indexes

    dsn, _, _ = snapshot_db
    schema_name = "shiori_471_race_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
    options = f"-c search_path={schema_name},public"
    try:
        with psycopg.connect(dsn, options=options) as conn:
            migrate_light(conn, Settings())
            conn.execute(
                """
                ALTER TABLE search_log
                  ADD COLUMN IF NOT EXISTS format_version INTEGER,
                  ADD COLUMN IF NOT EXISTS original_result_count INTEGER,
                  ADD COLUMN IF NOT EXISTS removed_result_count INTEGER
                """
            )
            create_heavy_indexes(conn, Settings())
            conn.commit()
        yield dsn, options
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )


def _race_connection(race_db):
    dsn, options = race_db
    return psycopg.connect(dsn, options=options)


def _seed_race_chunk(conn, repo: str = "race/target") -> int:
    return _chunk(conn, repo, "guide.md", "raceprobe target searchable content")


def _set_search_log_sequence(conn) -> None:
    conn.execute(
        """
        SELECT setval(pg_get_serial_sequence('search_log', 'id'), 585, true)
        """
    )


def _is_blocked(conn, pid: int, timeout: float = 3.0) -> bool:
    """Poll PostgreSQL's blocker graph; timing alone is not race evidence."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        blocked = conn.execute(
            "SELECT cardinality(pg_blocking_pids(%s)) > 0", (pid,)
        ).fetchone()[0]
        if blocked:
            return True
        time.sleep(0.02)
    return False


def _join_thread(thread: threading.Thread, label: str) -> None:
    thread.join(timeout=5)
    assert not thread.is_alive(), f"{label} thread did not finish within 5 seconds"


def test_search_first_forget_waits_for_repo_unspecified_search_snapshot(race_db, monkeypatch) -> None:
    """A search that read a candidate commits before concurrent forget can finish."""
    import shiori.search as search_module
    from shiori.search import keyword_search

    settings = Settings()
    candidate_read = threading.Event()
    allow_search = threading.Event()
    forget_ready = threading.Event()
    failures = []
    search_hits = []
    forget_results = {}
    pids = {}

    with _race_connection(race_db) as seed:
        _seed_race_chunk(seed)
        _set_search_log_sequence(seed)
        seed.commit()

    actual_candidates = search_module._keyword_candidates

    def paused_candidates(*args, **kwargs):
        rows = actual_candidates(*args, **kwargs)
        candidate_read.set()
        if not allow_search.wait(timeout=5):
            raise AssertionError("search candidate barrier was not released")
        return rows

    monkeypatch.setattr(search_module, "_keyword_candidates", paused_candidates)

    def search_worker():
        try:
            with _race_connection(race_db) as conn:
                pids["search"] = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
                search_hits.extend(keyword_search(settings, conn, "raceprobe"))
                conn.commit()
        except Exception as exc:  # noqa: BLE001 - relay worker failures to the test thread
            failures.append(exc)

    def forget_worker():
        try:
            with _race_connection(race_db) as conn:
                pids["forget"] = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
                forget_ready.set()
                forget_results.update(forget_repo(conn, "race/target"))
                conn.commit()
        except Exception as exc:  # noqa: BLE001 - relay worker failures to the test thread
            failures.append(exc)

    search_thread = threading.Thread(target=search_worker, daemon=True)
    forget_thread = threading.Thread(target=forget_worker, daemon=True)
    search_thread.start()
    was_blocked = False
    try:
        assert candidate_read.wait(5), "keyword_search did not reach its real candidate-read barrier"
        forget_thread.start()
        assert forget_ready.wait(5), "forget_repo did not start"
        with _race_connection(race_db) as observer:
            was_blocked = _is_blocked(observer, pids["forget"])
    finally:
        allow_search.set()
        if forget_thread.ident is None:
            # The worker may not have started if the search barrier failed.
            pass
        else:
            _join_thread(forget_thread, "forget")
        _join_thread(search_thread, "search")

    assert not failures, f"race worker failed: {failures!r}"
    assert was_blocked, "forget_repo was not blocked until the open search snapshot committed"
    assert any(hit["repo"] == "race/target" for hit in search_hits)
    with _race_connection(race_db) as conn:
        assert conn.execute(
            "SELECT count(*) FROM chunks WHERE repo = 'race/target'"
        ).fetchone()[0] == 0
        assert conn.execute(
            """
            SELECT count(*)
              FROM search_log log, jsonb_array_elements(log.results) item
             WHERE log.format_version = 2
               AND item->>'repo' = 'race/target'
            """
        ).fetchone()[0] == 0


def test_forget_first_search_waits_and_does_not_log_deleted_content(race_db, monkeypatch) -> None:
    """Search locks before candidate SQL and waits for forget to commit."""
    from shiori.search import keyword_search

    settings = Settings()
    forget_deleted = threading.Event()
    allow_forget_commit = threading.Event()
    search_started = threading.Event()
    search_done = threading.Event()
    allow_search_commit = threading.Event()
    failures = []
    pids = {}
    search_hits = []

    with _race_connection(race_db) as seed:
        _seed_race_chunk(seed)
        _set_search_log_sequence(seed)
        seed.commit()

    def forget_worker():
        try:
            with _race_connection(race_db) as conn:
                pids["forget"] = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
                forget_repo(conn, "race/target")
                forget_deleted.set()
                if not allow_forget_commit.wait(timeout=5):
                    raise AssertionError("forget commit barrier was not released")
                conn.commit()
        except Exception as exc:  # noqa: BLE001 - relay worker failures to the test thread
            failures.append(exc)

    def search_worker():
        try:
            with _race_connection(race_db) as conn:
                pids["search"] = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
                search_started.set()
                search_hits.extend(keyword_search(settings, conn, "raceprobe"))
                search_done.set()
                if not allow_search_commit.wait(timeout=5):
                    raise AssertionError("search commit barrier was not released")
                conn.commit()
        except Exception as exc:  # noqa: BLE001 - relay worker failures to the test thread
            failures.append(exc)
            search_done.set()

    forget_thread = threading.Thread(target=forget_worker, daemon=True)
    search_thread = threading.Thread(target=search_worker, daemon=True)
    forget_thread.start()
    was_blocked = False
    try:
        assert forget_deleted.wait(5), "forget_repo did not reach its pre-commit deletion barrier"
        search_thread.start()
        assert search_started.wait(5), "search worker did not start before lock acquisition"
        with _race_connection(race_db) as observer:
            was_blocked = _is_blocked(observer, pids["search"])
    finally:
        allow_forget_commit.set()
        _join_thread(forget_thread, "forget")
        if search_thread.ident is not None:
            allow_search_commit.set()
            _join_thread(search_thread, "search")

    assert search_done.is_set(), "keyword_search did not finish after forget committed"
    assert not failures, f"race worker failed: {failures!r}"
    assert was_blocked, "keyword_search was not blocked until forget committed"
    assert search_hits == []
    with _race_connection(race_db) as conn:
        assert conn.execute(
            "SELECT count(*) FROM chunks WHERE repo = 'race/target'"
        ).fetchone()[0] == 0
        assert conn.execute(
            """
            SELECT count(*)
              FROM search_log log, jsonb_array_elements(log.results) item
             WHERE log.format_version = 2
               AND item->>'repo' = 'race/target'
            """
        ).fetchone()[0] == 0


def test_forget_sql_error_rolls_back_index_and_full_search_log(race_db) -> None:
    """A real mid-forget SQL failure preserves both repo rows and complete logs."""
    with _race_connection(race_db) as conn:
        chunk_id = _seed_race_chunk(conn)
        item = _snapshot(chunk_id, "race/target", "raceprobe original snapshot")
        _log(
            conn, 600, [item], format_version=2,
            original_count=1, removed_count=0,
        )
        conn.execute(
            "INSERT INTO doc_files (repo, path, content_sha) VALUES (%s, %s, %s)",
            ("race/target", "guide.md", "sha"),
        )
        conn.execute(
            """
            CREATE FUNCTION reject_doc_file_delete() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'injected forget failure';
            END;
            $$
            """
        )
        conn.execute(
            """
            CREATE TRIGGER reject_doc_file_delete
            BEFORE DELETE ON doc_files
            FOR EACH ROW EXECUTE FUNCTION reject_doc_file_delete()
            """
        )
        conn.commit()

        before_log = conn.execute(
            """
            SELECT results, format_version, original_result_count, removed_result_count
              FROM search_log WHERE id = 600
            """
        ).fetchone()
        with pytest.raises(psycopg.Error, match="injected forget failure"):
            forget_repo(conn, "race/target")
        conn.rollback()

        assert conn.execute(
            "SELECT count(*) FROM chunks WHERE id = %s", (chunk_id,)
        ).fetchone()[0] == 1
        assert conn.execute(
            """
            SELECT results, format_version, original_result_count, removed_result_count
              FROM search_log WHERE id = 600
            """
        ).fetchone() == before_log
        assert conn.execute(
            "SELECT count(*) FROM doc_files WHERE repo = 'race/target'"
        ).fetchone()[0] == 1

        conn.execute("DROP TRIGGER reject_doc_file_delete ON doc_files")
        conn.execute("DROP FUNCTION reject_doc_file_delete()")
        deleted = forget_repo(conn, "race/target")
        conn.commit()
        assert deleted["search_log_results"] == 1
        assert conn.execute(
            "SELECT count(*) FROM chunks WHERE repo = 'race/target'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT results, original_result_count, removed_result_count "
            "FROM search_log WHERE id = 600"
        ).fetchone() == ([], 1, 1)

def test_search_rejects_repeatable_read_and_autocommit_before_candidates(race_db, monkeypatch) -> None:
    from shiori import search as search_module
    from shiori.search import keyword_search

    called = []
    monkeypatch.setattr(
        search_module, "_keyword_candidates",
        lambda *args, **kwargs: called.append(True) or [],
    )
    settings = Settings()
    with _race_connection(race_db) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        with pytest.raises(RuntimeError, match="READ COMMITTED"):
            keyword_search(settings, conn, "unused")
        conn.rollback()
    dsn, options = race_db
    with psycopg.connect(dsn, options=options, autocommit=True) as conn:
        with pytest.raises(RuntimeError, match="autocommit"):
            keyword_search(settings, conn, "unused")
        with pytest.raises(RuntimeError, match="autocommit"):
            forget_repo(conn, "race/target")
    with _race_connection(race_db) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        with pytest.raises(RuntimeError, match="READ COMMITTED"):
            forget_repo(conn, "race/target")
        conn.rollback()
    assert called == []


def test_semantic_search_embeds_before_taking_shared_lock(race_db, monkeypatch) -> None:
    import shiori.search as search_module
    from shiori.search import semantic_search

    dsn, options = race_db
    own_connection_holds_search_lock_during_embedding = []
    search_pid = None

    class LockCheckingEmbedder:
        def embed_query(self, query):
            with psycopg.connect(dsn, options=options) as observer:
                own_connection_holds_search_lock_during_embedding.append(
                    observer.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1
                              FROM pg_locks
                             WHERE locktype = 'advisory'
                               AND pid = %s
                               AND classid = %s::oid
                               AND objid = %s::oid
                               AND objsubid = 2
                               AND granted
                        )
                        """,
                        (
                            search_pid,
                            search_module.SEARCH_LOG_LOCK_CLASS,
                            search_module.SEARCH_LOG_LOCK_OBJECT,
                        ),
                    ).fetchone()[0]
                )
            # Release the auxiliary lock before semantic_search requests its own.
            unrelated.commit()
            return [0.0]

    monkeypatch.setattr(search_module, "_vector_candidates", lambda *args: [])
    monkeypatch.setattr(search_module, "_keyword_candidates", lambda *args: [])

    with _race_connection(race_db) as unrelated:
        unrelated.execute(
            "SELECT pg_advisory_xact_lock_shared(%s, %s)",
            (
                search_module.SEARCH_LOG_LOCK_CLASS,
                search_module.SEARCH_LOG_LOCK_OBJECT,
            ),
        )
        with _race_connection(race_db) as conn:
            search_pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
            semantic_search(Settings(), conn, LockCheckingEmbedder(), "slow embedding")
            assert own_connection_holds_search_lock_during_embedding == [False]
            conn.commit()
        unrelated.commit()


def test_semantic_keyword_error_uses_savepoint_and_keeps_shared_lock(
    race_db, monkeypatch,
) -> None:
    import shiori.search as search_module
    from shiori.search import semantic_search

    dsn, options = race_db
    with _race_connection(race_db) as seed:
        chunk_id = _seed_race_chunk(seed)
        seed.commit()

    def vector_rows(conn, qvec, filters, limit):
        return conn.execute(
            """SELECT id, source_type, repo, path, issue_no, comment_id, kind, language,
                      heading_path, content, state, author, line, created_at, updated_at,
                      url, chunk_key, chunk_index, end_line, commit_sha, prog_lang, 0.9
                 FROM chunks WHERE id = %s""",
            (chunk_id,),
        ).fetchall()

    def invalid_keyword_sql(conn, query, filters, limit):
        conn.execute("SELECT FROM intentionally_invalid_syntax")
        return []

    monkeypatch.setattr(search_module, "_vector_candidates", vector_rows)
    monkeypatch.setattr(search_module, "_keyword_candidates", invalid_keyword_sql)

    class EmbedderStub:
        def embed_query(self, query):
            return [0.0]

    settings = Settings()
    with _race_connection(race_db) as conn:
        hits = semantic_search(settings, conn, EmbedderStub(), "fallback query")
        assert len(hits) == 1
        assert conn.execute("SELECT 1").fetchone() == (1,)
        logged = conn.execute(
            "SELECT format_version, original_result_count FROM search_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert logged == (2, 1)
        with psycopg.connect(dsn, options=options) as observer:
            held = observer.execute(
                "SELECT pg_try_advisory_xact_lock(%s, %s)",
                (search_module.SEARCH_LOG_LOCK_CLASS, search_module.SEARCH_LOG_LOCK_OBJECT),
            ).fetchone()[0]
            observer.commit()
        assert held is False, "semantic fallback released the shared search/forget lock"
        conn.commit()


def test_export_rejects_bad_hash_and_incomplete_v2_snapshots(db, tmp_path: Path) -> None:
    from scripts.search_log_export import export_search_logs

    chunk_id = _chunk(db, "export/repo", "guide.md", "no join required")
    bad_hash = _snapshot(chunk_id, "export/repo", "snapshot")
    bad_hash["content_sha256"] = "0" * 64
    incomplete = _snapshot(chunk_id, "export/repo", "snapshot")
    del incomplete["content_snapshot"]
    _log(db, 610, [bad_hash], format_version=2, original_count=1, removed_count=0)
    _log(db, 611, [incomplete], format_version=2, original_count=1, removed_count=0)
    db.commit()

    root = tmp_path / "calibration"
    output = root / "valid-only.jsonl"
    summary = export_search_logs(db, output, calibration_root=root)
    assert summary["searches_seen"] == 2
    assert summary["searches_exported"] == 0
    assert summary["results_exported"] == 0
    assert summary["legacy_searches_skipped"] == 2
    assert output.read_text(encoding="utf-8") == ""


def test_synthetic_snapshot_pg_column_size_measurement(db, capsys) -> None:
    """Measure JSONB storage for representative synthetic content, not private data."""
    chunk_id = _chunk(db, "measure/repo", "guide.md", "x" * 991)
    item = _snapshot(chunk_id, "measure/repo", "x" * 991)
    _log(db, 620, [item], format_version=2, original_count=1, removed_count=0)
    db.commit()

    jsonb_bytes, row_bytes = db.execute(
        "SELECT pg_column_size(results), pg_column_size(search_log) "
        "FROM search_log WHERE id = 620"
    ).fetchone()
    print(f"synthetic v2 snapshot: JSONB={jsonb_bytes} B, whole row={row_bytes} B")
    assert jsonb_bytes > 991
    assert row_bytes >= jsonb_bytes


def test_export_preserves_original_and_removed_counts_for_empty_snapshots(db, tmp_path: Path) -> None:
    from scripts.search_log_export import export_search_logs

    _log(db, 612, [], format_version=2, original_count=0, removed_count=0)
    _log(db, 613, [], format_version=2, original_count=4, removed_count=4)
    db.commit()
    root = tmp_path / "calibration"
    output = root / "empty.jsonl"
    summary = export_search_logs(db, output, calibration_root=root)
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert summary["searches_exported"] == 2
    assert summary["results_exported"] == 0
    assert [(r["original_result_count"], r["removed_result_count"]) for r in records] == [
        (0, 0), (4, 4),
    ]
    assert [r["empty_search"] for r in records] == [True, True]

@pytest.mark.parametrize("layout", ["empty_search_log", "legacy_search_log"])
def test_forget_without_snapshot_schema(snapshot_db, layout: str) -> None:
    """Forget remains safe before snapshots are installed and on old log schemas."""
    dsn, _, _ = snapshot_db
    schema_name = "shiori_471_forget_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema_name}"')
    options = f"-c search_path={schema_name},public"
    try:
        with psycopg.connect(dsn, options=options) as conn:
            conn.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, repo TEXT NOT NULL)")
            conn.execute("INSERT INTO chunks VALUES (1, 'gone/repo')")
            if layout == "legacy_search_log":
                conn.execute(
                    """
                    CREATE TABLE search_log (
                        id BIGINT PRIMARY KEY,
                        query TEXT NOT NULL,
                        filters JSONB NOT NULL,
                        results JSONB NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO search_log (id, query, filters, results)
                    VALUES (1, 'legacy query', %s, %s)
                    """,
                    (
                        Jsonb({"repo": "gone/repo", "source_type": "doc"}),
                        Jsonb([{"id": 11, "score": 0.75, "repo": "gone/repo"}]),
                    ),
                )
            conn.commit()

            legacy_before = None
            if layout == "legacy_search_log":
                legacy_before = conn.execute(
                    """
                    SELECT query, filters::text, results::text
                      FROM search_log WHERE id = 1
                    """
                ).fetchone()

            deleted = forget_repo(conn, "gone/repo")
            conn.commit()

            assert deleted["search_log_results"] == 0
            assert conn.execute(
                "SELECT count(*) FROM chunks WHERE repo = 'gone/repo'"
            ).fetchone()[0] == 0
            if layout == "legacy_search_log":
                assert conn.execute(
                    """
                    SELECT query, filters::text, results::text
                      FROM search_log WHERE id = 1
                    """
                ).fetchone() == legacy_before
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
