#!/usr/bin/env python3
"""Long-lived MCP transport for safe-edit structured requests."""

import copy
import json
import math
import re
import secrets
import sys
import time
from typing import Any, Dict, List, Optional

import safe_edit as core

from . import __version__


MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_FILES = 128
MAX_OPERATIONS_PER_FILE = 256
MAX_TOTAL_OPERATIONS = 1024
MAX_JSON_INTEGER_DIGITS = 128
MAX_JSON_NESTING = 128
SERVER_NAME = "safe-edit"
SERVER_VERSION = __version__
SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2024-11-05",
)
PENDING_TRANSACTION_TTL_SECONDS = 10 * 60
MAX_PENDING_TRANSACTIONS = 32
MAX_PENDING_TRANSACTION_BYTES = 64 * 1024 * 1024
MAX_PENDING_TOTAL_BYTES = 128 * 1024 * 1024
MAX_PENDING_PREPARED_OUTPUT_BYTES = 16 * 1024 * 1024
PENDING_TRANSACTION_OVERHEAD_BYTES = 512
MAX_LOCK_TIMEOUT_SECONDS = 60 * 60
MAX_COMPAT_TEXT_BYTES = 256 * 1024
_PENDING_TRANSACTIONS: Dict[str, Dict[str, Any]] = {}


class ToolInputError(Exception):
    pass


class ToolExecutionError(Exception):
    """A valid tool request that cannot execute in the current server state."""


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_PREFLIGHT_ARGUMENT_KEYS = frozenset({"file"})
_STAT_ARGUMENT_KEYS = frozenset(
    {
        "files",
        "encoding",
        "maxBytes",
        "lockTimeout",
        "lockStaleSeconds",
    }
)
_STAT_FILE_KEYS = frozenset(
    {
        "file",
        "encoding",
        "inputEncoding",
        "followSymlink",
        "maxBytes",
        "expectedSha256",
    }
)
_TRANSACTION_ARGUMENT_KEYS = frozenset(
    {
        "files",
        "transactionId",
        "dryRun",
        "maxBytes",
        "autoMatch",
        "autoEolMatch",
        "fuzzy",
        "fuzzyWorkers",
        "lockTimeout",
        "lockStaleSeconds",
    }
)
_TRANSACTION_FILE_KEYS = frozenset(
    {
        "file",
        "action",
        "expectedSha256",
        "operations",
        "text",
        "encoding",
        "inputEncoding",
        "lineEnding",
        "finalNewline",
        "trimTrailingWhitespace",
        "forceWrite",
        "allowNul",
        "followSymlink",
        "diff",
    }
)


PARSER = core.build_parser()
ARG_TEMPLATES = {
    command: PARSER.parse_args([command, "--json"])
    for command in ("preflight", "stat-many", "transaction")
}


def _fresh_args(command: str):
    args = copy.copy(ARG_TEMPLATES[command])
    args._fs_capability_cache = None
    return args


