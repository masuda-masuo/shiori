"""Generate exact replacement pairs for Japanese docstrings - handles all quote styles."""
import ast, os, re

JP = re.compile(r'[\u3000-\u9fff]')
TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

for fn in sorted(os.listdir('src/shiori')):
    if not fn.endswith('.py'):
        continue
    path = os.path.join('src/shiori', fn)
    with open(path) as f:
        text = f.read()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        continue
    lines = text.split('\n')
    
    for node in ast.walk(tree):
        if not isinstance(node, TYPES):
            continue
        doc = ast.get_docstring(node)
        if not doc or not JP.search(doc):
            continue
        
        # Find the docstring in source by searching for the doc content
        # at the expected position
        body = node.body if hasattr(node, 'body') else []
        for stmt in body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                if isinstance(stmt.value.value, str) and stmt.value.value == doc:
                    first = stmt.lineno
                    last = stmt.end_lineno
                    raw = '\n'.join(lines[first-1:last])
                    print(f'=== {fn}:{first} ===')
                    print(raw)
                    print('---END---')
                    break
