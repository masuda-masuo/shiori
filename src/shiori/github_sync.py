    return False


# 制御文字除去用の正規表現: 改行(\n=0x0A)とタブ(\t=0x09)以外の制御文字(0x00-0x1F)にマッチ
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0B-\x1F]")


def _clean_text(s: str | None) -> str:
    """GitHub API から取得したテキストの制御文字を正規化する（issue #73）。

    PostgreSQL の text 型は NUL (0x00) を格納できず、埋め込みモデルの
    入力としても制御文字はノイズとなるため、改行・タブ以外の制御文字を除去する。
    """
    if not s:
        return ""
    return _CONTROL_CHARS_RE.sub("", s)


# ---------------------------------------------------------------------------