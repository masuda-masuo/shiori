"""Translate all src/shiori/*.py docstrings from Japanese to English."""
import os, subprocess, sys

# Read the script content from itself
# Pairs format: [(old_str, new_str), ...]
# old_str and new_str are read from files to avoid encoding issues

PAIRS_FILE = '/tmp/pairs_src.txt'

def run():
    if not os.path.exists(PAIRS_FILE):
        print("Pairs file not found")
        sys.exit(1)
    
    with open(PAIRS_FILE, encoding='utf-8') as f:
        content = f.read()
    
    blocks = content.split('\n===\n')
    
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines = block.split('\n')
        if len(lines) < 2:
            continue
        file_rel = lines[0].strip()
        count = (len(lines) - 1) // 2
        path = os.path.join('src', 'shiori', file_rel)
        with open(path, encoding='utf-8') as f:
            text = f.read()
        
        ok = 0
        for i in range(count):
            old_str = lines[1 + i*2].rstrip('\n')
            new_str = lines[2 + i*2].rstrip('\n')
            if old_str in text:
                text = text.replace(old_str, new_str, 1)
                ok += 1
            else:
                # Try to show context of mismatch
                idx = text.find(old_str[:20])
                if idx >= 0:
                    print(f"  Partial match at {idx}: {repr(text[idx:idx+60])}")
        
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f'{file_rel}: {ok} replaced')

if __name__ == '__main__':
    run()
