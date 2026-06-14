    ),


@mcp.tool(name="shiori_search")
def semantic_search(
    query: str,
    source_type: str | None = None,
    language: str | None = None,
    state: str | None = None,
    repo: str | None = None,
    path_prefix: str | None = None,
    updated_after: str | None = None,
    prog_lang: str | None = None,
    top_k: int | None = None,
    sort_by: str = "score",
    sort_order: str = "desc",
) -> list[dict[str, Any]]:
    """意味ベースの検索（入口ツール）。言い換え・概念・クロスリンガル（日本語クエリで英語ドキュメント）に強い。
    内部でキーワード検索とのハイブリッド融合 (RRF) を行う。
    ポインタ（path / heading_path / issue_no）＋スニペット＋URL を返す。全文は read 系で取得すること。
    filters: source_type は doc / issue / pr_review / code、language は ja / en、
    state は open / closed、prog_lang は python / go / rust 等、
    updated_after は ISO8601 日付。
    sort_by: "score"（既定）/ "updated_at" / "created_at"。
      updated_at / created_at でのソートは RRF の関連度順を破棄する。
      鮮度が絶対条件でない限り、既定（score）のまま使うことを推奨。
    sort_order: "desc"（既定）/ "asc"。"""
    with _conn() as conn:
        return search.semantic_search(
            settings, conn, _get_embedder(), query,
            _make_filters(source_type, language, state, repo, path_prefix, updated_after, prog_lang),
            top_k,
            sort_by,
            sort_order,
        )


@mcp.tool(name="shiori_keyword_search")
def keyword_search(
    query: str,
    source_type: str | None = None,
    language: str | None = None,
    state: str | None = None,
    repo: str | None = None,
    path_prefix: str | None = None,
    updated_after: str | None = None,
    prog_lang: str | None = None,
    top_k: int | None = None,
    sort_by: str = "score",
    sort_order: str = "desc",
) -> list[dict[str, Any]]:
    """キーワード検索（日本語対応トークナイズ）。関数名・API 名・エラーコード・設定キーなど
    固有の文字列の厳密一致に強い。通常は shiori_search を使い、厳密一致が必要なときに
    このツールを使うこと。
    code チャンクは content（シグネチャ＋docstring）と symbols（識別子分割済み文字列）の
    OR 検索になるため、camelCase/snake_case の部分一致でも発見できる。
    sort_by: "score"（既定）/ "updated_at" / "created_at"。
      updated_at / created_at でのソートは pgroonga の関連度順を破棄する。
      鮮度が絶対条件でない限り、既定（score）のまま使うことを推奨。
    sort_order: "desc"（既定）/ "asc"。"""
    with _conn() as conn:
        return search.keyword_search(
            settings, conn, query,
            _make_filters(source_type, language, state, repo, path_prefix, updated_after, prog_lang),
            top_k,
            sort_by,
            sort_order,
        )