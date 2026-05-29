---
name: safe-edit
description: Safely edit existing text, source, and config files through one cross-platform Python script while preserving encoding, BOM, newline style, permissions, and atomic-write integrity. Use for literal replacements, explicit regex replacements, line insertions/deletions, line-range replacement, diff previews, and JSON batch edits when avoiding mojibake, truncated files, silent no-ops, or noisy Git diffs matters.
---

# safe-edit

Use `safe_edit.py` as the single implementation and single documented entry point. It is portable across Windows, Linux, and macOS with only the Python standard library.

## Entry Point

```bash
python safe_edit.py edit --file path/to/file --old "foo" --new "bar" --expected-count 1
python safe_edit.py regex --file path/to/file --pattern "foo\\d+" --replacement "bar" --expected-count 1
python safe_edit.py insert --file path/to/file --line 10 --text "new line"
python safe_edit.py delete --file path/to/file --line 10
python safe_edit.py replace-lines --file path/to/file --start 10 --end 20 --text-file block.txt
python safe_edit.py delete-lines --file path/to/file --start 10 --end 20
python safe_edit.py batch --file path/to/file --ops-file ops.json
```

On Windows, `py -3 safe_edit.py ...` is acceptable when `python` is not on `PATH`.

## Operating Rules

1. Use this skill for existing text files where encoding, BOM, line endings, permissions, or Git diff cleanliness matters.
2. Keep `edit` literal-only. Use `regex` only when the task explicitly needs a regex.
3. Treat any nonzero exit as "file unchanged"; the script writes a same-directory temp file, atomically replaces the target, and verifies bytes after writing.
4. Prefer `--expected-count N` for replacements so wrong matches fail loudly.
5. Use `--dry-run --diff` before risky edits.
6. Use `--backup` when the user wants a retained timestamped `.bak` copy.
7. For pure ASCII files in GBK/Shift-JIS/Big5 projects, pass `--encoding gbk`, `--encoding shift-jis`, or `--encoding big5` before inserting non-ASCII text. ASCII is otherwise detected as UTF-8.
8. Do not use this skill for binary files, huge generated files, symlinks, or non-text formats unless the user explicitly accepts those risks.

## Commands

- `edit --old TEXT --new TEXT`: replace literal text. Empty `--new ""` is allowed; empty `--old ""` is refused.
- `regex --pattern PATTERN --replacement TEXT`: replace with Python `re.sub` semantics. Flags: `i`, `m`, `s`, `x`, `a`. Use `--literal-replacement` when backreferences must not be interpreted.
- `insert --line N --text TEXT`: insert before 1-based line `N`.
- `delete --line N`: delete one 1-based line.
- `replace-lines --start N --end M --text TEXT`: replace an inclusive 1-based line range.
- `delete-lines --start N --end M`: delete an inclusive 1-based line range.
- `batch --ops-file ops.json`: run multiple operations in memory, then write once.

## Large Or Multiline Values

Use file or stdin variants to avoid shell quoting problems:

```bash
python safe_edit.py edit --file a.cpp --old-file old.txt --new-file new.txt
python safe_edit.py insert --file a.cpp --line 5 --text-file block.txt
python safe_edit.py regex --file a.cpp --pattern-file pattern.txt --replacement-file replacement.txt
```

Available value sources:

- `--old`, `--old-file`, `--old-stdin`
- `--new`, `--new-file`, `--new-stdin`
- `--pattern`, `--pattern-file`, `--pattern-stdin`
- `--replacement`, `--replacement-file`, `--replacement-stdin`
- `--text`, `--text-file`, `--text-stdin`

Argument files default to UTF-8; override with `--arg-encoding`.

## Batch JSON

Use batch when an edit needs multiple transformations but should read and write the target only once:

```json
[
  {"op": "edit", "old": "foo", "new": "bar", "expected_count": 1},
  {"op": "regex", "pattern": "version = \"[^\"]+\"", "replacement": "version = \"1.2.3\"", "expected_count": 1},
  {"op": "replace-lines", "start": 10, "end": 12, "text_file": "block.txt"},
  {"op": "delete-lines", "start": 30, "end": 35}
]
```

Batch accepts a JSON list or an object with `operations` / `ops`. Relative `*_file` paths are resolved from the ops file directory.

## Guarantees

- Detects and preserves `utf-8`, `utf-8-bom`, `gbk`, UTF-16 with BOM, UTF-16 without BOM when NUL patterns are clear, plus manual `shift-jis`, `big5`, `latin-1`, `utf-16-le`, and `utf-16-be`.
- Preserves CRLF, LF, or CR newline style for generated separators; unchanged line separators remain untouched where the edit does not cross them.
- Preserves ordinary file permissions where the platform allows it.
- Refuses unknown encodings, decoded NUL characters, symlinks, missing matches, invalid lines, oversized files, and concurrent safe-edit lock conflicts by default.
- Performs same-directory temp-file writes followed by atomic replacement and byte-for-byte post-write verification.

## Useful Options

- `--encoding auto|utf-8|utf-8-bom|gbk|shift-jis|big5|latin-1|utf-16-le|utf-16-be`: override target decoding.
- `--expected-count N`: require exactly `N` literal occurrences or regex matches.
- `--first`: replace only the first literal/regex match.
- `--count N`: regex replacement limit; `0` means all.
- `--no-op-ok`: allow replacement text or pattern not to be found.
- `--dry-run`: validate and transform in memory without writing.
- `--diff --context N`: emit unified diff preview.
- `--backup`: keep a timestamped backup before replacement.
- `--allow-nul`: allow decoded NUL characters.
- `--follow-symlink`: edit the symlink target instead of refusing.
- `--max-bytes N`: raise or lower the default 50 MiB limit.
- `--lock-timeout N`: wait for another safe-edit process; default is 10 seconds.
- `--no-lock`: skip the cooperative lock.
- `--json`: emit machine-readable status.
