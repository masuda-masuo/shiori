"""Check exact module docstring of github_sync.py."""
with open('src/shiori/github_sync.py') as f:
    text = f.read()
idx = text.find("'''")
if idx >= 0:
    end = text.find("'''", idx + 3)
    print(f'start: {text[:idx].count(chr(10))+1}, end: {text[:end+3].count(chr(10))+1}')
    doc = text[idx:end+3]
    print(repr(doc))
else:
    print('not found')
