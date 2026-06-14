    """issue / PR のスレッド全体（本文＋コメント＋レビューコメント）を時系列で取得する。
    bot コメントも含まれる（is_bot で識別可能）。

    number: issue または PR 番号。
    repo: リポジトリ名（"owner/name"）。省略時は SHIORI_REPOS の最初のリポジトリ。
    exclude_noise_bots: True で allowlist 外の bot（CI / dependabot 等）を除外する（既定 False）。
        allowlist（SHIORI_INDEX_BOT_LOGINS）登録済み bot の投稿は残る（issue #44）。"""