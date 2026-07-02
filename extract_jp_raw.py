"""Extract exact raw text of remaining Japanese docstrings."""
import ast, re, os

JP = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]')
TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

SKIP = {'config.py', 'embedding.py'}

for fn in sorted(os.listdir('src/shiori')):
    if not fn.endswith('.py') or fn in SKIP: continue
    fpath = os.path.join('src/shiori', fn)
    with open(fpath) as f:
        text = f.read()
    lines = text.split('\n')
    tree = ast.parse(text)
    nodes = [n for n in ast.walk(tree) if isinstance(n, TYPES)]
    for node in nodes:
        doc = ast.get_docstring(node)
        if not doc or not JP.search(doc): continue
        name = getattr(node, 'name', '<module>')
        # Find the raw source text of the docstring
        body = node.body if hasattr(node, 'body') else []
        for stmt in body:
            if not isinstance(stmt, ast.Expr): continue
            val = stmt.value
            if not isinstance(val, ast.Constant) or not isinstance(val.value, str): continue
            if doc in val.value or val.value.strip() == doc.strip():
                raw_lines = lines[stmt.lineno-1:stmt.end_lineno]
                raw = '\n'.join(raw_lines)
                print(f'### {fn}:{name} (lines {stmt.lineno}-{stmt.end_lineno})')
                # Output in a format easy to copy
                for i, l in enumerate(raw_lines, stmt.lineno):
                    print(f'{i}:{l}')
                print('---')
                break
