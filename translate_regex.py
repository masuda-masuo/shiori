"""Translate Japanese docstrings using regex matching (most reliable approach)."""
import re, os, textwrap

JP = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]')

# Match any triple-quoted string (""" or ''') with DOTALL for multiline
DOC_RE = re.compile(r'"""(?:\\.|[^\\])*?"""|\'\'\'(?:\\.|[^\\])*?\'\'\'', re.DOTALL)

HERE = os.path.dirname(__file__)

REPLACE = {}

def repl(fn, old_jp, new_en):
    """Register a replacement. old_jp is the EXACT raw text including quotes and indentation.
    new_en is WITHOUT quotes and WITHOUT outer indentation (but with internal indentation)."""
    REPLACE[(fn, old_jp)] = new_en

# ====== chunking.py ======
repl('chunking.py',
    '"""Chunk splitting (detailed design/02).\n\nDecisions:\n- docs: Split by heading, preserve heading paths in metadata.\n  Long sections use character-based splitting (default 1200 chars), prioritizing sentence boundaries（。．.!? and newlines）.\n- issue/PR: Each comment is a natural unit, with `[title]` prepended as context prefix.\n- Language detected heuristically per chunk (effectively per file/comment) (ja/en).\n- code: Split by function/method/class via tree-sitter (map type: signature + docstring).\n  Unsupported languages fall back to _split_long_text (detailed design/10).\n"""',
    'Chunk splitting (detailed design/02).\n\nDecisions:\n- docs: Split by heading, preserve heading paths in metadata.\n  Long sections use character-based splitting (default 1200 chars), prioritizing sentence boundaries.\n- issue/PR: Each comment is a natural unit, with `[title]` prepended as context prefix.\n- Language detected heuristically per chunk (effectively per file/comment) (ja/en).\n- code: Split by function/method/class via tree-sitter (map type: signature + docstring).\n  Unsupported languages fall back to _split_long_text (detailed design/10).\n')

# _find_breakpoint
repl('chunking.py',
    '    """max_chars を超えない最も近い意味的境界を探す。\n\n    この関数が呼ばれる時点で _SENTENCE_END_RE による文分割は完了している。\n    句点（。．！？！?.) は _SENTENCE_END_RE が既に分割に使っているため、\n    この関数が句点を見つけるのは以下のケース:\n      1. _SENTENCE_END_RE の句点以外の分割条件（\\\\n{2,}）で生じた文内に\n         句点が残っている場合\n      2. 過去の分割で句点が先のチャンクに含まれ、現在の s に句点がない場合\n           → 読点（、，,）で代用\n    実用的には **読点フォールバック**が主な役割。\n\n    優先順:\n    1. 句点・終端記号（。．！？！?.) : 文の終わり\n    2. 読点（、，,）: 節の境界\n    3. 見つからなければ max_chars でハードカット\n    """',
    'Find the closest semantic boundary not exceeding max_chars.\n\nAt the point this function is called, sentence splitting by _SENTENCE_END_RE\nhas already completed. Punctuation marks are already used by\n_SENTENCE_END_RE for splitting, so this function finds punctuation in the\nfollowing cases:\n  1. Within text split by non-punctuation conditions (\\\\n{2,}), where\n     punctuation remains inside the segment.\n  2. When punctuation was consumed by the previous chunk and the current s\n     has none → fall back to clause delimiters\n\nIn practice, **clause-delimiter fallback** is the main role.\n\nPriority order:\n1. Sentence-ending marks: end of sentence\n2. Clause delimiters: clause boundary\n3. Hard-cut at max_chars if none found\n')

# _split_symbols
repl('chunking.py',
    '    """tree-sitter でシンボル（関数／メソッド／クラス）単位に分割する。\n\n    Supported languages and their tree-sitter query files:\n      Python (.py), TypeScript (.ts, .tsx), Go (.go), Rust (.rs)\n    query ファイル名: queries/{language}-symbols.scm\n    （詳細設計/10) 参照。\n    """',
    'Split by symbols (function/method/class) via tree-sitter.\n\nSupported languages and their tree-sitter query files:\n  Python (.py), TypeScript (.ts, .tsx), Go (.go), Rust (.rs)\nQuery file name: queries/{language}-symbols.scm\n(detailed design/10).\n')

# split_code
repl('chunking.py',
    '    """コードチャンクを文書化可能な単位に変換する。\n\n    source コードの特定言語の名前付きブロックを map 型の chunk に変換する。\n    """',
    'Convert code chunks to documentable units.\n\nConverts named blocks from source code into map-type chunks.\n')

# ====== db.py ======
repl('db.py',
    '"""データベーススキーマとクエリ（詳細設計/02）。\n\n各テーブルの説明と主要なカラムについてはクエリコメント参照。\nここでは判断理由のみ記録する。\n\n決定事項:\n- チャンクは 2 テーブル構成（doc 用と issue 用）で、型を target_type で区別する。\n- pgvector のインデックス: HNSW（IVFFlat より精度が高く、build 時間が短い）\n- pgroonga のインデックス: 日本語トークナイズ（TokenNgram）＋英語トークナイズ（TokenBigram）\n- 全文検索のフォールバック: pg_trgm（pgroonga が利用できない環境用）\n"""',
    'Database schema and queries (detailed design/02).\n\nSee query comments for table and column descriptions.\nOnly design decisions are recorded here.\n\nDecisions:\n- Chunks use 2-table structure (doc and issue) distinguished by target_type.\n- pgvector index: HNSW (higher accuracy and faster build than IVFFlat)\n- pgroonga index: Japanese tokenization (TokenNgram) + English tokenization (TokenBigram)\n- Full-text search fallback: pg_trgm (for environments without pgroonga)\n')

