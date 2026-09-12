"""Widened search-log exporter (shiori #451).

Exports search_log rows with id > 43 that contain widened multi-retriever
scoring data to private JSONL under runtime/calibration/.
"""

from __future__ import annotations

import datetime
import json
import os
import uuid
from pathlib import Path
from typing import Any


def _row_to_dict(cur: Any, row: Any) -> dict[str, Any]:
    """Convert a database row to a standard dictionary."""
    if isinstance(row, dict):
        return row
    if hasattr(row, "keys") and callable(row.keys):
        return {k: row[k] for k in row.keys()}
    if hasattr(cur, "description") and cur.description:
        col_names = [
            col[0] if isinstance(col, (list, tuple)) else getattr(col, "name", str(col))
            for col in cur.description
        ]
        return dict(zip(col_names, row))
    return dict(row)


def _validate_output_path(output_path: Path | str, calibration_root: Path | str) -> Path:
    """Validate that output_path resolves strictly below calibration_root."""
    resolved_root = Path(calibration_root).resolve()
    resolved_out = Path(output_path).resolve()

    try:
        is_sub = resolved_out.is_relative_to(resolved_root)
    except AttributeError:
        try:
            resolved_out.relative_to(resolved_root)
            is_sub = True
        except ValueError:
            is_sub = False

    if not is_sub or resolved_out == resolved_root:
        raise ValueError(
            f"output_path '{output_path}' must resolve strictly below "
            f"calibration_root '{calibration_root}'"
        )
    return resolved_out


def _is_widened_results(results: Any) -> bool:
    """Check if results list has the widened multi-retriever shape."""
    if not isinstance(results, list):
        return False
    required_keys = {"vec_score", "vec_rank", "kw_score", "kw_rank"}
    for item in results:
        if not isinstance(item, dict):
            return False
        if not required_keys.issubset(item.keys()):
            return False
    return True


def export_search_logs(
    connection: Any,
    output_path: Path | str,
    *,
    calibration_root: Path | str = Path("runtime/calibration"),
    snippet_chars: int = 500,
) -> dict[str, int]:
    """Export widened search_log rows to private JSONL."""
    resolved_out = _validate_output_path(output_path, calibration_root)
    resolved_out.parent.mkdir(parents=True, exist_ok=True)

    searches_seen = 0
    searches_exported = 0
    results_exported = 0
    legacy_searches_skipped = 0
    unresolved_results = 0

    temp_path = resolved_out.parent / f"{resolved_out.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"

    try:
        try:
            from psycopg.rows import dict_row

            cursor_cm = connection.cursor(row_factory=dict_row)
        except (TypeError, ImportError, AttributeError):
            cursor_cm = connection.cursor()

        with cursor_cm as cur:
            cur.execute(
                "SELECT id, created_at, search_type, caller, top_k, filters, query, results "
                "FROM search_log "
                "WHERE id > 43 "
                "ORDER BY id ASC"
            )
            raw_log_rows = cur.fetchall()

            widened_searches: list[dict[str, Any]] = []
            chunk_ids_to_fetch: set[int] = set()

            for raw_row in raw_log_rows:
                row_dict = _row_to_dict(cur, raw_row)
                searches_seen += 1

                results = row_dict.get("results")
                if isinstance(results, str):
                    try:
                        results = json.loads(results)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        results = None

                if not _is_widened_results(results):
                    legacy_searches_skipped += 1
                    continue

                searches_exported += 1
                row_dict["results"] = results
                widened_searches.append(row_dict)

                assert isinstance(results, list)
                for item in results:
                    cid = item.get("chunk_id", item.get("id"))
                    if cid is not None:
                        try:
                            chunk_ids_to_fetch.add(int(cid))
                        except (ValueError, TypeError):
                            pass

            chunk_map: dict[int, dict[str, Any]] = {}
            if chunk_ids_to_fetch:
                chunk_list = list(chunk_ids_to_fetch)
                batch_size = 2000
                for i in range(0, len(chunk_list), batch_size):
                    batch = chunk_list[i : i + batch_size]
                    cur.execute(
                        "SELECT id, source_type, repo, path, issue_no, comment_id, url, content "
                        "FROM chunks "
                        "WHERE id = ANY(%s)",
                        (batch,),
                    )
                    for raw_chunk in cur.fetchall():
                        c_dict = _row_to_dict(cur, raw_chunk)
                        chunk_map[c_dict["id"]] = c_dict

        with open(temp_path, "w", encoding="utf-8") as f:
            for search_row in widened_searches:
                log_id = search_row["id"]
                created_at = search_row.get("created_at")
                if isinstance(created_at, datetime.datetime):
                    created_at_str = created_at.isoformat()
                else:
                    created_at_str = str(created_at) if created_at is not None else ""

                search_type = search_row.get("search_type")
                caller = search_row.get("caller")
                top_k = search_row.get("top_k")
                filters = search_row.get("filters")
                if isinstance(filters, str):
                    try:
                        filters = json.loads(filters)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        pass

                query = search_row.get("query")
                results = search_row.get("results") or []

                for item in results:
                    cid = item.get("chunk_id", item.get("id"))
                    if cid is not None:
                        try:
                            cid_int = int(cid)
                        except (ValueError, TypeError):
                            cid_int = cid
                    else:
                        cid_int = None

                    chunk_data = chunk_map.get(cid_int) if cid_int is not None else None
                    chunk_resolved = chunk_data is not None

                    if chunk_resolved and chunk_data is not None:
                        source_type = chunk_data.get("source_type")
                        repo = chunk_data.get("repo")
                        path = chunk_data.get("path")
                        issue_no = chunk_data.get("issue_no")
                        comment_id = chunk_data.get("comment_id")
                        url = chunk_data.get("url")
                        raw_content = chunk_data.get("content")
                        if raw_content is not None:
                            limit_chars = max(0, snippet_chars) if snippet_chars >= 0 else 0
                            snippet = str(raw_content)[:limit_chars]
                        else:
                            snippet = ""
                    else:
                        unresolved_results += 1
                        source_type = None
                        repo = None
                        path = None
                        issue_no = None
                        comment_id = None
                        url = None
                        snippet = None

                    score = item.get("score")
                    vec_score = item.get("vec_score")
                    vec_rank = item.get("vec_rank")
                    kw_score = item.get("kw_score")
                    kw_rank = item.get("kw_rank")

                    out_line = {
                        "log_id": log_id,
                        "created_at": created_at_str,
                        "search_type": search_type,
                        "caller": caller,
                        "top_k": top_k,
                        "filters": filters,
                        "query": query,
                        "chunk_id": cid_int,
                        "source_type": source_type,
                        "repo": repo,
                        "path": path,
                        "issue_no": issue_no,
                        "comment_id": comment_id,
                        "url": url,
                        "snippet": snippet,
                        "score": score,
                        "vec_score": vec_score,
                        "vec_rank": vec_rank,
                        "kw_score": kw_score,
                        "kw_rank": kw_rank,
                        "chunk_resolved": chunk_resolved,
                    }
                    f.write(json.dumps(out_line, ensure_ascii=False) + "\n")
                    results_exported += 1

        temp_path.replace(resolved_out)
    except BaseException:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise

    return {
        "searches_seen": searches_seen,
        "searches_exported": searches_exported,
        "results_exported": results_exported,
        "legacy_searches_skipped": legacy_searches_skipped,
        "unresolved_results": unresolved_results,
    }
