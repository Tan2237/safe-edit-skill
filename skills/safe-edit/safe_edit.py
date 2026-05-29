#!/usr/bin/env python3
"""Safe cross-platform text-file edits with strict decoding and atomic replacement."""

from __future__ import annotations

import argparse
import codecs
import difflib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


class SafeEditError(Exception):
    pass


@dataclass(frozen=True)
class EncodingInfo:
    name: str
    codec: str
    bom: bytes = b""


ENCODING_CODECS = {
    "utf-8": "utf-8",
    "utf-8-bom": "utf-8",
    "gbk": "gbk",
    "shift-jis": "cp932",
    "big5": "big5",
    "latin-1": "latin-1",
    "utf-16-le": "utf-16-le",
    "utf-16-be": "utf-16-be",
}


def fail(message: str) -> None:
    raise SafeEditError(message)


def normalize_encoding(value: Optional[str]) -> str:
    value = (value or "auto").lower().replace("_", "-")
    aliases = {
        "utf8": "utf-8",
        "utf8-bom": "utf-8-bom",
        "utf-8-sig": "utf-8-bom",
        "cp936": "gbk",
        "gb2312": "gbk",
        "sjis": "shift-jis",
        "shift-jisx0213": "shift-jis",
        "shift-jis-2004": "shift-jis",
        "cp932": "shift-jis",
        "ms932": "shift-jis",
        "latin1": "latin-1",
        "iso-8859-1": "latin-1",
        "utf16-le": "utf-16-le",
        "utf16-be": "utf-16-be",
    }
    return aliases.get(value, value)


def make_encoding_info(name: str, data: bytes = b"") -> EncodingInfo:
    name = normalize_encoding(name)
    if name not in ENCODING_CODECS:
        fail(f"unsupported encoding: {name}")
    if name == "utf-8-bom":
        return EncodingInfo(name, "utf-8", codecs.BOM_UTF8)
    if name == "utf-8" and data.startswith(codecs.BOM_UTF8):
        return EncodingInfo("utf-8-bom", "utf-8", codecs.BOM_UTF8)
    if name == "utf-16-le" and data.startswith(codecs.BOM_UTF16_LE):
        return EncodingInfo(name, "utf-16-le", codecs.BOM_UTF16_LE)
    if name == "utf-16-be" and data.startswith(codecs.BOM_UTF16_BE):
        return EncodingInfo(name, "utf-16-be", codecs.BOM_UTF16_BE)
    return EncodingInfo(name, ENCODING_CODECS[name])


def strict_decode(data: bytes, info: EncodingInfo) -> str:
    payload = data
    if info.bom:
        if not data.startswith(info.bom):
            fail(f"{info.name} BOM is missing")
        payload = data[len(info.bom) :]
    try:
        return payload.decode(info.codec, errors="strict")
    except UnicodeDecodeError as exc:
        fail(f"failed to decode as {info.name}: {exc}")


def encode_text(text: str, info: EncodingInfo) -> bytes:
    try:
        return info.bom + text.encode(info.codec, errors="strict")
    except UnicodeEncodeError as exc:
        fail(f"failed to encode as {info.name}: {exc}")