repl('db.py',
    '    """既存 DB に対する冪等な ALTER。CREATE TABLE IF NOT EXISTS では新カラムが追加されないため。\n\n    migrate_light / migrate の両方から呼ばれる。\n    """',
    'Idempotent ALTER for existing DB. CREATE TABLE IF NOT EXISTS does not add new columns.\n\nCalled by both migrate_light and migrate.\n')

repl('db.py',
    '    """完全なスキーマ作成（テーブル＋全索引）。増分経路で使用（issue #72）。\n\n    バルク経路では migrate_light() + ロード後 create_heavy_indexes() を使う。\n    """',
    'Full schema creation (tables + all indexes). Used in incremental path (issue #72).\n\nBulk path uses migrate_light() + create_heavy_indexes() after loading.\n')

repl('db.py',
    '    """同期の成功をリポジトリ単位で記録し、完了時刻（DB の now()）を返す（issue #22 / #33）。\n\n    advisory lock で skip された実行は呼び出し側で記録しないこと（成功した同期のみ）。\n    時刻は DB の now() を使う: 複数経路・複数プロセスでも単一 DB の時計で一貫する。\n    """',
    "Record a successful sync per repository and return the completion timestamp (DB's now()) (issue #22 / #33).\n\nExecutions skipped by advisory lock are NOT recorded (only successful syncs).\nTimestamp uses DB now(): consistent across multiple paths and processes using a single DB clock.\n")

repl('db.py',
    '    """完全なスキーマ作成（テーブル＋全索引）。増分経路で使用（issue #72）。\n\n    バルク経路では migrate_light() + ロード後 create_heavy_indexes() を使う。\n    """',
    'Full schema creation (tables + all indexes). Used in incremental path (issue #72).\n\nBulk path uses migrate_light() + create_heavy_indexes() after loading.\n')

repl('db.py',
    '    """同期の成功をリポジトリ単位で記録し、完了時刻（DB の now()）を返す（issue #22 / #33）。\n\n    advisory lock で skip された実行は呼び出し側で記録しないこと（成功した同期のみ）。\n    時刻は DB の now() を使う: 複数経路・複数プロセスでも単一 DB の時計で一貫する。\n    """',
    "Record a successful sync per repository and return the completion timestamp (DB's now()) (issue #22 / #33).\n\nExecutions skipped by advisory lock are NOT recorded (only successful syncs).\nTimestamp uses DB now(): consistent across multiple paths and processes using a single DB clock.\n")

repl('db.py',
    '    """PR 変更ファイルマップを upsert する（issue #54）。同一 PR の既存行を削除してから insert。\n    """',
    'Upsert PR change file map (issue #54). Deletes existing entries for the same PR first, then inserts new ones.\n')

repl('db.py',
    '    """_create_pgroonga_index: pgroonga の全文検索インデックスを作成する。\n\n    pgroonga 拡張がない環境では pg_trgm がフォールバックとして使われる。\n    """',
    '_create_pgroonga_index: create pgroonga full-text search index.\n\nFalls back to pg_trgm when pgroonga extension is unavailable.\n')

repl('db.py',
    '    """直近の同期実行記録を返す（issue #22 / #33）。\n    """',
    'Return recent sync execution records (issue #22 / #33).\n')

repl('db.py',
    '    """リポジトリごとのチャンク数を集計する。\n    """',
    'Aggregate chunk counts per repository.\n')

repl('db.py',
    '    """issue_item の総数を返す（高速パス: COUNT は使わず reltuples を利用）。\n    """',
    'Return total issue_item count (fast path: uses reltuples instead of COUNT).\n')

repl('db.py',
    '    """PR head の SHA を取得する。\n\n    pr_changes テーブルに保存された head_sha を返す。\n    これは PR の force-push を検出するために使われる。\n    """',
    'Get PR head SHA.\n\nReturns head_sha from pr_changes table.\nUsed to detect force-pushes on PRs.\n')

print(f'Registered {len(REPLACE)} replacements')

# ====== Apply replacements ======
src = os.path.join(HERE, 'src', 'shiori')
for fn in sorted(os.listdir(src)):
    if not fn.endswith('.py') or fn in ('config.py', 'embedding.py'): continue
    fpath = os.path.join(src, fn)
    with open(fpath, encoding='utf-8') as f:
        text = f.read()
    original = text
    file_repls = [(old_jp, new_en) for (f, old_jp), new_en in REPLACE.items() if f == fn]
    if not file_repls:
        continue
    print(f'=== {fn}: {len(file_repls)} replacements ===')
    for old_jp, new_en in file_repls:
        if old_jp not in text:
            # Try to find similar text for debugging
            print(f'  NOT FOUND: {old_jp[:60]!r}...')
            continue
        q = old_jp[:3]
        indent = old_jp[:len(old_jp) - len(old_jp.lstrip())]
        # Build replacement with proper indentation
        en_lines = new_en.split('\n')
        result_lines = []
        for i, line in enumerate(en_lines):
            if i == 0:
                result_lines.append(indent + q + line)
            elif line.strip():
                result_lines.append(indent + line)
            else:
                result_lines.append('')
        # Add closing quotes
        # The original might have trailing spaces before closing q
        result_lines[-1] = result_lines[-1] + q
        replacement = '\n'.join(result_lines)
        text = text.replace(old_jp, replacement, 1)
        print(f'  replaced ({old_jp[:30]!r}...)')
    if text != original:
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f'{fn}: SAVED')
    else:
        print(f'{fn}: no changes')
