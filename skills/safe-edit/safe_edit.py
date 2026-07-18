#!/usr/bin/env python3
"""Safe cross-platform text-file edits with strict decoding and atomic replacement."""

from __future__ import annotations

import argparse
import base64
import binascii
import codecs
import contextlib
import difflib
import errno
import hashlib
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


def _is_process_alive(pid: int) -> bool:
    """Check whether a process with the given PID is still running.

    Pure stdlib, cross-platform:
    - Unix: os.kill(pid, 0) checks existence without sending a signal.
    - Windows: OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) via ctypes.

    Returns False on any error (missing PID, permission denied, etc.)
    so that stale-lock cleanup can proceed safely.
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        # Unix / macOS
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    else:
        # Windows — use ctypes to avoid subprocess overhead
        import ctypes
        import ctypes.wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False


def _read_lock_pid(lock_path: Path) -> Optional[int]:
    """Parse the PID from a safe-edit lock file.

    Lock file format: ``pid=12345 time=1710000000.123 file=foo.lock\\n``
    Returns None if the file is missing or the PID cannot be parsed.
    """
    try:
        content = lock_path.read_text("utf-8")
        for field in content.split():
            if field.startswith("pid="):
                return int(field.split("=", 1)[1])
    except Exception:
        pass
    return None


# Pre-compiled regex for line ending detection (used by split_records)
# Matches CRLF, CR, or LF - order matters: CRLF must be first to avoid partial matches
_LINE_ENDING_RE = re.compile(r'(\r\n|\r|\n)')


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
    - "format_error": invalid diff-input or SEARCH/REPLACE format
    - "unknown": unclassified error
    """
    msg = message.lower()
    # Order matters: more specific checks first to avoid misclassification
    if "was not found" in msg or ("not found" in msg and "refusing" in msg):
        return "match_not_found"
    if "anchor pattern" in msg and "not found" in msg:
        return "match_not_found"
    if "anchor pattern found" in msg and "times" in msg:
        return "match_ambiguous"
    if ("expected" in msg and "occurrence" in msg) or ("expected" in msg and "match" in msg and "found" in msg):
        return "match_count_mismatch"
    if "decode" in msg or "failed to encode" in msg or "unable to auto-detect encoding" in msg or "bom" in msg or "unsupported encoding" in msg:
        return "encoding_error"
    if ("file not found" in msg or "file already exists" in msg
            or "not a regular" in msg or "symlink" in msg
            or "failed to read" in msg or ("failed to" in msg and "file" in msg)
            or ("exceeding" in msg and "max-bytes" in msg)):
        return "file_error"
    if "lock already exists" in msg or "lock file" in msg or "stale lock" in msg:
        return "lock_error"
    if "diff-input format" in msg or "search/replace" in msg:
        return "format_error"
    if ("must" in msg or "requires" in msg or "missing" in msg
            or "unsupported" in msg or "invalid" in msg or "out of range" in msg
            or "mutually exclusive" in msg):
        return "validation_error"
    return "unknown"


def _diagnose_root_cause(old: str, fragment: str, similarity: float) -> str:
    """Determine the root cause of match failure based on closest match.

    Args:
        old: The expected text that wasn't found
        fragment: The closest matching fragment from the file
        similarity: Similarity score (0.0-1.0)

    Returns:
        Root cause string: indentation_difference, line_ending_difference,
        whitespace_difference, content_not_found, or similar_content_exists
    """
    if not fragment or similarity < 0.3:
        return "content_not_found"

    old_lines = old.splitlines()
    frag_lines = fragment.splitlines()

    # Check indentation differences (most common case)
    for ol, fl in zip(old_lines[:3], frag_lines[:3]):
        old_indent = len(ol) - len(ol.lstrip())
        frag_indent = len(fl) - len(fl.lstrip())
        old_has_tab = '\t' in ol[:old_indent] if old_indent > 0 else False
        frag_has_tab = '\t' in fl[:frag_indent] if frag_indent > 0 else False

        # Tab vs spaces difference
        if old_has_tab != frag_has_tab and old_indent > 0 and frag_indent > 0:
            return "indentation_difference"
        # Indent count difference
        if old_indent != frag_indent:
            return "indentation_difference"

    # Check line ending differences
    old_has_crlf = '\r\n' in old
    frag_has_crlf = '\r\n' in fragment
    if old_has_crlf != frag_has_crlf:
        return "line_ending_difference"

    # Check general whitespace differences
    old_normalized = re.sub(r'\s+', ' ', old)
    frag_normalized = re.sub(r'\s+', ' ', fragment)
    if old_normalized == frag_normalized:
        return "whitespace_difference"

    # Similar but not categorized above - may be stale context
    if similarity >= SIMILAR_CONTENT_THRESHOLD:
        return "similar_content_exists"

    return "content_not_found"


# Whitespace-related causes that can be fixed by retry with flags
WHITESPACE_CAUSES = frozenset([
    "indentation_difference",
    "line_ending_difference",
    "whitespace_difference",
])

# Threshold for detecting "similar content exists" (likely stale context)
# Below this: content_not_found (USER_INPUT)
# Above this: similar_content_exists (RE_READ_REQUIRED)
SIMILAR_CONTENT_THRESHOLD = 0.70


def _determine_failure_class(root_cause: str, error_type: str, similarity: Optional[float] = None) -> Tuple[str, str]:
    """Determine failure class and recommended action type.

    Returns:
        (failureClass, actionType) tuple where:
        - failureClass: RETRYABLE, RE_READ_REQUIRED, USER_INPUT, or FATAL
        - actionType: "retry", "re_read_file", "ask_user", or "stop"

    Logic:
    - WHITESPACE_CAUSES → RETRYABLE + retry
    - multiple_matches → RE_READ_REQUIRED + re_read_file
    - similar_content_exists → RE_READ_REQUIRED + re_read_file
    - content_not_found → USER_INPUT + ask_user
    """
    # Multiple matches from match_ambiguous error
    if error_type == "match_ambiguous":
        return ("RE_READ_REQUIRED", "re_read_file")

    # Whitespace differences - can retry with flags
    if root_cause in WHITESPACE_CAUSES:
        return ("RETRYABLE", "retry")

    # Multiple matches - need to re-read to find unique context
    if root_cause == "multiple_matches":
        return ("RE_READ_REQUIRED", "re_read_file")

    # Similar content exists but not clear whitespace issue - likely stale context
    if root_cause == "similar_content_exists":
        return ("RE_READ_REQUIRED", "re_read_file")

    # Content not found at all
    if root_cause == "content_not_found":
        return ("USER_INPUT", "ask_user")

    # Unknown/fatal
    return ("FATAL", "stop")


def _build_recommended_action(action_type: str, confidence: float) -> Dict[str, Any]:
    """Build recommendedAction object.

    Args:
        action_type: "retry", "re_read_file", "ask_user", or "stop"
        confidence: 0.0-1.0 confidence in the recommended ACTION (not root cause).

    Returns:
        {"type": action_type, "confidence": confidence}

    Note: confidence represents how confident we are that the recommended action
    will succeed, NOT how confident we are in the root cause classification.
    """
    return {
        "type": action_type,
        "confidence": round(confidence, 2),
    }


def _build_retry_strategy(root_cause: str) -> Optional[Dict[str, Any]]:
    """Build retryStrategy for RETRYABLE cases.

    Returns dict with flags and alternativeFlags, or None for non-retry cases.
    """
    strategies = {
        "indentation_difference": {
            "flags": ["--ignore-indent"],
            "alternativeFlags": ["--auto-match"]
        },
        "line_ending_difference": {
            "flags": ["--ignore-eol"],
            "alternativeFlags": ["--auto-match"]
        },
        "whitespace_difference": {
            "flags": ["--auto-match"],
            "alternativeFlags": ["--normalize-whitespace"]
        },
    }

    return strategies.get(root_cause)


# Confidence scores for different root causes
_CONFIDENCE_SCORES = {
    "indentation_difference": 0.90,
    "line_ending_difference": 0.95,
    "whitespace_difference": 0.85,
    "multiple_matches": 0.80,
    "similar_content_exists": 0.75,
    "content_not_found": 0.70,
}