def _require_arguments(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ToolInputError("arguments must be an object")
    return value


def _require_files(arguments: Dict[str, Any]) -> List[Any]:
    files = arguments.get("files")
    if not isinstance(files, list) or not files:
        raise ToolInputError("files must be a non-empty array")
    if len(files) > MAX_FILES:
        raise ToolInputError(f"files must contain at most {MAX_FILES} items")
    return files


def _reject_unknown_keys(
    value: Dict[str, Any], allowed: frozenset, label: str
) -> None:
    unknown = sorted(str(key) for key in set(value) - allowed)
    if unknown:
        raise ToolInputError(
            f"{label} contains unknown field(s): {', '.join(unknown)}"
        )


def _require_non_empty_string(
    value: Dict[str, Any], key: str, label: str, required: bool = False
) -> None:
    if key not in value:
        if required:
            raise ToolInputError(f"{label}.{key} is required")
        return
    item = value[key]
    if not isinstance(item, str) or not item:
        raise ToolInputError(f"{label}.{key} must be a non-empty string")


def _validate_boolean(value: Dict[str, Any], key: str, label: str) -> None:
    if key in value and not isinstance(value[key], bool):
        raise ToolInputError(f"{label}.{key} must be a boolean")


def _validate_sha256(
    value: Dict[str, Any], key: str, label: str, required: bool = False
) -> None:
    if key not in value:
        if required:
            raise ToolInputError(f"{label}.{key} is required")
        return
    digest = value[key]
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise ToolInputError(
            f"{label}.{key} must be a 64-character hexadecimal SHA-256"
        )


def _validate_positive_integer(
    value: Dict[str, Any],
    key: str,
    label: str,
    maximum: Optional[int] = None,
) -> None:
    if key not in value:
        return
    item = value[key]
    if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
        raise ToolInputError(f"{label}.{key} must be a positive integer")
    if maximum is not None and item > maximum:
        raise ToolInputError(f"{label}.{key} must be at most {maximum}")


def _validate_preflight_arguments(arguments: Dict[str, Any]) -> None:
    _reject_unknown_keys(arguments, _PREFLIGHT_ARGUMENT_KEYS, "arguments")
    _require_non_empty_string(arguments, "file", "arguments")


def _validate_stat_file(item: Any, index: int) -> None:
    label = f"files[{index}]"
    if isinstance(item, str):
        if not item:
            raise ToolInputError(f"{label} must be a non-empty string")
        return
    if not isinstance(item, dict):
        raise ToolInputError(f"{label} must be a string or object")
    _reject_unknown_keys(item, _STAT_FILE_KEYS, label)
    _require_non_empty_string(item, "file", label, required=True)
    _require_non_empty_string(item, "encoding", label)
    _require_non_empty_string(item, "inputEncoding", label)
    _validate_boolean(item, "followSymlink", label)
    _validate_positive_integer(
        item, "maxBytes", label, MAX_FILE_BYTES
    )
    _validate_sha256(item, "expectedSha256", label)


def _validate_stat_arguments(arguments: Dict[str, Any]) -> None:
    _reject_unknown_keys(arguments, _STAT_ARGUMENT_KEYS, "arguments")
    files = _require_files(arguments)
    for index, item in enumerate(files):
        _validate_stat_file(item, index)
    _require_non_empty_string(arguments, "encoding", "arguments")
    _validate_positive_integer(
        arguments, "maxBytes", "arguments", MAX_FILE_BYTES
    )


def _validate_transaction_file(item: Any, index: int) -> int:
    label = f"files[{index}]"
    if not isinstance(item, dict):
        raise ToolInputError(f"{label} must be an object")
    _reject_unknown_keys(item, _TRANSACTION_FILE_KEYS, label)
    _require_non_empty_string(item, "file", label, required=True)
    for key in ("encoding", "inputEncoding"):
        _require_non_empty_string(item, key, label)
    for key in (
        "trimTrailingWhitespace",
        "forceWrite",
        "allowNul",
        "followSymlink",
        "diff",
    ):
        _validate_boolean(item, key, label)
    _validate_sha256(item, "expectedSha256", label)

    if "lineEnding" in item and item["lineEnding"] not in (
        "lf",
        "crlf",
        "cr",
        "preserve",
    ):
        raise ToolInputError(
            f"{label}.lineEnding must be lf, crlf, cr, or preserve"
        )
    if "finalNewline" in item and item["finalNewline"] not in (
        "preserve",
        "ensure",
        "strip",
    ):
        raise ToolInputError(
            f"{label}.finalNewline must be preserve, ensure, or strip"
        )

    action = item.get("action")
    if action is not None and action not in ("edit", "batch", "create"):
        raise ToolInputError(f"{label}.action must be edit, batch, or create")
    has_text = "text" in item
    has_operations = "operations" in item
    if has_text and not isinstance(item["text"], str):
        raise ToolInputError(f"{label}.text must be a string")
    if has_text and has_operations:
        raise ToolInputError(f"{label} must not mix text and operations")
    if action is None:
        if has_text:
            action = "create"
        elif has_operations:
            action = "edit"
        else:
            raise ToolInputError(
                f"{label} requires text for create or operations for edit"
            )

    if action == "create":
        if not has_text:
            raise ToolInputError(f"{label}.text is required for create")
        if has_operations:
            raise ToolInputError(
                f"{label}.operations is not allowed for create"
            )
        if "expectedSha256" in item:
            raise ToolInputError(
                f"{label}.expectedSha256 is not allowed for create"
            )
        _require_non_empty_string(
            item, "encoding", label, required=True
        )
        if item["encoding"] == "preserve":
            raise ToolInputError(
                f"{label}.encoding must be explicit for create"
            )
        if item.get("lineEnding") not in ("lf", "crlf", "cr"):
            raise ToolInputError(
                f"{label}.lineEnding must be lf, crlf, or cr for create"
            )
        return 0

    if has_text:
        raise ToolInputError(f"{label}.text is not allowed for edit")
    operations = item.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ToolInputError(
            f"{label}.operations must be a non-empty array for edit"
        )
    if len(operations) > MAX_OPERATIONS_PER_FILE:
        raise ToolInputError(
            f"{label}.operations must contain at most "
            f"{MAX_OPERATIONS_PER_FILE} items"
        )
    for operation_index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise ToolInputError(
                f"{label}.operations[{operation_index}] must be an object"
            )
    _validate_sha256(
        item, "expectedSha256", label, required=True
    )
    return len(operations)


def _validate_transaction_arguments(arguments: Dict[str, Any]) -> None:
    _reject_unknown_keys(
        arguments, _TRANSACTION_ARGUMENT_KEYS, "arguments"
    )
    has_files = "files" in arguments
    has_transaction_id = "transactionId" in arguments
    if has_files == has_transaction_id:
        raise ToolInputError(
            "provide exactly one of files or transactionId"
        )
    if has_transaction_id:
        _require_non_empty_string(
            arguments, "transactionId", "arguments", required=True
        )
        if set(arguments) != {"transactionId"}:
            raise ToolInputError(
                "confirm with transactionId only; do not resend files or options"
            )
        return

    for key in ("dryRun", "autoMatch", "autoEolMatch", "fuzzy"):
        _validate_boolean(arguments, key, "arguments")
    _validate_positive_integer(
        arguments, "maxBytes", "arguments", MAX_FILE_BYTES
    )
    if "fuzzyWorkers" in arguments:
        workers = arguments["fuzzyWorkers"]
        if workers != "auto" and (
            isinstance(workers, bool)
            or not isinstance(workers, int)
            or workers < 1
            or workers > core._FUZZY_MAX_WORKERS
        ):
            raise ToolInputError(
                "fuzzyWorkers must be auto or an integer from 1 to 8"
            )
    files = _require_files(arguments)
    operation_count = 0
    for index, item in enumerate(files):
        operation_count += _validate_transaction_file(item, index)
        if operation_count > MAX_TOTAL_OPERATIONS:
            raise ToolInputError(
                "files must contain at most "
                f"{MAX_TOTAL_OPERATIONS} operations in total"
            )


def _prune_pending_transactions(now: Optional[float] = None) -> None:
    current = time.monotonic() if now is None else now
    expired = [
        transaction_id
        for transaction_id, item in _PENDING_TRANSACTIONS.items()
        if item["expiresAt"] <= current
    ]
    for transaction_id in expired:
        _PENDING_TRANSACTIONS.pop(transaction_id, None)


def _prepared_output_bytes(prepared: Any) -> Optional[int]:
    output_bytes = getattr(prepared, "output_bytes", None)
    if type(output_bytes) is int and output_bytes >= 0:
        return output_bytes

    plans = getattr(prepared, "plans", None)
    if not isinstance(plans, (list, tuple)):
        return None
    total = 0
    for plan in plans:
        if not isinstance(plan, dict):
            return None
        output = plan.get("output")
        if not isinstance(output, bytes):
            return None
        total += len(output)
    return total


def _json_bytes(value: Any) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("ascii")


def _json_string_size_with_limit(value: str, limit: int) -> Optional[int]:
    """Return an ensure-ASCII JSON string-size upper bound without copying it."""
    size = 2
    for character in value:
        codepoint = ord(character)
        if character in ('"', "\\") or character in "\b\f\n\r\t":
            increment = 2
        elif codepoint < 0x20 or codepoint <= 0xFFFF and codepoint >= 0x80:
            increment = 6
        elif codepoint > 0xFFFF:
            increment = 12
        else:
            increment = 1
        size += increment
        if size > limit:
            return None
    return size


def _json_size_with_limit(
    value: Any,
    limit: int,
    seen: Optional[set] = None,
) -> Optional[int]:
    """Measure a compact-JSON size upper bound without a large serialization."""
    if limit < 0:
        return None
    if value is None:
        return 4 if limit >= 4 else None
    if value is True:
        return 4 if limit >= 4 else None
    if value is False:
        return 5 if limit >= 5 else None
    if isinstance(value, str):
        return _json_string_size_with_limit(value, limit)
    if isinstance(value, int):
        size = len(str(value))
        return size if size <= limit else None
    if isinstance(value, float):
        rendered = json.dumps(
            value, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        )
        size = len(rendered)
        return size if size <= limit else None

    if not isinstance(value, (dict, list, tuple)):
        raise TypeError(
            f"Object of type {type(value).__name__} is not JSON serializable"
        )
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        raise ValueError("Circular reference detected")
    seen.add(identity)
    try:
        size = 2
        if size > limit:
            return None
        if isinstance(value, dict):
            iterator = value.items()
        else:
            iterator = ((None, item) for item in value)
        for index, (key, item) in enumerate(iterator):
            if index:
                size += 1
            if isinstance(value, dict):
                if not isinstance(key, str):
                    raise TypeError("JSON object keys must be strings")
                key_size = _json_string_size_with_limit(key, limit - size)
                if key_size is None:
                    return None
                size += key_size + 1
            item_size = _json_size_with_limit(item, limit - size, seen)
            if item_size is None:
                return None
            size += item_size
            if size > limit:
                return None
        return size
    finally:
        seen.remove(identity)


def _encode_pending_arguments(arguments: Dict[str, Any]) -> bytes:
    cached_arguments = dict(arguments)
    cached_arguments["dryRun"] = False
    try:
        return _json_bytes(cached_arguments)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ToolInputError(
            f"dryRun transaction cannot be cached: {exc}"
        ) from exc


def _pending_total_bytes() -> int:
    return sum(
        int(item.get("sizeBytes", 0))
        for item in _PENDING_TRANSACTIONS.values()
    )


def _remember_pending_transaction(
    arguments: Dict[str, Any], prepared: Any = None
) -> str:
    now = time.monotonic()
    _prune_pending_transactions(now)

    cached_prepared = None
    cached_arguments_json = None
    prepared_bytes = (
        _prepared_output_bytes(prepared) if prepared is not None else None
    )
    prepared_size = getattr(prepared, "retained_bytes", None)
    if (
        prepared_bytes is not None
        and prepared_bytes <= MAX_PENDING_PREPARED_OUTPUT_BYTES
        and type(prepared_size) is int
        and prepared_size >= prepared_bytes
        and prepared_size + PENDING_TRANSACTION_OVERHEAD_BYTES
        <= MAX_PENDING_TRANSACTION_BYTES
    ):
        cached_prepared = prepared
        retained_bytes = (
            prepared_size + PENDING_TRANSACTION_OVERHEAD_BYTES
        )
    else:
        cached_arguments_json = _encode_pending_arguments(arguments)
        retained_bytes = (
            len(cached_arguments_json)
            + PENDING_TRANSACTION_OVERHEAD_BYTES
        )

    if (
        retained_bytes > MAX_PENDING_TRANSACTION_BYTES
        or retained_bytes > MAX_PENDING_TOTAL_BYTES
    ):
        raise ToolExecutionError(
            "dryRun transaction exceeds the pending confirmation cache limit"
        )

    if (
        len(_PENDING_TRANSACTIONS) >= MAX_PENDING_TRANSACTIONS
        or _pending_total_bytes() + retained_bytes
        > MAX_PENDING_TOTAL_BYTES
    ):
        raise ToolExecutionError(
            "pending confirmation cache is full; confirm or wait for an "
            "existing transactionId before retrying dryRun"
        )
    transaction_id = "tx_" + secrets.token_urlsafe(24)
    _PENDING_TRANSACTIONS[transaction_id] = {
        "expiresAt": now + PENDING_TRANSACTION_TTL_SECONDS,
        "argumentsJson": cached_arguments_json,
        "prepared": cached_prepared,
        "sizeBytes": retained_bytes,
    }
    return transaction_id


def _consume_pending_transaction(transaction_id: str) -> Dict[str, Any]:
    _prune_pending_transactions()
    pending = _PENDING_TRANSACTIONS.pop(transaction_id, None)
    if pending is None:
        raise ToolExecutionError(
            "unknown or expired transactionId; run dryRun again"
        )
    return pending


def _set_positive_int(
    args: Any,
    arguments: Dict[str, Any],
    source: str,
    target: str,
    maximum: Optional[int] = None,
) -> None:
    if source not in arguments:
        return
    value = arguments[source]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ToolInputError(f"{source} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ToolInputError(f"{source} must be at most {maximum}")
    setattr(args, target, value)


def _set_finite_number(
    args: Any,
    arguments: Dict[str, Any],
    source: str,
    target: str,
    maximum: Optional[float] = None,
    allow_zero: bool = False,
) -> None:
    if source not in arguments:
        return
    value = arguments[source]
    requirement = "non-negative" if allow_zero else "positive"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolInputError(
            f"{source} must be a finite {requirement} number"
        )
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ToolInputError(
            f"{source} must be a finite {requirement} number"
        ) from None
    if (
        not math.isfinite(number)
        or number < 0
        or (number == 0 and not allow_zero)
    ):
        raise ToolInputError(
            f"{source} must be a finite {requirement} number"
        )
    if maximum is not None and number > maximum:
        raise ToolInputError(f"{source} must be at most {maximum:g}")
    setattr(args, target, number)


def _configure_common(args: Any, arguments: Dict[str, Any]) -> None:
    _set_positive_int(
        args, arguments, "maxBytes", "max_bytes", MAX_FILE_BYTES
    )
    _set_finite_number(
        args,
        arguments,
        "lockTimeout",
        "lock_timeout",
        MAX_LOCK_TIMEOUT_SECONDS,
        allow_zero=True,
    )
    _set_finite_number(
        args,
        arguments,
        "lockStaleSeconds",
        "lock_stale_seconds",
        allow_zero=True,
    )


def _configure_match_options(
    args: Any, arguments: Dict[str, Any]
) -> None:
    for source, target, default in (
        ("autoMatch", "auto_match", False),
        ("autoEolMatch", "auto_eol_match", True),
        ("fuzzy", "fuzzy", False),
    ):
        value = arguments.get(source, default)
        if not isinstance(value, bool):
            raise ToolInputError(f"{source} must be a boolean")
        setattr(args, target, value)

    if "fuzzyWorkers" not in arguments:
        return
    value = arguments["fuzzyWorkers"]
    if value == "auto":
        args.fuzzy_workers = "auto"
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > core._FUZZY_MAX_WORKERS
    ):
        raise ToolInputError(
            "fuzzyWorkers must be auto or an integer from 1 to 8"
        )
    args.fuzzy_workers = value


def execute_tool(name: str, raw_arguments: Any) -> Dict[str, Any]:
    """Execute one tool call with an already-decoded request object."""
    _prune_pending_transactions()
    arguments = _require_arguments(raw_arguments)
    started = time.perf_counter_ns()

    if name == "safe_edit_preflight":
        _validate_preflight_arguments(arguments)
        args = _fresh_args("preflight")
        file_value = arguments.get("file")
        if file_value is not None and (
            not isinstance(file_value, str) or not file_value
        ):
            raise ToolInputError("file must be a non-empty string")
        args.file = file_value
        summary = core.run_preflight(args)
    elif name == "safe_edit_stat":
        _validate_stat_arguments(arguments)
        args = _fresh_args("stat-many")
        _configure_common(args, arguments)
        payload: Dict[str, Any] = {"files": _require_files(arguments)}
        if "encoding" in arguments:
            encoding = arguments["encoding"]
            if not isinstance(encoding, str) or not encoding:
                raise ToolInputError("encoding must be a non-empty string")
            payload["encoding"] = encoding
        summary = core.run_stat_many_payload(args, payload)
    elif name == "safe_edit_transaction":
        _validate_transaction_arguments(arguments)
        confirmation_id = arguments.get("transactionId")
        pending = None
        prepared = None
        if confirmation_id is not None:
            if (
                not isinstance(confirmation_id, str)
                or not confirmation_id
            ):
                raise ToolInputError(
                    "transactionId must be a non-empty string"
                )
            if set(arguments) != {"transactionId"}:
                raise ToolInputError(
                    "confirm with transactionId only; do not resend files or options"
                )
            pending = _consume_pending_transaction(confirmation_id)
            prepared = pending.get("prepared")
            if prepared is None:
                arguments_json = pending.get("argumentsJson")
                if not isinstance(arguments_json, bytes):
                    raise RuntimeError(
                        "cached transaction is invalid; run dryRun again"
                    )
                arguments = _require_arguments(json.loads(arguments_json))
                _validate_transaction_arguments(arguments)

        if prepared is not None:
            summary = core.commit_prepared_transaction(prepared)
        else:
            args = _fresh_args("transaction")
            _configure_common(args, arguments)
            _configure_match_options(args, arguments)
            dry_run = arguments.get("dryRun", False)
            if not isinstance(dry_run, bool):
                raise ToolInputError("dryRun must be a boolean")
            args.dry_run = dry_run
            summary = core.run_transaction_payload(
                args, {"files": _require_files(arguments)}
            )
            if dry_run:
                transaction_id = _remember_pending_transaction(
                    arguments,
                    getattr(args, "_prepared_transaction", None),
                )
                summary["transactionId"] = transaction_id
                summary["transactionExpiresInSeconds"] = (
                    PENDING_TRANSACTION_TTL_SECONDS
                )

        if confirmation_id is not None:
            summary["transactionId"] = confirmation_id
            summary["confirmed"] = True
    else:
        raise ToolInputError(f"unknown tool: {name}")

    result = dict(summary)
    result["transport"] = "mcp-structured"
    result["elapsedMs"] = round(
        (time.perf_counter_ns() - started) / 1_000_000, 3
    )
    return result


def _tool_result(summary: Dict[str, Any]) -> Dict[str, Any]:
    compatibility_size = _json_size_with_limit(
        summary, MAX_COMPAT_TEXT_BYTES
    )
    if compatibility_size is not None:
        content_text = _json_bytes(summary).decode("utf-8")
    else:
        compatibility_summary = {
            "ok": summary.get("ok"),
            "command": summary.get("command"),
            "fileCount": summary.get("fileCount"),
            "written": summary.get("written"),
            "truncated": True,
            "compatibilityTextLimitBytes": MAX_COMPAT_TEXT_BYTES,
        }
        if summary.get("transactionId") is not None:
            compatibility_summary["transactionId"] = summary[
                "transactionId"
            ]
        content_text = _json_bytes(compatibility_summary).decode("utf-8")
    return {
        "content": [
            {
                "type": "text",
                "text": content_text,
            }
        ],
        "structuredContent": summary,
    }


def _tool_failure(error: Exception, command: str) -> Dict[str, Any]:
    if isinstance(error, core.SafeEditError):
        payload = core.build_error_payload(error, command=command)
    else:
        failure_reason = (
            "execution_error"
            if isinstance(error, ToolExecutionError)
            else "validation_error"
        )
        payload = {
            "ok": False,
            "command": command,
            "error": {
                "type": failure_reason,
                "message": str(error),
                "reason": failure_reason,
            },
            "failureReason": failure_reason,
            "written": False,
        }
    payload["transport"] = "mcp-structured"
    result = _tool_result(payload)
    result["isError"] = True
    return result


FILE_ITEM_SCHEMA = {
    "oneOf": [
        {"type": "string", "minLength": 1},
        {
            "type": "object",
            "properties": {
                "file": {"type": "string", "minLength": 1},
                "encoding": {"type": "string", "minLength": 1},
                "inputEncoding": {"type": "string", "minLength": 1},
                "followSymlink": {"type": "boolean"},
                "maxBytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_FILE_BYTES,
                },
                "expectedSha256": {
                    "type": "string",
                    "pattern": "^[0-9a-fA-F]{64}$",
                },
            },
            "required": ["file"],
            "additionalProperties": False,
        },
    ]
}

