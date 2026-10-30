# Protocol and transaction internals

Read this file when you need to reason about prepared plans, locking, rollback,
MCP limits, or error envelopes. It does not replace the hard rules in
`SKILL.md`.

## MCP boundary

- A JSON-RPC wire request is at most 64 MiB. `maxBytes` is a per-file
  input/output limit, defaulting to 50 MiB and capped at 128 MiB.
- Transaction input and output each total at most 128 MiB; a prepared plan
  retains at most 160 MiB. A call has at most 128 files, 256 operations per
  file, 1,024 operations total, and 128 JSON nesting levels.
- The current stable protocol is `2025-11-25`; the server also supports
  `2025-06-18` and `2024-11-05`, but not the transitional `2025-03-26` batch
  protocol.
- Invalid fields, types, hashes, limits, or execution state for a recognized
  tool return a normal `tools/call` result with `isError: true`. Malformed
  JSON-RPC envelopes, invalid request IDs, unknown tool names, and non-object
  `params` or `arguments` return JSON-RPC errors.
- Every structured result includes a random per-process `serverInstanceId`.
  Confirmation tokens bind their issuer instance. A token routed to another MCP
  process returns `failureReason: transaction_server_instance_mismatch` with
  `transactionIdConsumed: false`; run a new dry-run in that session.
- Unexpected recognized-tool failures return `incidentId`, `failureStage`,
  `writeAttempted`, `written`, `partialWrite`, `outcomeUncertain`,
  `transactionIdConsumed`, `statRequired`, and `recommendedAction`. When
  `statRequired` is true, stat every target before retrying. When
  `transactionIdConsumed` is true, never reuse that token.

## Prepared plans

- Both structured and CLI paths build an immutable prepared plan before commit.
- A successful dry-run may return a single-use `transactionId` valid for up to
  10 minutes. Admission is bounded to 32 tokens, 128 MiB total, and 64 MiB per
  token; a full cache rejects rather than evicting an unexpired token. Send only
  `cancelTransactionId` to release an abandoned preview without writing.
- Confirmation acquires cooperative locks in stable order, then jointly
  revalidates canonical paths, parent/target identities, and input hashes.
- A retained prepared plan does not repeat decoding or matching. Plans above
  16 MiB of prepared output use bounded JSON fallback storage and are prepared
  again at confirmation.
- Full diffs remain in `structuredContent`. Above 256 KiB, compatibility
  `content` retains a transaction-budgeted compact diff and truncation markers.

## Staging and rollback

- Each output is written to a random hidden sibling stage under its pinned
  parent, completely written, file-fsynced, and verified.
- An edit no-replace claims the current basename into a random quarantine,
  verifies identity, SHA-256, mode, size, and mtime, then no-replace installs the
  complete stage. A create only installs a complete verified stage.
- The target basename can be briefly absent between the two no-replace
  operations.
- Rollback claims the transaction output into another quarantine, verifies that
  generation, and only then restores the original or removes an owned create.
  It never overwrites an unknown or external target.
- Rollback requires Linux `renameat2(RENAME_NOREPLACE)`, macOS
  `renameatx_np(RENAME_EXCL)`, or non-replacing Windows `MoveFileExW`, plus
  filesystem support. Missing primitives fail closed.
- An in-memory journal entry is created before the first staging syscall for
  each file. It advances through `ATTEMPT_*` phases before state-changing
  syscalls. Recovery probes endpoints and rolls proven mutations back in reverse
  order.
- Original quarantines enter `ATTEMPT_FINALIZE` only after all planned mutations
  and the final parent validation sweep succeed. Cleanup problems appear in
  `cleanupWarnings`.
- Inspect `written`, `rolledBack`, `partialWrite`, `rollbackConflict`, and
  `rollbackErrors` for the real result. `crashAtomic` is `false`: no multi-file
  commit is guaranteed across process crashes, power loss, or filesystems.
- Recovery retains stage and quarantine artifacts when ownership cannot be
  proven. An artifact label uses the fixed format
  `artifact basename=...; pinned parent identity=(device=..., inode=...,
  file_type=...); best-effort path=...`.

## Concurrency boundaries

- No-replace publication and generation verification conflict safely with
  ordinary concurrent target-basename writers.
- They do not promise isolation from deliberate same-permission hijacking of
  random internal transaction names, mutation through an already-open writable
  descriptor, or mutation through another hardlink.
- POSIX has no portable unlink-by-inode CAS, so uncertain internal-name cleanup
  must retain and report the artifact.
- Direct single-file atomic replacement uses cooperative locking, not strict
  basename CAS. Use `transaction` / `safe_edit_transaction` for the strongest
  concurrency checks.
- Protocol 2 retains kernel stripes for the whole edit lifetime and keeps legacy
  markers in the protocol-1 namespace. Mixed protocol-1 operation retains the
  old protocol's narrow race window.

## Multi-block edits

For two or more edits in one file, use diff-input:

```text
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
python "SAFE_EDIT_SCRIPT" edit --file F --diff-input-stdin
python "SAFE_EDIT_SCRIPT" edit --file F --diff-input-base64 B64
python "SAFE_EDIT_SCRIPT" edit --file F --diff-input-file EXISTING_DIFF_FILE
```

Use `--diff-input-file` only when that file already exists or was created
through its own authorized edit workflow.
