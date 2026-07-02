"""Debug: why sync_docs and flush keys don't match"""
import ast, re, os, textwrap

JP = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]')

text = open('src/shiori/github_sync.py', encoding='utf-8').read()
tree = ast.parse(text)
lines = text.split('\n')

# Check sync_docs and flush
for name in ('sync_docs', 'flush'):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            doc = ast.get_docstring(node)
            first_line = doc.strip().split('\n')[0]
            truncated = first_line[:60]
            
            # Check raw source first line
            for stmt in node.body:
                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                    raw_lines = lines[stmt.lineno-1:stmt.end_lineno]
                    raw_text = '\n'.join(raw_lines)
                    break
            
            print(f'{name}:')
            print(f'  cleaned first line: {first_line!r}')
            print(f'  truncated (60): {truncated!r}')
            print(f'  truncated hex: {truncated.encode("utf-8").hex()}')
            
            # Now check what's in EN dict
            key = ('github_sync.py', truncated)
            
            # Reconstruct the add function logic
            registered_key1 = ('github_sync.py', 'リポジトリの Markdown を同期し、変化分だけ索引する。戻り値は更新ファイル数。'[:60])
            registered_key2 = ('github_sync.py', 'バッファをフラッシュ: 一括埋め込み → バルク挿入 → commit。戻り値は挿入件数。'[:60])
            
            print(f'  key == registered_key1: {key == registered_key1}')
            print(f'  key == registered_key2: {key == registered_key2}')
            
            if name == 'sync_docs':
                print(f'  EN has key: {key in {"github_sync.py": {"リポジトリの Markdown を同期し、変化分だけ索引する。戻り値は更新ファイル数。"[:60]}}}')
            
            # Check if there's a difference between the first line and what we registered
            expected_trunc = 'リポジトリの Markdown を同期し、変化分だけ索引する。戻り値は更新ファイル数。'[:60]
            actual_trunc = truncated
            print(f'  expected vs actual: {expected_trunc == actual_trunc}')
            if expected_trunc != actual_trunc:
                print(f'  expected hex: {expected_trunc.encode("utf-8").hex()}')
                print(f'  actual hex:   {actual_trunc.encode("utf-8").hex()}')
                # Find difference
                for i, (a, b) in enumerate(zip(expected_trunc, actual_trunc)):
                    if a != b:
                        print(f'  diff at pos {i}: expected U+{ord(a):04X} {a!r} != actual U+{ord(b):04X} {b!r}')
                        break
            
            break

# Also check _run_alter_statements
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef,)) and node.name == '_run_alter_statements':
        doc = ast.get_docstring(node)
        first_line = doc.strip().split('\n')[0]
        truncated = first_line[:60]
        print(f'\n_run_alter_statements:')
        print(f'  cleaned first line: {first_line!r}')
        print(f'  truncated (60): {truncated!r}')
        
        expected = '既存 DB に対する冪等な ALTER。CREATE TABLE IF NOT EXISTS では新カラムが追加されないため。'[:60]
        print(f'  eq expected: {truncated == expected}')
        if truncated != expected:
            print(f'  actual hex:   {truncated.encode("utf-8").hex()}')
            print(f'  expected hex: {expected.encode("utf-8").hex()}')
            for i, (a, b) in enumerate(zip(truncated, expected)):
                if a != b:
                    print(f'  diff at pos {i}: actual U+{ord(a):04X} {a!r} != expected U+{ord(b):04X} {b!r}')
                    break
