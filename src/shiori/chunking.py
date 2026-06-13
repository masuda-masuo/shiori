"""チャンク分割（詳細設計/02）。

決定事項:
- docs: 見出し単位で分割し、見出しパスをメタデータに保持。
  長い節は文字基準（既定 1200 字）で、文境界（。．.!? と改行）を優先して分割。
- issue/PR: コメント 1 件を自然な単位とし、`[タイトル]` を文脈プレフィックスとして付与。
- 言語はチャンク（実質ファイル/コメント）単位でヒューリスティック判定（ja/en）。
- code: tree-sitter で関数/メソッド/クラス単位に分割（地図型: シグネチャ＋docstring）。
  非対応言語は _split_long_text フォールバック（詳細設計/10）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# 文境界: 日本語句点・感嘆/疑問・英語ピリオド類の直後、または空行
_SENTENCE_END_RE = re.compile(r"(?<=[。．！？!?\\.])\s*|\n{2,}")
_JA_CHAR_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")

# --- tree-sitter ---
_TS_AVAILABLE = False
_TS_LANGUAGES: set[str] = set()
try:
    from tree_sitter_language_pack import get_binding, get_parser  # type: ignore[import-untyped]

    _TS_AVAILABLE = True
    _binding = get_binding()
    _TS_LANGUAGES = set(_binding.keys()) if hasattr(_binding, "keys") else set()
except ImportError:
    pass

# 拡張子 → tree-sitter 言語名
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cxx": "cpp",
    ".cc": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".json": "json",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".sql": "sql",
    ".lua": "lua",
    ".ml": "ocaml",
    ".mli": "ocaml",
    ".zig": "zig",
    ".cmake": "cmake",
    ".proto": "protobuf",
    ".sql": "sql",
    ".tf": "hcl",
    ".vue": "vue",
    ".svelte": "svelte",
}

# tree-sitter クエリ: 定義ノードをキャプチャする
# 言語ごとに異なるノードタイプを同一の抽象「定義」にマップする。
_TREE_SITTER_QUERIES: dict[str, str] = {
    "python": """
        (function_definition) @func
        (class_definition) @class
    """,
    "javascript": """
        (function_declaration) @func
        (method_definition) @method
        (class_declaration) @class
    """,
    "typescript": """
        (function_declaration) @func
        (method_definition) @method
        (class_declaration) @class
    """,
    "go": """
        (function_declaration) @func
        (method_declaration) @method
    """,
    "rust": """
        (function_item) @func
        (struct_item) @struct
        (impl_item) @impl
        (trait_item) @trait
    """,
    "java": """
        (method_declaration) @method
        (class_declaration) @class
    """,
    "ruby": """
        (method) @method
        (class) @class
        (module) @module
    """,
    "c": """
        (function_definition) @func
        (struct_specifier) @struct
    """,
    "cpp": """
        (function_definition) @func
        (class_specifier) @class
        (struct_specifier) @struct
    """,
    "csharp": """
        (method_declaration) @method
        (class_declaration) @class
    """,
    "php": """
        (function_definition) @func
        (class_declaration) @class
        (method_declaration) @method
    """,
    "swift": """
        (function_declaration) @func
        (class_declaration) @class
    """,
    "kotlin": """
        (function_declaration) @func
        (class_declaration) @class
    """,
    "bash": """
        (function_definition) @func
    """,
}

# コードブロックのデフォルト最大文字数
_CODE_MAX_CHARS = 1200


@dataclass
class Chunk:
    content: str
    heading_path: str | None = None
    chunk_index: int = 0
    start_line: int | None = None
    end_line: int | None = None
    symbols: str | None = None


def detect_language(text: str) -> str:
    """日本語文字（ひらがな・カタカナ・漢字）の比率で ja / en を判定する。"""
    if not text:
        return "en"
    ja = len(_JA_CHAR_RE.findall(text))
    letters = sum(1 for c in text if c.isalpha()) + ja
    if letters == 0:
        return "en"
    return "ja" if ja / letters >= 0.2 else "en"


def _split_long_text(text: str, max_chars: int) -> list[str]:
    """文境界を優先しつつ max_chars 以下の断片に貪欲に詰める。"""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    sentences = [s for s in _SENTENCE_END_RE.split(text) if s and s.strip()]
    parts: list[str] = []
    buf = ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        # 単一の文が長すぎる場合は機械的に切る（コードブロック等）
        while len(s) > max_chars:
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(s[:max_chars])
            s = s[max_chars:]
        if buf and len(buf) + len(s) + 1 > max_chars:
            parts.append(buf)
            buf = s
        else:
            buf = f"{buf}\n{s}" if buf else s
    if buf:
        parts.append(buf)
    return parts


def split_markdown(text: str, max_chars: int = 1200) -> list[Chunk]:
    """Markdown を見出し単位で分割する。

    - 見出しスタックから `親 > 子` 形式の heading_path を組み立てる。
    - コードフェンス内の `#` は見出しとして扱わない。
    - 各チャンク本文の先頭に heading_path を 1 行付け、チャンク単体でも文脈が分かるようにする。
    """
    lines = text.splitlines()
    sections: list[tuple[str | None, list[str]]] = []
    stack: list[tuple[int, str]] = []
    current: list[str] = []
    in_fence = False

    def flush() -> None:
        nonlocal current
        body = "\n".join(current).strip()
        if body:
            hp = " > ".join(t for _, t in stack) or None
            sections.append((hp, current))
        current = []

    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            current.append(line)
            continue
        m = None if in_fence else _HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        else:
            current.append(line)
    flush()

    chunks: list[Chunk] = []
    for heading_path, body_lines in sections:
        body = "\n".join(body_lines).strip()
        for part in _split_long_text(body, max_chars):
            content = f"[{heading_path}]\n{part}" if heading_path else part
            chunks.append(Chunk(content=content, heading_path=heading_path))
    for i, c in enumerate(chunks):
        c.chunk_index = i
    return chunks


def split_issue_text(
    title: str | None, body: str, max_chars: int = 1200
) -> list[Chunk]:
    """issue/PR 本文・コメントをチャンク化する。

    タイトルを `[title]` プレフィックスとして全チャンクに付け、
    コメント単体でも何の議論か分かるようにする（詳細設計/02）。
    """
    prefix = f"[{title.strip()}]\n" if title and title.strip() else ""
    budget = max(max_chars - len(prefix), 200)
    parts = _split_long_text(body or "", budget)
    if not parts and title:
        parts = [""]
    chunks = [
        Chunk(content=(prefix + p).strip(), chunk_index=i)
        for i, p in enumerate(parts)
        if (prefix + p).strip()
    ]
    return chunks


# ---------------------------------------------------------------------------
# Step 2: ソースコード分割（詳細設計/10）
# ---------------------------------------------------------------------------

# snake_case / camelCase / PascalCase の識別子を分割し、小文字スペース区切りにする。
# 例: "parse_config" -> "parse config", "parseConfig" -> "parse config",
#      "parseXML" -> "parse xml", "ParseXML" -> "parse xml"
_SYMBOL_SPLIT_RE = re.compile(
    r"""
    # 1) snake_case のアンダースコア → スペース
    | _
    # 2) 小文字の直後に大文字（camelCase 境界）
    #    lookbehind で小文字、lookahead で大文字 or 数字
    | (?<=[a-z])(?=[A-Z0-9])
    # 3) 大文字連続の後に小文字が来る（e.g., "parseXML" -> "parse" + "XML"）
    #    ただし先頭の大文字は維持したいので、大文字2文字以上の直後に小文字
    | (?<=[A-Z])(?=[A-Z][a-z])
    # 4) 数字と英字の境界
    | (?<=[a-zA-Z])(?=\d)
    | (?<=\d)(?=[a-zA-Z])
    """,
    re.VERBOSE,
)


def _split_symbols(text: str) -> str:
    """識別子を snake/camel 境界で分割し、小文字スペース区切り文字列を返す。

    >>> _split_symbols("parse_config")
    'parse config'
    >>> _split_symbols("parseConfig")
    'parse config'
    >>> _split_symbols("ParseXML")
    'parse xml'
    """
    if not text:
        return ""
    # アンダースコアと camelCase 境界で分割
    parts = _SYMBOL_SPLIT_RE.split(text)
    parts = [p for p in parts if p and p.strip()]
    # さらに非アルファベット文字（記号類）で分割
    clean = re.sub(r"[^a-zA-Z0-9\s]", " ", " ".join(parts))
    return " ".join(clean.lower().split())


def _detect_prog_lang(file_path: str) -> str | None:
    """ファイルパスの拡張子からプログラミング言語名を推測する。不明なら None。"""
    _, ext = os.path.splitext(file_path)
    return _EXT_TO_LANG.get(ext.lower())


def _ts_node_text(node) -> str:
    """tree-sitter ノードのテキストを安全に取得する。"""
    try:
        raw = node.text
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)
    except Exception:
        return ""


def _get_node_name(node) -> str:
    """tree-sitter ノードの 'name' フィールドのテキストを取得。"""
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return _ts_node_text(name_node)
    return ""


def _get_docstring_text(node, lang: str) -> str:
    """Python/JS/TS 等の docstring を抽出する。

    Python: body の先頭が expression_statement で単一文字列リテラル
    JS/TS/Rust: body の先頭が expression_statement か、leading comment 等
    """
    body = node.child_by_field_name("body")
    if body is None:
        return ""
    if lang == "python":
        # Python: body の最初の子が expression_statement で、その中に string がある
        for child in body.children:
            if child.type == "expression_statement":
                # string ノードを探す
                for sub in child.children:
                    if "string" in sub.type:
                        text = _ts_node_text(sub)
                        # クォートとトリプルクォートを除去
                        text = re.sub(r'^["\']{1,3}|["\']{1,3}$', "", text)
                        return text.strip()
    elif lang in ("javascript", "typescript"):
        for child in body.children:
            if child.type == "expression_statement":
                for sub in child.children:
                    if sub.type == "template_string" or "string" in sub.type:
                        text = _ts_node_text(sub)
                        text = re.sub(r'^[`"\']|[`"\']$', "", text)
                        return text.strip()
            # JSDoc comment
            if child.type == "comment":
                text = _ts_node_text(child)
                text = re.sub(r"^/\*+|\*+/$", "", text)
                text = re.sub(r"^\s*\* ?", "", text, flags=re.MULTILINE)
                return text.strip()
    elif lang == "rust":
        for child in body.children:
            if child.type == "expression_statement":
                for sub in child.children:
                    if sub.type == "string_literal":
                        text = _ts_node_text(sub)
                        text = re.sub(r'^["\']|["\']$', "", text)
                        return text.strip()
            # doc comment (/// or //!)
            if child.type == "line_comment":
                text = _ts_node_text(child)
                text = re.sub(r"^//[/!]?\s*", "", text)
                if text:
                    return text.strip()
    else:
        # 汎用: body 内の comment / string を探す
        for child in body.children:
            if child.type == "comment":
                text = _ts_node_text(child)
                text = re.sub(r"^/\*+|\*+/|^//\s*", "", text)
                return text.strip()
    return ""


def _get_signature_text(node, source_lines: list[str]) -> str:
    """定義ノードのシグネチャ（先頭行〜 '{' または ':' の前まで）を取得。"""
    start = node.start_point[0]
    end = node.end_point[0]

    # シグネチャはおおむね先頭の数行。body 直前まで。
    body = node.child_by_field_name("body")
    if body is not None:
        sig_end_line = body.start_point[0]
    else:
        # body が無い場合（プロトタイプ等）は全体
        sig_end_line = end

    # start_line から sig_end_line まで（ただし空行でない範囲）
    sig_lines = []
    for i in range(start, min(sig_end_line + 1, len(source_lines))):
        line = source_lines[i]
        sig_lines.append(line)

    sig = "\n".join(sig_lines)
    # 末尾の空白行と `{`, `:` 行以降の余計なものを除去
    sig = sig.strip()
    return sig


def _collect_def_nodes(node, query) -> list[tuple]:
    """tree-sitter クエリを実行し、(capture_name, node) のリストを返す。"""
    try:
        matches = query.matches(node)
        results: list[tuple[str, object]] = []
        for pattern_index, capture_map in matches:
            for capture_name, captured_nodes in capture_map.items():
                for n in captured_nodes:
                    results.append((capture_name, n))
        return results
    except Exception:
        return []


def _build_heading_path(path_prefix: str, name: str, kind: str) -> str:
    """シンボルパスの1要素を heading_path 用に整形。"""
    kind_label = {
        "func": "def",
        "method": "def",
        "class": "class",
        "struct": "struct",
        "impl": "impl",
        "trait": "trait",
        "module": "module",
    }.get(kind, kind)
    return f"{path_prefix} ({kind_label} {name})" if path_prefix else f"({kind_label} {name})"


def split_code(
    file_path: str,
    content: str,
    max_chars: int = _CODE_MAX_CHARS,
) -> list[Chunk]:
    """ソースコードを関数/メソッド/クラス単位でチャンク分割する（詳細設計/10 Step 2）。

    対応言語は tree-sitter で AST パースし、非対応言語は ``_split_long_text`` で
    フォールバック分割する。

    Parameters
    ----------
    file_path:
        ファイルパス（拡張子から言語判定に使う）。
    content:
        ファイルの全文。
    max_chars:
        フォールバック時の最大文字数。

    Returns
    -------
    list[Chunk]
        各 Chunk は以下を持つ:
        - content: ``[シンボルパス]\\nシグネチャ\\ndocstring``
        - heading_path: ``module.py > class Foo > def bar``
        - start_line / end_line: 行範囲（1-based、後方互換のため）
        - symbols: 識別子分割済み文字列
    """
    prog_lang = _detect_prog_lang(file_path)
    if not prog_lang or prog_lang not in _TS_LANGUAGES or not _TS_AVAILABLE:
        # tree-sitter 非対応 → フォールバック
        return _split_code_fallback(content, file_path, max_chars)

    parser = get_parser(prog_lang)
    tree = parser.parse(bytes(content, "utf-8"))
    root = tree.root_node
    source_lines = content.splitlines()

    # クエリを準備
    query_src = _TREE_SITTER_QUERIES.get(prog_lang)
    if query_src is None:
        return _split_code_fallback(content, file_path, max_chars)

    try:
        from tree_sitter import Query

        query = Query(parser.language, query_src)
    except Exception:
        return _split_code_fallback(content, file_path, max_chars)

    # 全定義ノードを収集（ネスト込み）
    def _walk_defs(node, depth=0) -> list[tuple[str, object, int]]:
        """(capture_name, node, depth) のリストを返す。"""
        results: list[tuple[str, object, int]] = []
        try:
            matches = query.matches(node)
            for _pattern_index, capture_map in matches:
                for capture_name, captured_nodes in capture_map.items():
                    for n in captured_nodes:
                        # トップレベルのマッチのみ（子孫のマッチは再帰でカバー）
                        if n == node or n.parent == node.parent:
                            results.append((capture_name, n, depth))
        except Exception:
            pass
        for child in node.children:
            results.extend(_walk_defs(child, depth + 1))
        return results

    def_nodes = _walk_defs(root)

    # 定義の親子関係を解決して heading_path を生成
    # 各定義について、自分を包含する定義を親とする
    chunks: list[Chunk] = []

    # start_line でソート
    def_nodes.sort(key=lambda x: x[1].start_point[0])

    for capture_name, ts_node, _depth in def_nodes:
        start_line = ts_node.start_point[0]  # 0-based
        end_line = ts_node.end_point[0]

        name = _get_node_name(ts_node) or f"<{capture_name}>"
        docstring = _get_docstring_text(ts_node, prog_lang)
        signature = _get_signature_text(ts_node, source_lines)

        # heading_path: 自分を含む親定義から構築
        # 親定義を探す（start_line が自分より前で、end_line が自分以上、最も近いもの）
        parent_path_parts: list[str] = []
        for p_capture, p_node, _p_depth in def_nodes:
            if p_node == ts_node:
                continue
            p_start = p_node.start_point[0]
            p_end = p_node.end_point[0]
            if p_start < start_line and p_end >= end_line:
                p_name = _get_node_name(p_node) or f"<{p_capture}>"
                parent_path_parts.append(f"({p_capture} {p_name})")

        # ファイル名をベースに heading_path 構築
        base_name = os.path.basename(file_path)
        path_prefix = " > ".join(parent_path_parts)
        heading_path = f"{base_name} > {path_prefix}" if path_prefix else base_name

        # content
        sym_path = _build_heading_path(path_prefix, name, capture_name)
        content_parts = [f"[{base_name} > {sym_path.split(' > ')[-1]}]"]
        if signature:
            content_parts.append(signature)
        if docstring:
            content_parts.append(docstring)
        content = "\n\n".join(content_parts)

        # symbols: 関数名＋クラス名を分割
        sym_text = _split_symbols(name)

        chunks.append(
            Chunk(
                content=content,
                heading_path=heading_path,
                start_line=start_line + 1,  # 1-based
                end_line=end_line + 1,
                symbols=sym_text,
            )
        )

    # チャンクが空（tree-sitter で何も見つからなかった）場合 → フォールバック
    if not chunks:
        return _split_code_fallback(content, file_path, max_chars)

    for i, c in enumerate(chunks):
        c.chunk_index = i
    return chunks


def _split_code_fallback(
    content: str,
    file_path: str,
    max_chars: int = _CODE_MAX_CHARS,
) -> list[Chunk]:
    """tree-sitter 非対応言語向けフォールバック分割。

    ファイル全体を ``_split_long_text`` で分割する。
    """
    base_name = os.path.basename(file_path)
    parts = _split_long_text(content, max_chars)
    chunks = []
    for i, part in enumerate(parts):
        chunks.append(
            Chunk(
                content=f"[{base_name}]\n{part}",
                heading_path=base_name,
                chunk_index=i,
                start_line=None,
                end_line=None,
                symbols=None,
            )
        )
    return chunks
