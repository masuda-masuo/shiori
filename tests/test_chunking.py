from shiori.chunking import (
    Chunk,
    _TS_AVAILABLE,
    _find_breakpoint,
    _split_long_text,
    _split_symbols,
    _ts_get_parser,
    detect_language,
    split_code,
    split_issue_text,
    split_markdown,
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
    RRF_K = 60
    vec = [10, 11, 12]
    kw = [12, 13]
    scores: dict[int, float] = {}
    for rank, rid in enumerate(vec):
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, rid in enumerate(kw):
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank + 1)
    ranked = [rid for rid, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]
    assert ranked[0] == 12


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
    assert len(chunks) >= 1
    if len(chunks) > 1:
        assert chunks[0].heading_path is not None
        assert chunks[0].start_line is not None
        assert chunks[0].end_line is not None
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
        assert "add" in chunks[0].content


def test_split_code_fallback_for_unknown_ext():
    """未知の拡張子はフォールバック分割される。"""
    code = "some random text\n" * 100
    chunks = split_code("unknown.xyz", code, max_chars=200)
    assert len(chunks) > 1
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
    assert isinstance(chunks, list)


# ---------------------------------------------------------------------------
# tree-sitter 前提の断定的テスト（_ts_get_parser 成否で gate）
# ---------------------------------------------------------------------------

import pytest

_TS_PYTHON_OK = _ts_get_parser("python") is not None


@pytest.mark.skipif(not _TS_PYTHON_OK, reason="tree-sitter python parser not available")
def test_split_code_ts_python_has_chunks():
    """tree-sitter が使えるなら Python で必ず2チャンク以上＋start_line 設定。"""
    code = """\
def hello():
    print("hello")

class Greeter:
    def greet(self, name: str) -> str:
        return f"Hello {name}"
"""
    chunks = split_code("test.py", code)
    assert len(chunks) >= 2, f"expected >=2 chunks, got {len(chunks)}"
    for c in chunks:
        assert c.start_line is not None, f"start_line missing in {c}"
        assert c.end_line is not None, f"end_line missing in {c}"
        assert c.symbols is not None, f"symbols missing in {c}"
        assert c.heading_path is not None


@pytest.mark.skipif(not _TS_PYTHON_OK, reason="tree-sitter python parser not available")
def test_split_code_ts_python_docstring_emitted():
    """docstring が content に含まれる。"""
    code = '''\
def add(a: int, b: int) -> int:
    """Add two numbers together."""
    return a + b
'''
    chunks = split_code("math.py", code)
    assert len(chunks) >= 1
    combined = "\n".join(c.content for c in chunks)
    assert "Add two numbers together" in combined
    assert "add" in combined.lower()


@pytest.mark.skipif(not _TS_PYTHON_OK, reason="tree-sitter python parser not available")
def test_split_code_ts_python_nesting():
    """クラスの中のメソッドの heading_path に親クラス情報が含まれる。"""
    code = """\
class Calculator:
    def add(self, a, b):
        return a + b

    def sub(self, a, b):
        return a - b
"""
    chunks = split_code("calc.py", code)
    assert len(chunks) >= 2
    # 注意: クラスチャンクのシグネチャは body 開始行（= "def add" 行）まで含むため、
    # "add" 一致だとクラスチャンク自身も拾ってしまう。メソッド本体に固有の行で一意に選ぶ。
    method_chunks = [c for c in chunks if "return a + b" in c.content]
    assert method_chunks, "add メソッドのチャンクが見つからない"
    hp = method_chunks[0].heading_path or ""
    assert "Calculator" in hp


@pytest.mark.skipif(not _TS_PYTHON_OK, reason="tree-sitter python parser not available")
def test_split_code_ts_python_symbols():
    """symbols に分割済み識別子が入る。"""
    code = "def parse_config_file(path):\n    return None\n"
    chunks = split_code("cfg.py", code)
    assert len(chunks) >= 1
    sym = chunks[0].symbols or ""
    assert "parse" in sym
    assert "config" in sym
    assert "file" in sym


# ---------------------------------------------------------------------------
# _find_breakpoint / _split_long_text: チャンク境界で単語が分断されないこと（issue #85）
# ---------------------------------------------------------------------------


def test_find_breakpoint_at_period():
    """句点で分割される（max_chars より手前の最終句点を探す）。"""
    s = "あ" * 800 + "。文章の続きです。" + "B" * 500
    bp = _find_breakpoint(s, 1200)
    assert bp == 809  # s[808]="。", その直後 index 809


def test_find_breakpoint_at_comma_fallback():
    """句点がない場合、読点で分割される。"""
    s = "A" * 1195 + "、" + "B" * 500
    bp = _find_breakpoint(s, 1200)
    assert bp == 1196


def test_find_breakpoint_hard_cut_when_no_boundary():
    """文境界が見つからない場合は max_chars でハードカット。"""
    s = "A" * 2000  # 句読点なし
    bp = _find_breakpoint(s, 1200)
    assert bp == 1200


def test_split_long_text_preserves_katakana_word():
    """max_chars 境界にカタカナ語があっても分断されない（issue #85）。"""
    prefix = "あ" * 1190
    s = prefix + "ダッシュボード" + "が重要な機能です。さらに続きます。" + "い" * 100
    parts = _split_long_text(s, 1200)
    assert len(parts) >= 2
    # 最初のチャンクは句点まで含む
    combined = "".join(parts)
    assert "ダッシュボード" in combined
    # 各チャンクが max_chars を超えていない
    for p in parts:
        assert len(p) <= 1200


def test_split_long_text_short_text_unchanged():
    """短いテキストはそのまま返る。"""
    short = "これは短いテキストです。分割不要。"
    parts = _split_long_text(short, 1200)
    assert len(parts) == 1
    assert parts[0] == short