def looks_like_utf16_without_bom(data: bytes) -> Optional[EncodingInfo]:
    sample = data[:4096]
    if len(sample) < 4:
        return None
    even_nuls = sample[0::2].count(0)
    odd_nuls = sample[1::2].count(0)
    pairs = max(1, len(sample) // 2)
    if odd_nuls / pairs > 0.30 and even_nuls / pairs < 0.05:
        info = EncodingInfo("utf-16-le", "utf-16-le")
    elif even_nuls / pairs > 0.30 and odd_nuls / pairs < 0.05:
        info = EncodingInfo("utf-16-be", "utf-16-be")
    else:
        return None
    try:
        data.decode(info.codec, errors="strict")
        return info
    except UnicodeDecodeError:
        return None


def detect_encoding(data: bytes, requested: str) -> EncodingInfo:
    requested = normalize_encoding(requested)
    if requested != "auto":
        return make_encoding_info(requested, data)

    if data.startswith(codecs.BOM_UTF8):
        return EncodingInfo("utf-8-bom", "utf-8", codecs.BOM_UTF8)
    if data.startswith(codecs.BOM_UTF16_LE):
        return EncodingInfo("utf-16-le", "utf-16-le", codecs.BOM_UTF16_LE)
    if data.startswith(codecs.BOM_UTF16_BE):
        return EncodingInfo("utf-16-be", "utf-16-be", codecs.BOM_UTF16_BE)
    if not data:
        return EncodingInfo("utf-8", "utf-8")

    utf16 = looks_like_utf16_without_bom(data)
    if utf16 is not None:
        return utf16

    try:
        data.decode("utf-8", errors="strict")
        return EncodingInfo("utf-8", "utf-8")
    except UnicodeDecodeError:
        pass

    try:
        data.decode("gbk", errors="strict")
        return EncodingInfo("gbk", "gbk")
    except UnicodeDecodeError as exc:
        fail(
            "unable to auto-detect encoding as UTF-8, UTF-8 BOM, UTF-16 BOM/raw, or GBK; "
            f"use --encoding to override ({exc})"
        )


def detect_line_ending(text: str) -> Tuple[str, Dict[str, int], bool]:
    counts = {"crlf": 0, "lf": 0, "cr": 0}
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\r":
            if index + 1 < len(text) and text[index + 1] == "\n":
                counts["crlf"] += 1
                index += 2
            else:
                counts["cr"] += 1
                index += 1
        elif char == "\n":
            counts["lf"] += 1
            index += 1
        else:
            index += 1

    if not any(counts.values()):
        return ("lf", counts, False)

    priority = {"crlf": 3, "lf": 2, "cr": 1}
    dominant = max(counts, key=lambda key: (counts[key], priority[key]))
    mixed = sum(1 for value in counts.values() if value > 0) > 1
    return (dominant, counts, mixed)


def line_sep(style: str) -> str:
    return {"crlf": "\r\n", "cr": "\r", "lf": "\n"}[style]


def split_records(text: str) -> List[Tuple[str, str]]:
    records: List[Tuple[str, str]] = []
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\r":
            sep = "\r\n" if index + 1 < len(text) and text[index + 1] == "\n" else "\r"
            records.append((text[start:index], sep))
            index += len(sep)
            start = index
        elif char == "\n":
            records.append((text[start:index], "\n"))
            index += 1
            start = index
        else:
            index += 1
    if start < len(text):
        records.append((text[start:], ""))
    return records


def join_records(records: Iterable[Tuple[str, str]]) -> str:
    return "".join(line + sep for line, sep in records)


def normalize_user_newlines(text: str, sep: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", sep)


def block_records(text: str, sep: str, final_sep: str) -> List[Tuple[str, str]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized == "":
        return [("", final_sep)]
    ends_with_newline = normalized.endswith("\n")
    lines = normalized.split("\n")
    if ends_with_newline:
        lines = lines[:-1]
    if not lines:
        return []
    records = [(line, sep) for line in lines]
    if not ends_with_newline:
        records[-1] = (records[-1][0], final_sep)
    return records


def parse_regex_flags(value: str) -> int:
    flags = 0
    for char in value:
        if char in ",| ":
            continue
        if char == "i":
            flags |= re.IGNORECASE
        elif char == "m":
            flags |= re.MULTILINE
        elif char == "s":
            flags |= re.DOTALL
        elif char == "x":
            flags |= re.VERBOSE
        elif char == "a":
            flags |= re.ASCII
        else:
            fail(f"unsupported regex flag: {char}")
    return flags


def read_argument_file(path: str, arg_encoding: str) -> str:
    try:
        return Path(path).read_text(encoding=arg_encoding)
    except OSError as exc:
        fail(f"failed to read argument file {path}: {exc}")
    except UnicodeDecodeError as exc:
        fail(f"failed to decode argument file {path} as {arg_encoding}: {exc}")


def resolve_cli_value(
    args: argparse.Namespace,
    name: str,
    required: bool,
    *,
    stdin_taken: List[str],
) -> Optional[str]:
    direct = getattr(args, name, None)
    file_value = getattr(args, f"{name}_file", None)
    stdin_value = bool(getattr(args, f"{name}_stdin", False))
    provided = [direct is not None, file_value is not None, stdin_value].count(True)
    if provided > 1:
        fail(f"use only one of --{name}, --{name}-file, or --{name}-stdin")
    if provided == 0:
        if required:
            fail(f"missing --{name}")
        return None
    if direct is not None:
        return direct
    if file_value is not None:
        return read_argument_file(file_value, args.arg_encoding)
    if stdin_taken:
        fail(f"stdin is already used by --{stdin_taken[0]}-stdin")
    stdin_taken.append(name)
    return sys.stdin.read()


def resolve_operation_value(
    operation: Dict[str, Any],
    name: str,
    required: bool,
    arg_encoding: str,
    base_dir: Optional[Path],
) -> Optional[str]:
    direct = operation.get(name)
    file_value = operation.get(f"{name}_file")
    provided = [direct is not None, file_value is not None].count(True)
    if provided > 1:
        fail(f"batch operation uses both {name} and {name}_file")
    if provided == 0:
        if required:
            fail(f"batch operation missing {name}")
        return None
    if direct is not None:
        return str(direct)
    file_path = Path(str(file_value))
    if not file_path.is_absolute() and base_dir is not None:
        file_path = base_dir / file_path
    return read_argument_file(str(file_path), arg_encoding)


def apply_literal_edit(text: str, operation: Dict[str, Any], newline: str) -> Tuple[str, int]:
    old = str(operation["old"])
    new = normalize_user_newlines(str(operation["new"]), newline)
    if old == "":
        fail("old text must not be empty")
    actual = text.count(old)
    expected = operation.get("expected_count")
    if expected is not None and actual != int(expected):
        fail(f"expected {expected} occurrence(s), found {actual}")
    if actual == 0 and not bool(operation.get("no_op_ok", False)):
        fail("old text was not found; refusing a silent no-op")
    count = 1 if bool(operation.get("first", False)) else -1
    return (text.replace(old, new, count), min(actual, 1) if count == 1 else actual)


def apply_regex_edit(text: str, operation: Dict[str, Any], newline: str) -> Tuple[str, int]:
    pattern = str(operation["pattern"])
    replacement = normalize_user_newlines(str(operation["replacement"]), newline)
    flags = parse_regex_flags(str(operation.get("flags", "")))
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        fail(f"invalid regex pattern: {exc}")

    actual = sum(1 for _ in compiled.finditer(text))
    expected = operation.get("expected_count")
    if expected is not None and actual != int(expected):
        fail(f"expected {expected} regex match(es), found {actual}")
    if actual == 0 and not bool(operation.get("no_op_ok", False)):
        fail("regex pattern was not found; refusing a silent no-op")

    count = int(operation.get("count", 0) or 0)
    if bool(operation.get("first", False)):
        count = 1
    if bool(operation.get("literal_replacement", False)):
        return compiled.subn(lambda _match: replacement, text, count=count)
    try:
        return compiled.subn(replacement, text, count=count)
    except re.error as exc:
        fail(f"invalid regex replacement: {exc}")


def apply_insert(text: str, operation: Dict[str, Any], newline: str) -> Tuple[str, int]:
    records = split_records(text)
    line = int(operation["line"])
    line_count = len(records)
    if line < 1 or line > line_count + 1:
        fail(f"line must be between 1 and {line_count + 1}, got {line}")

    final_sep = newline
    if not records:
        final_sep = "" if not str(operation["text"]).endswith(("\n", "\r")) else newline
    to_insert = block_records(str(operation["text"]), newline, final_sep)

    index = line - 1
    if records and index == len(records) and records[-1][1] == "":
        records[-1] = (records[-1][0], newline)
    return (join_records(records[:index] + to_insert + records[index:]), len(to_insert))


def apply_prepend(text: str, operation: Dict[str, Any], newline: str) -> Tuple[str, int]:
    records = split_records(text)
    text_value = str(operation["text"])
    final_sep = newline if records else (newline if text_value.endswith(("\n", "\r")) else "")
    to_insert = block_records(text_value, newline, final_sep)
    return (join_records(to_insert + records), len(to_insert))


def apply_append(text: str, operation: Dict[str, Any], newline: str) -> Tuple[str, int]:
    records = split_records(text)
    text_value = str(operation["text"])
    final_sep = newline if text_value.endswith(("\n", "\r")) else ""
    to_insert = block_records(text_value, newline, final_sep)
    if records and records[-1][1] == "":
        records[-1] = (records[-1][0], newline)
    return (join_records(records + to_insert), len(to_insert))


def apply_delete_line(text: str, operation: Dict[str, Any]) -> Tuple[str, int]:
    records = split_records(text)
    line = int(operation["line"])
    line_count = len(records)
    if line < 1 or line > line_count:
        fail(f"line must be between 1 and {line_count}, got {line}")
    index = line - 1
    return (join_records(records[:index] + records[index + 1 :]), 1)


def range_bounds(operation: Dict[str, Any], records: List[Tuple[str, str]]) -> Tuple[int, int]:
    start = int(operation["start"])
    end = int(operation["end"])
    if start < 1:
        fail(f"start must be >= 1, got {start}")
    if end < start:
        fail(f"end must be >= start, got start={start}, end={end}")
    if end > len(records):
        fail(f"end must be <= line count {len(records)}, got {end}")
    return (start - 1, end)


def apply_delete_lines(text: str, operation: Dict[str, Any]) -> Tuple[str, int]:
    records = split_records(text)
    start, end = range_bounds(operation, records)
    return (join_records(records[:start] + records[end:]), end - start)


def apply_replace_lines(text: str, operation: Dict[str, Any], newline: str) -> Tuple[str, int]:
    records = split_records(text)
    start, end = range_bounds(operation, records)
    following_exists = end < len(records)
    original_final_sep = records[end - 1][1] if end > start else newline
    final_sep = newline if following_exists else original_final_sep
    replacement = block_records(str(operation["text"]), newline, final_sep)
    return (join_records(records[:start] + replacement + records[end:]), end - start)


def apply_operation(text: str, operation: Dict[str, Any], newline: str) -> Tuple[str, int, str]:
    op = str(operation.get("op") or operation.get("command") or "").replace("_", "-")
    if op == "edit":
        new_text, changed = apply_literal_edit(text, operation, newline)
    elif op == "regex":
        new_text, changed = apply_regex_edit(text, operation, newline)
    elif op == "insert":
        new_text, changed = apply_insert(text, operation, newline)
    elif op == "prepend":
        new_text, changed = apply_prepend(text, operation, newline)
    elif op == "append":
        new_text, changed = apply_append(text, operation, newline)
    elif op == "delete":
        new_text, changed = apply_delete_line(text, operation)
    elif op == "delete-lines":
        new_text, changed = apply_delete_lines(text, operation)
    elif op == "replace-lines":
        new_text, changed = apply_replace_lines(text, operation, newline)
    else:
        fail(f"unknown operation: {op or '<missing>'}")
    return (new_text, changed, op)


def generate_diff(path: Path, before: str, after: str, context: int) -> str:
    diff = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=f"{path} (before)",
        tofile=f"{path} (after)",
        n=context,
        lineterm="",
    )
    return "\n".join(diff)


class FileLock:
    def __init__(self, path: Path, timeout: float) -> None:
        self.path = path.with_name(f".{path.name}.safe-edit.lock")
        self.timeout = timeout
        self.acquired = False

    def __enter__(self) -> "FileLock":
        deadline = time.monotonic() + max(0.0, self.timeout)
        payload = f"pid={os.getpid()} time={time.time()} file={self.path.name}\n".encode("utf-8")
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                self.acquired = True
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    fail(f"lock already exists: {self.path}")
                time.sleep(0.05)

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


class NullLock:
    def __enter__(self) -> "NullLock":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        return None


def fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def resolve_target_path(path_value: str, follow_symlink: bool) -> Path:
    path = Path(path_value)
    if not path.exists():
        fail(f"file not found: {path}")
    if path.is_symlink() and not follow_symlink:
        fail("refusing to edit a symlink without --follow-symlink")
    if follow_symlink:
        path = path.resolve()
    if not path.is_file():
        fail(f"not a regular file: {path}")
    return path


def read_target(path: Path, max_bytes: int) -> bytes:
    size = path.stat().st_size
    if size > max_bytes:
        fail(f"file is {size} bytes, exceeding --max-bytes {max_bytes}")
    return path.read_bytes()


def inspect_target(path: Path, original: bytes, encoding: EncodingInfo, text: str) -> Dict[str, Any]:
    newline_style, line_counts, mixed_line_endings = detect_line_ending(text)
    records = split_records(text)
    mode = stat.S_IMODE(path.stat().st_mode)
    return {
        "file": str(path),
        "command": "inspect",
        "sizeBytes": len(original),
        "encoding": encoding.name,
        "codec": encoding.codec,
        "hasBom": bool(encoding.bom),
        "bomBytes": encoding.bom.hex("-") if encoding.bom else "",
        "lineEnding": newline_style,
        "mixedLineEndings": mixed_line_endings,
        "lineEndingCounts": line_counts,
        "lineCount": len(records),
        "endsWithNewline": bool(text.endswith(("\n", "\r"))),
        "hasNul": "\x00" in text,
        "permissionsOctal": oct(mode),
        "dryRun": True,
        "changed": 0,
        "operations": [],
        "backup": None,
        "written": False,
        "skipped": True,
        "wouldChangeBytes": False,
    }


def atomic_replace(path: Path, data: bytes, keep_backup: bool) -> Optional[str]:
    directory = path.parent
    prefix = f".{path.name}.safe-edit."
    fd = -1
    tmp_name = ""
    backup_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=str(directory))
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        original_mode = stat.S_IMODE(path.stat().st_mode)
        os.chmod(tmp_name, original_mode)

        if keep_backup:
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            backup_name = str(path.with_name(f"{path.name}.safe-edit-{timestamp}.bak"))
            shutil.copy2(path, backup_name)

        os.replace(tmp_name, path)
        tmp_name = ""
        fsync_directory(directory)
        return backup_name
    finally:
        if fd != -1:
            os.close(fd)
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def load_batch_operations(args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Optional[Path]]:
    sources = [args.ops is not None, args.ops_file is not None, args.ops_stdin].count(True)
    if sources != 1:
        fail("batch requires exactly one of --ops, --ops-file, or --ops-stdin")
    base_dir = None
    if args.ops is not None:
        raw = args.ops
    elif args.ops_file is not None:
        ops_path = Path(args.ops_file)
        raw = read_argument_file(str(ops_path), args.arg_encoding)
        base_dir = ops_path.parent
    else:
        raw = sys.stdin.read()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"invalid batch JSON: {exc}")
    if isinstance(payload, dict):
        payload = payload.get("operations", payload.get("ops"))
    if not isinstance(payload, list):
        fail("batch JSON must be a list or an object with operations/ops")
    operations: List[Dict[str, Any]] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            fail(f"batch operation {index} must be an object")
        operations.append(dict(item))
    return operations, base_dir


