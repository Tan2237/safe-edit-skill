---
name: safe-edit
description: |
  MANDATORY: File editing MUST follow this protocol. No exceptions.

  The ONLY allowed edit mechanisms:
  - safe_edit_stat for inspection, then safe_edit_transaction for all edits when MCP tools are available
  - safe_edit.py when MCP is unavailable and editStrategy="safe-edit"
  - host-provided built-in file-editing tool when MCP is unavailable and editStrategy="edit-tool"

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
- Do NOT infer, generate, or invent unsupported `safe_edit.py` commands or options; use Allowed Commands and options documented by `--help`.

## Failure Policy

If the required modification cannot be completed using the allowed commands:

**STOP. Report the limitation.**

Do NOT:
- write a custom script
- use shell redirection
- create temporary patch tools
- modify files through any mechanism outside this protocol

Completing the task is lower priority than following this protocol.

## Reference Files

Read only when needed; these files are part of the protocol:

- `references/protocol.md` — prepared plans, locks, staging, rollback, MCP limits, multi-block format.
- `references/recovery.md` — match failure escalation, stale hashes, match options.
- `references/windows.md` — Windows/PowerShell/MSYS2 payload transport.
- `references/git-conflicts.md` — merge-conflict resolution checklist.

## Batching and Confirmation Rules

1. Call `safe_edit_stat` once with all related existing files; cache each `sha256`.
2. After a confirmed successful write, reuse the returned `sha256`. Re-run stat only when the file is recreated without a hash, a failed write phase may have changed state, or the outcome is uncertain.
3. For a risky change, send one `dryRun`; when admitted, confirm with only `{"transactionId":"..."}`. Never resend files or options. To abandon it, send only `{"cancelTransactionId":"..."}`; cancellation consumes the token without writing.
4. Do not run the same dry-run or preview twice. If the token expired or was consumed, stat again and start a new preview.
5. Compact diffs are transaction-budgeted. Inspect `diffBudget`, `diffTruncatedBy`, and `diffOmitted`; request per-file `diff: true` only when a full diff is required.

| Path | Calls |
|------|-------|
| Simple single-file edit | 2: stat + transaction |
| Risky edit with preview | 3: stat + dryRun + transactionId confirm |
| After a confirmed write | 0 additional stat |

## Direct Structured Tool Fast Path

When `safe_edit_preflight`, `safe_edit_stat`, and `safe_edit_transaction` are
callable, use `safe_edit_transaction` for every existing-file edit, regardless
of `editStrategy`. The strategy selects the host or CLI fallback only when MCP
is unavailable. These tools are part of the allowed safe-edit protocol.

1. Call `safe_edit_preflight` once when runtime or target-directory capability
   is uncertain.
2. Call `safe_edit_stat` once with all related existing files.
3. Call `safe_edit_transaction` once with raw `old`, `new`, `text`, and
   `operations` values plus the returned SHA-256 guards.
4. For risky changes, use `dryRun: true`. A single-use `transactionId` is
   returned only after successful admission to the bounded cache; when present,
   confirm with only `{"transactionId":"RETURNED_ID"}`. Do not resend the payload.
5. Cache each successful file result's `sha256` as the guard for its next edit.
   Do not re-run stat after a confirmed successful write.

The server is long-lived: it imports the editing core and builds its parser only
once. Successfully admitted dry-runs receive single-use transaction IDs valid for
up to 10 minutes. Admission is bounded to 32 tokens, 128 MiB total, and 64 MiB
per token; a full cache rejects a new dry-run rather than evicting an unexpired
token. Cancel abandoned previews with `cancelTransactionId`. The cache normally
retains an immutable prepared plan; plans above 16 MiB of prepared output use
bounded JSON fallback storage and are prepared again at confirmation. Batch related files so
random per-parent short-lived best-effort capability observations, request validation, and lock coordination are shared. Do not
JSON-stringify, Base64-encode, or create payload files for these tool calls.
Dry-runs include compact diffs by default; set per-file `diff: true` only when a
full structured diff is needed. Above 256 KiB, compatibility `content` retains a
transaction-budgeted compact diff while `structuredContent` keeps the full result. Large batches of independent exact replacements use a single
scan only when equivalence to ordered per-operation semantics is strictly proven;
otherwise they automatically fall back to the ordinary path. ASCII space/tab trim uses staged short-run passes, with regex for long or residual patterns.