def analyze_match_failure(
    old: str,
    text: str,
    error_type: str = "match_not_found",
) -> Dict[str, Any]:
    """Analyze why a match failed and return structured recovery info.

    This is the main entry point for failure analysis. It finds the closest
    match, diagnoses the root cause, and recommends recovery strategy.

    Args:
        old: The expected text that wasn't found
        text: The actual file content
        error_type: The classified error type

    Returns:
        {
            "failureClass": "RETRYABLE" | "RE_READ_REQUIRED" | "USER_INPUT" | "FATAL",
            "rootCause": str,
            "closestMatch": {...} | None,
            "recommendedAction": {"type": str, "confidence": float},
            "retryStrategy": {...} | None  # only for RETRYABLE
        }
    """
    result: Dict[str, Any] = {
        "failureClass": "FATAL",
        "rootCause": "unknown",
        "closestMatch": None,
        "recommendedAction": None,
        "retryStrategy": None,
    }

    # Handle multiple matches case (match_ambiguous error)
    if error_type == "match_ambiguous":
        result["failureClass"] = "RE_READ_REQUIRED"
        result["rootCause"] = "multiple_matches"
        confidence = _CONFIDENCE_SCORES.get("multiple_matches", 0.70)
        result["recommendedAction"] = _build_recommended_action("re_read_file", confidence)
        return result

    # Pre-check line ending difference (before find_closest_match loses it)
    old_has_crlf = '\r\n' in old
    text_has_crlf = '\r\n' in text
    line_ending_diff = old_has_crlf != text_has_crlf

    # Find closest match for single match failures
    closest = find_closest_match(text, old)
    similarity = 0.0

    if closest is None:
        result["failureClass"] = "USER_INPUT"
        result["rootCause"] = "content_not_found"
        confidence = _CONFIDENCE_SCORES.get("content_not_found", 0.70)
        result["recommendedAction"] = _build_recommended_action("ask_user", confidence)
        return result

    line_num, fragment = closest

    # Calculate similarity at character level (not line level)
    # This gives more accurate similarity for content with minor differences
    matcher = difflib.SequenceMatcher(None, old, fragment)
    similarity = matcher.ratio()

    # Build closestMatch info
    result["closestMatch"] = {
        "line": line_num,
        "similarity": round(similarity, 2),
    }

    # Diagnose root cause - check line ending first (pre-detected)
    if line_ending_diff and similarity >= 0.9:
        root_cause = "line_ending_difference"
    else:
        root_cause = _diagnose_root_cause(old, fragment, similarity)
    result["rootCause"] = root_cause

    # Determine failure class and action type
    failure_class, action_type = _determine_failure_class(root_cause, error_type, similarity)
    result["failureClass"] = failure_class

    # Build recommendedAction
    confidence = _CONFIDENCE_SCORES.get(root_cause, 0.70)
    result["recommendedAction"] = _build_recommended_action(action_type, confidence)

    # Build retryStrategy only for RETRYABLE cases
    if failure_class == "RETRYABLE":
        result["retryStrategy"] = _build_retry_strategy(root_cause)

    return result


