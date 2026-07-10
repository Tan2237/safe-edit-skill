---
name: safe-edit
description: |
  MANDATORY: File editing MUST follow this protocol. No exceptions.

  The ONLY allowed edit mechanisms:
  - safe_edit.py (when editStrategy="safe-edit")
  - built-in Edit tool (when editStrategy="edit-tool")

  Any other editing mechanism is forbidden.

  Before the first edit of each file:
    1. Resolve SAFE_EDIT_SCRIPT from this SKILL.md location.
    2. Run: python "SAFE_EDIT_SCRIPT" stat --file FILE --json
    3. Cache editStrategy for that file.
    4. Follow the returned strategy for ALL edits on that file.

  Re-run stat only after: file recreation, edit failure.
---

# safe-edit — Hard Protocol

## Priority Order

```
1. Follow this editing protocol.
2. Use the lowest-risk allowed command.
3. Preserve file integrity.
4. Complete the requested change.
```

Never violate a higher priority to satisfy a lower priority.

## Forbidden

- Do NOT construct custom Python code to edit files.
- Do NOT create temporary scripts or helper programs to modify files (Python, Bash, PowerShell, Perl, Node.js, or any other language).
- Do NOT invoke `safe_edit.py` indirectly through another program or script.
- Do NOT replace `safe_edit.py` with inline Python, regex scripts, or ad-hoc file writes.
- Do NOT write files using shell redirection (`>`, `>>`, `cat <<EOF`, `echo > file`).
- Do NOT invent alternative edit workflows.
- Do NOT infer, generate, or invent `safe_edit.py` commands not listed in Allowed Commands.

## Failure Policy

If the required modification cannot be completed using the allowed commands:

**STOP. Report the limitation.**

Do NOT:
- write a custom script
- use shell redirection
- create temporary patch tools
- modify files through any mechanism outside this protocol

Completing the task is lower priority than following this protocol.

## Runtime Script Resolution

Before invoking any command, resolve `SAFE_EDIT_SCRIPT` once:

1. Start from the absolute path of this `SKILL.md` supplied by the skill loader.
2. Set `SAFE_EDIT_SCRIPT` to the sibling file `safe_edit.py` in the same directory.
3. Convert it to an absolute path and verify that it exists before the first invocation.
4. Reuse that exact absolute path for every `safe_edit.py` command in the current task.

`SAFE_EDIT_SCRIPT` in the examples below is a placeholder for that resolved absolute path. It is not a shell environment variable and must not be passed literally.

Never assume `safe_edit.py` is in the current working directory, never resolve it relative to the target file, and never search the whole filesystem. If the sibling script is missing, **STOP** and report both the resolved `SKILL.md` path and the expected script path.

## Required Workflow

Before the first edit of each file:

```bash
python "SAFE_EDIT_SCRIPT" stat --file F --json
```

The stat result is authoritative for the lifetime of the file. Do not re-run stat unless the file is recreated or an edit fails.

The selected editStrategy is locked for the lifetime of the file. Do NOT switch to another edit mechanism after a command failure. Continue using the cached editStrategy.

- `editStrategy: "safe-edit"` → ALL edits on this file MUST use `safe_edit.py`. Do not switch back to built-in Edit.
- `editStrategy: "edit-tool"` → Use built-in Edit tool for this file.

On Windows, `py -3` works when `python` is not on PATH.

---

## Allowed Commands

These are the ONLY permitted invocations. Any command not listed here is prohibited.

```bash
# Before editing any file (mandatory first step)
python "SAFE_EDIT_SCRIPT" stat --file F --json

# Replace text
python "SAFE_EDIT_SCRIPT" edit --file F --old "old" --new "new" --expected-count 1

# Replace with auto-match (tolerate whitespace)
python "SAFE_EDIT_SCRIPT" edit --file F --old "old" --new "new" --auto-match --expected-count 1

# Preview before applying
python "SAFE_EDIT_SCRIPT" edit --file F --old "old" --new "new" --dry-run --diff

# Replace function/class body (anchor)
python "SAFE_EDIT_SCRIPT" replace-lines --file F --anchor-pattern "sig" --text T

# Replace by line range
python "SAFE_EDIT_SCRIPT" replace-lines --file F --start N --end M --text T

# Insert before line N
python "SAFE_EDIT_SCRIPT" insert --file F --line N --text T

# Add at file boundaries
python "SAFE_EDIT_SCRIPT" prepend --file F --text T
python "SAFE_EDIT_SCRIPT" append --file F --text T

# Delete lines
python "SAFE_EDIT_SCRIPT" delete --file F --line N
python "SAFE_EDIT_SCRIPT" delete-lines --file F --start N --end M

# Fuzzy matching (for approximate text)
python "SAFE_EDIT_SCRIPT" edit --file F --old "old" --new "new" --fuzzy --expected-count 1

# Diagnose match failure
python "SAFE_EDIT_SCRIPT" edit --file F --old "old" --new "new" --explain-match-failure

# Large content — use file variants
python "SAFE_EDIT_SCRIPT" edit --file F --old-file old.txt --new-file new.txt
python "SAFE_EDIT_SCRIPT" replace-lines --file F --anchor-pattern "X" --text-file body.txt

# Multi-block edits (2+ edits in one file)
python "SAFE_EDIT_SCRIPT" edit --file F --diff-input-file diff.txt
```

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
│          (inserts BEFORE line N; new content becomes new line N)
│
├─ Add at file boundaries?
│      → prepend --text T  (at beginning)
│      → append --text T   (at end)
│
└─ Delete lines?
       → delete --line N
       → delete-lines --start N --end M
