---
name: safe-edit
description: |
  MANDATORY: File editing MUST follow this protocol. No exceptions.

  The ONLY allowed edit mechanisms:
  - safe_edit.py (when editStrategy="safe-edit")
  - built-in Edit tool (when editStrategy="edit-tool")

  Any other editing mechanism is forbidden.

  Before modifying or removing an existing file:
    1. Resolve SAFE_EDIT_SCRIPT from this SKILL.md location.
    2. Run: python "SAFE_EDIT_SCRIPT" stat --file FILE --json
    3. Cache editStrategy and sha256 for that file.
    4. Follow the returned strategy for edits; use remove-file only for an
       explicitly requested deletion.

  For a required new file, use the controlled create command. It refuses
  existing targets and requires explicit encoding and line-ending choices.

  Re-run stat only after: file recreation, a failed safe_edit.py invocation that may have reached the write phase, or an uncertain execution outcome.
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

Before the first edit or removal of each existing file:

```bash
python "SAFE_EDIT_SCRIPT" stat --file F --json
```

For an explicitly requested file deletion, pass the returned `sha256` and the
containing workspace root to `remove-file`. Never remove a directory or
symbolic link.

A new file cannot be inspected first. Create it only when it is a requested task artifact:

```bash
python "SAFE_EDIT_SCRIPT" create --file F --to-encoding utf-8 --to-line-ending lf --text-base64 B64
```

The target must not exist and its parent directory must already exist. `create` never overwrites and never creates parent directories. After creation, run `stat` before any later edit.

The stat result is authoritative for the lifetime of the file. Re-run it only when the file is recreated, a failed `safe_edit.py` invocation may have reached the write phase, or the execution outcome is uncertain. Do not re-run it when the shell/tool rejects the command before process start, or when argument parsing fails before target access.

The selected editStrategy is locked for the lifetime of the file. Do NOT switch to another edit mechanism after a command failure. Continue using the cached editStrategy.

- `editStrategy: "safe-edit"` → ALL edits on this file MUST use `safe_edit.py`. Do not switch back to built-in Edit.
- `editStrategy: "edit-tool"` → Use built-in Edit tool for this file.

On Windows, `py -3` works when `python` is not on PATH.

---

## Allowed Commands

These are the ONLY permitted invocations. Any command not listed here is prohibited.

