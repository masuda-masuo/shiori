# secrets/

GitHub App の秘密鍵 PEM を置くディレクトリ。`docker-compose.yml` の
`secrets.github_app_key` がここの `github-app.private-key.pem` を参照する。

- このディレクトリの中身は `.gitignore` で除外される（コミットされない）。
- GitHub App 認証を使う場合: ダウンロードした `.pem` を
  `secrets/github-app.private-key.pem` として配置する。
- App 認証を使わない場合（PAT / 匿名）: 空ファイルでよい。
  `touch secrets/github-app.private-key.pem`
