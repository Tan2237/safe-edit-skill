---
name: safe-edit
description: Agent-friendly safe text editing primitive. Inspect, edit, and convert existing text files while preserving encoding, BOM, line endings, permissions, and write integrity. Use for any file edit where mojibake, truncated files, silent no-ops, or noisy Git diffs matter.
---

# safe-edit — Agent Edit Protocol

Use `safe_edit.py` as the single entry point. Pure Python standard library, portable across Windows/Linux/macOS.

## Quick Reference (90% of edits)

```bash
# 1. Check file before editing
python safe_edit.py stat --file path/to/file

# 2. Simple text replacement (most common)
python safe_edit.py edit --file path/to/file --old "old text" --new "new text" --expected-count 1

# 3. Replace with auto-match (tolerate whitespace differences)
python safe_edit.py edit --file path/to/file --old "old text" --new "new text" --auto-match --expected-count 1

# 4. Diff-input format (multi-block edit in one shot)
python safe_edit.py edit --file path/to/file --diff-input "------- SEARCH
old text
=======
new text
+++++++ REPLACE"

# 5. Insert / append / delete lines
python safe_edit.py insert --file path/to/file --line 10 --text "new line"
python safe_edit.py append --file path/to/file --text "added at end"
python safe_edit.py delete --file path/to/file --line 10

# 6. Preview before applying
python safe_edit.py edit --file path/to/file --old "old" --new "new" --dry-run --diff
```

On Windows, `py -3` works when `python` is not on PATH.

## Edit Strategy Decision Tree

```
Need to edit a file?
│
├─ Is it a text file? ─── No → STOP, use another tool
│
├─ ⚠️ Shell parses args first. Need escaping for special chars or newlines?
│
├─ Do you know the encoding? ─── No → run `stat --file F --json`
│
├─ What kind of edit?
│   │
│   ├─ Replace exact text → `edit --old X --new Y --expected-count 1`
│   │   └─ Failed? → `edit --old X --new Y --auto-match --expected-count 1`
│   │       └─ Still failed? → `edit --old X --new Y --auto-match --fuzzy --expected-count 1`
│   │
│   ├─ Replace with regex → `regex --pattern P --replacement R --expected-count 1`
│   │
│   ├─ Multiple replacements → `edit --diff-input "SEARCH/REPLACE blocks"`
│   │   or → `batch --ops-file ops.json`
│   │
│   ├─ Insert/append text → `insert --line N` / `append` / `prepend`
│   │
│   ├─ Delete lines → `delete --line N` / `delete-lines --start S --end E`
│   │
│   └─ Replace line range → `replace-lines --start S --end E --text T`
│       or with anchor → `replace-lines --anchor-pattern A --offset-start +N --offset-end +M --text T`
│
├─ Ambiguous match (text appears multiple times)?
│   └─ Add `--context-before "unique text before"` or `--context-after "unique text after"`
│
└─ Need format conversion? → `convert --to-encoding X --to-line-ending Y --final-newline Z`
```

## Match Failure Handling Protocol

When `edit` fails with "old text was not found":

1. **Read the error** — if `--json`, check `error.type` and `nearbyContent`
2. **Diagnose** — common causes:
   - CRLF vs LF → add `--ignore-eol`
   - Tab vs space indentation → add `--ignore-indent`
   - Extra whitespace → add `--normalize-whitespace`
   - Text slightly different → add `--auto-match`
   - Completely wrong text → check `nearbyContent.similarity` and fix `--old`
3. **Retry with relaxation**:
   - First: `--auto-match` (tries exact → ignore-eol → ignore-indent → normalize-whitespace)
   - Last resort: `--auto-match --fuzzy` (similarity ≥ 0.6)
4. **Always verify** — check `matchStrategy` in output to know which level matched

### Structured Error JSON (--json mode)

