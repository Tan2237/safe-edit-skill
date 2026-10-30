# Windows payload transport

Read this file when editing on Windows, PowerShell, MSYS2, or Git Bash. The
hard rules in `SKILL.md` still apply.

PowerShell and the Windows native argv layer can rewrite quotes before Python
receives them. MSYS2/Git Bash can additionally convert leading `/` and `//` as
paths. `safe_edit.py` cannot reconstruct the caller's original intent after that
transformation.

Windows kernel locking assumes that `TEMP` is protected by a user-private ACL.
Treat a weakly protected or shared `TEMP` directory as a known locking boundary.

Use this order:

1. `safe_edit_stat` and `safe_edit_transaction` with raw structured arguments.
2. Native execution-tool stdin with `--request-stdin`, `--ops-stdin`,
   `--diff-input-stdin`, or `--text-stdin`.
3. An existing payload file through `--request-file` or another `--*-file`.
4. Unpadded URL-safe UTF-8 Base64 with `--request-base64`, `--ops-base64`,
   `--diff-input-base64`, or a field-specific `--*-base64`.
5. Literal `--old`/`--new`/`--text` only for short, shell-insensitive text.

For exact replacement, one stdin stream cannot carry both `old` and `new`
independently. Use a batch JSON envelope:

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

Pass that JSON through `batch --ops-stdin`, or encode the entire JSON document
as URL-safe UTF-8 Base64 and pass it through `batch --ops-base64 B64`.

Do not put a PowerShell here-string or quoted source literal into the command
merely to feed stdin; that still relies on shell parsing. When the execution
tool has no native stdin field, use Base64.

For short literal arguments under MSYS2, `MSYS2_ARG_CONV_EXCL="*"` remains a
fallback. `safe-edit` emits a `"warnings"` field in JSON output when it detects
likely MSYS2 path corruption.
