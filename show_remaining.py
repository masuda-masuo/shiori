"""Show remaining Japanese docstrings with exact raw text."""
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
    lines = text.split('\n')
    for node in ast.walk(tree):
        if not isinstance(node, TYPES): continue
        doc = ast.get_docstring(node)
        if not doc or not JP.search(doc): continue
        name = getattr(node, 'name', '<module>')
        body = node.body if hasattr(node, 'body') else []
        for stmt in body:
            if not isinstance(stmt, ast.Expr): continue
            val = stmt.value
            if isinstance(val, ast.Constant) and isinstance(val.value, str) and val.value == doc:
                first, last = stmt.lineno, stmt.end_lineno
                raw = '\n'.join(lines[first-1:last])
                print(f'### {fn}:{first} ({name})')
                # Use a delimiter for copying
                print(raw)
                print('---')
                break