```json
{
  "ok": false,
  "error": {"type": "match_not_found", "message": "..."},
  "suggestions": [
    {"action": "retry_with_ignore_eol", "description": "..."},
    {"action": "retry_with_auto_match", "description": "..."}
  ],
  "nearbyContent": {"line": 42, "content": "...", "similarity": 0.85}
}
```

Error types: `match_not_found`, `match_ambiguous`, `match_count_mismatch`, `encoding_error`, `file_error`, `lock_error`, `validation_error`, `format_error`, `unknown`.

## Multi-Block Edit Protocol

### When to use each method

| Method | Best for | Token cost |
|--------|----------|------------|
| `--diff-input` | 2-5 edits in one file | Low — single command |
| `--diff-input-file` | Large multi-block diffs | Very low — file-based |
| `batch --ops-file` | Complex mixed operations | Low — JSON file |
| Sequential `edit` | Edits across multiple files | Higher — multiple commands |

### diff-input format

```
------- SEARCH
old text line 1
old text line 2
=======
new text line 1
new text line 2
+++++++ REPLACE
```

- Markers: `-{3,} SEARCH` / `={3,}` / `+{3,} REPLACE` (flexible length, case-insensitive)
- Also supports: `<<< SEARCH` / `===` / `>>> REPLACE`
- Multiple blocks allowed in one input
- Each block becomes a separate `edit` operation applied sequentially

## Operating Rules

1. Use this skill for **existing text files** where encoding/line-ending integrity matters.
2. Keep `edit` literal-only. Use `regex` only when the task explicitly needs regex.
3. Nonzero exit = "file unchanged". The script writes atomically and verifies bytes.
4. Prefer `--expected-count N` for replacements so wrong matches fail loudly.
5. Use `--dry-run --diff` before risky edits.
6. Run `stat --file F` before uncertain edits to verify encoding and line count.
7. Use `--backup` when the user wants a retained `.bak` copy.
8. For GBK/Shift-JIS/Big5 projects, pass `--encoding gbk` etc. before inserting non-ASCII text.
9. Do not use for binary files, huge generated files, or non-text formats.
10. Use `--auto-match` instead of guessing `--ignore-eol`/`--ignore-indent` manually.

## Commands

- `stat --file F`: concise metadata (encoding, line ending, size, line count). Use `--json` for machine-readable.
- `inspect --file F`: detailed metadata including BOM, mixed line endings, NUL, permissions.
- `convert --file F`: transform encoding/line endings/final newline without text replacement.
- `edit --old X --new Y`: literal text replacement. `--new ""` allowed; `--old ""` refused.
- `regex --pattern P --replacement R`: Python `re.sub` replacement. Flags: `i`, `m`, `s`, `x`, `a`. Use `--literal-replacement` when backrefs must not be interpreted.
- `insert --line N --text T`: insert before 1-based line N.
- `prepend --text T`: add text at file beginning.
- `append --text T`: add text at file end.
- `delete --line N`: delete one 1-based line.
- `replace-lines --start N --end M --text T`: replace inclusive 1-based line range.
- `delete-lines --start N --end M`: delete inclusive 1-based line range.
- `batch --ops-file ops.json`: run multiple operations in memory, write once.

## Match Options

| Option | Effect | When to use |
|--------|--------|-------------|
| `--auto-match` | Auto-try: exact → ignore-eol → ignore-indent → normalize-whitespace | Match fails and you're unsure why |
| `--fuzzy` | Enable fuzzy matching (≥ 0.6 similarity), requires `--auto-match` | Text is close but not exact |
| `--ignore-indent` | Ignore leading whitespace differences | Tab vs space indentation |
| `--ignore-eol` | Ignore CRLF vs LF differences | Cross-platform files |
| `--normalize-whitespace` | Collapse consecutive whitespace | Variable whitespace quantity |
| `--context-before T` | Text must appear before the match | Disambiguate multiple matches |
| `--context-after T` | Text must appear after the match | Disambiguate multiple matches |
| `--explain-match-failure` | Show detailed match diagnostics | Debug failed matches |

