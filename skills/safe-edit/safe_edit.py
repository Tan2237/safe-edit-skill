#!/usr/bin/env python3
"""Safe text-file edits with strict decoding and atomic replacement."""

from __future__ import annotations

import argparse
import codecs
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


class SafeEditError(Exception):
    pass


@dataclass(frozen=True)
class EncodingInfo:
    name: str
    codec: str
    bom: bytes = b""


ENCODINGS = {
    "utf-8": EncodingInfo("utf-8", "utf-8"),
    "utf-8-bom": EncodingInfo("utf-8-bom", "utf-8", codecs.BOM_UTF8),
    "gbk": EncodingInfo("gbk", "gbk"),
    "utf-16-le": EncodingInfo("utf-16-le", "utf-16-le", codecs.BOM_UTF16_LE),
    "utf-16-be": EncodingInfo("utf-16-be", "utf-16-be", codecs.BOM_UTF16_BE),
}


def fail(message: str) -> None:
    raise SafeEditError(message)


def normalize_encoding(value: str | None) -> str:
    value = (value or "auto").lower().replace("_", "-")
    aliases = {
        "utf8": "utf-8",
        "utf8-bom": "utf-8-bom",
        "utf-8-sig": "utf-8-bom",
        "cp936": "gbk",
        "gb2312": "gbk",
        "utf16-le": "utf-16-le",
        "utf16-be": "utf-16-be",
    }
    return aliases.get(value, value)


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


def detect_encoding(data: bytes, requested: str) -> EncodingInfo:
    requested = normalize_encoding(requested)
    if requested != "auto":
        if requested not in ENCODINGS:
            fail(f"unsupported encoding: {requested}")
        return ENCODINGS[requested]

    if data.startswith(codecs.BOM_UTF8):
        return ENCODINGS["utf-8-bom"]
    if data.startswith(codecs.BOM_UTF16_LE):
        return ENCODINGS["utf-16-le"]
    if data.startswith(codecs.BOM_UTF16_BE):
        return ENCODINGS["utf-16-be"]
    if not data:
        return ENCODINGS["utf-8"]

    try:
        data.decode("utf-8", errors="strict")
        return ENCODINGS["utf-8"]
    except UnicodeDecodeError:
        pass

    try:
        data.decode("gbk", errors="strict")
        return ENCODINGS["gbk"]
    except UnicodeDecodeError as exc:
        fail(
            "unable to auto-detect encoding as UTF-8, UTF-8 BOM, UTF-16 BOM, or GBK; "
            f"use --encoding to override ({exc})"
        )


def detect_line_ending(text: str) -> tuple[str, dict[str, int], bool]:
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


def split_records(text: str) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []
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


def join_records(records: list[tuple[str, str]]) -> str:
    return "".join(line + sep for line, sep in records)