The MCP boundary is explicit: a JSON-RPC wire request is at most 64 MiB.
`maxBytes` is a per-file input/output limit, defaulting to 50 MiB and capped at
128 MiB. Transaction input and output each total at most 128 MiB, with at most
160 MiB retained by a prepared plan. A call may contain at most 128 files, 256
operations per file, 1,024 operations total, and 128 JSON nesting levels. The current stable protocol is `2025-11-25`; the server also
supports `2025-06-18` and `2024-11-05`, but does not claim the transitional
`2025-03-26` batch protocol. Invalid fields, types, hashes, limits, or execution
state for a recognized tool return a normal `tools/call` result with
`isError: true`. Malformed JSON-RPC envelopes, invalid request IDs, unknown tool
names, and non-object `params` or `arguments` return JSON-RPC errors instead.

Every structured result includes a random per-process `serverInstanceId`, and new
confirmation tokens bind their issuer instance into the `transactionId`. A
confirmation routed to another MCP process returns
`failureReason: transaction_server_instance_mismatch` with
`transactionIdConsumed: false`; run a new dry-run in that tool session.
Unexpected recognized-tool failures remain normal `isError: true` tool results
instead of opaque JSON-RPC `-32603` responses. They report a content-safe
`incidentId`, `failureStage`, exception type, `writeAttempted`, `written`,
`partialWrite`, `outcomeUncertain`, `transactionIdConsumed`,
`statRequired`, and `recommendedAction`. When `statRequired` is true, stat
every target before retrying; when `transactionIdConsumed` is true, never reuse
that token. The matching stderr diagnostic uses the same incident and server
instance IDs and includes code locations plus safe OS error metadata, but omits
request payloads, file contents, transaction tokens, and arbitrary exception
messages.

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
Python startup and random per-parent capability observations are shared; one temp probe supplies temp/lock flags:

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

The target must not exist and its parent directory must already exist. `create` never overwrites and never creates parent directories. After creation, use the successful result's `sha256` for any later edit.

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
structured path tries an exact match first, then automatically retries only logical
LF/CRLF/CR differences in multiline targets or contexts. For a risky request, send
it once with `dryRun: true`; confirm with only the returned `transactionId` when
bounded-cache admission succeeds. The
structured path avoids shell parsing, Base64 expansion, Windows argv limits, and
per-call Python startup; native binding metadata is loaded lazily and cached per process, while handles, buffers, and error state remain per-call. Otherwise use `transaction --request-stdin` only with a
native stdin field; fall back to an existing request file, then URL-safe UTF-8
Base64.


The latest trusted hash is authoritative for the lifetime of the file. Initially it
comes from stat; after a successful transaction, replace it with that file result's
`sha256`. Re-run stat only when the file is recreated without a returned hash, a failed
write-phase invocation may have changed state, or the execution outcome is
uncertain. Do not re-run it after a confirmed successful write, when the shell/tool
rejects the command before process start, or when argument parsing fails before
target access.

For `stat`, `inspect`, `edit`, and `transaction`, a `hash_mismatch` error
already carries the current hash: use its `actualSha256` (equivalently
`retryStrategy.expectedSha256`) as the next `expectedSha256` instead of
re-running stat, and re-validate the edit context because the file changed.
Never apply this shortcut to `remove-file`: re-read and reconfirm the changed
file before deletion. A `create` failure on an existing file may stream at most 50 MiB in bounded chunks to return the
existing file's `actualSha256`, but it must not be converted automatically into
an edit; inspect the existing file first.

The selected editStrategy is locked for the lifetime of the file. Do NOT switch to another edit mechanism after a command failure. Continue using the cached editStrategy.