def emit_json_error(
    exc: SafeEditError,
    file_path: str = "",
    command: str = "",
    *,
    old: str = "",
    text: str = "",
    lock_info: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit a structured JSON error object to stdout for Agent consumption.

    When old and text are provided for match errors, provides:
    - failureClass: RETRYABLE / RE_READ_REQUIRED / USER_INPUT / FATAL
    - rootCause: specific reason for failure
    - closestMatch: nearest similar content location
    - recommendedAction: {"type": str, "confidence": float}
    - retryStrategy: recommended flags (only for RETRYABLE)

    When lock_info is provided for lock errors, provides:
    - targetFile: filename (relative, not absolute path)
    - lockPid: PID of the lock owner (may be absent)
    - lockAgeSeconds: age of the lock in seconds (may be absent)
    - failureClass: RETRYABLE
    - recommendedAction: {"type": "retry_after_lock_clears", "confidence": float}
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

    # Structured recovery info for match-related errors
    if error_type in ("match_not_found", "match_ambiguous", "match_count_mismatch"):
        if old and text:
            analysis = analyze_match_failure(old, text, error_type)
            error_obj["failureClass"] = analysis["failureClass"]
            error_obj["rootCause"] = analysis["rootCause"]
            if analysis["closestMatch"]:
                error_obj["closestMatch"] = analysis["closestMatch"]
            if analysis["recommendedAction"]:
                error_obj["recommendedAction"] = analysis["recommendedAction"]
            if analysis["retryStrategy"]:
                error_obj["retryStrategy"] = analysis["retryStrategy"]

    # Structured recovery info for lock errors
    if error_type == "lock_error" and lock_info:
        error_obj["failureClass"] = "RETRYABLE"
        target_file = lock_info.get("targetFile")
        if target_file is not None:
            error_obj["targetFile"] = target_file
        lock_pid = lock_info.get("lockPid")
        if lock_pid is not None:
            error_obj["lockPid"] = lock_pid
        lock_age = lock_info.get("lockAgeSeconds")
        if lock_age is not None:
            error_obj["lockAgeSeconds"] = lock_age
        error_obj["recommendedAction"] = {"type": "retry_after_lock_clears", "confidence": 0.8}

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


def _get_tmp_dir() -> str:
    """Get the best available temporary directory for sandbox environments.

    Returns /tmp if available, otherwise falls back to system temp.
    """
    # Try /tmp first (Unix sandbox environments)
    if os.name != "nt":
        try:
            test_path = "/tmp/.safe-edit-probe"
            fd = os.open(test_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            os.unlink(test_path)
            return "/tmp"
        except OSError:
            pass
    # Fallback to system temp
    return tempfile.gettempdir()


def _get_lock_dir() -> Path:
    """Get or create the lock directory under tmp.

    Returns /tmp/safe-edit/locks/ (or system temp equivalent).
    """
    lock_dir = Path(_get_tmp_dir()) / "safe-edit" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir


def _get_lock_key(file_path: str) -> str:
    """Generate a unique lock key based on file identity (device + inode).

    This ensures:
    - Same file via different paths (symlinks, junctions) → same lock
    - Same path after rename → same lock (inode unchanged)
    - Different files at same path → different lock

    Uses st_dev:st_ino on all platforms:
    - Unix: true device + inode numbers
    - Windows: volume serial number + file index (via GetFileInformationByHandle)

    Falls back to resolved path hash only when stat() fails (file not yet created).
    """
    p = Path(file_path).resolve()
    try:
        stat_info = p.stat()
        # st_dev:st_ino works on both Unix and Windows for file identity
        # Windows: st_dev = volume serial number, st_ino = file index
        identity = f"{stat_info.st_dev}:{stat_info.st_ino}"
    except OSError:
        # File may not exist yet, use resolved path as fallback
        identity = str(p)
    return hashlib.sha256(identity.encode()).hexdigest()[:32]


def check_fs_capability(target_file: str) -> Dict[str, Any]:
    """Detect filesystem capability WITHOUT writing into target directory.

    Designed for sandbox environments where target dir is write-protected.
    Tests /tmp (or system temp) for write capability instead of target dir.
    """
    result: Dict[str, Any] = {
        "directoryWritable": False,
        "canWriteTmp": False,
        "canCreateLock": False,
        "executionMode": "unknown",
        "suggestions": [],
    }

    tmp_dir = _get_tmp_dir()

    # 1. Check target directory writability (best-effort, non-destructive probe)
    target_dir = str(Path(target_file).resolve().parent)
    try:
        probe = os.path.join(target_dir, ".safe-edit-probe")
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        os.unlink(probe)
        result["directoryWritable"] = True
    except OSError:
        result["suggestions"].append(f"Target directory not writable: {target_dir}")

    # 2. Check tmp write
    try:
        fd, tmp = tempfile.mkstemp(dir=tmp_dir)
        os.close(fd)
        os.remove(tmp)
        result["canWriteTmp"] = True
    except Exception:
        result["suggestions"].append(f"Cannot write to {tmp_dir}")

    # 3. Lock test in tmp (NOT target dir)
    try:
        fd, lock = tempfile.mkstemp(prefix="safe_edit_lock_", dir=tmp_dir)
        os.close(fd)
        os.remove(lock)
        result["canCreateLock"] = True
    except Exception:
        result["suggestions"].append(f"Cannot create lock in {tmp_dir}")

    # 4. Derive mode
    if result["canWriteTmp"] and result["canCreateLock"]:
        result["executionMode"] = "sandbox-safe" if not result["directoryWritable"] else "full"
    elif result["canWriteTmp"]:
        result["executionMode"] = "no-lock-mode"
    else:
        result["executionMode"] = "readonly-fallback"
        result["suggestions"].append("Filesystem is effectively read-only")

    return result


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
    # Use C-optimized count() instead of Python character iteration
    # CRLF contains both \r and \n, so we need to subtract to get standalone counts
    crlf_count = text.count('\r\n')
    total_cr = text.count('\r')
    total_lf = text.count('\n')

    # Standalone CR: total CR minus CRs that are part of CRLF
    # Standalone LF: total LF minus LFs that are part of CRLF
    cr_count = total_cr - crlf_count
    lf_count = total_lf - crlf_count

    counts = {"crlf": crlf_count, "lf": lf_count, "cr": cr_count}

    if not any(counts.values()):
        return ("lf", counts, False)

    priority = {"crlf": 3, "lf": 2, "cr": 1}
    dominant = max(counts, key=lambda key: (counts[key], priority[key]))
    mixed = sum(1 for value in counts.values() if value > 0) > 1
    return (dominant, counts, mixed)


def line_sep(style: str) -> str:
    return {"crlf": "\r\n", "cr": "\r", "lf": "\n"}[style]


def split_records(text: str) -> List[Tuple[str, str]]:
    """Split text into (line_content, line_ending) tuples.

    Uses pre-compiled regex for C-optimized performance.
    Handles CRLF, CR, and LF line endings correctly.
    """
    if not text:
        return []

    # Split by line endings, keeping the separators
    parts = _LINE_ENDING_RE.split(text)

    # parts alternates: [content, ending, content, ending, ..., content]
    # If text ends with line ending, last part is empty string
    records: List[Tuple[str, str]] = []

    i = 0
    while i < len(parts):
        content = parts[i]
        if i + 1 < len(parts):
            # Has a line ending
            ending = parts[i + 1]
            records.append((content, ending))
            i += 2
        else:
            # Last content without ending
            if content:  # Skip empty trailing content
                records.append((content, ""))
            i += 1

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


def _detect_msys2_path_corruption(value: str, param_name: str) -> Optional[str]:
    """Detect if a CLI text argument was corrupted by MSYS2 path conversion.

    MSYS2/Git Bash on Windows automatically converts POSIX-style paths in CLI
    arguments: /foo → C:/Program Files/Git/foo, //foo → /foo.
    This corrupts text content parameters like --old, --new, etc.

    Returns None if the value looks clean, or a warning string if corruption detected.

    TODO: Add --msys-guard flag for opt-in automatic path restoration (currently
    warning-only). When implemented, also add --no-msys-guard to disable.
    TODO: Post-edit verification — after writing, read back changed lines and
    compare against expected --new text; emit warning on mismatch.
    """
    if sys.platform != "win32" or not value:
        return None

    msystem = os.environ.get("MSYSTEM", "")
    msys_prefix = os.environ.get("MINGW_PREFIX", "")
    if not msystem and not msys_prefix:
        return None

    # If user has already set MSYS2_ARG_CONV_EXCL, path conversion is disabled
    if os.environ.get("MSYS2_ARG_CONV_EXCL", ""):
        return None

    # Pattern 1: /foo → C:/Program Files/Git/foo
    git_prefix = msys_prefix.replace("\\", "/") if msys_prefix else ""
    known_prefixes = [git_prefix] if git_prefix else []
    if not known_prefixes:
        known_prefixes = [
            "C:/Program Files/Git/",
            "C:/Program Files (x86)/Git/",
        ]
    for prefix in known_prefixes:
        if prefix and value.startswith(prefix):
            return (
                f"--{param_name} value was likely corrupted by MSYS2 path conversion "
                f"(starts with {prefix}). Original content starting with '/' was "
                f"converted to a Windows path. Set MSYS2_ARG_CONV_EXCL=\"*\" before "
                f"calling safe_edit.py, or use --{param_name}-file to pass content via file."
            )

    # Pattern 2: //foo → /foo (double-slash collapsed to single-slash)
    # We can't distinguish this from a legitimate single-slash prefix, so warn
    # on any value starting with '/' when running under MSYS2.
    if value.startswith("/"):
        return (
            f"--{param_name} value starts with '/'. Under MSYS2/Git Bash, "
            f"arguments starting with '/' or '//' are automatically converted to Windows paths "
            f"('/foo' → 'C:/Program Files/Git/foo', '//foo' → '/foo'). "
            f"If your original text started with '//', it has been corrupted. "
            f"Set MSYS2_ARG_CONV_EXCL=\"*\" before calling safe_edit.py, "
            f"or use --{param_name}-file to pass content via file."
        )

    return None


def decode_base64_text(value: str, option_name: str) -> str:
    # Accept padded or unpadded URL-safe/standard Base64 and require UTF-8.
    compact = ''.join(value.split())
    if not compact:
        return ''
    padding = '=' * (-len(compact) % 4)
    normalized = (compact + padding).replace('-', '+').replace('_', '/')
    try:
        decoded = base64.b64decode(normalized, validate=True)
    except (ValueError, binascii.Error) as exc:
        fail(f'invalid --{option_name} data: expected Base64 text ({exc})')
    try:
        return decoded.decode('utf-8', errors='strict')
    except UnicodeDecodeError as exc:
        fail(f'invalid --{option_name} data: decoded bytes are not UTF-8 ({exc})')


def resolve_cli_value(
    args: argparse.Namespace,
    name: str,
    required: bool,
    *,
    stdin_taken: List[str],
    warnings: Optional[List[str]] = None,
) -> Optional[str]:
    direct = getattr(args, name, None)
    file_value = getattr(args, f'{name}_file', None)
    base64_value = getattr(args, f'{name}_base64', None)
    stdin_value = bool(getattr(args, f'{name}_stdin', False))
    provided = [direct is not None, file_value is not None, base64_value is not None, stdin_value].count(True)
    if provided > 1:
        fail(f'use only one of --{name}, --{name}-file, --{name}-base64, or --{name}-stdin')
    if provided == 0:
        if required:
            fail(f'missing --{name}')
        return None
    if direct is not None:
        if warnings is not None:
            warning = _detect_msys2_path_corruption(direct, name)
            if warning:
                warnings.append(warning)
        return direct
    if file_value is not None:
        return read_argument_file(file_value, args.arg_encoding)
    if base64_value is not None:
        return decode_base64_text(base64_value, f'{name}-base64')
    if stdin_taken:
        fail(f'stdin is already used by --{stdin_taken[0]}-stdin')
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
    if len(filtered) == 0 and bool(operation.get("no_op_ok", False)):
        # no_op_ok: if no matches after context filtering, skip count check and return unchanged
        return (text, 0, effective_strategy)
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
    if actual == 0 and bool(operation.get("no_op_ok", False)):
        # no_op_ok: if no matches, skip count check and return text unchanged
        return (text, 0, effective_strategy)
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
        pos = original_text.find(original_old, start_search_pos)
        return (pos, len(original_old)) if pos >= 0 else (-1, 0)
    
    # Fast path: try original_old as-is first (works when only line endings differ)
    candidate = original_text.find(original_old, start_search_pos)
    if candidate >= 0:
        normalized_candidate = normalize_for_match(
            original_text[candidate:candidate + len(original_old)],
            ignore_indent, ignore_eol, normalize_whitespace,
        )
        if normalized_candidate == normalized_old:
            return (candidate, len(original_old))
    
    # Line-based fast path for ignore_indent: compute original line offsets
    # and use normalized match position to find the corresponding original line.
    # This reduces the search space from O(n * max_len) to O(lines * max_line_len).
    if ignore_indent and not normalize_whitespace:
        result = _find_original_position_line_based(
            original_text, normalized_text, normalized_old, original_old,
            ignore_indent, ignore_eol, start_search_pos,
            norm_pos=norm_pos,
        )
        if result[0] >= 0:
            return result
    
    # General fallback: scan from start_search_pos with early termination.
    # Instead of trying all lengths from each position, we use a smart bound:
    # the original text cannot be shorter than the normalized text, and
    # unlikely to be more than 3x longer (handles CRLF→LF, tab→spaces).
    min_len = max(1, len(normalized_old))
    max_len = max(len(original_old) * 3, len(normalized_old) * 3, 200)

    search_start = start_search_pos
    while search_start < len(original_text):
        for length in range(min_len, min(max_len + 1, len(original_text) - search_start + 1)):
            candidate = original_text[search_start:search_start + length]
            normalized_candidate = normalize_for_match(candidate, ignore_indent, ignore_eol, normalize_whitespace)

            if normalized_candidate == normalized_old:
                # Extend match to include a trailing \n if the match ends with \r
                # (CRLF boundary: avoid splitting \r\n into \r matched + \n leftover)
                if ignore_eol and length < len(original_text) - search_start:
                    if candidate.endswith('\r') and original_text[search_start + length] == '\n':
                        length += 1
                return (search_start, length)

            # Early termination: if normalized candidate is already longer than target,
            # no point trying longer substrings from this position
            if len(normalized_candidate) > len(normalized_old):
                break

        search_start += 1

    return (-1, 0)


def _find_original_position_line_based(
    original_text: str, normalized_text: str, normalized_old: str, original_old: str,
    ignore_indent: bool, ignore_eol: bool, start_search_pos: int,
    norm_pos: int = 0,
) -> Tuple[int, int]:
    """Line-based fast path for find_original_position with ignore_indent.
    
    Uses line offset mapping to narrow the search, falling back to
    character-level verification within the candidate region.
    """
    # Build line offset mapping: for each original line, record its start offset
    # and the corresponding offset in the normalized text
    orig_lines = original_text.split('\n')
    orig_line_starts = []
    norm_line_starts = []
    
    offset = 0
    norm_offset = 0
    for line in orig_lines:
        orig_line_starts.append(offset)
        norm_line_starts.append(norm_offset)
        # Original line length (content only, no \n)
        offset += len(line) + 1  # +1 for \n (or \r\n handled below)
        # Normalized line length (with indent stripped)
        norm_line = line.lstrip(' \t') if ignore_indent else line
        norm_offset += len(norm_line)
        # Add newline in normalized text
        if ignore_eol:
            norm_offset += 1  # always \n
        else:
            # Count actual newline length
            # This is approximate; we'll verify below
            norm_offset += 1
    
    # Find which line the normalized match starts in
    # by finding the line whose normalized start is <= norm_pos
    # and whose next line's normalized start is > norm_pos
    target_line = 0
    for i in range(len(norm_line_starts) - 1, -1, -1):
        if norm_line_starts[i] <= norm_pos:
            target_line = i
            break
    
    # The original text should start at or near the original line start
    # Search within a small window around the target line
    search_start = max(start_search_pos, orig_line_starts[target_line])
    search_end = min(len(original_text), orig_line_starts[target_line] + len(original_old) * 3 + 200)
    
    min_len = max(1, len(normalized_old))
    max_len = max(len(original_old) * 3, len(normalized_old) * 3, 200)
    
    pos = search_start
    while pos < search_end:
        for length in range(min_len, min(max_len + 1, len(original_text) - pos + 1)):
            candidate = original_text[pos:pos + length]
            normalized_candidate = normalize_for_match(candidate, ignore_indent, ignore_eol, False)
            if normalized_candidate == normalized_old:
                return (pos, length)
            if len(normalized_candidate) > len(normalized_old):
                break
        pos += 1
    
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
    if actual == 0 and not bool(operation.get("no_op_ok", False)):
        if explain:
            # Try to find closest match using the pattern as literal text
            explanation = explain_match_failure(pattern, text)
            fail(f"regex pattern was not found; refusing a silent no-op\n\n{explanation}")
        else:
            fail("regex pattern was not found; refusing a silent no-op")
    if actual == 0 and bool(operation.get("no_op_ok", False)):
        # no_op_ok: if no matches, skip count check and return text unchanged
        return (text, 0, "regex")
    if expected is not None and actual != int(expected):
        fail(f"expected {expected} regex match(es), found {actual}")

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


def _extract_indent(line: str) -> str:
    """Extract leading whitespace from a line."""
    indent = ""
    for c in line:
        if c in ' \t':
            indent += c
        else:
            break
    return indent


def apply_replace_lines(text: str, operation: Dict[str, Any], newline: str) -> Tuple[str, int, str]:
    records = split_records(text)
    start, end = range_bounds(operation, records, text)
    preserve_indent = bool(operation.get("preserve_indent", False))
    replacement_text = str(operation["text"])

    if preserve_indent and start < len(records):
        original_indent = _extract_indent(records[start][0])
        if original_indent:
            lines = replacement_text.split('\n')
            adjusted = []
            for line in lines:
                if line and line[0] not in ' \t':
                    adjusted.append(original_indent + line)
                else:
                    adjusted.append(line)
            replacement_text = '\n'.join(adjusted)

    following_exists = end < len(records)
    original_final_sep = records[end - 1][1] if end > start else newline
    final_sep = newline if following_exists else original_final_sep
    replacement = block_records(replacement_text, newline, final_sep)
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
        self.file_path = path
        self.timeout = timeout
        self.stale_seconds = stale_seconds
        self.acquired = False
        # Use /tmp/safe-edit/locks/ (or system temp equivalent) — sandbox-safe
        # Key is based on file identity (inode on Unix, path on Windows)
        lock_key = _get_lock_key(str(path))
        self.lock_path = _get_lock_dir() / f"{lock_key}.lock"

    def __enter__(self) -> "FileLock":
        deadline = time.monotonic() + max(0.0, self.timeout)
        payload = f"pid={os.getpid()} time={time.time()} file={self.file_path.resolve()}\n".encode("utf-8")
        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                self.acquired = True
                return self
            except FileExistsError:
                self.remove_stale_lock()
                if time.monotonic() >= deadline:
                    fail(f"lock already exists: {self.lock_path}")
                time.sleep(0.05)

    def remove_stale_lock(self) -> None:
        # Path 1: PID check — if lock owner is dead, remove immediately
        pid = _read_lock_pid(self.lock_path)
        if pid is not None and not _is_process_alive(pid):
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass
            return

        # Path 2: Stale age check
        if self.stale_seconds <= 0:
            return
        try:
            age = time.time() - self.lock_path.stat().st_mtime
        except FileNotFoundError:
            return
        if age <= self.stale_seconds:
            return
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            fail(f"failed to remove stale lock {self.lock_path}: {exc}")

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        if self.acquired:
            try:
                self.lock_path.unlink()
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
    edit_plan = _compute_edit_plan(encoding, text, path,
                                   newline_style=newline_style,
                                   mixed=mixed_line_endings,
                                   line_count=len(records))
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
        "editMode": edit_plan["editMode"],
        "editStrategy": edit_plan["editStrategy"],
        "why": edit_plan["why"],
        "dryRun": True,
        "changed": 0,
        "operations": [],
        "backup": None,
        "written": False,
        "skipped": True,
        "wouldChangeBytes": False,
    }


def _compute_edit_plan(encoding: EncodingInfo, text: str, path: Path,
                       newline_style: Optional[str] = None, mixed: Optional[bool] = None,
                       line_count: Optional[int] = None) -> Dict[str, Any]:
    """Compute edit strategy based on file properties.

    Args:
        encoding: File encoding info
        text: Decoded file content
        path: File path (for extension check)
        newline_style: Pre-detected dominant line ending (None = auto-detect)
        mixed: Pre-detected mixed line endings flag (None = auto-detect)
        line_count: Pre-detected line count (None = auto-detect)

    Returns:
        {
            "editMode": "builtin" | "recommended" | "required",
            "editStrategy": "edit-tool" | "safe-edit",
            "why": [list of reason strings]
        }

    editMode levels:
        builtin     → built-in Edit tool OK (plain UTF-8 LF, no BOM, small file)
        recommended → safe-edit preferred (large file, structured format, etc.)
        required    → safe-edit mandatory (non-UTF8, BOM, CRLF, mixed EOL)
    """
    why: List[str] = []
    mode = "builtin"

    # Check encoding — required level
    is_plain_utf8 = encoding.name == "utf-8" and not encoding.bom
    if not is_plain_utf8:
        mode = "required"
        if encoding.bom:
            why.append("bom")
        if encoding.name not in ("utf-8", "utf-8-bom"):
            why.append(f"encoding_{encoding.name}")

    # Check line endings — required level
    if newline_style is None:
        newline_style, _counts, _mixed = detect_line_ending(text)
        mixed = _mixed
    if newline_style == "crlf":
        mode = "required"
        why.append("crlf")
    elif newline_style == "cr":
        mode = "required"
        why.append("cr")
    if mixed:
        mode = "required"
        why.append("mixed_line_endings")

    # Check file size / line count — recommended level
    if mode == "builtin":
        if line_count is None:
            records = split_records(text)
            line_count = len(records)
        file_size = len(text)

        if file_size > 500 * 1024:  # > 500KB
            mode = "recommended"
            why.append("large_file")
        elif line_count > 5000:
            mode = "recommended"
            why.append("many_lines")

    # Check file extension for structured formats — recommended level
    if mode == "builtin":
        ext = path.suffix.lower()
        if ext in (".json", ".yaml", ".yml", ".xml", ".toml"):
            mode = "recommended"
            why.append(f"structured_format_{ext.lstrip('.')}")

    strategy = "safe-edit" if mode != "builtin" else "edit-tool"

    return {
        "editMode": mode,
        "editStrategy": strategy,
        "why": why,
    }


def stat_target(path: Path, original: bytes, encoding: EncodingInfo, text: str) -> Dict[str, Any]:
    """Return a concise summary of file metadata with edit strategy for AI agents."""
    newline_style, _line_counts, _mixed_line_endings = detect_line_ending(text)
    records = split_records(text)
    edit_plan = _compute_edit_plan(encoding, text, path,
                                   newline_style=newline_style,
                                   mixed=_mixed_line_endings,
                                   line_count=len(records))
    cap = check_fs_capability(str(path))
    return {
        "ok": True,
        "file": str(path),
        "command": "stat",
        "encoding": encoding.name,
        "hasBom": bool(encoding.bom),
        "lineEnding": newline_style,
        "mixedLineEndings": _mixed_line_endings,
        "sizeBytes": len(original),
        "lineCount": len(records),
        "sha256": hashlib.sha256(original).hexdigest(),
        "editMode": edit_plan["editMode"],
        "editStrategy": edit_plan["editStrategy"],
        "why": edit_plan["why"],
        "directoryWritable": cap["directoryWritable"],
        "canCreateTemp": cap["canWriteTmp"],
        "canCreateLock": cap["canCreateLock"],
        "executionMode": cap["executionMode"],
        "suggestions": cap["suggestions"],
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


def _is_cross_device_error(exc: OSError) -> bool:
    """Return True if *exc* is a cross-device rename/replace failure.

    ``os.replace`` is atomic but cannot cross filesystem boundaries. On Unix
    this surfaces as ``errno.EXDEV``; on Windows the native
    ``ERROR_NOT_SAME_DEVICE`` (winerror 17) is raised when moving between
    drives (e.g. staging on the system temp drive while the target lives on
    another volume).
    """
    if exc.errno == errno.EXDEV:
        return True
    if getattr(exc, "winerror", None) == 17:
        return True
    return False


def _replace_file(src: str, dst: str) -> None:
    """Replace *dst* with *src*, tolerating cross-device staging.

    ``os.replace`` is atomic but fails across filesystems. When the staging
    temp file (kept in the sandbox tmp dir) and the target live on different
    volumes we first try to re-stage beside the target so the final replace
    stays atomic; if the target directory cannot hold a new file (e.g. a
    write-protected sandbox) we fall back to copy + delete, which is correct
    but not atomic. Anything other than a cross-device error is re-raised.
    """
    try:
        os.replace(src, dst)
        return
    except OSError as exc:
        if not _is_cross_device_error(exc):
            raise

    dst_dir = os.path.dirname(dst) or os.curdir
    fd, stage = -1, ""
    staged = False
    try:
        fd, stage = tempfile.mkstemp(
            prefix=os.path.basename(dst) + ".safe-edit.",
            suffix=".stage",
            dir=dst_dir,
        )
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            with open(src, "rb") as src_handle:
                shutil.copyfileobj(src_handle, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(stage, stat.S_IMODE(os.stat(src).st_mode))
        os.replace(stage, dst)
        stage = ""
        staged = True
    except OSError:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
            fd = -1
        if stage:
            try:
                os.unlink(stage)
            except OSError:
                pass
            stage = ""
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        if stage:
            try:
                os.unlink(stage)
            except OSError:
                pass

    if staged:
        try:
            os.unlink(src)
        except OSError:
            pass
        return

    shutil.move(src, dst)


def resolve_remove_path(path_value: str, workspace_root_value: Optional[str]) -> Tuple[Path, Path]:
    """Resolve a regular file that is contained by an explicit workspace root."""
    if not workspace_root_value:
        fail("remove-file requires --workspace-root")
    root = Path(workspace_root_value).resolve()
    if not root.exists():
        fail(f"workspace root not found: {root}")
    if not root.is_dir():
        fail(f"workspace root is not a directory: {root}")

    candidate = Path(path_value)
    if not os.path.lexists(candidate):
        fail(f"file not found: {candidate}")
    if candidate.is_symlink():
        fail("remove-file refuses symbolic links")
    path = candidate.resolve()
    if not path.is_file():
        fail(f"not a regular file: {path}")
    try:
        path.relative_to(root)
    except ValueError:
        fail(f"refusing to remove file outside workspace root: {path}")
    return path, root


def resolve_create_path(path_value: str) -> Path:
    """Validate a new-file target without creating parent directories."""
    path = Path(path_value)
    if os.path.lexists(path):
        fail(f"file already exists: {path}")
    parent = path.parent
    if not parent.exists():
        fail(f"parent directory not found: {parent}")
    if not parent.is_dir():
        fail(f"parent path is not a directory: {parent}")
    return path


def exclusive_create(path: Path, data: bytes) -> None:
    """Create *path* exclusively and remove partial output if writing fails."""
    fd = -1
    created = False
    try:
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            created = True
        except FileExistsError:
            fail(f"file already exists: {path}")
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(path.parent)
    except BaseException:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise


def atomic_replace(
    path: Path,
    data: bytes,
    keep_backup: bool,
    backup_dir: Optional[str],
    backup_suffix: str,
) -> Optional[str]:
    directory = path.parent
    # Use /tmp (or system temp) for staging — sandbox-safe
    tmp_dir = _get_tmp_dir()
    prefix = f".{path.name}.safe-edit."
    fd = -1
    tmp_name = ""
    backup_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=tmp_dir)
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

        _replace_file(tmp_name, str(path))
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
    sources = [
        args.ops is not None,
        args.ops_file is not None,
        args.ops_base64 is not None,
        args.ops_stdin,
    ].count(True)
    if sources != 1:
        fail('batch requires exactly one of --ops, --ops-file, --ops-base64, or --ops-stdin')
    base_dir = None
    if args.ops is not None:
        raw = args.ops
    elif args.ops_file is not None:
        ops_path = Path(args.ops_file)
        raw = read_argument_file(str(ops_path), args.arg_encoding)
        base_dir = ops_path.parent
    elif args.ops_base64 is not None:
        raw = decode_base64_text(args.ops_base64, 'ops-base64')
    else:
        raw = sys.stdin.read()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f'invalid batch JSON: {exc}')
    if isinstance(payload, dict):
        payload = payload.get('operations', payload.get('ops'))
    if not isinstance(payload, list):
        fail('batch JSON must be a list or an object with operations/ops')
    operations: List[Dict[str, Any]] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            fail(f'batch operation {index} must be an object')
        operations.append(dict(item))
    return operations, base_dir


def command_to_operations(args: argparse.Namespace, warnings: Optional[List[str]] = None) -> Tuple[List[Dict[str, Any]], Optional[Path]]:
    # Parse an optional SEARCH/REPLACE transport into edit operations.
    diff_input = getattr(args, 'diff_input', None)
    diff_input_file = getattr(args, 'diff_input_file', None)
    diff_input_base64 = getattr(args, 'diff_input_base64', None)
    diff_input_stdin = bool(getattr(args, 'diff_input_stdin', False))
    diff_sources = [
        diff_input is not None,
        diff_input_file is not None,
        diff_input_base64 is not None,
        diff_input_stdin,
    ].count(True)
    if diff_sources > 1:
        fail('use only one of --diff-input, --diff-input-file, --diff-input-base64, or --diff-input-stdin')
    if diff_sources == 1:
        if diff_input is not None:
            raw_diff = diff_input
            base_dir = None
        elif diff_input_file is not None:
            diff_path = Path(diff_input_file)
            raw_diff = read_argument_file(str(diff_path), args.arg_encoding)
            base_dir = diff_path.parent
        elif diff_input_base64 is not None:
            raw_diff = decode_base64_text(diff_input_base64, 'diff-input-base64')
            base_dir = None
        else:
            raw_diff = sys.stdin.read()
            base_dir = None
        operations = parse_diff_input(raw_diff)
        for op in operations:
            if getattr(args, 'context_before', None):
                op['context_before'] = args.context_before
            if getattr(args, 'context_after', None):
                op['context_after'] = args.context_after
            if args.expected_count is not None:
                op['expected_count'] = args.expected_count
            if args.first:
                op['first'] = True
            if args.no_op_ok:
                op['no_op_ok'] = True
        resolved: List[Dict[str, Any]] = []
        for operation in operations:
            current = dict(operation)
            op = str(current.get('op') or '').replace('_', '-')
            current['op'] = op
            if op == 'edit':
                current['old'] = resolve_operation_value(current, 'old', True, args.arg_encoding, base_dir)
                current['new'] = resolve_operation_value(current, 'new', True, args.arg_encoding, base_dir)
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
            operation["old"] = resolve_cli_value(args, "old", True, stdin_taken=stdin_taken, warnings=warnings)
            operation["new"] = resolve_cli_value(args, "new", True, stdin_taken=stdin_taken, warnings=warnings)
            operation["expected_count"] = args.expected_count
            operation["first"] = args.first
            operation["no_op_ok"] = args.no_op_ok
            if getattr(args, 'context_before', None):
                operation["context_before"] = args.context_before
            if getattr(args, 'context_after', None):
                operation["context_after"] = args.context_after
        elif args.command == "regex":
            operation["pattern"] = resolve_cli_value(args, "pattern", True, stdin_taken=stdin_taken, warnings=warnings)
            operation["replacement"] = resolve_cli_value(args, "replacement", True, stdin_taken=stdin_taken, warnings=warnings)
            operation["flags"] = args.flags
            if args.count and args.first:
                fail("--count and --first are mutually exclusive")
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
            operation["text"] = resolve_cli_value(args, "text", True, stdin_taken=stdin_taken, warnings=warnings)
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
            operation["text"] = resolve_cli_value(args, "text", True, stdin_taken=stdin_taken, warnings=warnings)
            operation["preserve_indent"] = not getattr(args, 'no_preserve_indent', False)
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


def load_request_payload(args: argparse.Namespace) -> Dict[str, Any]:
    sources = [
        args.request_file is not None,
        args.request_base64 is not None,
        args.request_stdin,
    ].count(True)
    if sources != 1:
        fail(
            "transaction requires exactly one of --request-file, "
            "--request-base64, or --request-stdin"
        )
    if args.request_file is not None:
        raw = read_argument_file(args.request_file, args.arg_encoding)
    elif args.request_base64 is not None:
        raw = decode_base64_text(args.request_base64, "request-base64")
    else:
        raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"invalid transaction JSON: {exc}")
    if isinstance(payload, list):
        payload = {"files": payload}
    if not isinstance(payload, dict):
        fail("transaction JSON must be an object or a list of file requests")
    if "files" not in payload and "file" in payload:
        payload = {"files": [payload]}
    return payload


