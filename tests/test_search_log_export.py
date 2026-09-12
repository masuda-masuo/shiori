"""Acceptance contract tests for widened search-log export (shiori #451).

Pins the public API contract of scripts/search_log_export.py before implementation exists.
All tests use deterministic fake connections/cursors and never touch real databases.
"""

from __future__ import annotations

import datetime
import importlib
import json
import re
import sys
from collections import namedtuple
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SENTINEL = object()


class ExportSearchLogsFn(Protocol):
    def __call__(
        self,
        connection: Any,
        output_path: Path,
        *,
        calibration_root: Path = ...,
        snippet_chars: int = ...,
    ) -> dict[str, int]: ...


@pytest.fixture
def export_search_logs() -> ExportSearchLogsFn:
    """Fixture providing export_search_logs from scripts.search_log_export.

    Converts module absence into an explicit pytest assertion failure inside the
    fixture, preventing collection errors on pristine checkout.
    """
    target_module = "scripts.search_log_export"
    try:
        mod = importlib.import_module(target_module)
    except ModuleNotFoundError:
        raise AssertionError(
            f"{target_module} is absent: export_search_logs acceptance test requires implementation"
        ) from None
    except ImportError as exc:
        raise AssertionError(f"{target_module} could not be imported: {exc}") from None

    fn = getattr(mod, "export_search_logs", None)
    assert fn is not None and callable(fn), (
        f"{target_module} must define public callable export_search_logs"
    )
    return fn


# ---------------------------------------------------------------------------
# Deterministic fake psycopg connection / cursor infrastructure
# ---------------------------------------------------------------------------

class _FakeRow:
    """Row wrapper supporting column-name indexing, positional indexing, attribute access, and mapping iteration."""

    def __init__(self, data: dict[str, Any], columns: Sequence[str]) -> None:
        self._data = dict(data)
        self._columns = list(columns)
        self._tuple = tuple(data.get(col) for col in columns)

    def __getitem__(self, item: Any) -> Any:
        if isinstance(item, str):
            return self._data[item]
        return self._tuple[item]

    def __getattr__(self, name: str) -> Any:
        if name in self._data:
            return self._data[name]
        raise AttributeError(f"_FakeRow has no attribute {name!r}")

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __iter__(self):
        return iter(self._tuple)

    def __len__(self) -> int:
        return len(self._tuple)

    def __repr__(self) -> str:
        return f"_FakeRow({self._data})"

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._tuple

    def items(self):
        return self._data.items()


_ColDesc = namedtuple(
    "_ColDesc",
    ["name", "type_code", "display_size", "internal_size", "precision", "scale", "null_ok"],
    defaults=[None, None, None, None, None, None],
)


class _FakePGResult:
    """Minimal PGresult stub for psycopg row_factory compatibility (e.g. dict_row)."""

    def __init__(self, col_names: Sequence[str]) -> None:
        self._names = list(col_names)
        self.nfields = len(col_names)
        self.status = 2  # psycopg.pq.ExecStatus.TUPLES_OK

    def fname(self, i: int) -> bytes:
        return self._names[i].encode("utf-8")