def normalize_user_newlines(text: str, sep: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", sep)


def insertion_records(text: str, sep: str) -> list[tuple[str, str]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if not lines:
        lines = [""]
    return [(line, sep) for line in lines]


def apply_edit(text: str, args: argparse.Namespace, newline: str) -> tuple[str, int]:
    old = args.old
    new = normalize_user_newlines(args.new, newline)
    if old == "":
        fail("--old must not be empty")
    actual = text.count(old)
    if args.expected_count is not None and actual != args.expected_count:
        fail(f"expected {args.expected_count} occurrence(s), found {actual}")
    if actual == 0 and not args.no_op_ok:
        fail("--old was not found; refusing a silent no-op")
    count = 1 if args.first else -1
    return (text.replace(old, new, count), min(actual, 1) if args.first else actual)


def apply_insert(text: str, args: argparse.Namespace, newline: str) -> tuple[str, int]:
    records = split_records(text)
    line_count = len(records)
    if args.line < 1 or args.line > line_count + 1:
        fail(f"--line must be between 1 and {line_count + 1}, got {args.line}")

    to_insert = insertion_records(args.text, newline)
    if not records:
        if to_insert:
            to_insert[-1] = (to_insert[-1][0], "")
        return (join_records(to_insert), len(to_insert))

    index = args.line - 1
    if index == len(records) and records[-1][1] == "":
        records[-1] = (records[-1][0], newline)
    return (join_records(records[:index] + to_insert + records[index:]), len(to_insert))


def apply_delete(text: str, args: argparse.Namespace) -> tuple[str, int]:
    records = split_records(text)
    line_count = len(records)
    if args.line < 1 or args.line > line_count:
        fail(f"--line must be between 1 and {line_count}, got {args.line}")
    index = args.line - 1
    return (join_records(records[:index] + records[index + 1 :]), 1)


def atomic_replace(path: Path, data: bytes, keep_backup: bool) -> str | None:
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


def read_target(path_value: str, follow_symlink: bool, max_bytes: int) -> tuple[Path, bytes]:
    path = Path(path_value)
    if not path.exists():
        fail(f"file not found: {path}")
    if path.is_symlink() and not follow_symlink:
        fail("refusing to edit a symlink without --follow-symlink")
    if follow_symlink:
        path = path.resolve()
    if not path.is_file():
        fail(f"not a regular file: {path}")
    size = path.stat().st_size
    if size > max_bytes:
        fail(f"file is {size} bytes, exceeding --max-bytes {max_bytes}")
    return (path, path.read_bytes())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely edit one text file.")
    parser.add_argument("command", choices=("edit", "insert", "delete"))
    parser.add_argument("--file", required=True)
    parser.add_argument("--encoding", default="auto")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-nul", action="store_true")
    parser.add_argument("--follow-symlink", action="store_true")
    parser.add_argument("--max-bytes", type=int, default=50 * 1024 * 1024)
    parser.add_argument("--json", action="store_true")

    parser.add_argument("--old")
    parser.add_argument("--new")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--first", action="store_true")
    parser.add_argument("--no-op-ok", action="store_true")

    parser.add_argument("--line", type=int)
    parser.add_argument("--text")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.command == "edit":
        if args.old is None:
            fail("missing --old")
        if args.new is None:
            fail("missing --new")
    elif args.command == "insert":
        if args.line is None:
            fail("missing --line")
        if args.text is None:
            fail("missing --text")
    elif args.command == "delete":
        if args.line is None:
            fail("missing --line")


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        validate_args(args)
        path, original = read_target(args.file, args.follow_symlink, args.max_bytes)
        encoding = detect_encoding(original, args.encoding)
        text = strict_decode(original, encoding)
        if "\x00" in text and not args.allow_nul:
            fail("decoded text contains NUL bytes; refusing likely binary content")

        newline_style, line_counts, mixed_line_endings = detect_line_ending(text)
        newline = line_sep(newline_style)

        if args.command == "edit":
            new_text, changed = apply_edit(text, args, newline)
        elif args.command == "insert":
            new_text, changed = apply_insert(text, args, newline)
        else:
            new_text, changed = apply_delete(text, args)

        output = encode_text(new_text, encoding)
        summary = {
            "file": str(path),
            "command": args.command,
            "encoding": encoding.name,
            "lineEnding": newline_style,
            "mixedLineEndings": mixed_line_endings,
            "lineEndingCounts": line_counts,
            "changed": changed,
            "dryRun": args.dry_run,
            "backup": None,
        }

        if not args.dry_run:
            backup = atomic_replace(path, output, args.backup)
            verification = path.read_bytes()
            if verification != output:
                fail("post-write verification failed: bytes on disk do not match intended output")
            summary["backup"] = backup

        if args.json:
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        else:
            note = "DRY-RUN " if args.dry_run else ""
            print(
                f"{note}Done: {args.command} on {path} "
                f"(encoding={encoding.name}, lineEnding={newline_style}, changed={changed})"
            )
            if mixed_line_endings:
                print(f"Warning: mixed line endings detected: {line_counts}", file=sys.stderr)
            if summary["backup"]:
                print(f"Backup: {summary['backup']}")
        return 0
    except SafeEditError as exc:
        print(f"safe-edit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
