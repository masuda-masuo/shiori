"""Export validated v2 search snapshots to private JSONL."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any


def _row_to_dict(cur: Any, row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    if hasattr(row, "keys") and callable(row.keys):
        return {k: row[k] for k in row.keys()}
    if getattr(cur, "description", None):
        names = [
            col[0] if isinstance(col, (list, tuple)) else getattr(col, "name", str(col))
            for col in cur.description
        ]
        return dict(zip(names, row))
    return dict(row)


def _validate_output_path(output_path: Path | str, calibration_root: Path | str) -> Path:
    """Require output_path to resolve strictly below calibration_root."""
    root = Path(calibration_root).resolve()
    out = Path(output_path).resolve()
    try:
        inside = out.is_relative_to(root)
    except AttributeError:
        try:
            out.relative_to(root)
            inside = True
        except ValueError:
            inside = False
    if not inside or out == root:
        raise ValueError(
            f"output_path '{output_path}' must resolve strictly below "
            f"calibration_root '{calibration_root}'"
        )
    return out


_REQUIRED_ITEM = {
    "id", "score", "vec_score", "vec_rank", "kw_score", "kw_rank",
    "source_type", "repo", "chunk_key", "chunk_index",
    "content_snapshot", "content_sha256",
}


def _valid_snapshot(row: dict[str, Any], results: Any) -> bool:
    if row.get("format_version") != 2 or not isinstance(results, list):
        return False
    original = row.get("original_result_count")
    removed = row.get("removed_result_count")
    if (not isinstance(original, int) or isinstance(original, bool) or original < 0
            or not isinstance(removed, int) or isinstance(removed, bool) or removed < 0
            or original != len(results) + removed):
        return False
    for item in results:
        if not isinstance(item, dict) or not _REQUIRED_ITEM.issubset(item):
            return False
        if not isinstance(item.get("id"), int) or isinstance(item.get("id"), bool):
            return False
        if not isinstance(item.get("source_type"), str) or not item["source_type"]:
            return False
        if not isinstance(item.get("repo"), str) or not item["repo"]:
            return False
        if not isinstance(item.get("chunk_key"), str) or not item["chunk_key"]:
            return False
        if (not isinstance(item.get("chunk_index"), int)
                or isinstance(item.get("chunk_index"), bool) or item["chunk_index"] < 0):
            return False
        if not isinstance(item.get("content_snapshot"), str):
            return False
        digest = hashlib.sha256(item["content_snapshot"].encode("utf-8")).hexdigest()
        if item.get("content_sha256") != digest:
            return False
    return True


def export_search_logs(
    connection: Any,
    output_path: Path | str,
    *,
    calibration_root: Path | str = Path("runtime/calibration"),
    snippet_chars: int = 500,
) -> dict[str, int]:
    """Export only complete, hash-valid version 2 snapshots; never join chunks."""
    del snippet_chars  # Inline snapshots are exported intact.
    out = _validate_output_path(output_path, calibration_root)
    out.parent.mkdir(parents=True, exist_ok=True)
    counts = {
        "searches_seen": 0, "searches_exported": 0, "results_exported": 0,
        "legacy_searches_skipped": 0, "unresolved_results": 0,
    }
    tmp = out.parent / f"{out.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        try:
            from psycopg.rows import dict_row
            cursor_cm = connection.cursor(row_factory=dict_row)
        except (TypeError, ImportError, AttributeError):
            cursor_cm = connection.cursor()
        with cursor_cm as cur:
            cur.execute(
                "SELECT id, created_at, search_type, caller, top_k, filters, query, results, "
                "format_version, original_result_count, removed_result_count "
                "FROM search_log WHERE id > 585 ORDER BY id ASC"
            )
            rows = []
            for raw in cur.fetchall():
                row = _row_to_dict(cur, raw)
                counts["searches_seen"] += 1
                results = row.get("results")
                if isinstance(results, str):
                    try:
                        results = json.loads(results)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        results = None
                if not _valid_snapshot(row, results):
                    counts["legacy_searches_skipped"] += 1
                    continue
                row["results"] = results
                rows.append(row)
                counts["searches_exported"] += 1

        with open(tmp, "w", encoding="utf-8") as stream:
            for row in rows:
                created = row.get("created_at")
                created_str = created.isoformat() if isinstance(created, datetime.datetime) else (
                    str(created) if created is not None else ""
                )
                filters = row.get("filters")
                if isinstance(filters, str):
                    try:
                        filters = json.loads(filters)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        pass
                common = {
                    "log_id": row["id"], "created_at": created_str,
                    "search_type": row.get("search_type"), "caller": row.get("caller"),
                    "top_k": row.get("top_k"), "filters": filters, "query": row.get("query"),
                    "original_result_count": row["original_result_count"],
                    "removed_result_count": row["removed_result_count"],
                }
                if not row["results"]:
                    stream.write(json.dumps(
                        {**common, "empty_search": True, "results": []},
                        ensure_ascii=False,
                    ) + "\n")
                for item in row["results"]:
                    record = {
                        **common, "chunk_id": item["id"], **item,
                        "empty_search": False,
                    }
                    record["snippet"] = item.get("display_snippet", item["content_snapshot"])
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    counts["results_exported"] += 1
        tmp.replace(out)
    except BaseException:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
    return counts
