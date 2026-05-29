---
name: safe-edit
description: Safely edit existing text, source, and config files through one cross-platform Python script while preserving encoding, BOM, newline style, permissions, and atomic-write integrity. Use for literal replacements, line insertions, and line deletions when avoiding mojibake, truncated files, silent no-ops, or noisy Git diffs matters; prefer this over ad-hoc cat, Set-Content, sed, or regex writes.
---

# safe-edit

Use `safe_edit.py` as the single implementation and single documented entry point. Do not maintain shell-specific editing logic; the Python script is the portable interface for Windows, Linux, and macOS.

## Entry Point

```bash
python safe_edit.py edit --file path/to/file --old "foo" --new "bar" --expected-count 1
python safe_edit.py insert --file path/to/file --line 10 --text "new line"
python safe_edit.py delete --file path/to/file --line 10
python safe_edit.py edit --file path/to/file --old "foo" --new "" --dry-run
```

On Windows, `py -3 safe_edit.py ...` is also acceptable when `python` is not on `PATH`.

## Operating Rules

1. Use this skill for existing text files where encoding, BOM, line endings, permissions, or Git diff cleanliness matters.
2. Use `edit` only for literal text replacement. It is not regex replacement.
3. Treat any nonzero exit as "file unchanged"; the script writes a same-directory temp file, atomically replaces the target, and verifies bytes after writing.
4. Prefer `--expected-count N` for replacements so wrong matches fail loudly.
5. Run `--dry-run` before risky edits to inspect detected encoding, newline style, and match count without writing.
6. Use `--backup` when the user wants a retained timestamped `.bak` copy.
7. Do not use this skill for binary files, huge generated files, symlinks, or non-text formats unless the user explicitly accepts those risks.

## Commands

- `edit --file FILE --old TEXT --new TEXT`: replace literal text. Empty `--new ""` is allowed; empty `--old ""` is refused.
- `insert --file FILE --line N --text TEXT`: insert before 1-based line `N`. Multi-line text is allowed.
- `delete --file FILE --line N`: delete 1-based line `N`.

## Guarantees

- Detects and preserves `utf-8`, `utf-8-bom`, `gbk`, `utf-16-le`, and `utf-16-be`.
- Preserves CRLF, LF, or CR newline style for generated separators; unchanged line separators remain untouched where the edit does not cross them.
- Preserves existing file permissions where the platform allows it.
- Refuses unknown encodings, decoded NUL characters, symlinks, missing matches, invalid lines, and oversized files by default.
- Performs same-directory temp-file writes followed by atomic replacement and byte-for-byte post-write verification.

## Escape Hatches

Use these only when the task explicitly requires them:

- `--encoding auto|utf-8|utf-8-bom|gbk|utf-16-le|utf-16-be`: override detection.
- `--no-op-ok`: allow replacement text not to be found.
- `--first`: replace only the first occurrence.
- `--allow-nul`: allow decoded NUL characters.
- `--follow-symlink`: edit the symlink target instead of refusing.
- `--max-bytes N`: raise or lower the default 50 MiB limit.
- `--json`: emit machine-readable status.