TRANSACTION_ITEM_SCHEMA = {
    "type": "object",
    "description": (
        "Edit an existing file with operations and expectedSha256, or create "
        "a new file with text, encoding, and lineEnding."
    ),
    "properties": {
        "file": {"type": "string", "minLength": 1},
        "action": {"type": "string", "enum": ["edit", "batch", "create"]},
        "expectedSha256": {
            "type": "string",
            "pattern": "^[0-9a-fA-F]{64}$",
        },
        "operations": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_OPERATIONS_PER_FILE,
            "items": {
                "type": "object",
                "description": (
                    "A native safe-edit operation such as edit, regex, "
                    "insert, prepend, append, replace-lines, or delete-lines. "
                    "Copy edit.old verbatim from the latest file and keep it "
                    "as short and unique as practical. Add context only when "
                    "the target itself is not unique, and set expected_count "
                    "on every edit before enabling transaction-wide relaxed "
                    "matching."
                ),
                "additionalProperties": True,
            },
        },
        "text": {"type": "string"},
        "encoding": {"type": "string", "minLength": 1},
        "inputEncoding": {"type": "string", "minLength": 1},
        "lineEnding": {
            "type": "string",
            "enum": ["lf", "crlf", "cr", "preserve"],
        },
        "finalNewline": {
            "type": "string",
            "enum": ["preserve", "ensure", "strip"],
        },
        "trimTrailingWhitespace": {"type": "boolean"},
        "forceWrite": {"type": "boolean"},
        "allowNul": {"type": "boolean"},
        "followSymlink": {"type": "boolean"},
        "diff": {
            "type": "boolean",
            "description": (
                "Request a full diff. Omit during dryRun for a compact diff."
            ),
        },
    },
    "required": ["file"],
    "oneOf": [
        {
            "required": ["text", "encoding", "lineEnding"],
            "properties": {
                "action": {"enum": ["create"]},
                "encoding": {
                    "type": "string",
                    "minLength": 1,
                    "not": {"const": "preserve"},
                },
                "lineEnding": {"enum": ["lf", "crlf", "cr"]},
            },
            "not": {
                "anyOf": [
                    {"required": ["operations"]},
                    {"required": ["expectedSha256"]},
                ]
            },
        },
        {
            "required": ["operations", "expectedSha256"],
            "properties": {
                "action": {"enum": ["edit", "batch"]}
            },
            "not": {"required": ["text"]},
        },
    ],
    "additionalProperties": False,
}

