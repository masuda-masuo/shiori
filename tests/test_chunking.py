from shiori.chunking import detect_language, split_issue_text, split_markdown


def test_split_markdown_heading_path():
    md = (
        "# 設計\n\nはじめに。\n\n## 認証\n\nトークンの話。\n\n### トークン\n\n詳細。\n"
    )
    chunks = split_markdown(md)
    paths = [c.heading_path for c in chunks]
    assert paths == ["設計", "設計 > 認証", "設計 > 認証 > トークン"]
    assert chunks[1].content.startswith("[設計 > 認証]")
    assert [c.chunk_index for c in chunks] == [0, 1, 2]


def test_split_markdown_ignores_headings_in_code_fence():
    md = "# A\n\n```\n# not a heading\n```\nafter\n"
    chunks = split_markdown(md)
    assert len(chunks) == 1
    assert "# not a heading" in chunks[0].content


def test_long_japanese_section_splits_on_sentence_boundary():
    sentence = "これは日本語の文章でありチャンク分割の検証に使う長めの文です。"
    md = "# 長文\n\n" + sentence * 60
    chunks = split_markdown(md, max_chars=500)
    assert len(chunks) > 1
    for c in chunks:
        body = c.content.split("\n", 1)[-1]
        assert len(body) <= 500
        # 文の途中で切れていない（プレフィックス行を除く）
        assert body.endswith("。")


def test_split_issue_text_prefixes_title():
    chunks = split_issue_text("バグ: 検索が落ちる", "再現手順は以下。")
    assert len(chunks) == 1
    assert chunks[0].content.startswith("[バグ: 検索が落ちる]")


def test_split_issue_text_empty_body():
    assert split_issue_text(None, "") == []
    chunks = split_issue_text("タイトルだけ", "")
    assert len(chunks) == 1


def test_detect_language():
    assert detect_language("これは日本語のドキュメントです。") == "ja"
    assert detect_language("This is an English document about search.") == "en"
    assert detect_language("Use the `read_file` ツールで全文を取得する。") == "ja"


def test_rrf_fusion_orders_overlap_first():
    # search.semantic_search の RRF 部分と同じ計算をスタンドアロンで検証
    RRF_K = 60
    vec = [10, 11, 12]
    kw = [12, 13]
    scores: dict[int, float] = {}
    for rank, rid in enumerate(vec):
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, rid in enumerate(kw):
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank + 1)
    ranked = [rid for rid, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]
    assert ranked[0] == 12  # 両方にヒットしたものが先頭
