# Git conflict resolution checklist

Read this file when a file contains merge-conflict markers. Removing markers is
not the same as completing a semantic merge.

1. Inspect both sides before editing: compare the stage 2 ("ours") and stage 3
   ("theirs") versions, and trace the functions or symbols that call each side.
2. Decide by complete function or logical unit: keep, merge, or delete a whole
   function. Do not resolve hunk-by-hunk by guessing.
3. Check callers, imports, tests, and related symbols before deleting an old
   entry point. A resolved block can still leave a broken caller behind.
4. Batch all conflicted files into one transaction. Use exact `old` fragments
   with `expected_count: 1`; do not use fuzzy or auto-match to resolve
   conflict markers.
5. For a risky merge, use one dry-run with per-file `diff: true` when a full
   diff is needed, review it, then confirm only with its `transactionId`.
6. Immediately after removing markers, build, compile, and run the targeted
   tests. A file with no markers can still be semantically wrong.
7. On `rollbackConflict`, `hash_mismatch`, or uncertain write state, re-read the
   affected file and revalidate the context. Do not infer success from the
   absence of an error message.
