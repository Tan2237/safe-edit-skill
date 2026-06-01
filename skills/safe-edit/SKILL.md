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

# Preview before applying
python safe_edit.py edit --file F --old "old" --new "new" --dry-run --diff
```

On Windows, `py -3` works when `python` is not on PATH.

---

## Core Rules (Priority Order)

1. **Always add `--expected-count 1`** — prevents wrong matches from silently succeeding. First priority.

2. **Prefer `--old-file` when** — multiline, >100 chars, or contains shell-sensitive chars (`$`, `%`, `\`, `` ` ``, `'`, `"`). Prevents shell escaping disasters.

3. **Use `--auto-match` for multiline edits** — automatically tries exact → ignore-eol → ignore-indent → normalize-whitespace.

4. **Line numbers invalidate after edits** — Any `insert`, `delete`, or `replace-lines` that changes line count invalidates all cached line numbers. Re-query with `grep -n` before next line-based operation.

5. **Prefer `edit` over `replace-lines`** — `edit` is safest. Use `replace-lines` only when `edit` cannot do the job.

6. **Verify anchor uniqueness before `replace-lines`** — Run `grep -n "pattern" file` first. If pattern appears multiple times, use more specific pattern or `--start`/`--end` instead.

7. **Re-read before structural edits** — Do not rely on stale context after file modifications. Function position, content, and bracket locations may have changed.

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
│          → Locate line range with grep -n
│          → replace-lines --start N --end M --text T
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
```
edit                 ← SAFEST
replace-lines (--start/--end)
replace-lines (--anchor-pattern)
insert / delete
regex                ← HIGHEST RISK
```

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
│   ├─ Step 3: --explain-match-failure (diagnose before giving up)
│   └─ Step 4: Use --anchor-pattern instead of full text match
│
└─ Ambiguous match?
    └─ Add --context-before "unique" or --context-after "unique"
```

---

**Shell-sensitive content** (newlines, quotes, `$`, `%`, `\`, `` ` ``, long text) → prefer `--old-file` / `--new-file`.

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

## Structural Editing Rules

### Critical Rules

1. **Avoid `insert` near closing braces** — `insert --line N` goes AFTER line N. For `}` boundaries, use `replace-lines --start N --end N --text "}\nnew_content"` to preserve structure.

2. **Verify anchor uniqueness** — Run `grep -n "pattern" file` first. If pattern appears multiple times (definition + calls), use more specific pattern like `"void FuncName("` or use `--start`/`--end` instead.

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
| Function/class body | Verify anchor first, then `--anchor-pattern` |

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