```

**Risk hierarchy (always choose lowest-risk command):**

```
edit                        ← SAFEST
replace-lines (--anchor-pattern)
replace-lines (--start/--end)
insert / delete
regex                       ← HIGHEST RISK
```

---

## Core Rules

1. **Always add `--expected-count 1` for normal text replacement** — prevents wrong matches from silently succeeding. Do not omit it unless intentionally targeting multiple matches, performing diagnosis, or using commands with their own matching semantics.

2. **Use `--old-file` for complex content** — multiline, >100 chars, or contains shell-sensitive chars (`$`, `%`, `\`, `` ` ``, `'`, `"`).

3. **Use `--auto-match` for multiline edits** — auto-tries: exact → ignore-eol → ignore-indent → normalize-whitespace.

4. **Use `edit` over `replace-lines`** — `edit` is safest. Use `replace-lines` only when `edit` cannot do the job.

5. **Re-read before structural edits** — line positions and content may have shifted after prior edits.

---

## Match Failure Escalation

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
    │   ├─ Still failed? → --explain-match-failure
    │   └─ Wrong old text? → fix old text, don't just switch to anchor
    │
    ├─ "text appears multiple times"
    │   └─ Add --context-before/after "unique"
    │
    └─ "expected count mismatch"
        └─ Adjust --expected-count or use --first
```

Most failures are whitespace differences. Use `--explain-match-failure` before abandoning text matching.

---

## Match Options

| Option | Effect | When to use |
|--------|--------|-------------|
| `--auto-match` | Auto-try: exact → ignore-eol → ignore-indent → normalize-whitespace | **Default for multiline** |
| `--fuzzy` | Fuzzy matching (≥0.6 similarity) | AI-generated approximate text |
| `--normalize-whitespace` | Collapse whitespace | **JSON/YAML/Markdown** |
| `--context-before/after` | Disambiguate matches | Multiple occurrences |

| Content | Default flags |
|---------|---------------|
| Single line | (none) |
| Multiline code | `--auto-match` |
| JSON/YAML/Markdown | `--auto-match --normalize-whitespace` |

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
python "SAFE_EDIT_SCRIPT" edit --file F --diff-input-file diff.txt
```

---

## Windows (Git Bash / MSYS2)

MSYS2 automatically converts POSIX-style paths in CLI arguments:

| User types | MSYS2 converts to | Effect |
|------------|-------------------|--------|
| `--old "//if"` | `--old "/if"` | Double slash collapsed |
| `--old "/foo"` | `--old "C:/Program Files/Git/foo"` | Single slash expanded |

**Fix**: Set environment variable before calling safe_edit.py:

```bash
export MSYS2_ARG_CONV_EXCL="*"
python "SAFE_EDIT_SCRIPT" edit --file F --old "//if (x > 0)" --new "if (x > 0)" --expected-count 1
```

Or use file variants to bypass shell entirely:

```bash
python "SAFE_EDIT_SCRIPT" edit --file F --old-file old.txt --new-file new.txt
```

safe-edit emits a `"warnings"` field in JSON output when MSYS2 path corruption is detected.

---

## Structural Editing Rules

1. **Avoid `insert` near closing braces** — `insert --line N` inserts BEFORE line N. For `}` boundaries, use `replace-lines --start N --end N --text "}\nnew_content"`.

2. **Verify anchor uniqueness** — Search for the pattern first. If it appears multiple times, use a more specific pattern or `--start`/`--end`.

3. **Line numbers invalidate after edits** — Re-query locations after any operation that changes line count.

4. **Bracket safety** — Verify the range includes/excludes braces as intended.

5. **Validate after structural edits** — Check bracket matching or compile immediately.

---

For full option list: `python "SAFE_EDIT_SCRIPT" --help`