def command_to_operations(args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Optional[Path]]:
    if args.command == "batch":
        operations, base_dir = load_batch_operations(args)
    else:
        stdin_taken: List[str] = []
        operation: Dict[str, Any] = {"op": args.command}
        if args.command == "edit":
            operation["old"] = resolve_cli_value(args, "old", True, stdin_taken=stdin_taken)
            operation["new"] = resolve_cli_value(args, "new", True, stdin_taken=stdin_taken)
            operation["expected_count"] = args.expected_count
            operation["first"] = args.first
            operation["no_op_ok"] = args.no_op_ok
        elif args.command == "regex":
            operation["pattern"] = resolve_cli_value(args, "pattern", True, stdin_taken=stdin_taken)
            operation["replacement"] = resolve_cli_value(args, "replacement", True, stdin_taken=stdin_taken)
            operation["flags"] = args.flags
            operation["count"] = args.count
            operation["expected_count"] = args.expected_count
            operation["first"] = args.first
            operation["no_op_ok"] = args.no_op_ok
            operation["literal_replacement"] = args.literal_replacement
        elif args.command in ("insert", "prepend", "append"):
            if args.line is None:
                if args.command == "insert":
                    fail("missing --line")
            if args.command == "insert":
                operation["line"] = args.line
            operation["text"] = resolve_cli_value(args, "text", True, stdin_taken=stdin_taken)
        elif args.command == "delete":
            if args.line is None:
                fail("missing --line")
            operation["line"] = args.line
        elif args.command == "replace-lines":
            if args.start is None or args.end is None:
                fail("replace-lines requires --start and --end")
            operation["start"] = args.start
            operation["end"] = args.end
            operation["text"] = resolve_cli_value(args, "text", True, stdin_taken=stdin_taken)
        elif args.command == "delete-lines":
            if args.start is None or args.end is None:
                fail("delete-lines requires --start and --end")
            operation["start"] = args.start
            operation["end"] = args.end
        else:
            fail(f"unknown command: {args.command}")
        operations = [operation]
        base_dir = None

    resolved: List[Dict[str, Any]] = []
    for operation in operations:
        op = str(operation.get("op") or operation.get("command") or "").replace("_", "-")
        current = dict(operation)
        current["op"] = op
        if op == "edit":
            current["old"] = resolve_operation_value(current, "old", True, args.arg_encoding, base_dir)
            current["new"] = resolve_operation_value(current, "new", True, args.arg_encoding, base_dir)
        elif op == "regex":
            current["pattern"] = resolve_operation_value(current, "pattern", True, args.arg_encoding, base_dir)
            current["replacement"] = resolve_operation_value(current, "replacement", True, args.arg_encoding, base_dir)
        elif op in ("insert", "prepend", "append", "replace-lines"):
            current["text"] = resolve_operation_value(current, "text", True, args.arg_encoding, base_dir)
        resolved.append(current)
    return resolved, base_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely edit one text file.")
    parser.add_argument(
        "command",
        choices=(
            "inspect",
            "edit",
            "regex",
            "insert",
            "prepend",
            "append",
            "delete",
            "replace-lines",
            "delete-lines",
            "batch",
        ),
    )
    parser.add_argument("--file", required=True)
    parser.add_argument(
        "--encoding",
        default="auto",
        help="auto, utf-8, utf-8-bom, gbk, shift-jis, big5, latin-1, utf-16-le, utf-16-be",
    )
    parser.add_argument("--arg-encoding", default="utf-8", help="encoding for --*-file and --ops-file")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-write", action="store_true")
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("--context", type=int, default=3)
    parser.add_argument("--allow-nul", action="store_true")
    parser.add_argument("--follow-symlink", action="store_true")
    parser.add_argument("--max-bytes", type=int, default=50 * 1024 * 1024)
    parser.add_argument("--lock-timeout", type=float, default=10.0)
    parser.add_argument("--no-lock", action="store_true")
    parser.add_argument("--json", action="store_true")

    parser.add_argument("--old")
    parser.add_argument("--old-file")
    parser.add_argument("--old-stdin", action="store_true")
    parser.add_argument("--new")
    parser.add_argument("--new-file")
    parser.add_argument("--new-stdin", action="store_true")

    parser.add_argument("--pattern")
    parser.add_argument("--pattern-file")
    parser.add_argument("--pattern-stdin", action="store_true")
    parser.add_argument("--replacement")
    parser.add_argument("--replacement-file")
    parser.add_argument("--replacement-stdin", action="store_true")
    parser.add_argument("--flags", default="")
    parser.add_argument("--count", type=int, default=0)
    parser.add_argument("--literal-replacement", action="store_true")

    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--first", action="store_true")
    parser.add_argument("--no-op-ok", action="store_true")

    parser.add_argument("--line", type=int)
    parser.add_argument("--start", type=int)
    parser.add_argument("--end", type=int)
    parser.add_argument("--text")
    parser.add_argument("--text-file")
    parser.add_argument("--text-stdin", action="store_true")

    parser.add_argument("--ops")
    parser.add_argument("--ops-file")
    parser.add_argument("--ops-stdin", action="store_true")
    return parser