class FakeCursor:
    def __init__(
        self,
        conn: FakeConnection,
        row_factory: Callable[[Any], Callable[[Sequence[Any]], Any]] | None = None,
    ) -> None:
        self.conn = conn
        self.row_factory = row_factory
        self.description: list[Any] | None = None
        self.pgresult: _FakePGResult | None = None
        self._encoding = "utf-8"
        self._rows: list[Any] = []
        self._rows_iter: Any = None
        self.executed: list[tuple[str, Any]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        return False

    def execute(self, sql: Any, params: Any = None) -> FakeCursor:
        sql_str = str(sql)
        self.executed.append((sql_str, params))
        self.conn.executed.append((sql_str, params))

        if self.conn.fail_on_execute:
            raise RuntimeError("Simulated DB error during cursor.execute")

        sql_lower = sql_str.lower()
        if "search_log" in sql_lower:
            cols = [
                "id",
                "created_at",
                "search_type",
                "caller",
                "top_k",
                "filters",
                "query",
                "results",
            ]
            rows_data: list[dict[str, Any]] = []
            for row in self.conn.search_logs:
                # If query filters id > 43
                if ">" in sql_lower and (
                    "43" in sql_lower
                    or (params and 43 in (params if isinstance(params, (list, tuple)) else [params]))
                ):
                    if row.get("id", 0) > 43:
                        rows_data.append(row)
                else:
                    rows_data.append(row)
            if "order by" in sql_lower and "id" in sql_lower:
                rows_data.sort(key=lambda r: r.get("id", 0))

            self._set_result(cols, rows_data)

        elif "chunks" in sql_lower:
            cols = [
                "id",
                "source_type",
                "repo",
                "path",
                "issue_no",
                "comment_id",
                "url",
                "content",
            ]
            requested_ids: set[int] | None = None
            if params is not None:
                if isinstance(params, (list, tuple)):
                    flattened: list[int] = []
                    for p in params:
                        if isinstance(p, (list, tuple, set)):
                            flattened.extend(int(x) for x in p)
                        elif p is not None:
                            flattened.append(int(p))
                    requested_ids = set(flattened)
                elif isinstance(params, (int, str)):
                    requested_ids = {int(params)}

            if requested_ids is None:
                found_literal = [
                    int(m) for m in re.findall(r"\b(?:id\s*=\s*|id\s+in\s*\(\s*)(\d+)", sql_lower)
                ]
                if found_literal:
                    requested_ids = set(found_literal)

            rows_data = []
            for cid, cdata in self.conn.chunks.items():
                if requested_ids is None or cid in requested_ids:
                    row_dict = dict(cdata)
                    row_dict.setdefault("id", cid)
                    rows_data.append(row_dict)

            self._set_result(cols, rows_data)
        else:
            self._set_result([], [])

        return self

    def _set_result(self, cols: list[str], raw_rows: list[dict[str, Any]]) -> None:
        self.description = [_ColDesc(name=c) for c in cols]
        self.pgresult = _FakePGResult(cols)
        if self.row_factory is not None:
            make_row = self.row_factory(self)
            self._rows = [make_row(tuple(r.get(c) for c in cols)) for r in raw_rows]
        else:
            self._rows = [_FakeRow(r, cols) for r in raw_rows]
        self._rows_iter = None

    def fetchone(self) -> Any:
        if self._rows_iter is None:
            self._rows_iter = iter(self._rows)
        return next(self._rows_iter, None)

    def fetchall(self) -> list[Any]:
        if self._rows_iter is not None:
            rem = list(self._rows_iter)
            self._rows_iter = iter([])
            return rem
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class FakeConnection:
    def __init__(
        self,
        search_logs: list[dict[str, Any]] | None = None,
        chunks: dict[int, dict[str, Any]] | None = None,
        fail_on_execute: bool = False,
    ) -> None:
        self.search_logs = list(search_logs or [])
        self.chunks = dict(chunks or {})
        self.fail_on_execute = fail_on_execute
        self.row_factory: Callable[[Any], Callable[[Sequence[Any]], Any]] | None = None
        self.executed: list[tuple[str, Any]] = []

    def cursor(
        self,
        row_factory: Callable[[Any], Callable[[Sequence[Any]], Any]] | None = None,
    ) -> FakeCursor:
        return FakeCursor(self, row_factory=row_factory or self.row_factory)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        return False


def _make_search_log(
    log_id: int,
    *,
    created_at: datetime.datetime | str = "2026-08-26T12:00:00+00:00",
    search_type: str = "semantic",
    caller: str = "mcp",
    top_k: int = 10,
    filters: dict[str, Any] | None = None,
    query: str = "test query",
    results: Any = _SENTINEL,
) -> dict[str, Any]:
    if results is _SENTINEL:
        results = []
    return {
        "id": log_id,
        "created_at": created_at,
        "search_type": search_type,
        "caller": caller,
        "top_k": top_k,
        "filters": filters,
        "query": query,
        "results": results,
    }


def _make_result_item(
    chunk_id: int,
    *,
    score: float = 0.0318,
    vec_score: float | None = 0.85,
    vec_rank: int | None = 0,
    kw_score: float | None = 0.75,
    kw_rank: int | None = 0,
) -> dict[str, Any]:
    return {
        "id": chunk_id,
        "chunk_id": chunk_id,
        "score": score,
        "vec_score": vec_score,
        "vec_rank": vec_rank,
        "kw_score": kw_score,
        "kw_rank": kw_rank,
    }


def _make_chunk(
    chunk_id: int,
    *,
    source_type: str = "doc",
    repo: str = "masuda-masuo/shiori",
    path: str | None = "docs/architecture.md",
    issue_no: int | None = None,
    comment_id: int | None = None,
    url: str | None = "https://github.com/masuda-masuo/shiori/blob/main/docs/architecture.md",
    content: str = "Sample chunk content for testing resolution and snippet truncation.",
) -> dict[str, Any]:
    return {
        "id": chunk_id,
        "source_type": source_type,
        "repo": repo,
        "path": path,
        "issue_no": issue_no,
        "comment_id": comment_id,
        "url": url,
        "content": content,
    }


# ---------------------------------------------------------------------------
# Acceptance contract tests
# ---------------------------------------------------------------------------

EXACT_OUTPUT_KEYS = {
    "log_id",
    "created_at",
    "search_type",
    "caller",
    "top_k",
    "filters",
    "query",
    "chunk_id",
    "source_type",
    "repo",
    "path",
    "issue_no",
    "comment_id",
    "url",
    "snippet",
    "score",
    "vec_score",
    "vec_rank",
    "kw_score",
    "kw_rank",
    "chunk_resolved",
}

EXACT_SUMMARY_KEYS = {
    "searches_seen",
    "searches_exported",
    "results_exported",
    "legacy_searches_skipped",
    "unresolved_results",
}


def test_absent_module_fixture_failure(export_search_logs: ExportSearchLogsFn) -> None:
    """Criterion: Pristine checkout failure converted into fixture assertion failure."""
    assert callable(export_search_logs)


def test_export_search_logs_basic_structure_and_exact_keys(
    export_search_logs: ExportSearchLogsFn,
    tmp_path: Path,
) -> None:
    """Criterion: Public API return dictionary shape, exact JSONL line keys, and types."""
    calib_root = tmp_path / "runtime" / "calibration"
    out_file = calib_root / "sheet.jsonl"

    res1 = _make_result_item(101, score=0.0318, vec_score=0.85, vec_rank=0, kw_score=0.75, kw_rank=0)
    res2 = _make_result_item(102, score=0.0250, vec_score=0.70, vec_rank=1, kw_score=0.60, kw_rank=1)
    log_row = _make_search_log(
        44,
        created_at=datetime.datetime(2026, 8, 26, 12, 34, 56, tzinfo=datetime.timezone.utc),
        search_type="semantic",
        caller="mcp",
        top_k=5,
        filters={"repo": "masuda-masuo/shiori"},
        query="relevance floor calibration",
        results=[res1, res2],
    )
    chunk1 = _make_chunk(101, source_type="doc", content="Doc snippet text")
    chunk2 = _make_chunk(102, source_type="code", content="Code snippet text")

    conn = FakeConnection(search_logs=[log_row], chunks={101: chunk1, 102: chunk2})

    summary = export_search_logs(conn, out_file, calibration_root=calib_root)

    assert set(summary.keys()) == EXACT_SUMMARY_KEYS
    assert summary == {
        "searches_seen": 1,
        "searches_exported": 1,
        "results_exported": 2,
        "legacy_searches_skipped": 0,
        "unresolved_results": 0,
    }

    assert out_file.is_file()
    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    row0 = json.loads(lines[0])
    assert set(row0.keys()) == EXACT_OUTPUT_KEYS
    assert row0["log_id"] == 44
    assert row0["created_at"] == "2026-08-26T12:34:56+00:00"
    assert row0["search_type"] == "semantic"
    assert row0["caller"] == "mcp"
    assert row0["top_k"] == 5
    assert row0["filters"] == {"repo": "masuda-masuo/shiori"}
    assert row0["query"] == "relevance floor calibration"
    assert row0["chunk_id"] == 101
    assert row0["source_type"] == "doc"
    assert row0["repo"] == "masuda-masuo/shiori"
    assert row0["snippet"] == "Doc snippet text"
    assert row0["score"] == pytest.approx(0.0318)
    assert row0["vec_score"] == pytest.approx(0.85)
    assert row0["vec_rank"] == 0
    assert row0["kw_score"] == pytest.approx(0.75)
    assert row0["kw_rank"] == 0
    assert row0["chunk_resolved"] is True


def test_input_selection_id_filter_and_order(
    export_search_logs: ExportSearchLogsFn,
    tmp_path: Path,
) -> None:
    """Criterion: Query search_log rows with id > 43, ordered by id."""
    calib_root = tmp_path / "runtime" / "calibration"
    out_file = calib_root / "sheet.jsonl"

    res = _make_result_item(100)
    # Rows with id <= 43 must be excluded at the query boundary
    rows = [
        _make_search_log(1, results=[res]),
        _make_search_log(42, results=[res]),
        _make_search_log(43, results=[res]),
        _make_search_log(44, results=[res]),
        _make_search_log(50, results=[res]),
    ]
    chunk = _make_chunk(100)
    conn = FakeConnection(search_logs=rows, chunks={100: chunk})

    summary = export_search_logs(conn, out_file, calibration_root=calib_root)

    # SQL query must filter search_log rows with id > 43 ordered by id
    search_log_queries = [sql for sql, _ in conn.executed if "search_log" in sql.lower()]
    assert search_log_queries, "Expected at least one search_log query"
    query_text = search_log_queries[0].lower()
    assert re.search(r"\bid\s*>\s*(43|%s)", query_text), "Query must enforce id > 43"
    assert re.search(r"order\s+by\s+([a-z0-9_]+\.)?id", query_text), "Query must enforce ORDER BY id"

    assert summary["searches_seen"] == 2
    assert summary["searches_exported"] == 2
    assert summary["results_exported"] == 2


def test_legacy_searches_skipped_shape_check(
    export_search_logs: ExportSearchLogsFn,
    tmp_path: Path,
) -> None:
    """Criterion: A row is widened only when results is a list and every result has all 4 retriever keys.

    Failing rows increment legacy_searches_skipped and emit no output, even if id > 43.
    """
    calib_root = tmp_path / "runtime" / "calibration"
    out_file = calib_root / "sheet.jsonl"

    # id > 43, but various non-widened shapes
    row_none_results = _make_search_log(44, results=None)
    row_dict_results = _make_search_log(45, results={"id": 1})  # not a list
    # pre-#445 shape: contains id and score, but missing vec_score/vec_rank/kw_score/kw_rank
    row_old_shape = _make_search_log(46, results=[{"id": 1, "score": 0.03}])
    # partial shape: first item widened, second item missing kw_rank
    partial_item = {
        "id": 2,
        "chunk_id": 2,
        "score": 0.02,
        "vec_score": 0.5,
        "vec_rank": 0,
        "kw_score": 0.5,
    }
    row_partial_shape = _make_search_log(
        47,
        results=[_make_result_item(1), partial_item],
    )
    # properly widened row
    row_widened = _make_search_log(48, results=[_make_result_item(10)])

    conn = FakeConnection(
        search_logs=[
            row_none_results,
            row_dict_results,
            row_old_shape,
            row_partial_shape,
            row_widened,
        ],
        chunks={10: _make_chunk(10)},
    )

    summary = export_search_logs(conn, out_file, calibration_root=calib_root)

    assert summary["searches_seen"] == 5
    assert summary["legacy_searches_skipped"] == 4
    assert summary["searches_exported"] == 1
    assert summary["results_exported"] == 1

    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["log_id"] == 48
    assert data["chunk_id"] == 10


def test_widened_results_with_null_scores_and_ranks(
    export_search_logs: ExportSearchLogsFn,
    tmp_path: Path,
) -> None:
    """Criterion: Widened keys may be null (single-retriever hit). Null scores remain JSON null."""
    calib_root = tmp_path / "runtime" / "calibration"
    out_file = calib_root / "sheet.jsonl"

    # Vector-only hit (keyword fields are null)
    vec_only = _make_result_item(
        101, score=0.016, vec_score=0.92, vec_rank=0, kw_score=None, kw_rank=None
    )
    # Keyword-only hit (vector fields are null)
    kw_only = _make_result_item(
        102, score=0.016, vec_score=None, vec_rank=None, kw_score=0.88, kw_rank=0
    )
    # Hit where score and ranks are all null
    all_null = _make_result_item(
        103, score=0.0, vec_score=None, vec_rank=None, kw_score=None, kw_rank=None
    )

    row = _make_search_log(44, results=[vec_only, kw_only, all_null])
    conn = FakeConnection(
        search_logs=[row],
        chunks={
            101: _make_chunk(101),
            102: _make_chunk(102),
            103: _make_chunk(103),
        },
    )

    summary = export_search_logs(conn, out_file, calibration_root=calib_root)

    assert summary["searches_exported"] == 1
    assert summary["legacy_searches_skipped"] == 0
    assert summary["results_exported"] == 3

    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    r1 = json.loads(lines[0])
    assert r1["chunk_id"] == 101
    assert r1["vec_score"] == pytest.approx(0.92)
    assert r1["vec_rank"] == 0
    assert r1["kw_score"] is None
    assert r1["kw_rank"] is None

    r2 = json.loads(lines[1])
    assert r2["chunk_id"] == 102
    assert r2["vec_score"] is None
    assert r2["vec_rank"] is None
    assert r2["kw_score"] == pytest.approx(0.88)
    assert r2["kw_rank"] == 0

    r3 = json.loads(lines[2])
    assert r3["chunk_id"] == 103
    assert r3["vec_score"] is None
    assert r3["vec_rank"] is None
    assert r3["kw_score"] is None
    assert r3["kw_rank"] is None


def test_empty_widened_results(
    export_search_logs: ExportSearchLogsFn,
    tmp_path: Path,
) -> None:
    """Criterion: Empty widened results emits no result lines but counts as one exported search."""
    calib_root = tmp_path / "runtime" / "calibration"
    out_file = calib_root / "sheet.jsonl"

    row = _make_search_log(44, results=[])
    conn = FakeConnection(search_logs=[row], chunks={})

    summary = export_search_logs(conn, out_file, calibration_root=calib_root)

    assert summary == {
        "searches_seen": 1,
        "searches_exported": 1,
        "results_exported": 0,
        "legacy_searches_skipped": 0,
        "unresolved_results": 0,
    }

    assert out_file.is_file()
    assert out_file.read_text(encoding="utf-8") == ""


def test_preserve_search_type_semantic_and_keyword(
    export_search_logs: ExportSearchLogsFn,
    tmp_path: Path,
) -> None:
    """Criterion: Preserve search_type; semantic and keyword searches are not pooled."""
    calib_root = tmp_path / "runtime" / "calibration"
    out_file = calib_root / "sheet.jsonl"

    row_sem = _make_search_log(44, search_type="semantic", results=[_make_result_item(101)])
    row_kw = _make_search_log(45, search_type="keyword", results=[_make_result_item(102)])

    conn = FakeConnection(
        search_logs=[row_sem, row_kw],
        chunks={101: _make_chunk(101), 102: _make_chunk(102)},
    )

    summary = export_search_logs(conn, out_file, calibration_root=calib_root)
    assert summary["searches_exported"] == 2

    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[0])["search_type"] == "semantic"
    assert json.loads(lines[1])["search_type"] == "keyword"


