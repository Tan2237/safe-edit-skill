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

## Structural Editing Rules

These rules prevent the most common Agent editing mistakes.

### 1. Command Selection Priority

```
Need modification?
│
├─ Replace exact text (within a line)?
│      → edit --old X --new Y
│
├─ Replace entire function/class body?
│      → replace-lines --anchor-pattern "unique_signature" --text T
│
├─ Replace known line range?
│      → replace-lines --start N --end M --text T
│
├─ Add content at file boundaries?
│      → prepend --text T  (beginning)
│      → append --text T   (end)
│
├─ Delete lines?
│      → delete --line N
│      → delete-lines --start N --end M
│
└─ Insert near structural boundary (closing braces, etc.)?
       → CAUTION: Use replace-lines on the boundary line instead
```

### 2. Avoid `insert` Near Structural Boundaries

**Problem:** `insert --line N` inserts AFTER line N. Near closing braces `}`, agents often misjudge whether to insert before or after.

**Bad:**
```bash
insert --line 18639  # inserts after "}" but where exactly?
```

**Good:** Replace the boundary line itself, preserving structure:
```bash
replace-lines --start 18639 --end 18639 --text "}
new_function_here"
```

### 3. Verify Anchor Uniqueness Before Using

**Problem:** `--anchor-pattern "iSoftTimes"` may match multiple locations (definition + calls).

**Before using anchor-pattern:**
```bash
grep -n "pattern" file.cpp  # Check occurrence count
```

If pattern appears multiple times:
- Use more specific pattern (include function signature)
- Or use `--anchor-occurrence` with correct count
- Or fall back to `--start`/`--end` line numbers

**Bad:** `--anchor-pattern "iSoftTimes"` (appears 5 times)

**Good:** `--anchor-pattern "void RasterizeFilterNullHlr("` (unique)

### 4. Line Numbers Invalidate After Edits

**Rule:** After ANY operation that changes line count, ALL cached line numbers become invalid.

Operations that change line count:
- `insert` (adds lines)
- `append` / `prepend` (may add lines)
- `delete` / `delete-lines` (removes lines)
- `replace-lines` (may change line count)

**Workflow:**
```bash
# Step 1: Do first edit
replace-lines --start 100 --end 105 --text "..."

# Step 2: Re-query line numbers BEFORE next edit
grep -n "target_function" file.cpp

# Step 3: Calculate new line numbers
# Step 4: Do second edit
```

### 5. Bracket Safety for Block Replacement

When replacing code blocks:
- Include the opening line (with `{`) in the range
- Include the closing line (with `}`) in the range
- Or: ensure replacement text has matching braces

**Before replacing:**
```
--start 100 --end 150  # Does this include the closing "}"?
```

### 6. Validate After Structural Edits

After editing functions, classes, or structured blocks:
```bash
# Check bracket matching
python -c "print(open('file.cpp').read().count('{') == open('file.cpp').read().count('}'))"

# Or compile immediately
cl file.cpp  # or g++, javac, etc.
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