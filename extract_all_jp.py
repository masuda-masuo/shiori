"""Extract ALL Japanese docstrings with exact source text."""
import ast, re, os

JP = re.compile(r'[\u3000-\u9fff]')
TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

src = 'src/shiori'
for fn in sorted(os.listdir(src)):
    if not fn.endswith('.py') or fn == '__init__.py': continue
    fpath = os.path.join(src, fn)
    with open(fpath) as f:
        text = f.read()
    lines = text.split('\n')
    try:
        tree = ast.parse(text)
    except SyntaxError:
        print(f'=== {fn}: SYNTAX ERROR ===')
        continue
    nodes = []
    for node in ast.walk(tree):
        if not isinstance(node, TYPES): continue
        doc = ast.get_docstring(node)
        if not doc or not JP.search(doc): continue
        nodes.append(node)
    if not nodes:
        continue
    print(f'=== {fn}: {len(nodes)} nodes ===')
    for node in nodes:
        name = getattr(node, 'name', '<module>')
        doc_raw = ast.get_docstring(node, clean=False)
        body = node.body if hasattr(node, 'body') else []
        found = False
        for stmt in body:
            if not isinstance(stmt, ast.Expr): continue
            val = stmt.value
            if not isinstance(val, ast.Constant) or not isinstance(val.value, str): continue
            if val.value == doc_raw or doc_raw in val.value or val.value.strip() == doc_raw.strip():
                raw = '\n'.join(lines[stmt.lineno-1:stmt.end_lineno])
                print(f'\n  ## {name} (lines {stmt.lineno}-{stmt.end_lineno})')
                print(raw)
                found = True
                break
        if not found:
            print(f'\n  ## {name} (could not locate raw text)')