def test_chunk_resolution_resolved_and_unresolved(
    export_search_logs: ExportSearchLogsFn,
    tmp_path: Path,
) -> None:
    """Criterion: Missing chunk emits chunk_resolved=false, logged scores, and null identity fields."""
    calib_root = tmp_path / "runtime" / "calibration"
    out_file = calib_root / "sheet.jsonl"

    res_resolved = _make_result_item(
        101, score=0.03, vec_score=0.8, vec_rank=0, kw_score=0.7, kw_rank=0
    )
    res_missing = _make_result_item(
        999, score=0.015, vec_score=0.5, vec_rank=1, kw_score=0.4, kw_rank=2
    )

    row = _make_search_log(44, results=[res_resolved, res_missing])
    resolved_chunk = _make_chunk(
        101,
        source_type="issue",
        repo="masuda-masuo/shiori",
        path=None,
        issue_no=451,
        comment_id=1234,
        url="https://github.com/masuda-masuo/shiori/issues/451#issuecomment-1234",
        content="Resolved issue chunk body",
    )
    # chunk 999 is intentionally omitted from chunks mapping
    conn = FakeConnection(search_logs=[row], chunks={101: resolved_chunk})

    summary = export_search_logs(conn, out_file, calibration_root=calib_root)

    assert summary["results_exported"] == 2
    assert summary["unresolved_results"] == 1

    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    r0 = json.loads(lines[0])
    assert r0["chunk_id"] == 101
    assert r0["chunk_resolved"] is True
    assert r0["source_type"] == "issue"
    assert r0["repo"] == "masuda-masuo/shiori"
    assert r0["path"] is None
    assert r0["issue_no"] == 451
    assert r0["comment_id"] == 1234
    assert r0["url"] == "https://github.com/masuda-masuo/shiori/issues/451#issuecomment-1234"
    assert r0["snippet"] == "Resolved issue chunk body"

    r1 = json.loads(lines[1])
    assert r1["chunk_id"] == 999
    assert r1["chunk_resolved"] is False
    assert r1["source_type"] is None
    assert r1["repo"] is None
    assert r1["path"] is None
    assert r1["issue_no"] is None
    assert r1["comment_id"] is None
    assert r1["url"] is None
    assert r1["snippet"] is None
    assert r1["score"] == pytest.approx(0.015)
    assert r1["vec_score"] == pytest.approx(0.5)
    assert r1["vec_rank"] == 1
    assert r1["kw_score"] == pytest.approx(0.4)
    assert r1["kw_rank"] == 2