def request_item_args(
    parent: argparse.Namespace,
    item: Dict[str, Any],
    dry_run: bool,
) -> argparse.Namespace:
    if not isinstance(item, dict):
        fail("each transaction file request must be an object")
    file_value = item.get("file")
    if not isinstance(file_value, str) or not file_value:
        fail("each transaction file request requires a non-empty file")
    action = str(item.get("action", item.get("command", ""))).replace("_", "-")
    has_text = "text" in item
    has_operations = "operations" in item or "ops" in item
    if has_text and has_operations:
        fail(f"transaction request for {file_value} mixes text and operations")
    if not action:
        if has_text:
            action = "create"
        elif has_operations:
            action = "edit"
    child = argparse.Namespace(**vars(parent))
    child.file = file_value
    child.dry_run = dry_run
    child.no_lock = True
    child.interactive = False
    child.backup = False
    child._capture_transaction_plan = True
    child.backup_dir = None
    child.follow_symlink = bool(item.get("followSymlink", False))
    child.expected_sha256 = item.get("expectedSha256")
    child.encoding = item.get("inputEncoding", "auto")
    child.to_encoding = item.get("encoding", item.get("toEncoding", "preserve"))
    child.to_line_ending = item.get("lineEnding", "preserve")
    child.final_newline = item.get("finalNewline", "preserve")
    child.trim_trailing_whitespace = bool(item.get("trimTrailingWhitespace", False))
    child.force_write = bool(item.get("forceWrite", False))
    child.allow_nul = bool(item.get("allowNul", False))
    child.diff = bool(item.get("diff", False))

    if action == "create":
        text_value = item.get("text")
        if not isinstance(text_value, str):
            fail(f"create request for {file_value} requires string text")
        child.command = "create"
        child.text = text_value
        child.text_file = None
        child.text_base64 = None
        child.text_stdin = False
    elif action in ("edit", "batch"):
        operations = item.get("operations", item.get("ops"))
        if not isinstance(operations, list) or not operations:
            fail(f"edit request for {file_value} requires non-empty operations")
        expected = child.expected_sha256
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            fail(f"edit request for {file_value} requires expectedSha256 from stat")
        child.command = "batch"
        child.ops = json.dumps(operations, ensure_ascii=False)
        child.ops_file = None
        child.ops_base64 = None
        child.ops_stdin = False
    else:
        fail(f"unsupported transaction action for {file_value}: {action or '<missing>'}")
    return child