Key principle: match options affect matching only, not replacement text.

## Large or Multiline Values

Use file or stdin variants to avoid shell quoting:

```bash
python safe_edit.py edit --file a.cpp --old-file old.txt --new-file new.txt
python safe_edit.py edit --file a.cpp --diff-input-file diff.txt
python safe_edit.py insert --file a.cpp --line 5 --text-file block.txt
```

Available: `--old`/`--old-file`/`--old-stdin`, `--new`/`--new-file`/`--new-stdin`, `--pattern`/`--pattern-file`/`--pattern-stdin`, `--replacement`/`--replacement-file`/`--replacement-stdin`, `--text`/`--text-file`/`--text-stdin`, `--diff-input`/`--diff-input-file`.

Argument files default to UTF-8; override with `--arg-encoding`.

## Anchor-Based Positioning

For `replace-lines` and `delete-lines`, use anchors instead of absolute line numbers:

```bash
python safe_edit.py replace-lines --file a.cpp \
  --anchor-pattern "AcGePoint3d ptCenter" \
  --offset-start +2 --offset-end +3 --text "new line"
```

- `--anchor-pattern`: literal text to search for as anchor
- `--offset-start/end +N/-N`: offset from anchor line (inclusive)
- `--anchor-occurrence N`: disambiguate when pattern appears multiple times

## Interactive Mode

```bash
python safe_edit.py edit --file a.cpp --old "foo" --new "bar" -i
```

Prompts: `y` (yes), `n` (no), `a` (all remaining), `q` (quit), `?` (help). Requires TTY.

## Batch JSON

```json
[
  {"op": "edit", "old": "foo", "new": "bar", "expected_count": 1},
  {"op": "regex", "pattern": "version = \"[^\"]+\"", "replacement": "version = \"1.2.3\"", "expected_count": 1},
  {"op": "replace-lines", "start": 10, "end": 12, "text_file": "block.txt"},
  {"op": "delete-lines", "start": 30, "end": 35}
]
```

## Cross-Platform Shell Escaping

| Shell | Escape char | Special chars | Example |
|-------|-------------|---------------|---------|
| Bash | `\` | `$` `` ` `` `"` `\` | `\$` or use single quotes |
| PowerShell | `` ` `` | `` ` `` `$` `"` | `` `$ `` or use --old-file |
| CMD | `^` | `%` `^` | `^%` or use --old-file |

**Safe alternative:** `--old-file`, `--new-file`, `--diff-input-file` bypass shell parsing entirely.

**PowerShell encoding fix:** `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`

## Appendix: Full Option Reference

- `--encoding auto|utf-8|utf-8-bom|gbk|shift-jis|big5|latin-1|utf-16-le|utf-16-be`
- `--to-encoding preserve|utf-8|utf-8-bom|gbk|shift-jis|big5|latin-1|utf-16-le|utf-16-be`
- `--to-line-ending preserve|lf|crlf|cr`
- `--final-newline preserve|ensure|strip`
- `--trim-trailing-whitespace`
- `--expected-count N`, `--first`, `--count N`, `--no-op-ok`
- `--auto-match`, `--fuzzy`
- `--ignore-indent`, `--ignore-eol`, `--normalize-whitespace`
- `--context-before T`, `--context-after T`
- `--diff-input TEXT`, `--diff-input-file PATH`
- `--anchor-pattern T`, `--offset-start +/-N`, `--offset-end +/-N`, `--anchor-occurrence N`
- `--dry-run`, `--force-write`, `--diff --context N`
- `--backup`, `--backup-dir DIR`, `--backup-suffix SUFFIX`
- `--allow-nul`, `--follow-symlink`
- `--interactive` / `-i`
- `--explain-match-failure`
- `--max-bytes N`, `--lock-timeout N`, `--lock-stale-seconds N`, `--no-lock`
- `--json`, `--arg-encoding ENC`