def run(args: argparse.Namespace) -> Dict[str, Any]:
    path = resolve_target_path(args.file, args.follow_symlink)
    if args.command == "inspect":
        original = read_target(path, args.max_bytes)
        encoding = detect_encoding(original, args.encoding)
        text = strict_decode(original, encoding)
        return inspect_target(path, original, encoding, text)

    operations, _base_dir = command_to_operations(args)
    lock_context = NullLock() if args.no_lock or args.dry_run else FileLock(path, args.lock_timeout)

    with lock_context:
        original = read_target(path, args.max_bytes)
        encoding = detect_encoding(original, args.encoding)
        text = strict_decode(original, encoding)
        if "\x00" in text and not args.allow_nul:
            fail("decoded text contains NUL bytes; refusing likely binary content")

        newline_style, line_counts, mixed_line_endings = detect_line_ending(text)
        newline = line_sep(newline_style)

        new_text = text
        operation_results: List[Dict[str, Any]] = []
        for index, operation in enumerate(operations, start=1):
            new_text, changed, op = apply_operation(new_text, operation, newline)
            operation_results.append({"index": index, "op": op, "changed": changed})

        output = encode_text(new_text, encoding)
        diff_text = generate_diff(path, text, new_text, args.context) if args.diff else ""
        summary: Dict[str, Any] = {
            "file": str(path),
            "command": args.command,
            "encoding": encoding.name,
            "lineEnding": newline_style,
            "mixedLineEndings": mixed_line_endings,
            "lineEndingCounts": line_counts,
            "changed": sum(item["changed"] for item in operation_results),
            "operations": operation_results,
            "dryRun": args.dry_run,
            "backup": None,
            "written": False,
            "skipped": False,
            "wouldChangeBytes": output != original,
        }
        if args.diff:
            summary["diff"] = diff_text

        if not args.dry_run:
            if output == original and not args.force_write:
                summary["skipped"] = True
            else:
                backup = atomic_replace(path, output, args.backup)
                verification = path.read_bytes()
                if verification != output:
                    fail("post-write verification failed: bytes on disk do not match intended output")
                summary["backup"] = backup
                summary["written"] = True
        return summary


