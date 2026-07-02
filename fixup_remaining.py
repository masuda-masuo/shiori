"""Fix remaining docstrings in test_pr_diff.py and test_search.py."""
import os

def replace_all(path, pairs):
    with open(path, encoding='utf-8') as f:
        text = f.read()
    ok = 0
    for old, new in pairs:
        if old in text:
            text = text.replace(old, new, 1)
            ok += 1
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return ok

# test_pr_diff.py - 2 remaining
pairs = [
    ('"""include_diff=True \u3067\u30af\u30ed\u30fc\u30f3\u304c\u7121\u3051\u308c\u3070 FileNotFoundError\u3002"""',
     '"""include_diff=True raises FileNotFoundError when clone is missing."""'),
    ('"""include_diff=True \u3067\u4f8b\u5916\u767a\u751f\u6642\u3082 tmp_ref \u3092\u78ba\u5b9f\u306b\u524a\u9664\u3059\u308b\u3002"""',
     '"""include_diff=True always cleans up tmp_ref even on exception."""'),
]
ok = replace_all(os.path.join('tests', 'test_pr_diff.py'), pairs)
print(f'test_pr_diff.py: {ok} fixed')

# test_search.py - 7 remaining
pairs = [
    ('"""source-aware \u306a pool \u6bb5\u8907\u5408\u30e9\u30f3\u30ad\u30f3\u30b0\u306e\u632f\u308b\u821e\u3044\u3002"""',
     '"""Behavior of source-aware pool-stage composite ranking."""'),
    ('"""\u4e00\u6b21\u30bd\u30fc\u30b9\u3067\u306f sort_by=updated_at \u3067\u3082\u30b9\u30b3\u30a2\u9806\u304c\u7dad\u6301\u3055\u308c\u308b\u3002"""',
     '"""Primary sources maintain score order even with sort_by=updated_at."""'),
    ('"""sort_by=created_at \u306f updated_at \u3068\u540c\u3058 tie-break \u3092\u751f\u6210\u3059\u308b\u3002"""',
     '"""sort_by=created_at produces the same tie-break as updated_at."""'),
    ('"""\u540c\u30b9\u30b3\u30a2\u3067\u306f\u4e00\u6b21\u30bd\u30fc\u30b9\u304c sentinel \u306b\u3088\u308a\u4e8c\u6b21\u3088\u308a\u524d\u306b\u6765\u308b\u3002"""',
     '"""At equal scores, primary source comes before secondary via sentinel."""'),
    ('"""sort_order=asc \u6642\u306f\u8907\u5408\u30ad\u30fc\u5168\u4f53\u304c\u53cd\u8ee2\u3057\u3001closed\u2192open\u30fb\u53e4\u3044\u2192\u65b0\u3057\u3044 \u306e\u9806\u306b\u306a\u308b\u3002"""',
     '"""sort_order=asc inverts the composite key so closed\u2192open, old\u2192new."""'),
    ('"""rows_by_id \u306b\u5b58\u5728\u3057\u306a\u3044 ID \u306f\u6700\u4e0b\u4f4d\u306b\u6c88\u3080\uff08\u9632\u5fa1\u7684\u30d5\u30a9\u30fc\u30eb\u30d0\u30c3\u30af\uff09\u3002"""',
     '"""IDs not in rows_by_id sink to the bottom (defensive fallback)."""'),
    ('"""sort_by \u306b\u95a2\u308f\u3089\u305a method \u306f\u5e38\u306b "rrf"\u3002"""',
     '"""method is always "rrf" regardless of sort_by."""'),
]
ok = replace_all(os.path.join('tests', 'test_search.py'), pairs)
print(f'test_search.py: {ok} fixed')
