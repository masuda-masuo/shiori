"""Debug: check EN keys for github_sync.py"""
code = open('translate_ast3.py').read()

# Get setup part before main loop
setup_code = code.split("print(f'Registered {len(EN)} translations')")[0]

# Remove docstring
if setup_code.startswith('"""'):
    end = setup_code.index('"""', 3) + 3
    setup_code = setup_code[end:]

ns = {}
exec(setup_code, ns)
EN = ns['EN']

for k in sorted(EN):
    if k[0] == 'github_sync.py':
        print(f'  {k[1]!r}')

print()
print('sync_docs key exists:', ('github_sync.py', 'リポジトリの Markdown を同期し、変化分だけ索引する。戻り値は更新ファイル数。') in EN)
print('_is_code_file key exists:', ('github_sync.py', 'コード索引対象のファイルか判定する。') in EN)
print('flush key exists:', ('github_sync.py', 'バッファをフラッシュ: 一括埋め込み → バルク挿入 → commit。戻り値は挿入件数。') in EN)
