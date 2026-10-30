# Recovery and match escalation

Read this file when a match fails, a hash is stale, or a transaction reports a
rollback conflict. The hard rules in `SKILL.md` still apply.

## Match failure escalation

```text
edit --old X --new Y --expected-count 1
|
+- SUCCESS -> Done
|
+- FAIL -> Read the structured recovery fields:
    |
    +- failureClass = RETRYABLE
    |   +- every edit has expected_count -> merge argumentsPatch into one dryRun
    |   +- successful preview -> review it, then confirm only its transactionId
    |   +- missing count or unsafe preview -> re-read; do not broaden the transaction
    |
    +- rootCause = count_mismatch
    |   +- compare expectedCount/actualCount and re-read; do not add context automatically
    |
    +- rootCause = multiple_matches
    |   +- keep expected_count; add the shortest unique context_before/after
    |
    +- failureStage = context_filter
    |   +- use contextField, re-read that candidate window, refresh only stale context
    |
    +- RE_READ_REQUIRED / USER_INPUT
        +- re-read or ask; do not jump directly to fuzzy matching
```

When `writeAttempted` is false, a prepare-stage retry does not require another
stat. Because `argumentsPatch` is transaction-wide, merge it only when every
edit operation has an explicit `expected_count`, force that retry to
`dryRun: true`, review the preview, then confirm only its `transactionId`.
Otherwise re-read instead of broadening the request. Use
`--explain-match-failure` when no structured patch is available.

## Stale hashes

For `stat`, `inspect`, `edit`, and `transaction`, a `hash_mismatch` error
already carries the current hash. Use its `actualSha256` (equivalently
`retryStrategy.expectedSha256`) as the next `expectedSha256`, and re-validate the
edit context because the file changed.

Never apply this shortcut to `remove-file`: re-read and reconfirm the changed
file before deletion. A `create` failure on an existing file may stream at most
50 MiB in bounded chunks to return the existing file's `actualSha256`, but it
must not be converted automatically into an edit; inspect the existing file
first.

## Match options

| Option | Effect | When to use |
|--------|--------|-------------|
| transaction `autoEolMatch` | Exact first, then EOL-only matching for multiline targets and contexts | Default for transactions |
| `--auto-match` | Auto-try: exact -> ignore-eol -> ignore-indent -> normalize-whitespace | Indentation or prose line-wrap drift; pair with `expected_count` and `dryRun` |
| `--fuzzy` | Fuzzy matching (>=0.6); ignores per-line boundary whitespace, EOL style, and final EOL | Last-resort approximate text after re-reading |
| `--fuzzy-workers auto\|N` | Conditional low-priority fuzzy processes; `1` forces serial, `N` is 2-8 | Large CPU-heavy fuzzy searches |
| `--normalize-whitespace` | Collapse whitespace | Explicit prose/formatting recovery, not a structured-data default |
| `--context-before/after` | Disambiguate matches using the active EOL strategy | Only when the target occurs more than once |

| Content | Default flags |
|---------|---------------|
| Unique single line | none |
| Multiline code | transaction `autoEolMatch` (default) |
| Prose with line-wrap drift | `--auto-match --dry-run --expected-count 1` |
| JSON/YAML | exact safe-edit matching; re-read on drift, do not normalize whitespace by default |

## Transaction failure state

When publish, inspection, rollback, or finalization state cannot be proven,
recovery retains stage and quarantine artifacts and does not intentionally
delete unverified or unknown objects. Inspect `written`, `rolledBack`,
`partialWrite`, `rollbackConflict`, `rollbackErrors`, and `cleanupWarnings`.
`crashAtomic` is `false`; never assume crash-atomicity.
