"""Check exact docstring first lines for problematic functions"""
import ast

with open('src/shiori/github_sync.py', 'rb') as f:
    text = f.read()

tree = ast.parse(text)

for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ('sync_docs', 'flush', '_is_code_file'):
        doc = ast.get_docstring(node)
        fl = doc.strip().split('\n')[0][:60]
        print(f'{node.name}:')
        print(f'  first line: {fl!r}')
        print(f'  bytes: {fl.encode("utf-8")!r}')
        
        # Check raw text
        lines = text.decode('utf-8').split('\n')
        for stmt in node.body:
            if isinstance(stmt, ast.Expr) and hasattr(stmt.value, 'value') and isinstance(stmt.value.value, str):
                raw_lines = lines[stmt.lineno-1:stmt.end_lineno]
                raw_text = '\n'.join(raw_lines)
                first_raw = raw_lines[0].strip()
                if first_raw.startswith(('"""', "'''")):
                    first_raw = first_raw[3:]
                print(f'  raw first: {first_raw!r}')
                print(f'  raw bytes: {first_raw.encode("utf-8")!r}')
                break
