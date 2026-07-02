"""Count remaining Japanese docstrings, excluding CJK punctuation."""
import ast, re, os

# Hiragana, Katakana, Kanji only — NOT CJK symbols/punctuation
JP = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]')
TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

total = 0
for fn in sorted(os.listdir('src/shiori')):
    if not fn.endswith('.py'): continue
    with open(os.path.join('src/shiori', fn)) as f:
        text = f.read()
    tree = ast.parse(text)
    cnt = 0
    for node in ast.walk(tree):
        if not isinstance(node, TYPES): continue
        d = ast.get_docstring(node)
        if d and JP.search(d):
            cnt += 1
            name = getattr(node, 'name', '<module>')
            print(f'{fn}:{name}')
    if cnt:
        print(f'{fn}: {cnt}')
    total += cnt
print(f'TOTAL: {total}')
