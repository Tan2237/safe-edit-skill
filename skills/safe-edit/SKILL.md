---
name: safe-edit
description: Agent-friendly safe text editing primitive. Inspect, edit, and convert existing text files while preserving encoding, BOM, line endings, permissions, and write integrity. Use for any file edit where mojibake, truncated files, silent no-ops, or noisy Git diffs matter.
---

# safe-edit — Agent Edit Protocol

Use `safe_edit.py` as the single entry point. Pure Python standard library, portable across Windows/Linux/macOS.

## Quick Reference

```bash
# Check file before editing
python safe_edit.py stat --file path/to/file

# Replace text
python safe_edit.py edit --file F --old "old" --new "new" --expected-count 1

# Replace with auto-match (tolerate whitespace)
python safe_edit.py edit --file F --old "old" --new "new" --auto-match --expected-count 1

# Replace function body
python safe_edit.py replace-lines --file F --anchor-pattern "funcName" --offset-start +0 --offset-end +N --text "..."

# Preview before applying
python safe_edit.py edit --file F --old "old" --new "new" --dry-run --diff
```

On Windows, `py -3` works when `python` is not on PATH.

---

## Command Selection

```
Need modification?
│
├─ Replace exact text (single/multiline)?
│      → edit --old X --new Y
│
├─ Replace entire function/class body?
│      → replace-lines --anchor-pattern "name" --offset-start +0 --offset-end +N --text T
│
├─ Insert content at specific location?
│      → insert --line N --text T
│      → prepend --text T  (at beginning)
│      → append --text T   (at end)
│
└─ Delete lines?
       → delete --line N
       → delete-lines --start N --end M
```

---

## Core Rules

1. **Default to `--old-file` for special characters**: `$`, `%`, `\`, `` ` ``, `'`, `"`, newline, >100 chars.
2. **Use `--auto-match` for multiline edits**: automatically tries exact → ignore-eol → ignore-indent → normalize-whitespace.
3. **Use `--anchor-pattern` for function/class body replacement**: code formatters change whitespace, but names stay stable.
4. **Always add `--expected-count 1`**: prevents wrong matches from silently succeeding.
5. **Run `stat --file F` before uncertain edits**: verify encoding and line count.

---

## Edit Strategy Decision Tree

```
Need to edit a file?
│
├─ Is it a text file? ─── No → STOP
│
├─ ⚠️ PARAM MODE CHECK
│   │
│   ├─ old/new contains ANY of:
│   │     • newline (multiline)
│   │     • quote (' or ")
│   │     • backslash (\)
│   │     • dollar ($)
│   │     • percent (%)
│   │     • backtick (`)
│   │     • >100 characters
│   │
│   │   YES → use --old-file / --new-file
│   │   NO  → use --old "..." --new "..."
│
├─ EDIT TYPE CHECK
│   │
│   ├─ SINGLE LINE:
│   │     edit --old X --new Y --expected-count 1
│   │
│   ├─ MULTILINE:
│   │     edit --old X --new Y --auto-match --expected-count 1
│   │
│   └─ CODE BLOCK (json/yaml/markdown):
│        edit --old X --new Y --auto-match --normalize-whitespace --expected-count 1
│
├─ MATCH FAILED?
│   │
│   ├─ Step 1: Was --auto-match on? If no, add it
│   ├─ Step 2: --auto-match --fuzzy
│   ├─ Step 3: Use --anchor-pattern instead of full text match
│   └─ Step 4: --explain-match-failure (diagnose)
│
└─ Ambiguous match?
    └─ Add --context-before "unique" or --context-after "unique"
```

---

## Shell Escaping Quick Reference

| Character | Problem | Example | Solution |
|-----------|---------|---------|----------|
| `$VAR` | Shell expansion | `$env:PATH` | `--old-file` |
| `%VAR%` | CMD expansion | `%PATH%` | `--old-file` |
| `` ` `` | PowerShell escape | `` `n `` | `--old-file` |
| `\` | Backslash escape | `C:\temp` | `--old-file` |
| `'` or `"` | Quote parsing | `it's` | `--old-file` |
| Newline | Multi-line args | Code blocks | `--old-file` |
| >100 chars | Long args | Large blocks | `--old-file` |

**Rule: When in doubt, use `--old-file`.**

---

## Match Failure Escalation Path

```
edit --old X --new Y --expected-count 1
│
├─ SUCCESS → Done
│
└─ FAIL → Check error:
    │
    ├─ "old text was not found"
    │   ├─ No --auto-match? → add it
    │   ├─ Has --auto-match? → add --fuzzy
    │   └─ Still failed? → try --anchor-pattern
    │
    ├─ "text appears multiple times"
    │   └─ Add --context-before/after "unique"
    │
    └─ "expected count mismatch"
        └─ Adjust --expected-count or use --first
```

**Key insight:** Most multiline failures are whitespace differences. `--auto-match` handles this.

---

## Match Options

| Option | Effect | When to use |
|--------|--------|-------------|
| `--auto-match` | Auto-try: exact → ignore-eol → ignore-indent → normalize-whitespace | **Default for multiline** |
| `--fuzzy` | Fuzzy matching (≥0.6 similarity) | AI-generated approximate text |
| `--normalize-whitespace` | Collapse whitespace | **JSON/YAML/Markdown** |
| `--context-before/after` | Disambiguate matches | Multiple occurrences |

**Content type → Default strategy:**

| Content | Default flags |
|---------|---------------|
| Single line | (none) |
| Multiline code | `--auto-match` |
| JSON/YAML/Markdown | `--auto-match --normalize-whitespace` |
| Function/class body | `--anchor-pattern` |

---

## Anchor-First Strategy

For function/class/config block replacement, prefer anchors over full-text matching.

**Why:** Code formatters change whitespace, but function names stay stable.

**Example:**

```cpp
void Process() {  // formatter may change braces/indentation
    // 50 lines
}
```

**Bad:** `edit --old "void Process()\n{\n..."` → FAILS after formatting

**Good:** `replace-lines --anchor-pattern "void Process" --offset-start +0 --offset-end +50 --text "..."`

**Use anchor-first for:**
- Function/class body replacement
- JSON/YAML config blocks
- Any block with distinctive header

---

## Large Content

Use file variants for anything complex:

```bash
python safe_edit.py edit --file F --old-file old.txt --new-file new.txt
python safe_edit.py replace-lines --file F --anchor-pattern "X" --text-file body.txt
```

Available: `--old-file`, `--new-file`, `--text-file`, `--diff-input-file`.

---

## Multi-Block Edits

For 2+ edits in one file, use diff-input:

```
------- SEARCH
old text
=======
new text
+++++++ REPLACE
------- SEARCH
another old
=======
another new
+++++++ REPLACE
```

```bash
python safe_edit.py edit --file F --diff-input-file diff.txt
```

---

For full option list: `python safe_edit.py --help`