```bash
# Before editing or removing any existing file (mandatory first step)
python "SAFE_EDIT_SCRIPT" stat --file F --json

# Remove one explicitly requested regular file inside a workspace root
python "SAFE_EDIT_SCRIPT" remove-file --file F --workspace-root ROOT --expected-sha256 SHA256
python "SAFE_EDIT_SCRIPT" remove-file --file F --workspace-root ROOT --expected-sha256 SHA256 --dry-run --json

# Create a requested task artifact; target must not exist
python "SAFE_EDIT_SCRIPT" create --file F --to-encoding ENC --to-line-ending EOL --text-stdin
python "SAFE_EDIT_SCRIPT" create --file F --to-encoding ENC --to-line-ending EOL --text-base64 B64
python "SAFE_EDIT_SCRIPT" create --file F --to-encoding ENC --to-line-ending EOL --text-file EXISTING_FILE
python "SAFE_EDIT_SCRIPT" create --file F --to-encoding ENC --to-line-ending EOL --text T
python "SAFE_EDIT_SCRIPT" create --file F --to-encoding ENC --to-line-ending EOL --text-base64 B64 --dry-run --diff

# Shell-safe payload transports (preferred for complex or sensitive content)
python "SAFE_EDIT_SCRIPT" batch --file F --ops-stdin
python "SAFE_EDIT_SCRIPT" batch --file F --ops-base64 B64
python "SAFE_EDIT_SCRIPT" edit --file F --diff-input-stdin
python "SAFE_EDIT_SCRIPT" edit --file F --diff-input-base64 B64
python "SAFE_EDIT_SCRIPT" edit --file F --old-stdin --new-base64 B64 --expected-count 1
python "SAFE_EDIT_SCRIPT" edit --file F --old-base64 B64 --new-stdin --expected-count 1
python "SAFE_EDIT_SCRIPT" edit --file F --old-base64 OLD_B64 --new-base64 NEW_B64 --expected-count 1
python "SAFE_EDIT_SCRIPT" regex --file F --pattern-stdin --replacement-base64 B64
python "SAFE_EDIT_SCRIPT" regex --file F --pattern-base64 B64 --replacement-stdin
python "SAFE_EDIT_SCRIPT" regex --file F --pattern-base64 PATTERN_B64 --replacement-base64 REPLACEMENT_B64
python "SAFE_EDIT_SCRIPT" insert --file F --line N --text-stdin
python "SAFE_EDIT_SCRIPT" insert --file F --line N --text-base64 B64
python "SAFE_EDIT_SCRIPT" prepend --file F --text-stdin
python "SAFE_EDIT_SCRIPT" prepend --file F --text-base64 B64
python "SAFE_EDIT_SCRIPT" append --file F --text-stdin
python "SAFE_EDIT_SCRIPT" append --file F --text-base64 B64
python "SAFE_EDIT_SCRIPT" replace-lines --file F --start N --end M --text-stdin
python "SAFE_EDIT_SCRIPT" replace-lines --file F --start N --end M --text-base64 B64
python "SAFE_EDIT_SCRIPT" replace-lines --file F --anchor-pattern X --offset-start A --offset-end B --text-stdin
python "SAFE_EDIT_SCRIPT" replace-lines --file F --anchor-pattern X --offset-start A --offset-end B --text-base64 B64

# Replace text
python "SAFE_EDIT_SCRIPT" edit --file F --old "old" --new "new" --expected-count 1

# Replace with auto-match (tolerate whitespace)
python "SAFE_EDIT_SCRIPT" edit --file F --old "old" --new "new" --auto-match --expected-count 1

# Preview before applying
python "SAFE_EDIT_SCRIPT" edit --file F --old "old" --new "new" --dry-run --diff

# Replace function/class body (anchor)
python "SAFE_EDIT_SCRIPT" replace-lines --file F --anchor-pattern "sig" --offset-start A --offset-end B --text T

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

# Existing payload files (when they already exist)
python "SAFE_EDIT_SCRIPT" edit --file F --old-file old.txt --new-file new.txt
python "SAFE_EDIT_SCRIPT" replace-lines --file F --anchor-pattern "X" --offset-start A --offset-end B --text-file body.txt

# Multi-block edits (2+ edits in one file)
python "SAFE_EDIT_SCRIPT" edit --file F --diff-input-file diff.txt
python "SAFE_EDIT_SCRIPT" edit --file F --diff-input-stdin
python "SAFE_EDIT_SCRIPT" edit --file F --diff-input-base64 B64
```

---

## Command Selection

Handle whole-file removal separately: only an explicit deletion request may select
`remove-file`, and it must follow the `stat` + workspace-root + SHA-256 workflow.

```
Need modification?
│
├─ Target does not exist and is a required task artifact?
│      → create --to-encoding ENC --to-line-ending EOL --text-{stdin|base64}
│      → Never use create for temporary scripts, patch tools, or payload bootstrapping
│
├─ Replace exact text (single/multiline)?
│      → edit --old X --new Y --expected-count 1
│
├─ Replace function/class body?
│   │
│   ├─ Anchor pattern unique?
│   │      YES → replace-lines --anchor-pattern "unique_sig" --offset-start A --offset-end B --text T
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
regex                       ← HIGHEST EDIT RISK
```

`remove-file` is outside the edit hierarchy because it is destructive.

---

## Core Rules

1. **Create only requested task artifacts** — `create` is for source, test, configuration, documentation, and other deliverables required by the task. It does not authorize temporary scripts, patch tools, or payload files. The target must not exist, the parent directory must already exist, and both `--to-encoding` and `--to-line-ending` are mandatory.

2. **Remove only explicitly requested files** — run `stat --json` immediately before removal, then pass its `sha256` with the exact workspace root. `remove-file` is limited to one regular file, refuses directories and symbolic links, and never accepts recursion or wildcards. Prefer `--dry-run --json` when the target is uncertain.

