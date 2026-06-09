"""チャンク分割（詳細設計/02）。

決定事項:
- docs: 見出し単位で分割し、見出しパスをメタデータに保持。
  長い節は文字基準（既定 1200 字）で、文境界（。．.!? と改行）を優先して分割。
- issue/PR: コメント 1 件を自然な単位とし、`[タイトル]` を文脈プレフィックスとして付与。
- 言語はチャンク（実質ファイル/コメント）単位でヒューリスティック判定（ja/en）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# 文境界: 日本語句点・感嘆/疑問・英語ピリオド類の直後、または空行
_SENTENCE_END_RE = re.compile(r"(?<=[。．！？!?\.])\s*|\n{2,}")
_JA_CHAR_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")


@dataclass
class Chunk:
    content: str
    heading_path: str | None = None
    chunk_index: int = 0


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
