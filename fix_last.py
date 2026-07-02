"""Fix the last remaining JP docstring in config.py module."""
import ast, re, os

JP = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]')

fn = 'config.py'
fpath = 'src/shiori/config.py'
with open(fpath, encoding='utf-8') as f:
    text = f.read()
original = text
lines = text.split('\n')

tree = ast.parse(text)

for node in ast.walk(tree):
    if not isinstance(node, ast.Module):
        continue
    doc = ast.get_docstring(node)
    if not doc or not JP.search(doc):
        continue

    # Find raw text
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

    print(f'raw: {raw_text!r}')

    new_body = 'shiori configuration.\n\nEverything reads from environment variables. Passed via docker compose `.env`.'

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

    print(f'replacement: {replacement!r}')

    text = text.replace(raw_text, replacement, 1)

with open(fpath, 'w', encoding='utf-8') as f:
    f.write(text)
print('SAVED config.py')
