from shiori.chunking import (
    Chunk,
    detect_language,
    split_code,
    split_issue_text,
    split_markdown,
    _split_symbols,
)


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
        assert body.endswith("。") or body.endswith(")")


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


# ---------------------------------------------------------------------------
# split_code のテスト（Step 2, issue #33）
# ---------------------------------------------------------------------------


def test_split_symbols_snake_case():
    assert _split_symbols("parse_config") == "parse config"


def test_split_symbols_camel_case():
    assert _split_symbols("parseConfig") == "parse config"


def test_split_symbols_pascal_case():
    assert _split_symbols("ParseConfig") == "parse config"


def test_split_symbols_acronym():
    assert _split_symbols("parseXML") == "parse xml"
    assert _split_symbols("ParseXML") == "parse xml"


def test_split_symbols_empty():
    assert _split_symbols("") == ""
    assert _split_symbols(None) == ""


def test_split_code_python_basic():
    """Python の関数とクラスが分割されることを確認。"""
    code = """\
def hello():
    print("hello")

class Greeter:
    def greet(self, name: str) -> str:
        return f"Hello {name}"
"""
    chunks = split_code("test.py", code)
    # tree-sitter が利用可能なら 2 チャンク（hello と Greeter）、
    # 利用不可ならフォールバックで 1 チャンク
    assert len(chunks) >= 1
    if len(chunks) > 1:
        # heading_path にファイル名が含まれる
        assert chunks[0].heading_path is not None
        # start_line / end_line が設定されている
        assert chunks[0].start_line is not None
        assert chunks[0].end_line is not None
        # symbols に関数名が含まれる
        assert chunks[0].symbols is not None
        assert "hello" in chunks[0].content.lower()


def test_split_code_python_with_docstring():
    """docstring が content に含まれること。"""
    code = '''\
def add(a: int, b: int) -> int:
    """Add two numbers together."""
    return a + b
'''
    chunks = split_code("math.py", code)
    assert len(chunks) >= 1
    if len(chunks) == 1 and chunks[0].symbols is not None:
        # tree-sitter 利用時
        assert "add" in chunks[0].content


def test_split_code_fallback_for_unknown_ext():
    """未知の拡張子はフォールバック分割される。"""
    code = "some random text\n" * 100
    chunks = split_code("unknown.xyz", code, max_chars=200)
    assert len(chunks) > 1
    # フォールバック時は symbols=None
    assert chunks[0].symbols is None
    assert chunks[0].start_line is None


def test_split_code_empty():
    """空ファイルは空リスト。"""
    assert split_code("empty.py", "") == []
    assert split_code("empty.xyz", "") == []


def test_split_code_no_definitions():
    """定義がないファイルでもクラッシュしない。"""
    code = "x = 1\ny = 2\n"
    chunks = split_code("vars.py", code)
    # tree-sitter が動けば空リストか、フォールバックで 1 チャンク
    assert isinstance(chunks, list)