TOOLS = [
    {
        "name": "safe_edit_preflight",
        "title": "Check safe-edit runtime",
        "description": (
            "Check Python, temporary storage, locking, and target-directory "
            "capabilities before a related edit set."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Optional absolute target file path.",
                }
            },
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "safe_edit_stat",
        "title": "Inspect files for safe editing",
        "description": (
            "Inspect one or more files in a single call. Returns encoding, "
            "line endings, editStrategy, and SHA-256 guards for transactions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_FILES,
                    "items": FILE_ITEM_SCHEMA,
                },
                "encoding": {"type": "string", "default": "auto"},
                "maxBytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_FILE_BYTES,
                },
                "lockTimeout": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": MAX_LOCK_TIMEOUT_SECONDS,
                },
                "lockStaleSeconds": {
                    "type": "number",
                    "minimum": 0,
                },
            },
            "required": ["files"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "safe_edit_transaction",
        "title": "Apply a safe-edit transaction",
        "description": (
            "Apply raw structured text edits and controlled creates without "
            "shell quoting or Base64. Existing files require SHA-256 values "
            "from safe_edit_stat. All files are prevalidated and writes roll "
            "back if a later write fails. A successful dryRun returns a "
            "transactionId that can be confirmed without resending the payload."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_FILES,
                    "items": TRANSACTION_ITEM_SCHEMA,
                    "description": (
                        "Payload for preview or direct apply. Omit when "
                        "confirming a cached dry-run transaction."
                    ),
                },
                "transactionId": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Returned by dryRun. Call again with only this field "
                        "to apply the cached, SHA-guarded request."
                    ),
                },
                "dryRun": {"type": "boolean", "default": False},
                "maxBytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_FILE_BYTES,
                },
                "autoMatch": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "After exact and EOL-only matching fail, also try "
                        "indentation and general-whitespace normalization. "
                        "This affects the whole transaction: require an "
                        "expected_count on every edit, retry with dryRun, and "
                        "confirm only the returned transactionId."
                    ),
                },
                "autoEolMatch": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Try exact matching first, then retry multiline targets "
                        "and contexts with EOL-only normalization. Count "
                        "mismatches never trigger a broader retry."
                    ),
                },
                "fuzzy": {"type": "boolean", "default": False},
                "fuzzyWorkers": {
                    "oneOf": [
                        {"const": "auto"},
                        {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 8,
                        },
                    ],
                    "default": "auto",
                },
                "lockTimeout": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": MAX_LOCK_TIMEOUT_SECONDS,
                },
                "lockStaleSeconds": {
                    "type": "number",
                    "minimum": 0,
                },
            },
            "oneOf": [
                {
                    "required": ["files"],
                    "not": {"required": ["transactionId"]},
                },
                {
                    "required": ["transactionId"],
                    "maxProperties": 1,
                },
            ],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
]
TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)