def test_snippet_truncation_default_and_custom(
    export_search_logs: ExportSearchLogsFn,
    tmp_path: Path,
) -> None:
    """Criterion: Content snippet truncated to snippet_chars (default 500, or caller override)."""
    calib_root = tmp_path / "runtime" / "calibration"
    out_default = calib_root / "default_snippet.jsonl"
    out_custom = calib_root / "custom_snippet.jsonl"

    long_text = "A" * 700 + "B" * 300  # 1000 chars
    short_text = "Short text"

    row = _make_search_log(44, results=[_make_result_item(101), _make_result_item(102)])
    conn = FakeConnection(
        search_logs=[row],
        chunks={
            101: _make_chunk(101, content=long_text),
            102: _make_chunk(102, content=short_text),
        },
    )

    # 1. Default snippet_chars = 500
    export_search_logs(conn, out_default, calibration_root=calib_root)
    lines_default = out_default.read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines_default[0])["snippet"] == long_text[:500]
    assert len(json.loads(lines_default[0])["snippet"]) == 500
    assert json.loads(lines_default[1])["snippet"] == short_text

    # 2. Custom snippet_chars = 50
    export_search_logs(conn, out_custom, calibration_root=calib_root, snippet_chars=50)
    lines_custom = out_custom.read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines_custom[0])["snippet"] == long_text[:50]
    assert len(json.loads(lines_custom[0])["snippet"]) == 50
    assert json.loads(lines_custom[1])["snippet"] == short_text


