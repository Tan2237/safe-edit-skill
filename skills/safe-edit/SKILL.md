---
name: safe-edit
description: |
  MANDATORY: File editing MUST follow this protocol. No exceptions.

  The ONLY allowed edit mechanisms:
  - safe_edit_stat and safe_edit_transaction (preferred when available)
  - safe_edit.py (CLI fallback for editStrategy="safe-edit")
  - built-in Edit tool (for editStrategy="edit-tool")

  Before modifying an existing file, inspect it once and cache editStrategy
  plus sha256. Batch related files in one stat and one transaction. Use
  controlled create for required new artifacts. Remove a file only when the
  user explicitly requests deletion.

  Re-run stat only after file recreation, a failed write-phase invocation, or
  an uncertain execution outcome. Any other edit mechanism is forbidden.
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

## Direct Structured Tool Fast Path

When `safe_edit_preflight`, `safe_edit_stat`, and `safe_edit_transaction` are
callable, use them instead of transporting source text through a shell command.
They are part of the allowed safe-edit protocol.

1. Call `safe_edit_preflight` once when runtime or target-directory capability
   is uncertain.
2. Call `safe_edit_stat` once with all related existing files.
3. Call `safe_edit_transaction` once with raw `old`, `new`, `text`, and
   `operations` values plus the returned SHA-256 guards.
4. Use `dryRun: true` in the same structured tool for risky previews.

The server is long-lived: it imports the editing core and builds its parser only
once. Batch related files so filesystem probes, request validation, and lock
coordination are shared. Do not JSON-stringify, Base64-encode, or create payload
files for these tool calls.

The CLI sections below are the fallback when the structured tools are not
available.

---

## Runtime Script Resolution

Before invoking any command, resolve `SAFE_EDIT_SCRIPT` once:

1. Start from the absolute path of this `SKILL.md` supplied by the skill loader.
2. Set `SAFE_EDIT_SCRIPT` to the sibling file `safe_edit.py` in the same directory.
3. Convert it to an absolute path and verify that it exists before the first invocation.
4. Reuse that exact absolute path for every `safe_edit.py` command in the current task.

`SAFE_EDIT_SCRIPT` in the examples below is a placeholder for that resolved absolute path. It is not a shell environment variable and must not be passed literally.

Never assume `safe_edit.py` is in the current working directory, never resolve it relative to the target file, and never search the whole filesystem. If the sibling script is missing, **STOP** and report both the resolved `SKILL.md` path and the expected script path.

## Required Workflow

Before the first edit or removal of each existing file, prefer the structured
batch stat tool:

```text
safe_edit_stat({"files":["F"]})
```

CLI fallback:

```bash
python "SAFE_EDIT_SCRIPT" stat --file F --json
```

For two or more related existing files on the CLI fallback, use one request so
Python startup and filesystem capability probes are shared:

```json
{"files":["src/a.py",{"file":"src/b.py","encoding":"utf-8"}]}
```

```bash
python "SAFE_EDIT_SCRIPT" stat-many --request-stdin --json
```

The returned `files` array contains the same per-file `editStrategy` and
`sha256` fields as individual `stat` calls.

For an explicitly requested file deletion, pass the returned `sha256` and the
containing workspace root to `remove-file`. Never remove a directory or
symbolic link.

A new file cannot be inspected first. Create it only when it is a requested task artifact:

```bash
python "SAFE_EDIT_SCRIPT" create --file F --to-encoding utf-8 --to-line-ending lf --text-base64 B64
```

The target must not exist and its parent directory must already exist. `create` never overwrites and never creates parent directories. After creation, run `stat` before any later edit.

For one new file, the structured request may be exactly
`{"file":"new.txt","text":"...","encoding":"utf-8","lineEnding":"lf"}`;
`action: "create"` is inferred. For related edits, use a `files` list:

```json
{
  "files": [
    {
      "file": "existing.py",
      "action": "edit",
      "expectedSha256": "SHA256_FROM_STAT",
      "operations": [
        {"op": "edit", "old": "before", "new": "after", "expected_count": 1}
      ]
    },
    {
      "file": "new.py",
      "action": "create",
      "text": "print('ready')\n",
      "encoding": "utf-8",
      "lineEnding": "lf"
    }
  ]
}
```

Pass this object directly to `safe_edit_transaction` when it is callable. The
structured path avoids shell parsing, Base64 expansion, Windows argv limits,
and per-call Python startup. Otherwise use `transaction --request-stdin` only
with a native stdin field; fall back to an existing request file, then URL-safe
UTF-8 Base64. Both paths lock targets in stable order, prevalidate every file,
and roll back completed writes on failure. Neither claims crash-atomicity
across multiple files.