3. **Always add `--expected-count 1` for normal text replacement** — prevents wrong matches from silently succeeding. Do not omit it unless intentionally targeting multiple matches, performing diagnosis, or using commands with their own matching semantics.

4. **Do not send complex content through literal argv** — for multiline content, >100 characters, or shell-sensitive characters (`$`, `%`, `!`, `\`, `` ` ``, `'`, `"`), prefer `--ops-stdin` when the execution tool provides native stdin. Otherwise use URL-safe UTF-8 Base64 via `--ops-base64`, `--diff-input-base64`, or `--text-base64`.

5. **Do not bootstrap payload files outside this protocol** — use a `--*-file` option only when that payload file already exists or was created through its own authorized edit workflow. The need for a payload file never authorizes shell redirection or an ad-hoc writer.

6. **Use URL-safe Base64 for Windows argv** — unpadded URL-safe Base64 avoids quotes, whitespace, `+`, and `/`. The CLI also accepts padded and standard Base64, and always decodes the result as strict UTF-8.

7. **Use `--auto-match` for multiline edits** — auto-tries: exact → ignore-eol → ignore-indent → normalize-whitespace.

8. **Use `edit` over `replace-lines`** — `edit` is safest. Use `replace-lines` only when `edit` cannot do the job.

9. **Re-read and validate after edits** — successful execution confirms only the payload received by `safe_edit.py`. For literal argv, re-read or compile/test to verify intent. Base64/stdin transports substantially reduce this risk.

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
# Preferred when native stdin is available
python "SAFE_EDIT_SCRIPT" edit --file F --diff-input-stdin

# Preferred argv fallback
python "SAFE_EDIT_SCRIPT" edit --file F --diff-input-base64 B64

# Use only when diff.txt already exists
python "SAFE_EDIT_SCRIPT" edit --file F --diff-input-file diff.txt
```

---

## Windows Payload Transport

PowerShell and the Windows native argv layer can rewrite quotes before Python receives them. MSYS2/Git Bash can additionally convert leading `/` and `//` as paths. `safe_edit.py` cannot reconstruct the caller's original intent after that transformation.

Use this order:

1. Native execution-tool stdin with `--ops-stdin`, `--diff-input-stdin`, or `--text-stdin`.
2. Unpadded URL-safe UTF-8 Base64 with `--ops-base64`, `--diff-input-base64`, or a field-specific `--*-base64`.
3. An existing payload file through `--*-file`.
4. Literal `--old`/`--new`/`--text` only for short, shell-insensitive text.

For exact replacement, one stdin stream cannot carry both `old` and `new` independently. Use a batch JSON envelope:

```json
[
  {
    "op": "edit",
    "old": "original text",
    "new": "replacement text",
    "expected_count": 1
  }
]
```

Pass that JSON through `batch --ops-stdin`, or encode the entire JSON document as URL-safe UTF-8 Base64 and pass it through `batch --ops-base64 B64`.

Do not put a PowerShell here-string or quoted source literal into the command merely to feed stdin; that still relies on shell parsing. When the execution tool has no native stdin field, use Base64.

For short literal arguments under MSYS2, `MSYS2_ARG_CONV_EXCL="*"` remains a fallback. `safe-edit` emits a `"warnings"` field in JSON output when it detects likely MSYS2 path corruption.

---

## Structural Editing Rules

1. **Avoid `insert` near closing braces** — `insert --line N` inserts BEFORE line N. For `}` boundaries, use `replace-lines --start N --end N --text "}\nnew_content"`.

2. **Verify anchor uniqueness** — Search for the pattern first. If it appears multiple times, use a more specific pattern or `--start`/`--end`.

3. **Line numbers invalidate after edits** — Re-query locations after any operation that changes line count.

4. **Bracket safety** — Verify the range includes/excludes braces as intended.

5. **Validate after structural edits** — Check bracket matching or compile immediately.

---

For full option list: `python "SAFE_EDIT_SCRIPT" --help`
