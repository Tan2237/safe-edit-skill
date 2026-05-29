---
name: safe-edit
description: Safely inspect, edit, and explicitly convert existing text, source, and config files through one cross-platform Python script while preserving encoding, BOM, newline style, permissions, and atomic-write integrity by default. Use for encoding/line-ending inspection, literal replacements, explicit regex replacements, line prepends/appends/insertions/deletions, line-range replacement, explicit encoding/newline/final-newline normalization, diff previews, and JSON batch edits when avoiding mojibake, truncated files, silent no-ops, unnecessary writes, or noisy Git diffs matters.
---

# safe-edit

Use `safe_edit.py` as the single implementation and single documented entry point. It is portable across Windows, Linux, and macOS with only the Python standard library.

## Entry Point

```bash
python safe_edit.py edit --file path/to/file --old "foo" --new "bar" --expected-count 1
python safe_edit.py inspect --file path/to/file --json
python safe_edit.py convert --file path/to/file --to-encoding utf-8-bom --to-line-ending crlf --final-newline ensure
python safe_edit.py regex --file path/to/file --pattern "foo\\d+" --replacement "bar" --expected-count 1
python safe_edit.py insert --file path/to/file --line 10 --text "new line"
python safe_edit.py prepend --file path/to/file --text-file header.txt
python safe_edit.py append --file path/to/file --text-file footer.txt
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
5. Use `convert` or post-transform flags only when the task explicitly asks for format normalization.
6. Use `--dry-run --diff` before risky edits.
7. Run `inspect --json` before uncertain edits to verify encoding, line endings, and binary risk.
8. Use `--backup` when the user wants a retained timestamped `.bak` copy.
9. For pure ASCII files in GBK/Shift-JIS/Big5 projects, pass `--encoding gbk`, `--encoding shift-jis`, or `--encoding big5` before inserting non-ASCII text. ASCII is otherwise detected as UTF-8.
10. Do not use this skill for binary files, huge generated files, symlinks, or non-text formats unless the user explicitly accepts those risks.

## Commands

- `inspect`: report encoding, BOM, line ending counts, file size, line count, NUL presence, and permission bits without writing.
- `convert`: explicitly convert encoding, line endings, final newline, or trailing whitespace without textual replacement.
- `edit --old TEXT --new TEXT`: replace literal text. Empty `--new ""` is allowed; empty `--old ""` is refused.
- `regex --pattern PATTERN --replacement TEXT`: replace with Python `re.sub` semantics. Flags: `i`, `m`, `s`, `x`, `a`. Use `--literal-replacement` when backreferences must not be interpreted.
- `insert --line N --text TEXT`: insert before 1-based line `N`.
- `prepend --text TEXT`: add text at the beginning of the file.
- `append --text TEXT`: add text at the end of the file.
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
  {"op": "prepend", "text_file": "header.txt"},
  {"op": "replace-lines", "start": 10, "end": 12, "text_file": "block.txt"},
  {"op": "append", "text": "done"},
  {"op": "delete-lines", "start": 30, "end": 35}
]
```

Batch accepts a JSON list or an object with `operations` / `ops`. Relative `*_file` paths are resolved from the ops file directory.

## Explicit Normalization

Default edits preserve the original format. Use these only when the requested task includes normalization:

```bash
python safe_edit.py convert --file a.cpp --to-encoding utf-8-bom
python safe_edit.py convert --file a.cpp --to-line-ending crlf
python safe_edit.py convert --file a.cpp --final-newline ensure
python safe_edit.py convert --file a.cpp --trim-trailing-whitespace
```

The same post-transform flags can be combined with `edit`, `regex`, `batch`, and other mutating commands when one read/write cycle is preferred.

## Guarantees

- Detects and preserves `utf-8`, `utf-8-bom`, `gbk`, UTF-16 with BOM, UTF-16 without BOM when NUL patterns are clear, plus manual `shift-jis`, `big5`, `latin-1`, `utf-16-le`, and `utf-16-be`.
- Preserves CRLF, LF, or CR newline style for generated separators; unchanged line separators remain untouched where the edit does not cross them.
- Preserves ordinary file permissions where the platform allows it.
- Refuses unknown encodings, decoded NUL characters, symlinks, missing matches, invalid lines, oversized files, and concurrent safe-edit lock conflicts by default.
- Performs same-directory temp-file writes followed by atomic replacement and byte-for-byte post-write verification.
- Skips the write when transformed bytes are identical to the original, unless `--force-write` is set.