The stat result is authoritative for the lifetime of the file. Re-run it only when the file is recreated, a failed `safe_edit.py` invocation may have reached the write phase, or the execution outcome is uncertain. Do not re-run it when the shell/tool rejects the command before process start, or when argument parsing fails before target access.

The selected editStrategy is locked for the lifetime of the file. Do NOT switch to another edit mechanism after a command failure. Continue using the cached editStrategy.

- `editStrategy: "safe-edit"` → Use `safe_edit_transaction` when callable; otherwise ALL edits on this file MUST use `safe_edit.py`. Do not switch back to built-in Edit.
- `editStrategy: "edit-tool"` → Use built-in Edit tool for this file.

Resolve the Python executable before editing: prefer a runtime path supplied by
the host, then `python`, then `py -3` on Windows. Run `preflight --json`
before a related edit set or when runtime/transport support is uncertain. If no
Python runtime can execute the script, stop before modifying any file.

---

## Allowed Commands

`safe_edit_preflight`, `safe_edit_stat`, and `safe_edit_transaction` are the
preferred permitted tool calls. The commands below are the ONLY CLI fallback
invocations; any unlisted CLI invocation is prohibited.

```bash
# Check runtime, stdin/Base64 support, temp storage, locks, and target writability
python "SAFE_EDIT_SCRIPT" preflight --json
python "SAFE_EDIT_SCRIPT" preflight --file F --json

# Before editing or removing any existing file (mandatory first step)
python "SAFE_EDIT_SCRIPT" stat --file F --json

# Inspect a related multi-file set in one process
python "SAFE_EDIT_SCRIPT" stat-many --request-stdin --json
python "SAFE_EDIT_SCRIPT" stat-many --request-file EXISTING_JSON --json
python "SAFE_EDIT_SCRIPT" stat-many --request-base64 B64 --json

# Prevalidate and apply a related multi-file request with rollback on failure
python "SAFE_EDIT_SCRIPT" transaction --request-stdin --json
python "SAFE_EDIT_SCRIPT" transaction --request-file EXISTING_JSON --json
python "SAFE_EDIT_SCRIPT" transaction --request-base64 B64 --json
python "SAFE_EDIT_SCRIPT" transaction --request-stdin --dry-run --json

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
python "SAFE_EDIT_SCRIPT" edit --file F --old "old" --new "new" --auto-match --fuzzy --expected-count 1

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

4. **Keep complex content structured** — send multiline, large, or shell-sensitive values directly through `safe_edit_transaction`. On the CLI fallback, prefer `--ops-stdin` only when the execution tool provides native stdin; otherwise use URL-safe UTF-8 Base64.

5. **Do not bootstrap payload files outside this protocol** — use a `--*-file` option only when that payload file already exists or was created through its own authorized edit workflow. The need for a payload file never authorizes shell redirection or an ad-hoc writer.

6. **Use URL-safe Base64 only for CLI fallback** — unpadded URL-safe Base64 avoids quotes, whitespace, `+`, and `/`, but expands payloads and remains subject to argv limits. Never Base64-encode data for the structured tools.

7. **Use `--auto-match` for multiline edits** — auto-tries: exact → ignore-eol → ignore-indent → normalize-whitespace.

8. **Use `edit` over `replace-lines`** — `edit` is safest. Use `replace-lines` only when `edit` cannot do the job.

9. **Use transactions for related files** — obtain hashes with one `safe_edit_stat` call (or CLI `stat-many` fallback), require `expectedSha256` on every edit request, and include controlled creates in the same transaction. Treat `atomicity: prevalidated-with-rollback` as process-level rollback, not crash-atomicity.

10. **Re-read and validate after edits** — successful execution confirms only the payload received by the safe-edit core. Re-read or compile/test to verify intent.

11. **Protect the hot path** — prefer one batched stat and one batched transaction. Do not split related files into repeated tool calls, rebuild the parser, spawn helper processes, or serialize an already-structured request again.

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
| `--fuzzy` | Fuzzy matching (≥0.6); ignores per-line boundary whitespace, EOL style, and final EOL | AI-generated approximate text |
| `--fuzzy-workers auto\|N` | Conditional low-priority fuzzy processes; `1` forces serial, `N` is 2–8 | Large CPU-heavy fuzzy searches |
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

1. `safe_edit_stat` and `safe_edit_transaction` with raw structured arguments.
2. Native execution-tool stdin with `--request-stdin`, `--ops-stdin`, `--diff-input-stdin`, or `--text-stdin`.
3. An existing payload file through `--request-file` or another `--*-file`.
4. Unpadded URL-safe UTF-8 Base64 with `--request-base64`, `--ops-base64`, `--diff-input-base64`, or a field-specific `--*-base64`.
5. Literal `--old`/`--new`/`--text` only for short, shell-insensitive text.

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
