#!/usr/bin/env python3
"""Long-lived MCP transport for safe-edit structured requests."""

import copy
import json
import sys
import time
from typing import Any, Dict, List, Optional

import safe_edit as core

from . import __version__


MAX_REQUEST_BYTES = 64 * 1024 * 1024
SERVER_NAME = "safe-edit"
SERVER_VERSION = __version__
SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)


class ToolInputError(Exception):
    pass


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
    return files


def _set_positive_int(
    args: Any, arguments: Dict[str, Any], source: str, target: str
) -> None:
    if source not in arguments:
        return
    value = arguments[source]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ToolInputError(f"{source} must be a positive integer")
    setattr(args, target, value)


def _set_positive_number(
    args: Any, arguments: Dict[str, Any], source: str, target: str
) -> None:
    if source not in arguments:
        return
    value = arguments[source]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value <= 0
    ):
        raise ToolInputError(f"{source} must be a positive number")
    setattr(args, target, float(value))


def _configure_common(args: Any, arguments: Dict[str, Any]) -> None:
    _set_positive_int(args, arguments, "maxBytes", "max_bytes")
    _set_positive_number(args, arguments, "lockTimeout", "lock_timeout")
    _set_positive_number(
        args, arguments, "lockStaleSeconds", "lock_stale_seconds"
    )


def _configure_match_options(
    args: Any, arguments: Dict[str, Any]
) -> None:
    for source, target in (
        ("autoMatch", "auto_match"),
        ("fuzzy", "fuzzy"),
    ):
        if source not in arguments:
            continue
        value = arguments[source]
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
    arguments = _require_arguments(raw_arguments)
    started = time.perf_counter_ns()

    if name == "safe_edit_preflight":
        args = _fresh_args("preflight")
        file_value = arguments.get("file")
        if file_value is not None and (
            not isinstance(file_value, str) or not file_value
        ):
            raise ToolInputError("file must be a non-empty string")
        args.file = file_value
        summary = core.run_preflight(args)
    elif name == "safe_edit_stat":
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
    else:
        raise ToolInputError(f"unknown tool: {name}")

    result = dict(summary)
    result["transport"] = "mcp-structured"
    result["elapsedMs"] = round(
        (time.perf_counter_ns() - started) / 1_000_000, 3
    )
    return result


def _summary_text(summary: Dict[str, Any]) -> str:
    command = summary.get("command")
    elapsed = summary.get("elapsedMs", 0)
    if command == "preflight":
        return (
            "safe-edit preflight completed: "
            f"mode={summary.get('executionMode')} elapsedMs={elapsed}"
        )
    if command == "stat-many":
        return (
            "safe-edit stat completed: "
            f"files={summary.get('fileCount')} elapsedMs={elapsed}"
        )
    mode = "dry-run" if summary.get("dryRun") else "apply"
    return (
        f"safe-edit transaction {mode} completed: "
        f"files={summary.get('fileCount')} "
        f"written={summary.get('written')} elapsedMs={elapsed}"
    )


def _tool_result(summary: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": _summary_text(summary)}],
        "structuredContent": summary,
    }


def _tool_failure(error: Exception, command: str) -> Dict[str, Any]:
    if isinstance(error, core.SafeEditError):
        error_type = core.classify_error_type(str(error))
    else:
        error_type = "validation_error"
    payload = {
        "ok": False,
        "command": command,
        "transport": "mcp-structured",
        "error": {"type": error_type, "message": str(error)},
        "written": False,
    }
    return {
        "content": [{"type": "text", "text": f"safe-edit: {error}"}],
        "structuredContent": payload,
        "isError": True,
    }


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
                "maxBytes": {"type": "integer", "minimum": 1},
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
            "items": {
                "type": "object",
                "description": (
                    "A native safe-edit operation such as edit, regex, "
                    "insert, prepend, append, replace-lines, or delete-lines."
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
        "diff": {"type": "boolean"},
    },
    "required": ["file"],
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
                    "items": FILE_ITEM_SCHEMA,
                },
                "encoding": {"type": "string", "default": "auto"},
                "maxBytes": {"type": "integer", "minimum": 1},
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
            "back if a later write fails."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "minItems": 1,
                    "items": TRANSACTION_ITEM_SCHEMA,
                },
                "dryRun": {"type": "boolean", "default": False},
                "maxBytes": {"type": "integer", "minimum": 1},
                "autoMatch": {"type": "boolean", "default": False},
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
                    "exclusiveMinimum": 0,
                },
                "lockStaleSeconds": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                },
            },
            "required": ["files"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
]


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


def handle_message(message: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, "Invalid Request")

    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params")
    if params is None:
        params = {}
    elif not isinstance(params, dict):
        return _rpc_error(request_id, -32602, "Invalid params")

    if method == "initialize":
        requested_version = params.get("protocolVersion")
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
                    "safe_edit_transaction. Use dryRun for risky changes."
                ),
            },
        )
    if method == "ping":
        return _rpc_result(request_id, {})
    if method == "tools/list":
        return _rpc_result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name", "")
        try:
            summary = execute_tool(name, params.get("arguments"))
            result = _tool_result(summary)
        except Exception as error:
            result = _tool_failure(error, name)
        return _rpc_result(request_id, result)
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if request_id is None:
        return None
    return _rpc_error(request_id, -32601, f"Method not found: {method}")


def _write_message(stream: Any, message: Dict[str, Any]) -> None:
    data = json.dumps(
        message, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    stream.write(data + b"\n")
    stream.flush()


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
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
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
