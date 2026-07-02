"""Count remaining Japanese docstrings."""
import ast, os, re
JP = re.compile(r'[\u3000-\u9fff]')
TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
for fn in sorted(os.listdir('src/shiori')):
    if not fn.endswith('.py'): continue
    path = os.path.join('src/shiori', fn)
    with open(path) as f:
        text = f.read()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        continue
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, TYPES): continue
        doc = ast.get_docstring(node)
        if doc and JP.search(doc):
            count += 1
    print(f'{fn}: {count}' if count else '', end='')
