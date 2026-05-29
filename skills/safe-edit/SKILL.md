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
python safe_edit.py stat --file path/to/file
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
- `stat`: concise summary for AI agents - shows encoding, line endings, size, and line count only. Use `--json` for machine-readable output.
  ```text
  Encoding: UTF-8
  Line endings: LF
  Size: 12 KB
  Lines: 392
  ```
- `convert`: explicitly convert encoding, line endings, final newline, or trailing whitespace without textual replacement.
- `edit --old TEXT --new TEXT`: replace literal text. Empty `--new ""` is allowed; empty `--old ""` is refused.
  ```bash
  python safe_edit.py edit --file path/to/file --old "foo" --new "bar" --interactive
  ```
- `regex --pattern PATTERN --replacement TEXT`: replace with Python `re.sub` semantics. Flags: `i`, `m`, `s`, `x`, `a`. Use `--literal-replacement` when backreferences must not be interpreted.
- `insert --line N --text TEXT`: insert before 1-based line `N`.
- `prepend --text TEXT`: add text at the beginning of the file.
- `append --text TEXT`: add text at the end of the file.
- `delete --line N`: delete one 1-based line.
- `replace-lines --start N --end M --text TEXT`: replace an inclusive 1-based line range.
- `delete-lines --start N --end M`: delete an inclusive 1-based line range.
- `batch --ops-file ops.json`: run multiple operations in memory, then write once.

## Regex Examples

Use `regex` only when exact `edit` is not possible.

### Replace version number
```bash
python safe_edit.py regex --file config.py \
  --pattern 'version = "[^"]+"' \
  --replacement 'version = "2.0.0"'
```

### Replace all numbers
```bash
python safe_edit.py regex --file data.txt \
  --pattern '\d+' \
  --replacement '0'
```

### Use capture groups
```bash
python safe_edit.py regex --file code.cpp \
  --pattern 'foo\((\w+)\)' \
  --replacement 'bar(\1)'
```

### Literal replacement (no backreference)
```bash
python safe_edit.py regex --file text.txt \
  --pattern 'error' \
  --replacement '\1test' \
  --literal-replacement
```

### Anti-patterns (DO NOT)
```bash
# BAD: Too greedy, may match too much
--pattern '.*'

# BAD: Could match unintended content
--pattern 'foo.*bar'

# GOOD: Be specific
--pattern 'version = "\d+\.\d+"'
```

### Key Principles
- Prefer `edit` over `regex` when possible
- Test with `--dry-run --diff` first
- Use `--expected-count` to verify match count
- Use `--literal-replacement` when replacement contains backslashes

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

Argument files default to UTF-8; override with `--arg-encoding` (aliases: `--param-encoding`, `--input-encoding`).

## Cross-Platform Shell Escaping

Different shells have different quoting and escaping rules. When passing complex text through command-line arguments, special characters can cause unexpected behavior.

### PowerShell (Windows)

PowerShell interprets several special characters in double-quoted strings:

- `` ` `` - Escape character (backtick)
- `$` - Variable expansion (e.g., `$var`)
- `%` - Environment variable expansion in CMD compatibility mode
- Double quotes allow expansion, single quotes are literal

**Problem examples:**
```powershell
# WRONG: ` gets interpreted as escape sequence
py -3 safe_edit.py edit --file a.cpp --old "foo`bar" --new "baz"

# WRONG: $var gets expanded to variable value
py -3 safe_edit.py edit --file a.cpp --old "$variable" --new "bar"
```

### CMD (Windows)

CMD has simpler but different rules:

- `%` - Environment variable expansion
- `^` - Escape character
- Double quotes preserve most characters

### Bash (Linux/macOS)

Bash uses standard shell escaping:

- `$` - Variable expansion
- `\` - Escape character
- Single quotes are literal (no expansion)

### Recommended Solutions

**1. Use stdin for complex text:**

```powershell
# PowerShell: Pipe text to avoid quoting issues
"foo bar" | py -3 safe_edit.py edit --file a.cpp --old-stdin --new "new text"

# Read old text from file
Get-Content old.txt | py -3 safe_edit.py edit --file a.cpp --old-stdin --new-file new.txt
```

```cmd
:: CMD: Use type command
type old.txt | py -3 safe_edit.py edit --file a.cpp --old-stdin --new-file new.txt
```

```bash
# Bash: Use cat command
cat old.txt | python3 safe_edit.py edit --file a.cpp --old-stdin --new-file new.txt
```

**2. Use file-based arguments:**

```bash
# Works on all platforms
python safe_edit.py edit --file a.cpp --old-file old.txt --new-file new.txt
```

**3. PowerShell encoding considerations:**

PowerShell's default encoding may not match your file. Use explicit encoding:

```powershell
# Force UTF-8 output
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
"foo bar" | py -3 safe_edit.py edit --file a.cpp --old-stdin --new "bar"

# Or use Out-File with encoding
"old text" | Out-File -Encoding utf8 old.txt
py -3 safe_edit.py edit --file a.cpp --old-file old.txt --new "new text"
```

### When to Use Each Approach

| Scenario | Recommended Method |
|----------|-------------------|
| Simple ASCII text | Direct `--old` / `--new` arguments |
| Text with special characters | Use `--old-stdin` / `--new-stdin` |
| Multiline text | Use `--old-file` / `--new-file` |
| Large text (>1KB) | Use `--old-file` / `--new-file` |
| Cross-platform scripts | Use file-based arguments |


## Common Issues and Solutions

### Line Ending Mismatches

When matching text fails due to line ending differences (CRLF vs LF), use `--ignore-eol`:

```bash
# File has CRLF, but your pattern uses LF
python safe_edit.py edit --file code.cpp \
    --old "line1\nline2" \
    --new "new1\nnew2" \
    --ignore-eol
