"""Debug AST structure for github_sync.py."""
import ast, re

JP = re.compile(r'[\u3000-\u9fff]')

with open('src/shiori/github_sync.py') as f:
    text = f.read()
tree = ast.parse(text)
doc = ast.get_docstring(tree)
print(f'Module doc len: {len(doc)}')
print(f'Has JP: {bool(JP.search(doc))}')
body = tree.body
first = body[0]
print(f'First stmt: {type(first).__name__}')
print(f'First stmt lineno: {first.lineno}')
if isinstance(first, ast.Expr):
    val = first.value
    print(f'Value type: {type(val).__name__}')
    if isinstance(val, ast.Constant):
        print(f'Value match: {val.value == doc}')
        
# Also check all nodes that count_jp finds
TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
cnt = 0
for node in ast.walk(tree):
    if not isinstance(node, TYPES): continue
    d = ast.get_docstring(node)
    if d and JP.search(d):
        cnt += 1
        name = getattr(node, 'name', '<module>')
        print(f'  Node: {type(node).__name__} name={name}')
print(f'Total: {cnt}')