def run_preflight(args: argparse.Namespace) -> Dict[str, Any]:
    target = args.file or str(Path.cwd() / ".safe-edit-preflight-target")
    capability = check_fs_capability(target)
    return {
        "ok": True,
        "command": "preflight",
        "file": args.file,
        "pythonExecutable": sys.executable,
        "pythonVersion": ".".join(str(part) for part in sys.version_info[:3]),
        "stdinReadable": sys.stdin is not None and not sys.stdin.closed,
        "stdinIsTty": bool(sys.stdin.isatty()) if sys.stdin is not None else False,
        "base64Available": True,
        "requestTransports": ["stdin", "base64", "file"],
        "directoryWritable": capability["directoryWritable"],
        "canCreateTemp": capability["canWriteTmp"],
        "canCreateLock": capability["canCreateLock"],
        "executionMode": capability["executionMode"],
        "suggestions": capability["suggestions"],
        "dryRun": True,
        "written": False,
        "skipped": True,
    }


def run_transaction(args: argparse.Namespace) -> Dict[str, Any]:
    payload = load_request_payload(args)
    items = payload.get("files")
    if not isinstance(items, list) or not items:
        fail("transaction request requires a non-empty files list")

    children = [request_item_args(args, item, True) for item in items]
    identities: List[str] = []
    for child in children:
        identity = os.path.normcase(os.path.abspath(child.file))
        if identity in identities:
            fail(f"transaction contains duplicate file: {child.file}")
        identities.append(identity)

    lock_paths = [Path(child.file) for child in children]
    lock_paths.sort(key=lambda path: os.path.normcase(os.path.abspath(str(path))))
    previews: List[Dict[str, Any]] = []
    plans: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    attempted: List[Dict[str, Any]] = []

    lock_stack = contextlib.ExitStack()
    try:
        if not args.no_lock and not args.dry_run:
            for path in lock_paths:
                lock_stack.enter_context(
                    FileLock(path, args.lock_timeout, args.lock_stale_seconds)
                )

        # Each child runs once in dry-run mode and captures the exact bytes that
        # would be committed. This combines the original snapshot and
        # prevalidation pass instead of reading and transforming every file twice.
        for child in children:
            preview = run(child)
            plan = getattr(child, "_transaction_plan", None)
            if not isinstance(plan, dict):
                fail(f"transaction failed to prepare file: {child.file}")
            previews.append(preview)
            plans.append(plan)

        if args.dry_run:
            return {
                "ok": True,
                "command": "transaction",
                "file": None,
                "files": previews,
                "fileCount": len(previews),
                "dryRun": True,
                "written": False,
                "rolledBack": False,
                "atomicity": "prevalidated",
            }

        try:
            for child, plan in zip(children, plans):
                path = plan["path"]
                output = plan["output"]
                result = dict(plan["summary"])
                result["dryRun"] = False

                if plan["action"] == "create":
                    # Recheck immediately before the exclusive create so a file
                    # that appeared after prevalidation is never overwritten.
                    resolve_create_path(str(path))
                    exclusive_create(path, output)
                    attempted.append(plan)
                    if path.read_bytes() != output:
                        fail("post-write verification failed: bytes on disk do not match intended output")
                    result["written"] = True
                    result["created"] = True
                    plan["output"] = b""
                else:
                    # Locks are cooperative. Re-read immediately before commit to
                    # catch writers that ignored the lock, but reuse the prepared
                    # output instead of decoding and applying operations again.
                    current = read_target(path, args.max_bytes)
                    current_sha256 = hashlib.sha256(current).hexdigest()
                    if current_sha256 != plan["originalSha256"]:
                        fail(f"target changed after transaction prevalidation: {path}")
                    if output == current and not child.force_write:
                        result["skipped"] = True
                    else:
                        plan["original"] = current
                        attempted.append(plan)
                        backup = atomic_replace(
                            path, output, False, None, child.backup_suffix
                        )
                        if path.read_bytes() != output:
                            fail("post-write verification failed: bytes on disk do not match intended output")
                        result["backup"] = backup
                        result["written"] = True
                    plan["output"] = b""
                results.append(result)
        except Exception as exc:
            rollback_errors: List[str] = []
            for plan in reversed(attempted):
                path = plan["path"]
                try:
                    if plan["action"] == "create":
                        if path.exists():
                            path.unlink()
                            fsync_directory(path.parent)
                    else:
                        original = plan["original"]
                        if not path.exists():
                            fail("target disappeared during transaction")
                        if path.read_bytes() != original:
                            atomic_replace(path, original, False, None, ".bak")
                except Exception as rollback_exc:
                    rollback_errors.append(f"{path}: {rollback_exc}")
            if rollback_errors:
                fail(
                    f"transaction failed ({exc}); rollback also failed: "
                    + "; ".join(rollback_errors)
                )
            if isinstance(exc, SafeEditError):
                fail(f"transaction rolled back: {exc}")
            fail(f"transaction rolled back after unexpected error: {exc}")
    finally:
        lock_stack.close()

    return {
        "ok": True,
        "command": "transaction",
        "file": None,
        "files": results,
        "fileCount": len(results),
        "dryRun": False,
        "written": any(item.get("written") for item in results),
        "rolledBack": False,
        "atomicity": "prevalidated-with-rollback",
        "crashAtomic": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely edit text files.")
    parser.add_argument(
        "command",
        choices=(
            "inspect",
            "stat",
            "preflight",
            "transaction",
            "create",
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
            "remove-file",
        ),
    )
    parser.add_argument("--file")
    parser.add_argument("--workspace-root",
                        help="required workspace boundary for remove-file")
    parser.add_argument("--expected-sha256",
                        help="optional SHA-256 guard; required by remove-file and transaction edits")
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
    parser.add_argument("--lock-stale-seconds", type=float, default=120.0)
    parser.add_argument("--no-lock", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--interactive", "-i", action="store_true",
                        help="prompt for confirmation before each write operation")

    parser.add_argument('--old')
    parser.add_argument('--old-file')
    parser.add_argument('--old-base64', help='URL-safe or standard Base64 containing UTF-8 text')
    parser.add_argument('--old-stdin', action='store_true')
    parser.add_argument('--new')
    parser.add_argument('--new-file')
    parser.add_argument('--new-base64', help='URL-safe or standard Base64 containing UTF-8 text')
    parser.add_argument('--new-stdin', action='store_true')

    parser.add_argument('--pattern')
    parser.add_argument('--pattern-file')
    parser.add_argument('--pattern-base64', help='URL-safe or standard Base64 containing UTF-8 text')
    parser.add_argument('--pattern-stdin', action='store_true')
    parser.add_argument('--replacement')
    parser.add_argument('--replacement-file')
    parser.add_argument('--replacement-base64', help='URL-safe or standard Base64 containing UTF-8 text')
    parser.add_argument('--replacement-stdin', action='store_true')
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
    parser.add_argument('--diff-input',
                        help='SEARCH/REPLACE diff format input for edit operations')
    parser.add_argument('--diff-input-file',
                        help='read SEARCH/REPLACE diff from file')
    parser.add_argument('--diff-input-base64',
                        help='read SEARCH/REPLACE diff from URL-safe or standard Base64 UTF-8 text')
    parser.add_argument('--diff-input-stdin', action='store_true',
                        help='read SEARCH/REPLACE diff from stdin')

    parser.add_argument("--line", type=int)
    parser.add_argument("--start", type=int)
    parser.add_argument("--end", type=int)
    parser.add_argument('--text')
    parser.add_argument('--text-file')
    parser.add_argument('--text-base64', help='URL-safe or standard Base64 containing UTF-8 text')
    parser.add_argument('--text-stdin', action='store_true')
    
    parser.add_argument("--anchor-pattern", 
                        help="context anchor pattern for relative line positioning")
    parser.add_argument("--offset-start", 
                        help="offset from anchor for start line (e.g., +2, -1)")
    parser.add_argument("--offset-end",
                        help="offset from anchor for end line (e.g., +4, -1)")
    parser.add_argument("--anchor-occurrence", type=int,
                        help="which occurrence of anchor pattern to use (1-based)")
    parser.add_argument("--no-preserve-indent", action="store_true",
                        help="disable automatic indent preservation in replace-lines (default: preserve)")

    parser.add_argument('--ops')
    parser.add_argument('--ops-file')
    parser.add_argument('--ops-base64', help='URL-safe or standard Base64 containing UTF-8 batch JSON')
    parser.add_argument('--ops-stdin', action='store_true')
    parser.add_argument('--request-file')
    parser.add_argument('--request-base64',
                        help='URL-safe or standard Base64 containing UTF-8 transaction JSON')
    parser.add_argument('--request-stdin', action='store_true')
    return parser


def run_remove_file(args: argparse.Namespace) -> Dict[str, Any]:
    """Remove one verified regular file inside an explicit workspace root."""
    if args.follow_symlink:
        fail("remove-file does not support --follow-symlink")
    if args.backup or args.backup_dir:
        fail("remove-file does not support backup options")
    if args.force_write:
        fail("--force-write is not applicable to remove-file")
    if args.diff:
        fail("--diff is not applicable to remove-file")
    if args.interactive:
        fail("remove-file requires explicit task authorization, not --interactive")

    expected = (args.expected_sha256 or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        fail("remove-file requires --expected-sha256 with exactly 64 hexadecimal characters")

    path, root = resolve_remove_path(args.file, args.workspace_root)
    cap = check_fs_capability(str(path))
    if not cap["directoryWritable"]:
        fail(f"target directory is not writable: {path.parent}")

    lock_context = NullLock() if args.no_lock or args.dry_run else FileLock(
        path, args.lock_timeout, args.lock_stale_seconds
    )
    with lock_context:
        path, root = resolve_remove_path(str(path), str(root))
        before_stat = path.stat()
        original = read_target(path, args.max_bytes)
        after_stat = path.stat()
        before_identity = (
            before_stat.st_dev,
            before_stat.st_ino,
            before_stat.st_size,
            before_stat.st_mtime_ns,
        )
        after_identity = (
            after_stat.st_dev,
            after_stat.st_ino,
            after_stat.st_size,
            after_stat.st_mtime_ns,
        )
        if before_identity != after_identity:
            fail("file changed while remove-file was verifying it")
        actual = hashlib.sha256(original).hexdigest()
        if actual != expected:
            fail(
                "SHA-256 mismatch: "
                f"expected {expected}, actual {actual}; run stat again before removing"
            )

        summary: Dict[str, Any] = {
            "ok": True,
            "file": str(path),
            "workspaceRoot": str(root),
            "command": "remove-file",
            "sizeBytes": len(original),
            "expectedSha256": expected,
            "sha256": actual,
            "dryRun": args.dry_run,
            "changed": 1,
            "operations": [
                {"index": 1, "op": "remove-file", "changed": 1, "matchStrategy": "sha256"}
            ],
            "backup": None,
            "written": False,
            "removed": False,
            "skipped": False,
            "wouldRemove": True,
            "wouldChangeBytes": True,
        }
        if args.dry_run:
            return summary

        final_stat = path.stat()
        final_identity = (
            final_stat.st_dev,
            final_stat.st_ino,
            final_stat.st_size,
            final_stat.st_mtime_ns,
        )
        if final_identity != after_identity:
            fail("file changed after remove-file verification")
        path.unlink()
        fsync_directory(path.parent)
        if os.path.lexists(path):
            fail("post-remove verification failed: target still exists")
        summary["written"] = True
        summary["removed"] = True
        return summary


def run_create(args: argparse.Namespace) -> Dict[str, Any]:
    """Create one new text file without permitting implicit overwrite."""
    path = resolve_create_path(args.file)
    if args.to_encoding == "preserve":
        fail("create requires explicit --to-encoding")
    if args.to_line_ending == "preserve":
        fail("create requires explicit --to-line-ending")
    if args.backup or args.backup_dir:
        fail("create does not support backups because no original file exists")
    if args.force_write:
        fail("--force-write is not applicable to create")

    cap = check_fs_capability(str(path))
    if not cap["directoryWritable"]:
        fail(f"target directory is not writable: {path.parent}")

    warnings: List[str] = []
    text = resolve_cli_value(
        args,
        "text",
        True,
        stdin_taken=[],
        warnings=warnings,
    )
    newline = line_sep(args.to_line_ending)
    new_text = apply_post_transforms(text or "", args, newline)
    if "\x00" in new_text and not args.allow_nul:
        fail("text contains NUL bytes; refusing likely binary content")
    output_encoding = make_encoding_info(args.to_encoding)
    output = encode_text(new_text, output_encoding)
    if len(output) > args.max_bytes:
        fail(f"output is {len(output)} bytes, exceeding --max-bytes {args.max_bytes}")
    output_line_ending, output_line_counts, output_mixed = detect_line_ending(new_text)
    diff_text = generate_diff(path, "", new_text, args.context) if args.diff else ""

    summary: Dict[str, Any] = {
        "ok": True,
        "file": str(path),
        "command": "create",
        "encoding": output_encoding.name,
        "outputEncoding": output_encoding.name,
        "lineEnding": output_line_ending,
        "outputLineEnding": output_line_ending,
        "mixedLineEndings": output_mixed,
        "outputMixedLineEndings": output_mixed,
        "lineEndingCounts": output_line_counts,
        "outputLineEndingCounts": output_line_counts,
        "changed": 1,
        "operations": [
            {"index": 1, "op": "create", "changed": 1, "matchStrategy": "exact"}
        ],
        "postTransforms": {
            "toEncoding": args.to_encoding,
            "toLineEnding": args.to_line_ending,
            "finalNewline": args.final_newline,
            "trimTrailingWhitespace": bool(args.trim_trailing_whitespace),
        },
        "matchOptions": {
            "autoMatch": False,
            "fuzzy": False,
            "ignoreIndent": False,
            "ignoreEol": False,
            "normalizeWhitespace": False,
        },
        "dryRun": args.dry_run,
        "backup": None,
        "written": False,
        "created": False,
        "skipped": False,
        "wouldChangeBytes": True,
        "wouldCreate": True,
    }
    if warnings:
        summary["warnings"] = warnings
    if args.diff:
        summary["diff"] = diff_text

    if getattr(args, "_capture_transaction_plan", False):
        args._transaction_plan = {
            "action": "create",
            "path": path,
            "output": output,
            "summary": summary,
        }
    if args.dry_run:
        return summary

    lock_context = NullLock() if args.no_lock else FileLock(
        path, args.lock_timeout, args.lock_stale_seconds
    )
    with lock_context:
        resolve_create_path(str(path))
        if args.interactive:
            apply_this, _apply_all = prompt_interactive(
                path, "", new_text, args.context, "create"
            )
            if not apply_this:
                summary["skipped"] = True
                summary["interactiveSkipped"] = True
                return summary
        exclusive_create(path, output)
        if path.read_bytes() != output:
            fail("post-write verification failed: bytes on disk do not match intended output")
        summary["written"] = True
        summary["created"] = True
    return summary


def run(args: argparse.Namespace) -> Dict[str, Any]:
    if args.command == "preflight":
        return run_preflight(args)
    if args.command == "transaction":
        return run_transaction(args)
    if not args.file:
        fail(f"{args.command} requires --file")

    # Validate --interactive constraints
    if getattr(args, 'interactive', False):
        if args.dry_run:
            fail("--interactive cannot be used with --dry-run (dry-run doesn't write anyway)")
        if args.command == "inspect":
            fail("--interactive is not applicable to inspect command (read-only)")
    
    if args.command == "create":
        return run_create(args)
    if args.command == "remove-file":
        return run_remove_file(args)

    path = resolve_target_path(args.file, args.follow_symlink)
    if args.command == "inspect":
        original = read_target(path, args.max_bytes)
        if args.expected_sha256:
            expected = args.expected_sha256.lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                fail("--expected-sha256 must contain exactly 64 hexadecimal characters")
            actual = hashlib.sha256(original).hexdigest()
            if actual != expected:
                fail(
                    "SHA-256 mismatch: "
                    f"expected {expected}, actual {actual}; run stat again"
                )
        encoding = detect_encoding(original, args.encoding)
        text = strict_decode(original, encoding)
        return inspect_target(path, original, encoding, text)
    if args.command == "stat":
        original = read_target(path, args.max_bytes)
        if args.expected_sha256:
            expected = args.expected_sha256.lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                fail("--expected-sha256 must contain exactly 64 hexadecimal characters")
            actual = hashlib.sha256(original).hexdigest()
            if actual != expected:
                fail(
                    "SHA-256 mismatch: "
                    f"expected {expected}, actual {actual}; run stat again"
                )
        encoding = detect_encoding(original, args.encoding)
        text = strict_decode(original, encoding)
        return stat_target(path, original, encoding, text)

    # Sandbox capability detection
    cap = check_fs_capability(str(path))
    if cap["executionMode"] == "readonly-fallback":
        fail(
            "Sandbox does not allow file writes or temp file creation.\n"
            "Suggested actions:\n"
            "1. Use /tmp workspace\n"
            "2. Enable external workspace mount\n"
            "3. Use patch/diff output mode instead of in-place edit"
        )
    # Auto-disable locking in no-lock-mode
    disable_locking = cap["executionMode"] == "no-lock-mode"

    warnings: List[str] = []
    operations, _base_dir = command_to_operations(args, warnings=warnings)
    if args.command == "convert" and (
        args.to_encoding == "preserve"
        and args.to_line_ending == "preserve"
        and args.final_newline == "preserve"
        and not args.trim_trailing_whitespace
    ):
        fail("convert requires --to-encoding, --to-line-ending, --final-newline, or --trim-trailing-whitespace")
    lock_context = NullLock() if args.no_lock or args.dry_run or disable_locking else FileLock(path, args.lock_timeout, args.lock_stale_seconds)

    with lock_context:
        original = read_target(path, args.max_bytes)
        if args.expected_sha256:
            expected = args.expected_sha256.lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                fail("--expected-sha256 must contain exactly 64 hexadecimal characters")
            actual = hashlib.sha256(original).hexdigest()
            if actual != expected:
                fail(
                    "SHA-256 mismatch: "
                    f"expected {expected}, actual {actual}; run stat again"
                )
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
        if warnings:
            summary["warnings"] = warnings
        if args.diff:
            summary["diff"] = diff_text

        if getattr(args, "_capture_transaction_plan", False):
            args._transaction_plan = {
                "action": "edit",
                "path": path,
                "originalSha256": args.expected_sha256.lower(),
                "output": output,
                "summary": summary,
            }
        if not args.dry_run:
            # Interactive mode: prompt before writing
            interactive = getattr(args, 'interactive', False)
            
            if interactive:
                apply_this, _apply_all = prompt_interactive(path, text, new_text, args.context)
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
    if summary.get("command") == "preflight":
        print(
            f"Preflight: python={summary['pythonVersion']} "
            f"mode={summary['executionMode']} stdin={summary['stdinReadable']} "
            f"base64={summary['base64Available']}"
        )
        return
    if summary.get("command") == "transaction":
        note = "DRY-RUN " if summary["dryRun"] else ""
        print(
            f"{note}Transaction: files={summary['fileCount']} "
            f"written={summary['written']} atomicity={summary['atomicity']}"
        )
        return
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
    if summary.get("command") == "remove-file":
        action = "Would remove" if summary["dryRun"] else "Removed"
        print(
            f"{action}: {summary['file']} "
            f"(bytes={summary['sizeBytes']}, sha256={summary['sha256']})"
        )
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

            # For match-related errors, read file and provide structured recovery info
            error_type = classify_error_type(str(exc))
            old = ""
            text = ""
            lock_info = None

            if error_type in ("match_not_found", "match_ambiguous", "match_count_mismatch"):
                # Try to read file content for analysis
                try:
                    path = resolve_target_path(file_path, getattr(args, 'follow_symlink', False))
                    original = read_target(path, getattr(args, 'max_bytes', 50 * 1024 * 1024))
                    encoding = detect_encoding(original, getattr(args, 'encoding', 'auto'))
                    text = strict_decode(original, encoding)

                    # Get the search pattern (may come from --old, --old-file, or --old-stdin)
                    if command == "edit":
                        # Use resolve_cli_value to handle all input modes
                        try:
                            stdin_taken: List[str] = []
                            old = resolve_cli_value(args, "old", False, stdin_taken=stdin_taken) or ""
                        except Exception:
                            old = getattr(args, 'old', '') or ""
                    elif command == "regex":
                        try:
                            stdin_taken: List[str] = []
                            old = resolve_cli_value(args, "pattern", False, stdin_taken=stdin_taken) or ""
                        except Exception:
                            old = getattr(args, 'pattern', '') or ""
                except Exception:
                    # If we can't read the file, emit basic error
                    pass

            elif error_type == "lock_error":
                # Try to read lock file for structured recovery info
                try:
                    target_name = Path(file_path).name if file_path else ""
                    # Lock file is now in /tmp/safe-edit/locks/ (or system temp equivalent)
                    lock_key = _get_lock_key(file_path)
                    lock_path = _get_lock_dir() / f"{lock_key}.lock"
                    content = lock_path.read_text("utf-8")
                    pid = _read_lock_pid(lock_path)
                    lock_time = None
                    for field in content.split():
                        if field.startswith("time="):
                            lock_time = float(field.split("=", 1)[1])
                    lock_age = None
                    if lock_time is not None:
                        lock_age = round(time.time() - lock_time, 1)
                    lock_info = {"targetFile": target_name}
                    if pid is not None:
                        lock_info["lockPid"] = pid
                    if lock_age is not None:
                        lock_info["lockAgeSeconds"] = lock_age
                except Exception:
                    # Lock file may have been deleted by a concurrent process
                    target_name = Path(file_path).name if file_path else ""
                    lock_info = {"targetFile": target_name}

            emit_json_error(
                exc,
                file_path=file_path,
                command=command,
                old=old,
                text=text,
                lock_info=lock_info,
            )
        else:
            print(f"safe-edit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