def emit_summary(summary: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return
    if "diff" in summary and summary["diff"]:
        print(summary["diff"])
    note = "DRY-RUN " if summary["dryRun"] else ""
    if summary.get("command") == "inspect":
        print(
            f"Inspect: {summary['file']} "
            f"(encoding={summary['encoding']}, lineEnding={summary['lineEnding']}, "
            f"lines={summary['lineCount']}, bytes={summary['sizeBytes']})"
        )
        if summary["mixedLineEndings"]:
            print(f"Warning: mixed line endings detected: {summary['lineEndingCounts']}", file=sys.stderr)
        if summary["hasNul"]:
            print("Warning: decoded text contains NUL bytes", file=sys.stderr)
        return
    print(
        f"{note}Done: {summary['command']} on {summary['file']} "
        f"(encoding={summary['encoding']}, lineEnding={summary['lineEnding']}, "
        f"changed={summary['changed']})"
    )
    if summary.get("skipped"):
        print("Skipped write: output bytes are identical to the original")
    if summary["mixedLineEndings"]:
        print(f"Warning: mixed line endings detected: {summary['lineEndingCounts']}", file=sys.stderr)
    if summary["backup"]:
        print(f"Backup: {summary['backup']}")


def main(argv: List[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        summary = run(args)
        emit_summary(summary, args.json)
        return 0
    except SafeEditError as exc:
        print(f"safe-edit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
