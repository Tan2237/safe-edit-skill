---
name: safe-edit
description: |
  Before the first edit of a file:

    python safe_edit.py stat --file FILE --json

  Use the returned editStrategy.

  Cache the result per file.

  Re-run stat only after:
    - file recreation
    - edit failure
---

# safe-edit — Agent Edit Protocol

Use `safe_edit.py` as the single entry point. Pure Python standard library, portable across Windows/Linux/macOS.

## Required Workflow (run once per file before first edit)

```
Need to edit a file?
│
├─ Step 1: Check file
│   python safe_edit.py stat --file F --json
│   │
│   ├─ editStrategy = "safe-edit"
│   │      → Use safe-edit for ALL edits on this file
│   │      → Do not switch back to built-in Edit
│   │
│   └─ editStrategy = "edit-tool"
│   │      → Use built-in Edit tool
│   │
│   Cache stat result per file. Re-run stat only after:
│     - file recreation
│     - edit failure
│
│   Example:
│     foo.cpp → stat → editStrategy=safe-edit
│     Subsequent edits → reuse cached result
│
├─ Step 2: Edit
│   Use editStrategy.
│   For safe-edit usage details, see Core Rules below.
│
└─ Step 3: Edit failed?
    → Read JSON error output
    → Follow recommendedAction
```

## Quick Reference

```bash
# First step before editing
python safe_edit.py stat --file path/to/file --json

# Replace text
python safe_edit.py edit --file F --old "old" --new "new" --expected-count 1

# Replace with auto-match (tolerate whitespace)
python safe_edit.py edit --file F --old "old" --new "new" --auto-match --expected-count 1

# Preview before applying
python safe_edit.py edit --file F --old "old" --new "new" --dry-run --diff
```

On Windows, `py -3` works when `python` is not on PATH.

---

## Core Rules

1. **Always add `--expected-count 1`** — prevents wrong matches from silently succeeding. Use this unless the edit is intentionally targeting multiple matches.

2. **Prefer `--old-file` when** — multiline, >100 chars, or contains shell-sensitive chars (`$`, `%`, `\`, `` ` ``, `'`, `"`). Prevents shell escaping disasters.

3. **Use `--auto-match` for multiline edits** — automatically tries exact → ignore-eol → ignore-indent → normalize-whitespace.

4. **Prefer `edit` over `replace-lines`** — `edit` is safest. Use `replace-lines` only when `edit` cannot do the job.

5. **Re-read before structural edits** — Do not rely on stale context after file modifications. Function position, content, and bracket locations may have changed.

---

## Command Selection

```
Need modification?
│
├─ Replace exact text (single/multiline)?
│      → edit --old X --new Y --expected-count 1
│
├─ Replace function/class body?
│   │
│   ├─ Anchor pattern unique?
│   │      YES → replace-lines --anchor-pattern "unique_sig" --text T
│   │
│   └─ Anchor not unique?
│          → Locate line range with search tool
│          → replace-lines --start N --end M --text T
│
│   Note: replace-lines preserves original indentation by default.
│   Use --no-preserve-indent to insert text exactly as provided.
│
├─ Insert content at specific location?
│   │
│   ├─ Structural boundary (}, class/namespace end)?
│   │      → replace-lines --start N --end N --text "}\nnew_content"
│   │
│   └─ Normal text location?
│          → insert --line N --text T
│
├─ Add at file boundaries?
│      → prepend --text T  (at beginning)
│      → append --text T   (at end)
│
└─ Delete lines?
       → delete --line N
       → delete-lines --start N --end M
```

**Risk hierarchy (prefer lower risk):**

Always choose the lowest-risk command that can complete the edit.

```
edit                        ← SAFEST
replace-lines (--anchor-pattern)
replace-lines (--start/--end)
insert / delete
regex                       ← HIGHEST RISK
```

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
    │   ├─ Still failed? → --explain-match-failure (diagnose)
    │   └─ Wrong old text? → fix old text, don't just switch to anchor
    │
    ├─ "text appears multiple times"
    │   └─ Add --context-before/after "unique"
    │
    └─ "expected count mismatch"
        └─ Adjust --expected-count or use --first
```

**Key insight:** Most failures are whitespace differences. `--auto-match` handles this. Use `--explain-match-failure` before abandoning text matching.

---

## Windows (Git Bash / MSYS2)

MSYS2 automatically converts POSIX-style paths in CLI arguments:

| User types | MSYS2 converts to | Effect |
|------------|-------------------|--------|
| `--old "//if"` | `--old "/if"` | Double slash collapsed |
| `--old "/foo"` | `--old "C:/Program Files/Git/foo"` | Single slash expanded |

This silently corrupts `--old`/`--new`/`--pattern`/`--text` values starting with `/` or `//`.

**Fix**: Set environment variable before calling safe_edit.py:

```bash
export MSYS2_ARG_CONV_EXCL="*"
python safe_edit.py edit --file F --old "//if (x > 0)" --new "if (x > 0)" --expected-count 1
```

Or use file variants to bypass shell entirely:

```bash
python safe_edit.py edit --file F --old-file old.txt --new-file new.txt
```

safe-edit emits a `"warnings"` field in JSON output when MSYS2 path corruption is detected.

---

## Structural Editing Rules

### Critical Rules

1. **Avoid `insert` near closing braces** — `insert --line N` goes AFTER line N. For `}` boundaries, use `replace-lines --start N --end N --text "}\nnew_content"` to preserve structure.

2. **Verify anchor uniqueness** — Search for the pattern first. If it appears multiple times (definition + calls), use more specific pattern like `"void FuncName("` or use `--start`/`--end` instead.

3. **Line numbers invalidate after edits** — After any operation that changes line count, re-query locations before next line-based operation.

4. **Bracket safety** — When replacing code blocks, verify the range includes/excludes braces as intended.

5. **Validate after structural edits** — Check bracket matching or compile immediately.

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