def _rpc_result(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(
    request_id: Any, code: int, message: str
) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _negotiate_protocol_version(requested: Any) -> str:
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return SUPPORTED_PROTOCOL_VERSIONS[0]


def _validate_initialize_params(params: Dict[str, Any]) -> None:
    _require_non_empty_string(
        params, "protocolVersion", "params", required=True
    )
    capabilities = params.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ToolInputError("params.capabilities must be an object")
    client_info = params.get("clientInfo")
    if not isinstance(client_info, dict):
        raise ToolInputError("params.clientInfo must be an object")
    _require_non_empty_string(
        client_info, "name", "params.clientInfo", required=True
    )
    _require_non_empty_string(
        client_info, "version", "params.clientInfo", required=True
    )


def handle_message(message: Any) -> Optional[Dict[str, Any]]:
    _prune_pending_transactions()
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, "Invalid Request")

    has_id = "id" in message
    request_id = message.get("id")
    method = message.get("method")
    if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _rpc_error(None, -32600, "Invalid Request")
    if has_id and (
        isinstance(request_id, bool)
        or not isinstance(request_id, (int, str))
    ):
        return _rpc_error(None, -32600, "Invalid Request")

    if "params" not in message:
        params = {}
    else:
        params = message["params"]
    if not isinstance(params, dict):
        if not has_id:
            return None
        return _rpc_error(request_id, -32602, "Invalid params")

    # Notifications have no response and must never execute request methods.
    if not has_id:
        return None

    if method == "initialize":
        try:
            _validate_initialize_params(params)
        except ToolInputError:
            return _rpc_error(request_id, -32602, "Invalid params")
        requested_version = params["protocolVersion"]
        return _rpc_result(
            request_id,
            {
                "protocolVersion": _negotiate_protocol_version(
                    requested_version
                ),
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
                "instructions": (
                    "Call safe_edit_stat before modifying existing files, "
                    "then pass its SHA-256 values to one batched "
                    "safe_edit_transaction. Use dryRun for risky changes, "
                    "then confirm with only the returned transactionId. "
                    "Reuse each successful file result's sha256 as the next "
                    "guard. On a RETRYABLE prepare error, keep the hashes and "
                    "apply retryStrategy.argumentsPatch only in a dryRun when "
                    "every edit has expected_count; otherwise re-read. Confirm "
                    "a successful retry only with its transactionId."
                ),
            },
        )
    if method == "ping":
        return _rpc_result(request_id, {})
    if method == "tools/list":
        return _rpc_result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOL_NAMES:
            return _rpc_error(request_id, -32602, "Invalid params")
        if "arguments" in params and not isinstance(
            params["arguments"], dict
        ):
            return _rpc_error(request_id, -32602, "Invalid params")
        try:
            summary = execute_tool(name, params.get("arguments"))
            result = _tool_result(summary)
        except ToolInputError as error:
            result = _tool_failure(error, name)
        except ToolExecutionError as error:
            result = _tool_failure(error, name)
        except core.SafeEditError as error:
            try:
                result = _tool_failure(error, name)
            except Exception:
                return _rpc_error(request_id, -32603, "Internal error")
        except Exception:
            return _rpc_error(request_id, -32603, "Internal error")
        return _rpc_result(request_id, result)
    return _rpc_error(request_id, -32601, f"Method not found: {method}")