def test_stable_ordering_by_log_id_then_result_order(
    export_search_logs: ExportSearchLogsFn,
    tmp_path: Path,
) -> None:
    """Criterion: Ordered first by search_log id and then by original result-list order."""
    calib_root = tmp_path / "runtime" / "calibration"
    out_file = calib_root / "sheet.jsonl"

    # Non-monotonic chunk ids within result list
    log44 = _make_search_log(
        44,
        results=[
            _make_result_item(30),
            _make_result_item(10),
            _make_result_item(20),
        ],
    )
    log45 = _make_search_log(
        45,
        results=[
            _make_result_item(50),
            _make_result_item(40),
        ],
    )

    conn = FakeConnection(
        search_logs=[log44, log45],
        chunks={
            10: _make_chunk(10),
            20: _make_chunk(20),
            30: _make_chunk(30),
            40: _make_chunk(40),
            50: _make_chunk(50),
        },
    )

    export_search_logs(conn, out_file, calibration_root=calib_root)

    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    sequence = [(json.loads(line)["log_id"], json.loads(line)["chunk_id"]) for line in lines]
    assert sequence == [
        (44, 30),
        (44, 10),
        (44, 20),
        (45, 50),
        (45, 40),
    ]


def test_output_boundary_rejects_paths_outside_calibration_root(
    export_search_logs: ExportSearchLogsFn,
    tmp_path: Path,
) -> None:
    """Criterion: Refuse with ValueError unless resolved output is strictly below resolved calibration root.

    Rejects root directory itself, sibling paths, .. escapes, and symlink escapes.
    """
    calib_root = tmp_path / "runtime" / "calibration"
    calib_root.mkdir(parents=True, exist_ok=True)
    conn = FakeConnection(search_logs=[], chunks={})

    # Case A: Root directory itself
    with pytest.raises(ValueError):
        export_search_logs(conn, calib_root, calibration_root=calib_root)

    # Case B: Sibling path
    sibling_path = tmp_path / "runtime" / "calibration_sibling" / "out.jsonl"
    with pytest.raises(ValueError):
        export_search_logs(conn, sibling_path, calibration_root=calib_root)

    # Case C: ".." escape
    dotdot_path = calib_root / ".." / "escape.jsonl"
    with pytest.raises(ValueError):
        export_search_logs(conn, dotdot_path, calibration_root=calib_root)

    # Case D: Symlink escape
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir(parents=True, exist_ok=True)
    symlink_dir = calib_root / "sym_link"
    try:
        symlink_dir.symlink_to(outside_dir)
        symlink_escape_path = symlink_dir / "out.jsonl"
        with pytest.raises(ValueError):
            export_search_logs(conn, symlink_escape_path, calibration_root=calib_root)
    finally:
        if symlink_dir.is_symlink():
            symlink_dir.unlink()


