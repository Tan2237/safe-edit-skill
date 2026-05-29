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


def classify_error_type(message: str) -> str:
    """Classify a SafeEditError message into a structured error type.
    
    Returns one of:
    - "match_not_found": old text / regex pattern not found in file
    - "match_ambiguous": multiple matches when one expected
    - "match_count_mismatch": expected_count doesn't match actual
    - "encoding_error": encoding/decoding failure
    - "file_error": file I/O or path issue
    - "validation_error": invalid arguments or constraint violation
    - "lock_error": file lock contention
    - "unknown": unclassified error
    """
    msg = message.lower()
    if "was not found" in msg or "not found" in msg and "refusing" in msg:
        return "match_not_found"
    if "anchor pattern found" in msg and "times" in msg:
        return "match_ambiguous"
    if "expected" in msg and "occurrence" in msg or "match" in msg and "found" in msg:
        return "match_count_mismatch"
    if "decode" in msg or "encode" in msg or "encoding" in msg or "bom" in msg:
        return "encoding_error"
    if "file not found" in msg or "not a regular" in msg or "symlink" in msg or "failed to read" in msg or "failed to" in msg and "file" in msg or "exceeding" in msg and "max-bytes" in msg:
        return "file_error"
    if "lock already exists" in msg or "lock file" in msg or "stale lock" in msg:
        return "lock_error"
    if "diff-input format" in msg or "search/replace" in msg:
        return "format_error"
    if "must" in msg or "requires" in msg or "missing" in msg or "unsupported" in msg or "invalid" in msg or "out of range" in msg:
        return "validation_error"
    return "unknown"