def _write_message(stream: Any, message: Dict[str, Any]) -> None:
    data = _json_bytes(message)
    stream.write(data)
    stream.write(b"\n")
    stream.flush()


def _reject_json_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON number: {token}")


def _parse_json_integer(token: str) -> int:
    digits = token[1:] if token.startswith("-") else token
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError("JSON integer is too long")
    return int(token)


def _parse_json_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("JSON float must be finite")
    return value


def _validate_json_nesting(value: Any) -> None:
    if not isinstance(value, (dict, list)):
        return
    children = value.values() if isinstance(value, dict) else value
    stack = [iter(children)]
    while stack:
        try:
            child = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if not isinstance(child, (dict, list)):
            continue
        if len(stack) >= MAX_JSON_NESTING:
            raise ValueError("JSON nesting is too deep")
        children = child.values() if isinstance(child, dict) else child
        stack.append(iter(children))


def _decode_wire_message(line: bytes) -> Any:
    value = json.loads(
        line.decode("utf-8"),
        parse_constant=_reject_json_constant,
        parse_int=_parse_json_integer,
        parse_float=_parse_json_float,
    )
    _validate_json_nesting(value)
    return value


def serve(input_stream: Any = None, output_stream: Any = None) -> None:
    source = input_stream or sys.stdin.buffer
    target = output_stream or sys.stdout.buffer

    while True:
        line = source.readline(MAX_REQUEST_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_REQUEST_BYTES:
            while line and not line.endswith(b"\n"):
                line = source.readline(MAX_REQUEST_BYTES + 1)
            _write_message(
                target,
                _rpc_error(None, -32600, "Request exceeds 64 MiB limit"),
            )
            continue
        if line.isspace():
            continue
        try:
            message = _decode_wire_message(line)
        except (UnicodeDecodeError, ValueError, RecursionError, OverflowError):
            _write_message(target, _rpc_error(None, -32700, "Parse error"))
            continue
        response = handle_message(message)
        if response is not None:
            _write_message(target, response)


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        if args[0] == "install":
            from .installer import main as install_main

            return install_main(args[1:])
        if args == ["--version"]:
            print(SERVER_VERSION)
            return 0
        print(
            "usage: safe-edit-mcp [--version] | "
            "safe-edit-mcp install [options]",
            file=sys.stderr,
        )
        return 2
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