def test_output_boundary_creates_missing_parent_directories(
    export_search_logs: ExportSearchLogsFn,
    tmp_path: Path,
) -> None:
    """Criterion: Create missing parent directories below the calibration root."""
    calib_root = tmp_path / "runtime" / "calibration"
    nested_out = calib_root / "deeply" / "nested" / "output.jsonl"

    assert not nested_out.parent.exists()

    conn = FakeConnection(search_logs=[], chunks={})
    summary = export_search_logs(conn, nested_out, calibration_root=calib_root)

    assert nested_out.parent.is_dir()
    assert nested_out.is_file()
    assert summary["searches_seen"] == 0


def test_atomic_write_no_partial_destination_on_failure(
    export_search_logs: ExportSearchLogsFn,
    tmp_path: Path,
) -> None:
    """Criterion: Write atomically via a temporary sibling; no partial destination remains on failure."""
    calib_root = tmp_path / "runtime" / "calibration"
    target_out = calib_root / "atomic_target.jsonl"

    # Simulate an error on database cursor execution during export
    conn = FakeConnection(
        search_logs=[_make_search_log(44, results=[_make_result_item(101)])],
        chunks={},
        fail_on_execute=True,
    )

    with pytest.raises(Exception):
        export_search_logs(conn, target_out, calibration_root=calib_root)

    assert not target_out.exists(), "Target file must not exist after failed export"
    # Ensure no temporary sibling left behind
    siblings = list(calib_root.glob("*.tmp*"))
    assert siblings == [], f"Temporary sibling files left behind: {siblings}"


