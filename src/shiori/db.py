def get_pr_changes(
    conn: psycopg.Connection, repo: str, issue_no: int
) -> tuple[list[dict], str | None]:
    """PR の変更ファイルマップを取得する（issue #54）。

    Returns:
        (files, head_sha) のタプル。
        files: ファイル一覧（path / status / additions / deletions / changes / blob_url）
        head_sha: PR の head_sha（全ファイル共通）。ファイル0件でも保持される。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT path, status, additions, deletions, changes, blob_url, head_sha
            FROM pr_changes
            WHERE repo = %s AND issue_no = %s
            ORDER BY path
            """,
            (repo, issue_no),
        )
        rows = cur.fetchall()
    head_sha = rows[0][6] if rows else None
    files = [
        {
            "path": r[0],
            "status": r[1],
            "additions": r[2],
            "deletions": r[3],
            "changes": r[4],
            "blob_url": r[5],
        }
        for r in rows
        if r[0]  # path が空文字の sentinel 行を除外
    ]
    return files, head_sha


def upsert_pr_changes(
    conn: psycopg.Connection,
    repo: str,
    issue_no: int,
    head_sha: str,
    files: list[dict],
) -> None:
    """PR の変更ファイルマップを upsert する（issue #54）。

    既存行を全削除してから新しいファイル一覧を挿入する（force-push 等で
    ファイル構成が変わるため、差分更新ではなく全置換）。
    ファイル0件の場合も head_sha を保持するため sentinel 行を挿入する。
    呼び出し側が commit すること（upsert_pr_changes 内では commit しない）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pr_changes WHERE repo = %s AND issue_no = %s",
            (repo, issue_no),
        )
        if files:
            for f in files:
                cur.execute(
                    """
                    INSERT INTO pr_changes (repo, issue_no, head_sha, path, status,
                                            additions, deletions, changes, blob_url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        repo, issue_no, head_sha,
                        f["filename"],
                        f.get("status"),
                        f.get("additions"),
                        f.get("deletions"),
                        f.get("changes"),
                        f.get("blob_url"),
                    ),
                )
        else:
            # ファイル0件の PR でも head_sha を保持（sentinel 行。path='' で識別）
            cur.execute(
                """
                INSERT INTO pr_changes (repo, issue_no, head_sha, path, status,
                                        additions, deletions, changes, blob_url)
                VALUES (%s, %s, %s, '', NULL, NULL, NULL, NULL, NULL)
                """,
                (repo, issue_no, head_sha),
            )


def get_pr_head_sha(
    conn: psycopg.Connection, repo: str, issue_no: int
) -> str | None:
    """PR の保存済み head_sha を取得する（変更検知用。issue #54）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT head_sha FROM pr_changes WHERE repo = %s AND issue_no = %s LIMIT 1",
            (repo, issue_no),
        )
        row = cur.fetchone()
        return row[0] if row else None