- `editStrategy: "safe-edit"` → When MCP is unavailable, ALL edits on this file MUST use `safe_edit.py`. Do not switch back to the host-provided built-in file-editing tool, whatever it is named.
- `editStrategy: "edit-tool"` → When MCP is unavailable, use the host-provided built-in file-editing tool available in the current environment; its actual name may vary.

When all three structured tools are callable, both strategies use `safe_edit_transaction`.

Resolve the Python executable before editing: prefer a runtime path supplied by
the host, then `python`, then `py -3` on Windows. Run `preflight --json`
before a related edit set or when runtime/transport support is uncertain. If no
Python runtime can execute the script, stop before modifying any file.

---

## Allowed Commands

`safe_edit_preflight`, `safe_edit_stat`, and `safe_edit_transaction` are the
preferred permitted tool calls. The commands below are allowed CLI fallback
patterns. Options documented by `--help` may be combined when applicable and
consistent with the payload rules; unsupported commands or options are prohibited.

```bash
# List supported commands and options
python "SAFE_EDIT_SCRIPT" --help

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

3. **Always add `--expected-count 1` for normal text replacement** — prevents wrong matches from silently succeeding. Copy `old` verbatim from the latest read and prefer the shortest stable fragment that is unique. Add short `context_before` or `context_after` only when the target itself is duplicated. Do not omit the count unless intentionally targeting multiple matches, performing diagnosis, or using commands with their own matching semantics.

4. **Keep complex content structured** — send multiline, large, or shell-sensitive values directly through `safe_edit_transaction`. On the CLI fallback, prefer `--ops-stdin` only when the execution tool provides native stdin; otherwise use URL-safe UTF-8 Base64.

5. **Do not bootstrap payload files outside this protocol** — use a `--*-file` option only when that payload file already exists or was created through its own authorized edit workflow. The need for a payload file never authorizes shell redirection or an ad-hoc writer.

6. **Use URL-safe Base64 only for CLI fallback** — unpadded URL-safe Base64 avoids quotes, whitespace, `+`, and `/`, but expands payloads and remains subject to argv limits. Never Base64-encode data for the structured tools.

7. **Use transaction EOL matching first** — structured and CLI transactions try exact matching first, then automatically tolerate only LF/CRLF/CR differences in multiline targets or contexts, including mixed-EOL files. They do not broaden a count mismatch. Use `--auto-match` only when indentation or prose line-wrap differences require broader whitespace relaxation.

8. **Use `edit` over `replace-lines`** — `edit` is safest. Use `replace-lines` only when `edit` cannot do the job.

9. **Use transactions for related files** — obtain initial hashes with one `safe_edit_stat` call (or CLI `stat-many` fallback), require `expectedSha256` on every edit request, and include controlled creates in the same transaction. Use returned `sha256` values for later transactions. Treat `atomicity: prevalidated-with-rollback` as conflict-safe best-effort rollback, require `crashAtomic: false`, inspect every rollback status field and recovery artifact, and never assume crash-atomicity.

10. **Treat explicit no-ops as skipped** — `old == new` returns `skipped: true` with `reason: old_equals_new`; do not retry it.

11. **Re-read and validate after edits** — direct create/edit uses strict 1 MiB-chunk byte/EOF comparison; success still confirms only the payload received by the safe-edit core. Re-read or compile/test to verify intent.

12. **Protect the hot path** — prefer one batched stat and one batched transaction. For previews, confirm the returned transaction ID when successful cache admission issues one instead of resending the payload. Do not split related files into repeated tool calls, rebuild the parser, spawn helper processes, or serialize an already-structured request again.

---

## Structural Editing Rules

1. **Avoid `insert` near closing braces** — `insert --line N` inserts BEFORE line N. For `}` boundaries, use `replace-lines --start N --end N --text "}\nnew_content"`.

2. **Verify anchor uniqueness** — Search for the pattern first. If it appears multiple times, use a more specific pattern or `--start`/`--end`.

3. **Line numbers invalidate after edits** — Re-query locations after any operation that changes line count.

4. **Bracket safety** — Verify the range includes/excludes braces as intended.

5. **Validate after structural edits** — Check bracket matching or compile immediately.

---

For full option list: `python "SAFE_EDIT_SCRIPT" --help`