def test_created_at_serialization_iso8601(
    export_search_logs: ExportSearchLogsFn,
    tmp_path: Path,
) -> None:
    """Criterion: Serialize created_at as ISO-8601 text."""
    calib_root = tmp_path / "runtime" / "calibration"
    out_file = calib_root / "sheet.jsonl"

    dt = datetime.datetime(2026, 8, 26, 9, 15, 30, tzinfo=datetime.timezone.utc)
    log = _make_search_log(44, created_at=dt, results=[_make_result_item(101)])
    conn = FakeConnection(search_logs=[log], chunks={101: _make_chunk(101)})

    export_search_logs(conn, out_file, calibration_root=calib_root)

    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    data = json.loads(lines[0])
    assert data["created_at"] == "2026-08-26T09:15:30+00:00"


def test_preserves_json_compatible_filters(
    export_search_logs: ExportSearchLogsFn,
    tmp_path: Path,
) -> None:
    """Criterion: Preserve JSON-compatible filters."""
    calib_root = tmp_path / "runtime" / "calibration"
    out_file = calib_root / "sheet.jsonl"

    complex_filters = {
        "repo": "masuda-masuo/shiori",
        "source_type": ["doc", "code"],
        "max_age_days": 30,
        "is_open": True,
    }
    log_with_filters = _make_search_log(44, filters=complex_filters, results=[_make_result_item(101)])
    log_null_filters = _make_search_log(45, filters=None, results=[_make_result_item(102)])

    conn = FakeConnection(
        search_logs=[log_with_filters, log_null_filters],
        chunks={101: _make_chunk(101), 102: _make_chunk(102)},
    )

    export_search_logs(conn, out_file, calibration_root=calib_root)

    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    data44 = json.loads(lines[0])
    assert data44["filters"] == complex_filters

    data45 = json.loads(lines[1])
    assert data45["filters"] is None