```

This is common when:
- Editing Windows files from WSL or Git Bash
- Files were converted by Git's `autocrlf` setting
- Copying text from web browsers or documentation

### Encoding Issues

**GBK/Shift-JIS/Big5 Projects:**

For projects using legacy encodings, specify the encoding explicitly:

```bash
# Chinese project using GBK encoding
python safe_edit.py edit --file source.cpp \
    --encoding gbk \
    --old "旧文本" \
    --new "新文本"
```

**PowerShell Encoding Problems:**

PowerShell 5.1 defaults to UTF-16LE for piping, which may cause issues. Solutions:

```powershell
# Solution 1: Use PowerShell 7+ (defaults to UTF-8)
pwsh -Command '"text" | python safe_edit.py edit --file a.cpp --old-stdin --new "new"'

# Solution 2: Write to file first
"text" | Out-File -Encoding utf8 temp.txt
python safe_edit.py edit --file a.cpp --old-file temp.txt --new "new"

# Solution 3: Use .NET encoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
"text" | python safe_edit.py edit --file a.cpp --old-stdin --new "new"
```

### Whitespace and Indentation

When indentation doesn't match (tabs vs spaces), use `--ignore-indent`:

```bash
# File uses tabs, pattern uses spaces
python safe_edit.py edit --file code.cpp \
    --old "    return 42" \
    --new "    return 43" \
    --ignore-indent
```

For multiple whitespace issues, combine flags:

```bash
python safe_edit.py edit --file code.cpp \
    --old "foo    bar" \
    --new "baz qux" \
    --normalize-whitespace
```

### Match Failure Diagnostics

When a match fails, use `--explain-match-failure` to understand why:

```bash
python safe_edit.py edit --file code.cpp \
    --old "return 42" \
    --new "return 43" \
    --explain-match-failure
```

This shows:
- Closest match location
- Expected vs actual whitespace
- Specific differences detected

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

## Interactive Mode

Apply changes with user confirmation. Similar to `git add -p`.

```bash
python safe_edit.py edit --file a.cpp --old "foo" --new "bar" -i
```

When prompted:
- `y` - yes, apply this change
- `n` - no, skip this change  
- `a` - all, apply all remaining without prompting
- `q` - quit, skip remaining changes
- `?` - help

**Constraints:**
- Requires interactive terminal (TTY)
- Cannot use with `--dry-run`
- Not applicable to `inspect` command

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
- `--ignore-indent`: ignore indentation differences when matching (tabs vs spaces).
- `--ignore-eol`: ignore line ending differences when matching (CRLF vs LF).
- `--normalize-whitespace`: treat consecutive whitespace as equivalent when matching.
- `--dry-run`: validate and transform in memory without writing.
- `--force-write`: write even when output bytes are identical to the original.
- `--diff --context N`: emit unified diff preview.
- `--backup`: keep a timestamped backup before replacement.
- `--backup-dir DIR`: place backups in a separate directory.
- `--backup-suffix SUFFIX`: customize backup suffix; `{timestamp}` is supported.
- `--allow-nul`: allow decoded NUL characters.
- `--follow-symlink`: edit the symlink target instead of refusing.
- `--interactive, -i`: prompt before applying changes.
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

## Controlled Whitespace Matching

By default, `edit` requires exact character-by-character matching. When whitespace differences cause match failures, use these **controlled relaxation flags**:

```bash
# Match despite indentation differences (tabs vs spaces)
python safe_edit.py edit --file code.cpp --old "    return 42" --new "    return 43" --ignore-indent

# Match despite line ending differences (CRLF vs LF)
python safe_edit.py edit --file code.cpp --old "line1\nline2" --new "new1\nnew2" --ignore-eol

# Match despite whitespace quantity differences (multiple spaces vs single space)
python safe_edit.py edit --file code.cpp --old "foo bar" --new "baz qux" --normalize-whitespace
```

### Key Principles

1. **Explicit, not magic**: Each flag must be explicitly requested. No automatic normalization.
2. **Match-only, not replace**: These flags affect `--old` matching only. The `--new` replacement text is inserted exactly as provided.
3. **Preserve original formatting**: The file's original whitespace (indentation, line endings, spacing) is preserved in unchanged portions.
4. **Composable**: Flags can be combined for complex scenarios.

### Flag Details

- `--ignore-indent`: Remove leading whitespace from each line before matching. Useful when files use tabs but your pattern uses spaces, or vice versa.
- `--ignore-eol`: Normalize all line endings to LF before matching. Useful when files use CRLF but your pattern uses LF, or vice versa.
- `--normalize-whitespace`: Collapse consecutive whitespace (spaces, tabs, newlines) to a single space before matching. Useful when whitespace quantity varies.

### Example: Cross-Platform Code

A file with Windows formatting (tabs + CRLF + extra spaces):

```
def foo():
		return    42
```

Match with Unix-style pattern:

```bash
python safe_edit.py edit --file code.cpp \
    --old "def foo():\n    return 42" \
    --new "def bar():\n    return 43" \
    --ignore-indent \
    --ignore-eol \
    --normalize-whitespace \
    --expected-count 1
```

Result: The replacement succeeds, but the file keeps its original formatting (tabs, CRLF, extra spaces).

### Limitations

- Only affects `edit` command (literal replacement). Does not affect `regex`, `insert`, or other commands.
- When normalization is used, the replacement text replaces the entire matched `--old` text exactly as provided.
- For complex whitespace transformations, consider using `regex` with explicit patterns instead.

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
