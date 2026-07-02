"""Translate remaining JP docstrings in config.py and embedding.py."""
import ast, re, os, textwrap

JP = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]')
TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

EN = {}
def add(fn, fl, body):
    EN[(fn, fl[:60])] = textwrap.dedent(body)

# ── config.py ──
add('config.py', 'SHIORI_INDEX_BOT_LOGINS を小文字の set で返す。',
    'Return SHIORI_INDEX_BOT_LOGINS as a lowercase set.')
add('config.py', 'SHIORI_CODE_EXTENSIONS を小文字の set で返す。',
    'Return SHIORI_CODE_EXTENSIONS as a lowercase set.')
add('config.py', 'SHIORI_CODE_EXCLUDE_GLOBS をリストで返す。',
    'Return SHIORI_CODE_EXCLUDE_GLOBS as a list.')
add('config.py', 'SHIORI_ALLOW_REBUILD を bool で返す。',
    'Return SHIORI_ALLOW_REBUILD as a bool.')
add('config.py', 'GitHub App の秘密鍵 PEM を返す。PATH 優先、無ければ本文、どちらも無ければ None。',
    'Return the GitHub App private key PEM. PATH preferred, then inline text, else None.')

# ── embedding.py ──
add('embedding.py', '埋め込み（詳細設計/03）。',
    'Embedding (detailed design/03).\n\nSingle provider architecture: Settings.embedding_provider controls everything.\nProvider config: model and dimensions.\nDimension detection: auto or override; dimension mismatch raises RuntimeError.\nMultiprocessing: uses ProcessPoolExecutor for CPU embedding.\n\nAPI: embed(texts: list[str]) -> np.ndarray (batch), embed_one(text: str) -> np.ndarray, embed_many(texts: list[str]) -> list[np.ndarray].')

print(f'Registered {len(EN)} translations')

src_dir = os.path.join(os.path.dirname(__file__), 'src', 'shiori')
include = {'config.py', 'embedding.py'}
total_replaced = 0

for fn in sorted(include):
    fpath = os.path.join(src_dir, fn)
    with open(fpath, encoding='utf-8') as f:
        text = f.read()
    original = text
    lines = text.split('\n')

    tree = ast.parse(text)
    file_replaced = 0

    for node in ast.walk(tree):
        if not isinstance(node, TYPES):
            continue
        name = getattr(node, 'name', '<module>')
        doc = ast.get_docstring(node)
        if not doc or not JP.search(doc):
            continue

        fl = doc.strip().split('\n')[0][:60]
        key = (fn, fl)
        if key not in EN:
            print(f'  {fn}:{name} MISSING {fl!r}')
            continue

        new_body = EN[key]

        raw_text = None
        for stmt in node.body:
            if not isinstance(stmt, ast.Expr):
                continue
            val = stmt.value
            if not isinstance(val, ast.Constant) or not isinstance(val.value, str):
                continue
            raw_lines = lines[stmt.lineno-1:stmt.end_lineno]
            raw_text = '\n'.join(raw_lines)
            break

        if raw_text is None:
            print(f'  {fn}:{name} could not locate')
            continue

        m = re.match(r'^(\s*)("""|\'\'\')', raw_text)
        if m:
            indent, q = m.group(1), m.group(2)
        else:
            indent, q = '', '"""'

        en_lines = new_body.split('\n')
        result_lines = []
        for i, line in enumerate(en_lines):
            if i == 0:
                result_lines.append(indent + q + line)
            elif line.strip():
                result_lines.append(indent + line)
            else:
                result_lines.append('')
        result_lines[-1] += q
        replacement = '\n'.join(result_lines)

        if replacement == raw_text:
            continue
        if raw_text not in text:
            print(f'  {fn}:{name} raw text vanished!')
            continue

        text = text.replace(raw_text, replacement, 1)
        file_replaced += 1
        print(f'  {fn}:{name} replaced')

    if text != original:
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f'{fn}: SAVED ({file_replaced})')
        total_replaced += file_replaced

print(f'Done: {total_replaced} total')
