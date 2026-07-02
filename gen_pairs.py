"""Generate exact replacement pairs for Japanese docstrings."""
import ast, os, re, json

JP = re.compile(r'[\u3000-\u9fff]')
TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

pairs = {}  # filepath -> [(exact_old, exact_new, lineno)]

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
        
        # Find the docstring in the source
        # The docstring is the first statement in the node body
        body = node.body if hasattr(node, 'body') else []
        for stmt in body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, (ast.Constant, ast.Str)):
                if hasattr(stmt.value, 'value'):
                    ds_value = stmt.value.value
                else:
                    ds_value = stmt.value.s
                if ds_value == doc:
                    # Found the exact node - extract the raw text
                    first_line = stmt.lineno
                    last_line = stmt.end_lineno
                    raw_lines = lines[first_line-1:last_line]
                    raw_text = '\n'.join(raw_lines)
                    jp_text = raw_text
                    
                    key = f'{fn}:{first_line}'
                    print(f'=== {key} ===')
                    print(raw_text)
                    print('---')
                    break