## Useful Options

- `--encoding auto|utf-8|utf-8-bom|gbk|shift-jis|big5|latin-1|utf-16-le|utf-16-be`: override target decoding.
- `--to-encoding preserve|utf-8|utf-8-bom|gbk|shift-jis|big5|latin-1|utf-16-le|utf-16-be`: override output encoding.
- `--to-line-ending preserve|lf|crlf|cr`: normalize output line endings.
- `--final-newline preserve|ensure|strip`: control final newline.
- `--trim-trailing-whitespace`: strip spaces and tabs before line endings.
- `--expected-count N`: require exactly `N` literal occurrences or regex matches.
- `--first`: replace only the first literal/regex match.
- `--count N`: regex replacement limit; `0` means all.
- `--no-op-ok`: allow replacement text or pattern not to be found.
- `--explain-match-failure`: show detailed diagnostics when a match fails (see below).
- `--dry-run`: validate and transform in memory without writing.
- `--force-write`: write even when output bytes are identical to the original.
- `--diff --context N`: emit unified diff preview.
- `--backup`: keep a timestamped backup before replacement.
- `--backup-dir DIR`: place backups in a separate directory.
- `--backup-suffix SUFFIX`: customize backup suffix; `{timestamp}` is supported.
- `--allow-nul`: allow decoded NUL characters.
- `--follow-symlink`: edit the symlink target instead of refusing.
- `--max-bytes N`: raise or lower the default 50 MiB limit.
- `--lock-timeout N`: wait for another safe-edit process; default is 10 seconds.
- `--lock-stale-seconds N`: remove a safe-edit lock older than `N` seconds.
- `--no-lock`: skip the cooperative lock.
- `--json`: emit machine-readable status.

## Match Failure Diagnostics

When `--old` text is not found, use `--explain-match-failure` to get detailed diagnostics:

```bash
python safe_edit.py edit --file code.cpp --old "    return 42" --new "    return 43" --explain-match-failure
```

Output example when the file uses tabs instead of spaces:

```
safe-edit: old text was not found; refusing a silent no-op

Match failed. Closest match found:
  at line 2:

EXPECTED:
  [SP][SP][SP][SP]return[SP]42

ACTUAL:
  [TAB]return[SP]42

Differences:
  - line 1: indentation uses tabs instead of spaces
```

The diagnostic shows:
- Line number of the closest match
- Expected pattern with whitespace visualized (`[SP]` = space, `[TAB]` = tab, `[CR]` = carriage return, `[LF]` = line feed)
- Actual content at that location
- Specific differences detected (indentation type, line endings, etc.)

## Anchor-Based Line Positioning

For `replace-lines` and `delete-lines`, use anchor patterns to position relative to a context line instead of absolute line numbers:

```bash
# Replace lines relative to an anchor pattern
python safe_edit.py replace-lines --file code.cpp \
    --anchor-pattern "AcGePoint3d ptCenter" \
    --offset-start "+2" \
    --offset-end "+3" \
    --text "new_line"

# Delete lines relative to an anchor
python safe_edit.py delete-lines --file code.cpp \
    --anchor-pattern "AcGePoint3d ptCenter" \
    --offset-start "+2" \
    --offset-end "+3"
```

### Anchor Options

- `--anchor-pattern "PATTERN"`: Literal text to search for as the anchor point.
- `--offset-start +N/-N`: Start line offset from anchor. `+2` means 2 lines after anchor; `-1` means 1 line before.
- `--offset-end +N/-N`: End line offset from anchor (inclusive).
- `--anchor-occurrence N`: Use the Nth occurrence when the pattern appears multiple times (1-based).

### Example

Given a file:
```
header
AcGePoint3d ptCenter
line1
line2
line3
footer
```

The command `--anchor-pattern "AcGePoint3d ptCenter" --offset-start "+2" --offset-end "+3"` targets lines 4-5 (line2 and line3), because:
- Anchor "AcGePoint3d ptCenter" is at line 2
- `+2` offset = line 4
- `+3` offset = line 5

### Disambiguation

When the anchor pattern appears multiple times, use `--anchor-occurrence`:

```bash
# Use the second occurrence of the pattern
python safe_edit.py replace-lines --file code.cpp \
    --anchor-pattern "pattern" \
    --anchor-occurrence 2 \
    --offset-start "+1" \
    --offset-end "+1" \
    --text "new"
```

Without `--anchor-occurrence`, the command fails with a message showing all match locations.