def emit_json_error(
    exc: SafeEditError,
    file_path: str = "",
    command: str = "",
    *,
    suggestions: Optional[List[Dict[str, Any]]] = None,
    nearby_content: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit a structured JSON error object to stdout for Agent consumption.
    
    This ensures that even on failure, --json mode returns parseable JSON
    with actionable information for automatic retry.
    """
    error_type = classify_error_type(str(exc))
    error_obj: Dict[str, Any] = {
        "ok": False,
        "error": {
            "type": error_type,
            "message": str(exc),
        },
        "file": file_path,
        "command": command,
        "changed": 0,
        "operations": [],
        "dryRun": False,
        "written": False,
        "skipped": False,
    }
    if suggestions:
        error_obj["suggestions"] = suggestions
    if nearby_content:
        error_obj["nearbyContent"] = nearby_content
    print(json.dumps(error_obj, ensure_ascii=False, sort_keys=True))


def visualize_whitespace(text: str) -> str:
    """Convert whitespace characters to visible symbols for debugging."""
    return (
        text.replace("\t", "[TAB]")
        .replace(" ", "[SP]")
        .replace("\r", "[CR]")
        .replace("\n", "[LF]\n")
    )


def find_closest_match(text: str, pattern: str, max_lines: int = 10) -> Optional[Tuple[int, str]]:
    """Find the closest matching fragment in text for a pattern.
    
    Returns (line_number, fragment) or None if no reasonable match found.
    Uses difflib to find the most similar substring.
    """
    if not pattern or not text:
        return None
    
    # Split pattern into lines for comparison
    pattern_lines = pattern.splitlines()
    if not pattern_lines:
        return None
    
    text_lines = text.splitlines()
    pattern_len = len(pattern_lines)
    
    # For single-line patterns, do character-level comparison with each line
    if pattern_len == 1:
        best_score = 0.0
        best_line = 0
        best_fragment = ""
        
        for i, line in enumerate(text_lines):
            # Check if pattern is substring of line
            if pattern in line:
                return (i + 1, line)
            
            # Check if line is substring of pattern
            if line in pattern:
                score = len(line) / len(pattern)
                if score > best_score:
                    best_score = score
                    best_line = i
                    best_fragment = line
                continue
            
            # Character-level similarity
            matcher = difflib.SequenceMatcher(None, pattern, line)
            score = matcher.ratio()
            if score > best_score:
                best_score = score
                best_line = i
                best_fragment = line
        
        # Lower threshold for single-line patterns (30% instead of 50%)
        if best_score >= 0.3:
            return (best_line + 1, best_fragment)
        
        return None
    
    # Multi-line pattern matching
    best_score = 0.0
    best_start = 0
    
    for i in range(len(text_lines) - pattern_len + 1):
        fragment = text_lines[i:i + pattern_len]
        # Calculate similarity using SequenceMatcher
        matcher = difflib.SequenceMatcher(None, pattern_lines, fragment)
        score = matcher.ratio()
        if score > best_score:
            best_score = score
            best_start = i
    
    # Only return if we found something reasonably close (>= 50% similar)
    if best_score >= 0.5:
        fragment_lines = text_lines[best_start:best_start + pattern_len]
        fragment_text = "\n".join(fragment_lines)
        return (best_start + 1, fragment_text)  # 1-based line number
    
    return None


def extract_nearby_content(
    text: str,
    pattern: str,
    context_lines: int = 5,
) -> Optional[Dict[str, Any]]:
    """Extract content near the closest match for Agent retry.
    
    Returns a dict with:
    - line: 1-based line number of closest match
    - content: the nearby content snippet
    - similarity: 0.0-1.0 similarity score
    
    Or None if no reasonable match found.
    """
    result = find_closest_match(text, pattern)
    if result is None:
        return None
    
    line_num, fragment = result
    text_lines = text.splitlines()
    
    # Calculate similarity score
    pattern_lines = pattern.splitlines()
    if len(pattern_lines) == 1:
        matcher = difflib.SequenceMatcher(None, pattern, fragment)
        similarity = matcher.ratio()
    else:
        fragment_lines = fragment.splitlines()
        matcher = difflib.SequenceMatcher(None, pattern_lines, fragment_lines)
        similarity = matcher.ratio()
    
    # Extract context around the match
    start = max(0, line_num - 1 - context_lines)
    end = min(len(text_lines), line_num - 1 + len(pattern_lines) + context_lines)
    context = "\n".join(text_lines[start:end])
    
    return {
        "line": line_num,
        "content": context,
        "similarity": round(similarity, 3),
    }


def explain_match_failure(expected: str, actual_text: str, context_lines: int = 3) -> str:
    """Generate a detailed explanation of why a match failed."""
    lines = []
    lines.append("Match failed. Closest match found:")
    
    result = find_closest_match(actual_text, expected)
    if result:
        line_num, fragment = result
        lines.append(f"  at line {line_num}:")
        lines.append("")
        lines.append("EXPECTED:")
        for line in expected.splitlines()[:context_lines]:
            lines.append(f"  {visualize_whitespace(line)}")
        if len(expected.splitlines()) > context_lines:
            lines.append("  ...")
        
        lines.append("")
        lines.append("ACTUAL:")
        for line in fragment.splitlines()[:context_lines]:
            lines.append(f"  {visualize_whitespace(line)}")
        if len(fragment.splitlines()) > context_lines:
            lines.append("  ...")
        
        # Analyze differences
        lines.append("")
        lines.append("Differences:")
        
        expected_lines = expected.splitlines()
        actual_lines = fragment.splitlines()
        
        # Check indentation
        for i, (e, a) in enumerate(zip(expected_lines[:3], actual_lines[:3])):
            e_indent = len(e) - len(e.lstrip())
            a_indent = len(a) - len(a.lstrip())
            if e_indent != a_indent:
                e_ws = e[:e_indent]
                a_ws = a[:a_indent]
                if '\t' in a_ws and '\t' not in e_ws:
                    lines.append(f"  - line {i+1}: indentation uses tabs instead of spaces")
                elif ' ' in a_ws and '\t' not in a_ws and '\t' in e_ws:
                    lines.append(f"  - line {i+1}: indentation uses spaces instead of tabs")
                else:
                    lines.append(f"  - line {i+1}: indentation differs ({e_indent} vs {a_indent} chars)")
        
        # Check line ending
        if '\r\n' in expected and '\r\n' not in fragment:
            lines.append("  - line ending differs (expected CRLF, found LF)")
        elif '\r\n' not in expected and '\r\n' in fragment:
            lines.append("  - line ending differs (expected LF, found CRLF)")
        
        # Check for missing/extra lines
        if len(expected_lines) != len(actual_lines):
            lines.append(f"  - line count differs (expected {len(expected_lines)}, found {len(actual_lines)})")
    else:
        lines.append("  No close match found in file.")
    
    return "\n".join(lines)


def find_context_anchor(text: str, context_pattern: str, occurrence: Optional[int] = None) -> int:
    """Find the line number of a context anchor pattern.
    
    Args:
        text: The file content
        context_pattern: The pattern to search for (literal match)
        occurrence: Which occurrence to use (1-based). If None and multiple matches, raises error.
    
    Returns:
        1-based line number where the anchor was found
    
    Raises:
        SafeEditError if pattern not found or ambiguous
    """
    lines = text.splitlines()
    matches = []
    
    for i, line in enumerate(lines):
        if context_pattern in line:
            matches.append(i + 1)  # 1-based line number
    
    if len(matches) == 0:
        fail(f"anchor pattern not found: {context_pattern}")
    
    if occurrence is None:
        if len(matches) > 1:
            fail(
                f"anchor pattern found {len(matches)} times at lines {matches}; "
                f"use --anchor-occurrence to disambiguate"
            )
        return matches[0]
    
    if occurrence < 1 or occurrence > len(matches):
        fail(
            f"anchor-occurrence {occurrence} out of range; "
            f"pattern found {len(matches)} times at lines {matches}"
        )
    
    return matches[occurrence - 1]


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


def encoding_for_output(name: str, original: EncodingInfo) -> EncodingInfo:
    name = normalize_encoding(name)
    if name == "preserve":
        return original
    return make_encoding_info(name)


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


def convert_line_endings(text: str, style: str) -> str:
    if style == "preserve":
        return text
    return normalize_user_newlines(text, line_sep(style))


def trim_trailing_whitespace(text: str) -> str:
    return join_records((line.rstrip(" \t"), sep) for line, sep in split_records(text))


def set_final_newline(text: str, mode: str, sep: str) -> str:
    if mode == "preserve":
        return text
    if mode == "ensure":
        return text if text.endswith(("\n", "\r")) else text + sep
    if mode == "strip":
        while text.endswith("\r\n"):
            text = text[:-2]
        while text.endswith(("\n", "\r")):
            text = text[:-1]
        return text
    fail(f"unsupported final newline mode: {mode}")


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


def normalize_for_match(text: str, ignore_indent: bool = False, ignore_eol: bool = False, normalize_whitespace: bool = False) -> str:
    """Normalize text for matching with controlled whitespace flexibility.
    
    This is a controlled relaxation of strict matching, not magic.
    Each flag explicitly enables a specific normalization.
    
    Args:
        text: The text to normalize
        ignore_indent: If True, remove leading whitespace from each line
        ignore_eol: If True, normalize all line endings to LF
        normalize_whitespace: If True, collapse consecutive whitespace to single space
    
    Returns:
        Normalized text for matching
    """
    if ignore_indent:
        # Remove leading whitespace from each line
        text = '\n'.join(line.lstrip() for line in text.split('\n'))
    
    if ignore_eol:
        # Normalize all line endings to LF
        text = text.replace('\r\n', '\n').replace('\r', '\n')
    
    if normalize_whitespace:
        # Collapse consecutive whitespace to single space
        text = re.sub(r'\s+', ' ', text)
    
    return text


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


def _determine_match_strategy(ignore_indent: bool, ignore_eol: bool, normalize_whitespace: bool) -> str:
    """Return a human-readable match strategy label for JSON output."""
    if normalize_whitespace:
        return "normalize-whitespace"
    if ignore_indent and ignore_eol:
        return "ignore-indent+ignore-eol"
    if ignore_indent:
        return "ignore-indent"
    if ignore_eol:
        return "ignore-eol"
    return "exact"


# The auto-match cascade: each entry is (strategy_label, ignore_indent, ignore_eol, normalize_whitespace)
_AUTO_MATCH_PIPELINE: List[Tuple[str, bool, bool, bool]] = [
    ("exact", False, False, False),
    ("ignore-eol", False, True, False),
    ("ignore-indent", True, False, False),
    ("ignore-indent+ignore-eol", True, True, False),
    ("normalize-whitespace", False, False, True),
]


def apply_literal_edit_cascade(
    text: str,
    operation: Dict[str, Any],
    newline: str,
    explain: bool = False,
    fuzzy: bool = False,
    context_before: Optional[str] = None,
    context_after: Optional[str] = None,
) -> Tuple[str, int, str]:
    """Apply a literal edit with automatic progressive relaxation of match strictness.
    
    Tries each match strategy in order from most strict to most relaxed:
      exact → ignore-eol → ignore-indent → ignore-indent+ignore-eol → normalize-whitespace
    
    If --fuzzy is enabled and all above fail, attempts a fuzzy match using
    find_closest_match() with similarity >= 0.6.
    
    Returns (new_text, changed_count, match_strategy) where match_strategy
    indicates which level succeeded.
    
    Raises SafeEditError if no level can find a match.
    """
    last_error: Optional[SafeEditError] = None
    
    for strategy_label, ignore_indent, ignore_eol, normalize_whitespace in _AUTO_MATCH_PIPELINE:
        try:
            new_text, changed, _ = apply_literal_edit(
                text, operation, newline,
                explain=False,  # suppress per-level explanation; we'll report the final one
                ignore_indent=ignore_indent,
                ignore_eol=ignore_eol,
                normalize_whitespace=normalize_whitespace,
                match_strategy=strategy_label,
                context_before=context_before,
                context_after=context_after,
            )
            # Success — return with the strategy that worked
            return (new_text, changed, strategy_label)
        except SafeEditError as exc:
            # Only catch "not found" errors; let other errors (count mismatch, etc.) propagate
            if "was not found" not in str(exc) and "not found" not in str(exc):
                raise
            last_error = exc
            continue
    
    # All normalization levels failed — try fuzzy if enabled
    if fuzzy:
        old = str(operation["old"])
        result = find_closest_match(text, old)
        if result is not None:
            line_num, fragment = result
            pattern_lines = old.splitlines()
            if len(pattern_lines) == 1:
                matcher = difflib.SequenceMatcher(None, old, fragment)
                similarity = matcher.ratio()
            else:
                fragment_lines = fragment.splitlines()
                matcher = difflib.SequenceMatcher(None, pattern_lines, fragment_lines)
                similarity = matcher.ratio()
            
            if similarity >= 0.6:
                # Fuzzy match found — replace the closest match
                new = normalize_user_newlines(str(operation["new"]), newline)
                new_text = text.replace(fragment, new, 1)
                return (new_text, 1, "fuzzy")
    
    # Nothing worked — raise the original error (with explanation if requested)
    if last_error is not None:
        if explain:
            old = str(operation["old"])
            explanation = explain_match_failure(old, text)
            fail(f"old text was not found (auto-match exhausted all strategies); refusing a silent no-op\n\n{explanation}")
        else:
            fail("old text was not found (auto-match exhausted all strategies); refusing a silent no-op")
    raise last_error  # type: ignore[misc]


def parse_diff_input(diff_text: str) -> List[Dict[str, Any]]:
    """Parse SEARCH/REPLACE diff format into a list of edit operations.
    
    Supported marker formats (case-insensitive, flexible marker length):
      ------- SEARCH  /  =======  /  +++++++ REPLACE
      --- SEARCH      /  ===      /  +++ REPLACE
      <<< SEARCH      /  ===      /  >>> REPLACE
    
    Multiple SEARCH/REPLACE blocks are allowed in one input.
    
    Returns:
        List of {"op": "edit", "old": "...", "new": "..."} dicts.
    
    Raises:
        SafeEditError if format is invalid or no valid blocks found.
    """
    operations: List[Dict[str, Any]] = []
    lines = diff_text.split('\n')
    
    search_open_re = re.compile(r'^[-<]{3,}\s*SEARCH\s*$', re.IGNORECASE)
    separator_re = re.compile(r'^={3,}\s*$', re.IGNORECASE)
    replace_close_re = re.compile(r'^[+>]{3,}\s*REPLACE\s*$', re.IGNORECASE)
    
    state = 'outside'
    old_lines: List[str] = []
    new_lines: List[str] = []
    
    for line in lines:
        if state == 'outside':
            if search_open_re.match(line):
                state = 'search'
                old_lines = []
                new_lines = []
        elif state == 'search':
            if separator_re.match(line):
                state = 'replace'
            else:
                old_lines.append(line)
        elif state == 'replace':
            if replace_close_re.match(line):
                old_text = '\n'.join(old_lines)
                new_text = '\n'.join(new_lines)
                if old_text:
                    operations.append({"op": "edit", "old": old_text, "new": new_text})
                state = 'outside'
            else:
                new_lines.append(line)
    
    # Handle unterminated block (missing REPLACE marker)
    if state == 'replace' and old_lines:
        old_text = '\n'.join(old_lines)
        new_text = '\n'.join(new_lines)
        if old_text:
            operations.append({"op": "edit", "old": old_text, "new": new_text})
    
    if not operations:
        fail("invalid diff-input format: no valid SEARCH/REPLACE blocks found")
    
    return operations


def _apply_edit_with_context(
    text: str,
    old: str,
    new: str,
    normalized_text: str,
    normalized_old: str,
    operation: Dict[str, Any],
    effective_strategy: str,
    explain: bool,
    ignore_indent: bool,
    ignore_eol: bool,
    normalize_whitespace: bool,
    context_before: str,
    context_after: str,
) -> Tuple[str, int, str]:
    """Apply a literal edit with context-based disambiguation.
    
    When --context-before or --context-after is specified, find all matches,
    filter by context text, and replace only context-matching occurrences.
    Context matching uses substring containment (``in`` operator).
    """
    # Find all match positions
    if ignore_indent or ignore_eol or normalize_whitespace:
        positions: List[Tuple[int, int]] = []
        search_start = 0
        search_start_orig = 0
        while True:
            norm_pos = normalized_text.find(normalized_old, search_start)
            if norm_pos < 0:
                break
            original_pos, original_len = find_original_position(
                text, normalized_text, norm_pos, normalized_old, old,
                ignore_indent, ignore_eol, normalize_whitespace,
                start_search_pos=search_start_orig,
            )
            if original_pos >= 0:
                positions.append((original_pos, original_len))
                search_start_orig = original_pos + original_len
            search_start = norm_pos + len(normalized_old)
    else:
        positions = []
        start = 0
        while True:
            pos = text.find(old, start)
            if pos < 0:
                break
            positions.append((pos, len(old)))
            start = pos + len(old)
    
    # Filter by context (search within a window near the match, not the entire file)
    old_line_count = max(1, old.count('\n') + 1)
    ctx_window = max(old_line_count, 2)  # lines of context to check
    filtered: List[Tuple[int, int]] = []
    for pos, length in positions:
        if context_before:
            # Check only the few lines immediately before the match
            before_text = text[:pos]
            before_lines = before_text.split('\n')
            window = '\n'.join(before_lines[-ctx_window:])
            if context_before not in window:
                continue
        if context_after:
            # Check only the few lines immediately after the match
            after_text = text[pos + length:]
            after_lines = after_text.split('\n')
            window = '\n'.join(after_lines[:ctx_window])
            if context_after not in window:
                continue
        filtered.append((pos, length))
    
    # Check expected_count against filtered matches
    expected = operation.get("expected_count")
    # When filtered is empty, always report "not found" regardless of expected_count
    if len(filtered) == 0 and not bool(operation.get("no_op_ok", False)):
        if explain:
            explanation = explain_match_failure(old, text)
            fail(f"old text was not found (after context filtering); refusing a silent no-op\n\n{explanation}")
        else:
            fail("old text was not found (after context filtering); refusing a silent no-op")
    if expected is not None and len(filtered) != int(expected):
        fail(f"expected {expected} occurrence(s) after context filtering, found {len(filtered)}")
    
    # Apply --first if specified
    use_first = bool(operation.get("first", False))
    matches_to_replace = filtered[:1] if use_first else filtered
    
    # Replace from end to start to preserve positions
    result_text = text
    count_replaced = 0
    for pos, length in sorted(matches_to_replace, key=lambda x: x[0], reverse=True):
        original_matched = result_text[pos:pos + length]
        adjusted_new = adjust_replacement_for_indent(original_matched, new, ignore_indent)
        result_text = result_text[:pos] + adjusted_new + result_text[pos + length:]
        count_replaced += 1
    
    return (result_text, count_replaced, effective_strategy)


def apply_literal_edit(text: str, operation: Dict[str, Any], newline: str, explain: bool = False, 
                        ignore_indent: bool = False, ignore_eol: bool = False, normalize_whitespace: bool = False,
                        match_strategy: Optional[str] = None, context_before: Optional[str] = None,
                        context_after: Optional[str] = None) -> Tuple[str, int, str]:
    """Apply a literal text replacement.
    
    Returns (new_text, changed_count, match_strategy).
    match_strategy indicates how the match was performed (e.g. "exact", "ignore-eol").
    """
    old = str(operation["old"])
    new = normalize_user_newlines(str(operation["new"]), newline)
    if old == "":
        fail("old text must not be empty")
    
    # Determine and record the effective match strategy
    effective_strategy = match_strategy or _determine_match_strategy(ignore_indent, ignore_eol, normalize_whitespace)
    
    # Apply controlled whitespace normalization for matching only
    # The replacement text (new) is NOT normalized - it's used as-is
    normalized_text = normalize_for_match(text, ignore_indent, ignore_eol, normalize_whitespace)
    normalized_old = normalize_for_match(old, ignore_indent, ignore_eol, normalize_whitespace)
    
    # If context filtering is needed, delegate to position-based approach
    if context_before or context_after:
        return _apply_edit_with_context(
            text, old, new, normalized_text, normalized_old,
            operation, effective_strategy, explain,
            ignore_indent, ignore_eol, normalize_whitespace,
            context_before, context_after,
        )
    
    # Count matches using normalized versions
    actual = normalized_text.count(normalized_old)
    expected = operation.get("expected_count")
    # When actual is 0, always report "not found" regardless of expected_count
    # (the real problem is missing text, not wrong count)
    if actual == 0 and not bool(operation.get("no_op_ok", False)):
        if explain:
            explanation = explain_match_failure(old, text)
            fail(f"old text was not found; refusing a silent no-op\n\n{explanation}")
        else:
            fail("old text was not found; refusing a silent no-op")
    if expected is not None and actual != int(expected):
        fail(f"expected {expected} occurrence(s), found {actual}")
    
    # Perform replacement on original text (not normalized)
    # We need to find the actual positions in the original text
    if ignore_indent or ignore_eol or normalize_whitespace:
        # When normalization is used, we need to find matches in original text
        # by mapping normalized positions back to original positions
        result_text = text
        count_replaced = 0
        
        # Find all matches in normalized text and map to original
        if bool(operation.get("first", False)):
            # Replace only first match
            # Find the first occurrence in normalized text
            norm_pos = normalized_text.find(normalized_old)
            if norm_pos >= 0:
                # Find corresponding position in original text
                # The matched content in original text may have different length than original_old
                original_pos, original_len = find_original_position(
                    text, normalized_text, norm_pos, normalized_old, old,
                    ignore_indent, ignore_eol, normalize_whitespace
                )
                if original_pos >= 0:
                    # Adjust replacement to preserve original indentation
                    original_matched = text[original_pos:original_pos + original_len]
                    adjusted_new = adjust_replacement_for_indent(original_matched, new, ignore_indent)
                    result_text = text[:original_pos] + adjusted_new + text[original_pos + original_len:]
                    count_replaced = 1
        else:
            # Replace all matches
            # Find all occurrences and replace them
            positions = []
            search_start = 0
            search_start_orig = 0
            while True:
                norm_pos = normalized_text.find(normalized_old, search_start)
                if norm_pos < 0:
                    break
                original_pos, original_len = find_original_position(
                    text, normalized_text, norm_pos, normalized_old, old,
                    ignore_indent, ignore_eol, normalize_whitespace,
                    start_search_pos=search_start_orig
                )
                if original_pos >= 0:
                    positions.append((original_pos, original_len))
                    search_start_orig = original_pos + original_len
                search_start = norm_pos + len(normalized_old)
            
            # Replace from end to start to preserve positions
            for original_pos, original_len in sorted(positions, key=lambda x: x[0], reverse=True):
                # Adjust replacement to preserve original indentation
                original_matched = result_text[original_pos:original_pos + original_len]
                adjusted_new = adjust_replacement_for_indent(original_matched, new, ignore_indent)
                result_text = result_text[:original_pos] + adjusted_new + result_text[original_pos + original_len:]
                count_replaced += 1
        
        return (result_text, count_replaced, effective_strategy)
    else:
        # No normalization - use original simple logic
        count = 1 if bool(operation.get("first", False)) else -1
        return (text.replace(old, new, count), min(actual, 1) if count == 1 else actual, effective_strategy)


def find_original_position(original_text: str, normalized_text: str, norm_pos: int, 
                            normalized_old: str, original_old: str,
                            ignore_indent: bool, ignore_eol: bool, normalize_whitespace: bool,
                            start_search_pos: int = 0) -> Tuple[int, int]:
    """Find the position in original text corresponding to a normalized match position.
    
    This maps a match found in normalized text back to the original text.
    Returns (position, length) tuple where position is the start in original text
    and length is the length of the matched content in original text.
    """
    # Simple case: if no normalization, position is same
    if not ignore_indent and not ignore_eol and not normalize_whitespace:
        pos = original_text.find(original_old)
        return (pos, len(original_old)) if pos >= 0 else (-1, 0)
    
    # Strategy: scan through original text, extract substrings of varying lengths,
    # normalize them, and compare with normalized_old
    # The matched content in original text may have different length than original_old
    
    # For efficiency, we use a heuristic: the matched content in original text
    # should have similar length to original_old (within a factor of 2)
    # This handles cases like: tab vs 4 spaces, CRLF vs LF, multiple spaces vs single space
    
    min_len = max(1, len(normalized_old))  # Minimum possible length
    max_len = max(len(original_old), len(original_old) * 2, len(normalized_old) * 3)  # Maximum possible length
    
    search_start = start_search_pos
    while search_start < len(original_text):
        # Try different lengths from this position
        for length in range(min_len, min(max_len + 1, len(original_text) - search_start + 1)):
            candidate = original_text[search_start:search_start + length]
            normalized_candidate = normalize_for_match(candidate, ignore_indent, ignore_eol, normalize_whitespace)
            
            if normalized_candidate == normalized_old:
                return (search_start, length)
        
        search_start += 1
    
    return (-1, 0)


def adjust_replacement_for_indent(original_matched: str, new_text: str, ignore_indent: bool) -> str:
    """Adjust replacement text to preserve original indentation style.
    
    When --ignore-indent is used, the replacement should preserve the original
    indentation style (tabs vs spaces) from the matched content.
    
    Args:
        original_matched: The matched content from the original file
        new_text: The replacement text provided by user
        ignore_indent: Whether --ignore-indent was used
    
    Returns:
        Adjusted replacement text with original indentation preserved
    """
    if not ignore_indent:
        return new_text
    
    # Extract original indentation (leading whitespace of first line)
    original_indent = ""
    for char in original_matched:
        if char in ' \t':
            original_indent += char
        else:
            break
    
    # Extract new text's indentation
    new_indent = ""
    for char in new_text:
        if char in ' \t':
            new_indent += char
        else:
            break
    
    # If both have indentation, replace new indentation with original
    if original_indent and new_indent:
        return original_indent + new_text[len(new_indent):]
    
    # If only original has indentation, prepend it
    if original_indent and not new_indent:
        return original_indent + new_text
    
    # Otherwise, return as-is
    return new_text


def apply_regex_edit(text: str, operation: Dict[str, Any], newline: str, explain: bool = False) -> Tuple[str, int, str]:
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
        if explain:
            # Try to find closest match using the pattern as literal text
            explanation = explain_match_failure(pattern, text)
            fail(f"regex pattern was not found; refusing a silent no-op\n\n{explanation}")
        else:
            fail("regex pattern was not found; refusing a silent no-op")

    count = int(operation.get("count", 0) or 0)
    if bool(operation.get("first", False)):
        count = 1
    if bool(operation.get("literal_replacement", False)):
        new_text, n = compiled.subn(lambda _match: replacement, text, count=count)
        return (new_text, n, "regex")
    try:
        new_text, n = compiled.subn(replacement, text, count=count)
        return (new_text, n, "regex")
    except re.error as exc:
        fail(f"invalid regex replacement: {exc}")


def apply_insert(text: str, operation: Dict[str, Any], newline: str) -> Tuple[str, int, str]:
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
    return (join_records(records[:index] + to_insert + records[index:]), len(to_insert), "line-based")


def apply_prepend(text: str, operation: Dict[str, Any], newline: str) -> Tuple[str, int, str]:
    records = split_records(text)
    text_value = str(operation["text"])
    final_sep = newline if records else (newline if text_value.endswith(("\n", "\r")) else "")
    to_insert = block_records(text_value, newline, final_sep)
    return (join_records(to_insert + records), len(to_insert), "line-based")


def apply_append(text: str, operation: Dict[str, Any], newline: str) -> Tuple[str, int, str]:
    records = split_records(text)
    text_value = str(operation["text"])
    final_sep = newline if text_value.endswith(("\n", "\r")) else ""
    to_insert = block_records(text_value, newline, final_sep)
    if records and records[-1][1] == "":
        records[-1] = (records[-1][0], newline)
    return (join_records(records + to_insert), len(to_insert), "line-based")


def apply_delete_line(text: str, operation: Dict[str, Any]) -> Tuple[str, int, str]:
    records = split_records(text)
    line = int(operation["line"])
    line_count = len(records)
    if line < 1 or line > line_count:
        fail(f"line must be between 1 and {line_count}, got {line}")
    index = line - 1
    return (join_records(records[:index] + records[index + 1 :]), 1, "line-based")


def range_bounds(operation: Dict[str, Any], records: List[Tuple[str, str]], text: str = "") -> Tuple[int, int]:
    """Calculate start and end bounds for line operations.
    
    Supports both absolute line numbers and anchor-based offsets.
    """
    # Check if anchor-based positioning is used
    anchor_pattern = operation.get("anchor_pattern")
    if anchor_pattern:
        occurrence = operation.get("anchor_occurrence")
        anchor_line = find_context_anchor(text, anchor_pattern, occurrence)
        
        # Parse offset values (can be like "+2", "-1", or absolute)
        offset_start = operation.get("offset_start", 0)
        offset_end = operation.get("offset_end", 0)
        
        # Convert string offsets to integers
        if isinstance(offset_start, str):
            if offset_start.startswith("+"):
                start = anchor_line + int(offset_start[1:])
            elif offset_start.startswith("-"):
                start = anchor_line - int(offset_start[1:])
            else:
                start = int(offset_start)
        else:
            start = anchor_line + int(offset_start)
        
        if isinstance(offset_end, str):
            if offset_end.startswith("+"):
                end = anchor_line + int(offset_end[1:])
            elif offset_end.startswith("-"):
                end = anchor_line - int(offset_end[1:])
            else:
                end = int(offset_end)
        else:
            end = anchor_line + int(offset_end)
    else:
        # Traditional absolute line numbers
        start = int(operation["start"])
        end = int(operation["end"])
    
    if start < 1:
        fail(f"start must be >= 1, got {start}")
    if end < start:
        fail(f"end must be >= start, got start={start}, end={end}")
    if end > len(records):
        fail(f"end must be <= line count {len(records)}, got {end}")
    return (start - 1, end)


def apply_delete_lines(text: str, operation: Dict[str, Any]) -> Tuple[str, int, str]:
    records = split_records(text)
    start, end = range_bounds(operation, records, text)
    return (join_records(records[:start] + records[end:]), end - start, "line-based")


def apply_replace_lines(text: str, operation: Dict[str, Any], newline: str) -> Tuple[str, int, str]:
    records = split_records(text)
    start, end = range_bounds(operation, records, text)
    following_exists = end < len(records)
    original_final_sep = records[end - 1][1] if end > start else newline
    final_sep = newline if following_exists else original_final_sep
    replacement = block_records(str(operation["text"]), newline, final_sep)
    return (join_records(records[:start] + replacement + records[end:]), end - start, "line-based")


def apply_operation(text: str, operation: Dict[str, Any], newline: str, explain: bool = False,
                    ignore_indent: bool = False, ignore_eol: bool = False, normalize_whitespace: bool = False,
                    auto_match: bool = False, fuzzy: bool = False,
                    context_before: Optional[str] = None, context_after: Optional[str] = None) -> Tuple[str, int, str, str]:
    """Apply a single operation and return (new_text, changed_count, op_name, match_strategy).
    
    match_strategy is "exact", "ignore-eol", "ignore-indent", "normalize-whitespace",
    "regex", "line-based", or "fuzzy" — indicating how matching was performed.
    """
    op = str(operation.get("op") or operation.get("command") or "").replace("_", "-")
    match_strategy = "exact"  # default
    if op == "edit":
        if auto_match:
            new_text, changed, match_strategy = apply_literal_edit_cascade(
                text, operation, newline, explain=explain, fuzzy=fuzzy,
                context_before=context_before, context_after=context_after)
        else:
            new_text, changed, match_strategy = apply_literal_edit(text, operation, newline, explain, 
                                                    ignore_indent, ignore_eol, normalize_whitespace,
                                                    context_before=context_before, context_after=context_after)
    elif op == "regex":
        new_text, changed, match_strategy = apply_regex_edit(text, operation, newline, explain)
    elif op == "insert":
        new_text, changed, match_strategy = apply_insert(text, operation, newline)
    elif op == "prepend":
        new_text, changed, match_strategy = apply_prepend(text, operation, newline)
    elif op == "append":
        new_text, changed, match_strategy = apply_append(text, operation, newline)
    elif op == "delete":
        new_text, changed, match_strategy = apply_delete_line(text, operation)
    elif op == "delete-lines":
        new_text, changed, match_strategy = apply_delete_lines(text, operation)
    elif op == "replace-lines":
        new_text, changed, match_strategy = apply_replace_lines(text, operation, newline)
    else:
        fail(f"unknown operation: {op or '<missing>'}")
    return (new_text, changed, op, match_strategy)


def apply_post_transforms(text: str, args: argparse.Namespace, newline: str) -> str:
    if args.trim_trailing_whitespace:
        text = trim_trailing_whitespace(text)
    if args.to_line_ending != "preserve":
        text = convert_line_endings(text, args.to_line_ending)
        newline = line_sep(args.to_line_ending)
    text = set_final_newline(text, args.final_newline, newline)
    return text


def is_interactive_terminal() -> bool:
    """Check if we're running in an interactive terminal.
    
    For testing purposes, set SAFE_EDIT_FORCE_INTERACTIVE=1 environment variable
    to bypass the TTY check.
    """
    # Allow forcing interactive mode for testing
    if os.environ.get('SAFE_EDIT_FORCE_INTERACTIVE', '').lower() in ('1', 'true', 'yes'):
        return True
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def prompt_interactive(
    path: Path,
    before: str,
    after: str,
    context: int,
    operation_desc: Optional[str] = None,
) -> Tuple[bool, bool]:
    """Prompt user for confirmation before applying changes.
    
    Returns:
        (apply_this, apply_all) tuple:
        - apply_this: True if this change should be applied
        - apply_all: True if all remaining changes should be applied without prompting
    
    Raises:
        SafeEditError: If not in interactive terminal
    """
    if not is_interactive_terminal():
        fail("--interactive requires an interactive terminal (stdin/stdout must be TTY)")
    
    # Show diff
    diff_text = generate_diff(path, before, after, context)
    if diff_text:
        print(diff_text)
        print()
    
    if operation_desc:
        print(f"Operation: {operation_desc}")
    
    while True:
        try:
            response = input("Apply this change? [y/n/a/q/?] ").strip().lower()
        except EOFError:
            return (False, False)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return (False, False)
        
        if response in ("y", "yes"):
            return (True, False)
        elif response in ("n", "no"):
            return (False, False)
        elif response in ("a", "all"):
            return (True, True)
        elif response in ("q", "quit"):
            return (False, False)
        elif response in ("?", "h", "help"):
            print("Options:")
            print("  y - yes, apply this modification")
            print("  n - no, skip this modification")
            print("  a - all, apply all remaining modifications without prompting")
            print("  q - quit, exit without applying remaining modifications")
            print("  ? - help, show this help message")
        else:
            print(f"Unknown response: {response}. Use y/n/a/q/?")



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
    def __init__(self, path: Path, timeout: float, stale_seconds: float) -> None:
        self.path = path.with_name(f".{path.name}.safe-edit.lock")
        self.timeout = timeout
        self.stale_seconds = stale_seconds
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
                self.remove_stale_lock()
                if time.monotonic() >= deadline:
                    fail(f"lock already exists: {self.path}")
                time.sleep(0.05)

    def remove_stale_lock(self) -> None:
        if self.stale_seconds <= 0:
            return
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if age < self.stale_seconds:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            fail(f"failed to remove stale lock {self.path}: {exc}")

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
        "ok": True,
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


def stat_target(path: Path, original: bytes, encoding: EncodingInfo, text: str) -> Dict[str, Any]:
    """Return a concise summary of file metadata for AI agents."""
    newline_style, _line_counts, _mixed_line_endings = detect_line_ending(text)
    records = split_records(text)
    return {
        "ok": True,
        "file": str(path),
        "command": "stat",
        "encoding": encoding.name,
        "lineEnding": newline_style,
        "sizeBytes": len(original),
        "lineCount": len(records),
        "dryRun": True,
        "changed": 0,
        "operations": [],
        "backup": None,
        "written": False,
        "skipped": True,
        "wouldChangeBytes": False,
    }


def make_backup_path(path: Path, backup_dir: Optional[str], backup_suffix: str) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = backup_suffix.replace("{timestamp}", timestamp)
    if any(part in suffix for part in ("/", "\\")):
        fail("--backup-suffix must not contain path separators")
    directory = Path(backup_dir) if backup_dir else path.parent
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{path.name}{suffix}"


def atomic_replace(
    path: Path,
    data: bytes,
    keep_backup: bool,
    backup_dir: Optional[str],
    backup_suffix: str,
) -> Optional[str]:
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
            backup_name = str(make_backup_path(path, backup_dir, backup_suffix))
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
    # Handle --diff-input: parse SEARCH/REPLACE format into edit operations
    diff_input = getattr(args, 'diff_input', None)
    diff_input_file = getattr(args, 'diff_input_file', None)
    if diff_input or diff_input_file:
        if diff_input and diff_input_file:
            fail("use only one of --diff-input or --diff-input-file")
        if diff_input:
            raw_diff = diff_input
            base_dir = None
        else:
            diff_path = Path(diff_input_file)
            raw_diff = read_argument_file(str(diff_path), args.arg_encoding)
            base_dir = diff_path.parent
        operations = parse_diff_input(raw_diff)
        # Add context_before/context_after and expected_count/first/no_op_ok to each operation
        for op in operations:
            if getattr(args, 'context_before', None):
                op["context_before"] = args.context_before
            if getattr(args, 'context_after', None):
                op["context_after"] = args.context_after
            if args.expected_count is not None:
                op["expected_count"] = args.expected_count
            if args.first:
                op["first"] = True
            if args.no_op_ok:
                op["no_op_ok"] = True
        # Resolve file-based values in each operation
        resolved: List[Dict[str, Any]] = []
        for operation in operations:
            current = dict(operation)
            op = str(current.get("op") or "").replace("_", "-")
            current["op"] = op
            if op == "edit":
                current["old"] = resolve_operation_value(current, "old", True, args.arg_encoding, base_dir)
                current["new"] = resolve_operation_value(current, "new", True, args.arg_encoding, base_dir)
            resolved.append(current)
        return resolved, base_dir
    
    if args.command == "batch":
        operations, base_dir = load_batch_operations(args)
    elif args.command == "convert":
        operations = []
        base_dir = None
    else:
        stdin_taken: List[str] = []
        operation: Dict[str, Any] = {"op": args.command}
        if args.command == "edit":
            operation["old"] = resolve_cli_value(args, "old", True, stdin_taken=stdin_taken)
            operation["new"] = resolve_cli_value(args, "new", True, stdin_taken=stdin_taken)
            operation["expected_count"] = args.expected_count
            operation["first"] = args.first
            operation["no_op_ok"] = args.no_op_ok
            if getattr(args, 'context_before', None):
                operation["context_before"] = args.context_before
            if getattr(args, 'context_after', None):
                operation["context_after"] = args.context_after
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
            if args.start is None and args.end is None:
                # Check if anchor-based positioning is used
                if args.anchor_pattern is None:
                    fail("replace-lines requires --start and --end, or --anchor-pattern with --offset-start and --offset-end")
                if args.offset_start is None or args.offset_end is None:
                    fail("replace-lines with --anchor-pattern requires --offset-start and --offset-end")
                operation["anchor_pattern"] = args.anchor_pattern
                operation["offset_start"] = args.offset_start
                operation["offset_end"] = args.offset_end
                operation["anchor_occurrence"] = args.anchor_occurrence
            else:
                if args.start is None or args.end is None:
                    fail("replace-lines requires --start and --end")
                operation["start"] = args.start
                operation["end"] = args.end
            operation["text"] = resolve_cli_value(args, "text", True, stdin_taken=stdin_taken)
        elif args.command == "delete-lines":
            if args.start is None and args.end is None:
                # Check if anchor-based positioning is used
                if args.anchor_pattern is None:
                    fail("delete-lines requires --start and --end, or --anchor-pattern with --offset-start and --offset-end")
                if args.offset_start is None or args.offset_end is None:
                    fail("delete-lines with --anchor-pattern requires --offset-start and --offset-end")
                operation["anchor_pattern"] = args.anchor_pattern
                operation["offset_start"] = args.offset_start
                operation["offset_end"] = args.offset_end
                operation["anchor_occurrence"] = args.anchor_occurrence
            else:
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
            "stat",
            "convert",
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
        help="input decoding: auto, utf-8, utf-8-bom, gbk, shift-jis, big5, latin-1, utf-16-le, utf-16-be",
    )
    parser.add_argument(
        "--to-encoding",
        default="preserve",
        help="output encoding: preserve, utf-8, utf-8-bom, gbk, shift-jis, big5, latin-1, utf-16-le, utf-16-be",
    )
    parser.add_argument("--to-line-ending", choices=("preserve", "lf", "crlf", "cr"), default="preserve")
    parser.add_argument("--final-newline", choices=("preserve", "ensure", "strip"), default="preserve")
    parser.add_argument("--trim-trailing-whitespace", action="store_true")
    parser.add_argument("--arg-encoding", "--param-encoding", "--input-encoding", default="utf-8", help="encoding for --*-file and --ops-file")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--backup-dir")
    parser.add_argument("--backup-suffix", default=".safe-edit-{timestamp}.bak")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-write", action="store_true")
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("--context", type=int, default=3)
    parser.add_argument("--allow-nul", action="store_true")
    parser.add_argument("--follow-symlink", action="store_true")
    parser.add_argument("--max-bytes", type=int, default=50 * 1024 * 1024)
    parser.add_argument("--lock-timeout", type=float, default=10.0)
    parser.add_argument("--lock-stale-seconds", type=float, default=0.0)
    parser.add_argument("--no-lock", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--interactive", "-i", action="store_true",
                        help="prompt for confirmation before each write operation")

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
    parser.add_argument("--explain-match-failure", action="store_true",
                        help="show detailed diagnostics when match fails")
    
    # Controlled whitespace matching flags (only affect --old matching, not --new replacement)
    parser.add_argument("--ignore-indent", action="store_true",
                        help="ignore indentation differences when matching (tabs vs spaces)")
    parser.add_argument("--ignore-eol", action="store_true",
                        help="ignore line ending differences when matching (CRLF vs LF)")
    parser.add_argument("--normalize-whitespace", action="store_true",
                        help="treat consecutive whitespace as equivalent when matching")
    parser.add_argument("--auto-match", action="store_true",
                        help="automatically try progressively relaxed matching: exact → ignore-eol → ignore-indent → normalize-whitespace")
    parser.add_argument("--fuzzy", action="store_true",
                        help="enable fuzzy matching as last resort (requires --auto-match, similarity >= 0.6)")
    parser.add_argument("--context-before",
                        help="text that must appear before the match for disambiguation")
    parser.add_argument("--context-after",
                        help="text that must appear after the match for disambiguation")
    parser.add_argument("--diff-input",
                        help="SEARCH/REPLACE diff format input for edit operations")
    parser.add_argument("--diff-input-file",
                        help="read SEARCH/REPLACE diff from file")

    parser.add_argument("--line", type=int)
    parser.add_argument("--start", type=int)
    parser.add_argument("--end", type=int)
    parser.add_argument("--text")
    parser.add_argument("--text-file")
    parser.add_argument("--text-stdin", action="store_true")
    
    parser.add_argument("--anchor-pattern", 
                        help="context anchor pattern for relative line positioning")
    parser.add_argument("--offset-start", 
                        help="offset from anchor for start line (e.g., +2, -1)")
    parser.add_argument("--offset-end",
                        help="offset from anchor for end line (e.g., +4, -1)")
    parser.add_argument("--anchor-occurrence", type=int,
                        help="which occurrence of anchor pattern to use (1-based)")

    parser.add_argument("--ops")
    parser.add_argument("--ops-file")
    parser.add_argument("--ops-stdin", action="store_true")
    return parser


def run(args: argparse.Namespace) -> Dict[str, Any]:
    # Validate --interactive constraints
    if getattr(args, 'interactive', False):
        if args.dry_run:
            fail("--interactive cannot be used with --dry-run (dry-run doesn't write anyway)")
        if args.command == "inspect":
            fail("--interactive is not applicable to inspect command (read-only)")
    
    path = resolve_target_path(args.file, args.follow_symlink)
    if args.command == "inspect":
        original = read_target(path, args.max_bytes)
        encoding = detect_encoding(original, args.encoding)
        text = strict_decode(original, encoding)
        return inspect_target(path, original, encoding, text)
    if args.command == "stat":
        original = read_target(path, args.max_bytes)
        encoding = detect_encoding(original, args.encoding)
        text = strict_decode(original, encoding)
        return stat_target(path, original, encoding, text)

    operations, _base_dir = command_to_operations(args)
    if args.command == "convert" and (
        args.to_encoding == "preserve"
        and args.to_line_ending == "preserve"
        and args.final_newline == "preserve"
        and not args.trim_trailing_whitespace
    ):
        fail("convert requires --to-encoding, --to-line-ending, --final-newline, or --trim-trailing-whitespace")
    lock_context = NullLock() if args.no_lock or args.dry_run else FileLock(path, args.lock_timeout, args.lock_stale_seconds)

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
        explain = getattr(args, "explain_match_failure", False)
        ignore_indent = getattr(args, "ignore_indent", False)
        ignore_eol = getattr(args, "ignore_eol", False)
        normalize_whitespace = getattr(args, "normalize_whitespace", False)
        auto_match = getattr(args, "auto_match", False)
        fuzzy = getattr(args, "fuzzy", False)
        context_before = getattr(args, "context_before", None)
        context_after = getattr(args, "context_after", None)
        for index, operation in enumerate(operations, start=1):
            # Per-operation context_before/context_after overrides global
            op_ctx_before = operation.get("context_before", context_before)
            op_ctx_after = operation.get("context_after", context_after)
            new_text, changed, op, match_strategy = apply_operation(new_text, operation, newline, explain,
                                                     ignore_indent, ignore_eol, normalize_whitespace,
                                                     auto_match=auto_match, fuzzy=fuzzy,
                                                     context_before=op_ctx_before, context_after=op_ctx_after)
            operation_results.append({"index": index, "op": op, "changed": changed, "matchStrategy": match_strategy})

        new_text = apply_post_transforms(new_text, args, newline)
        output_encoding = encoding_for_output(args.to_encoding, encoding)
        output = encode_text(new_text, output_encoding)
        output_line_ending, output_line_counts, output_mixed_line_endings = detect_line_ending(new_text)
        diff_text = generate_diff(path, text, new_text, args.context) if args.diff else ""
        summary: Dict[str, Any] = {
            "ok": True,
            "file": str(path),
            "command": args.command,
            "encoding": encoding.name,
            "outputEncoding": output_encoding.name,
            "lineEnding": newline_style,
            "outputLineEnding": output_line_ending,
            "mixedLineEndings": mixed_line_endings,
            "outputMixedLineEndings": output_mixed_line_endings,
            "lineEndingCounts": line_counts,
            "outputLineEndingCounts": output_line_counts,
            "changed": sum(item["changed"] for item in operation_results),
            "operations": operation_results,
            "postTransforms": {
                "toEncoding": args.to_encoding,
                "toLineEnding": args.to_line_ending,
                "finalNewline": args.final_newline,
                "trimTrailingWhitespace": bool(args.trim_trailing_whitespace),
            },
            "matchOptions": {
                "autoMatch": auto_match,
                "fuzzy": fuzzy,
                "ignoreIndent": ignore_indent,
                "ignoreEol": ignore_eol,
                "normalizeWhitespace": normalize_whitespace,
            },
            "dryRun": args.dry_run,
            "backup": None,
            "written": False,
            "skipped": False,
            "wouldChangeBytes": output != original,
        }
        if args.diff:
            summary["diff"] = diff_text

        if not args.dry_run:
            # Interactive mode: prompt before writing
            interactive = getattr(args, 'interactive', False)
            apply_all = False
            
            if interactive and not apply_all:
                # Show diff and prompt
                diff_for_prompt = generate_diff(path, text, new_text, args.context) if not args.diff else diff_text
                apply_this, apply_all = prompt_interactive(path, text, new_text, args.context)
                if not apply_this:
                    summary["skipped"] = True
                    summary["interactiveSkipped"] = True
                    return summary
            
            if output == original and not args.force_write:
                summary["skipped"] = True
            else:
                backup = atomic_replace(path, output, args.backup, args.backup_dir, args.backup_suffix)
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
    if summary.get("command") == "stat":
        # Concise output for AI agents
        size_kb = summary['sizeBytes'] / 1024
        if size_kb >= 1:
            size_str = f"{size_kb:.0f} KB"
        else:
            size_str = f"{summary['sizeBytes']} bytes"
        print(f"Encoding: {summary['encoding'].upper()}")
        print(f"Line endings: {summary['lineEnding'].upper()}")
        print(f"Size: {size_str}")
        print(f"Lines: {summary['lineCount']}")
        return
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
        f"(encoding={summary['encoding']}->{summary['outputEncoding']}, "
        f"lineEnding={summary['lineEnding']}->{summary['outputLineEnding']}, "
        f"changed={summary['changed']})"
    )
    if summary.get("skipped"):
        if summary.get("interactiveSkipped"):
            print("Skipped write: user declined in interactive mode")
        else:
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
        if args.json:
            # When --json is active, emit structured JSON error for Agent consumption
            file_path = args.file if hasattr(args, 'file') else ""
            command = args.command if hasattr(args, 'command') else ""
            
            suggestions = None
            nearby_content = None
            
            # For match_not_found errors, extract nearby content for retry
            error_type = classify_error_type(str(exc))
            if error_type == "match_not_found":
                suggestions = [
                    {"action": "retry_with_ignore_eol", "description": "Retry with --ignore-eol to tolerate CRLF/LF differences"},
                    {"action": "retry_with_ignore_indent", "description": "Retry with --ignore-indent to tolerate indentation differences"},
                    {"action": "retry_with_normalize_whitespace", "description": "Retry with --normalize-whitespace to tolerate whitespace differences"},
                ]
                # Try to extract nearby content from the file
                try:
                    path = resolve_target_path(file_path, getattr(args, 'follow_symlink', False))
                    original = read_target(path, getattr(args, 'max_bytes', 50 * 1024 * 1024))
                    encoding = detect_encoding(original, getattr(args, 'encoding', 'auto'))
                    text = strict_decode(original, encoding)
                    
                    # Determine the search pattern from the operation
                    pattern = ""
                    if command == "edit":
                        pattern = getattr(args, 'old', '') or ""
                    elif command == "regex":
                        pattern = getattr(args, 'pattern', '') or ""
                    
                    if pattern:
                        nearby_content = extract_nearby_content(text, pattern)
                except Exception:
                    # If we can't read the file for nearby content, just skip it
                    pass
            
            emit_json_error(
                exc,
                file_path=file_path,
                command=command,
                suggestions=suggestions,
                nearby_content=nearby_content,
            )
        else:
            print(f"safe-edit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
