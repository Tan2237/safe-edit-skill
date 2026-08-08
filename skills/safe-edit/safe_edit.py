#!/usr/bin/env python3
"""Safe cross-platform text-file edits with strict decoding and atomic replacement."""

from __future__ import annotations

import argparse
import base64
import binascii
import codecs
from bisect import bisect_right
from collections import Counter
import difflib
import errno
import functools
import hashlib
import heapq
import itertools
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple


class SafeEditError(Exception):
    pass


_PROCESS_ALIVE = "alive"
_PROCESS_DEAD = "dead"
_PROCESS_UNKNOWN = "unknown"


class _ProcessNativeBindings(NamedTuple):
    owner: Any
    open_process: Any
    close_handle: Any


@functools.lru_cache(maxsize=1)
def _load_process_native_bindings() -> _ProcessNativeBindings:
    import ctypes
    import ctypes.wintypes

    owner = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = owner.OpenProcess
    open_process.restype = ctypes.wintypes.HANDLE
    open_process.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
    ]
    close_handle = owner.CloseHandle
    close_handle.argtypes = [ctypes.wintypes.HANDLE]
    close_handle.restype = ctypes.wintypes.BOOL
    return _ProcessNativeBindings(owner, open_process, close_handle)


def _process_liveness(pid: int) -> str:
    """Return alive/dead/unknown without treating access denial as death."""
    if pid <= 0:
        return _PROCESS_DEAD
    if pid > 0x7FFFFFFF:
        return _PROCESS_UNKNOWN
    if pid == os.getpid():
        return _PROCESS_ALIVE
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return _PROCESS_ALIVE
        except ProcessLookupError:
            return _PROCESS_DEAD
        except PermissionError:
            return _PROCESS_UNKNOWN
        except OverflowError:
            return _PROCESS_UNKNOWN
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return _PROCESS_DEAD
            return _PROCESS_UNKNOWN

    import ctypes
    import ctypes.wintypes

    process_query_limited_information = 0x1000
    error_access_denied = 5
    error_invalid_parameter = 87
    bindings = _load_process_native_bindings()
    _reset_thread_last_error()
    try:
        handle = bindings.open_process(
            process_query_limited_information,
            False,
            pid,
        )
    except (OverflowError, ValueError):
        return _PROCESS_UNKNOWN
    if handle:
        bindings.close_handle(ctypes.wintypes.HANDLE(handle))
        return _PROCESS_ALIVE
    last_error = _read_thread_last_error()
    if last_error == error_invalid_parameter:
        return _PROCESS_DEAD
    if last_error == error_access_denied:
        return _PROCESS_UNKNOWN
    return _PROCESS_UNKNOWN


def _is_process_alive(pid: int) -> bool:
    """Compatibility boolean; unknown owners are conservatively alive."""
    return _process_liveness(pid) != _PROCESS_DEAD


def _reset_thread_last_error() -> None:
    """Clear the thread last-error slot.

    ``ctypes.set_last_error`` only exists on Python 3.12+; on older
    Windows interpreters fall back to ``SetLastError`` via ``windll``.
    Only called on Windows native paths.
    """
    import ctypes

    setter = getattr(ctypes, "set_last_error", None)
    if setter is not None:
        setter(0)
        return
    ctypes.windll.kernel32.SetLastError(0)


def _read_thread_last_error() -> int:
    """Return the thread last-error value across Python versions."""
    import ctypes

    getter = getattr(ctypes, "get_last_error", None)
    if getter is not None:
        return int(getter())
    return int(ctypes.GetLastError())


def _read_lock_pid(lock_path: Path) -> Optional[int]:
    """Read a bounded lock snapshot and return its validated PID."""
    try:
        snapshot = _read_lock_snapshot(lock_path)
    except Exception:
        return None
    if snapshot is None or not snapshot.complete:
        return None
    return snapshot.pid


# Pre-compiled regex for line ending detection (used by split_records)
# Matches CRLF, CR, or LF - order matters: CRLF must be first to avoid partial matches
_LINE_ENDING_RE = re.compile(r'(\r\n|\r|\n)')
_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_WHITESPACE_RE = re.compile(r"(?<![ \t])[ \t]+(?=\r\n|\r|\n|\Z)")
_NON_CRLF_LINE_SEPARATOR_RE = re.compile(r"[\v\f\x1c-\x1e\x85\u2028\u2029]")
_SPLITLINE_SEPARATOR_CHARS = frozenset(
    "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"
)
_COMPACT_DIFF_MAX_LINES = 80
_COMPACT_DIFF_MAX_CHARS = 12_000
_COMPACT_DIFF_MAX_INPUT_LINES = 512
_COMPACT_DIFF_MAX_INPUT_CHARS = 256_000
_COMPACT_DIFF_SCAN_CHUNK = 64 * 1024
_EXACT_LITERAL_BATCH_MIN_SAVED_SCAN_CHARS = 256 * 1024
# Bound pairwise containment/suffix proof work; over-budget batches fall back.
_EXACT_LITERAL_BATCH_OVERLAP_WORK_BUDGET = 4 * 1024 * 1024
_TRIM_TRAILING_CANDIDATES = (" \n", "\t\n", " \r", "\t\r")
_TRIM_TRAILING_SHORT_RUN_LENGTHS = (4, 3, 2, 1)
_TRIM_TRAILING_LONG_RUN_GUARD = 5
_TRIM_TRAILING_MIXED_ROUNDS = 4
_DIAGNOSTIC_FRAGMENT_MAX_CHARS = 1_000
_FUZZY_MATCH_UNSET = object()


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


_DIAGNOSTIC_ATTRS = (
    "_diagnostic_operation",
    "_diagnostic_operation_index",
    "_diagnostic_text",
    "_diagnostic_file",
    "_diagnostic_file_index",
    "_diagnostic_command",
    "_expected_sha256",
    "_actual_sha256",
    "_file_already_exists",
    "_file_not_found",
    "_diagnostic_closest_match",
    "_transaction_written",
    "_transaction_rolled_back",
    "_transaction_partial_write",
    "_transaction_rollback_conflict",
    "_transaction_rollback_errors",
)

_EXISTING_FILE_HASH_MAX_BYTES = 50 * 1024 * 1024
_EXISTING_FILE_HASH_CHUNK_BYTES = 1024 * 1024


def _fail_preserving_diagnostics(message: str, cause: BaseException) -> None:
    """Re-raise with a new message while keeping retry-oriented attributes."""
    exc = SafeEditError(message)
    for attr in _DIAGNOSTIC_ATTRS:
        if hasattr(cause, attr):
            setattr(exc, attr, getattr(cause, attr))
    raise exc


def _attach_transaction_cleanup_errors(
    exc: Optional[BaseException],
    errors: Iterable[str],
) -> None:
    if exc is None:
        return
    additions = tuple(str(item) for item in errors)
    if not additions:
        return
    existing = tuple(
        getattr(exc, "_transaction_rollback_errors", ())
    )
    setattr(
        exc,
        "_transaction_rollback_errors",
        existing + additions,
    )


def _fail_sha256_mismatch(message: str, expected: str, actual: str) -> None:
    exc = SafeEditError(message)
    setattr(exc, "_expected_sha256", expected)
    setattr(exc, "_actual_sha256", actual)
    raise exc


def _existing_regular_file_sha256(path: Path) -> Optional[str]:
    """Best-effort bounded SHA-256 of an existing regular file."""
    try:
        flags = os.O_RDONLY
        for flag_name in ("O_BINARY", "O_CLOEXEC", "O_NOINHERIT", "O_NOFOLLOW"):
            flag = getattr(os, flag_name, None)
            if isinstance(flag, int):
                flags |= flag

        fd = os.open(str(path), flags)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                return None
            size = int(before.st_size)
            if size < 0 or size > _EXISTING_FILE_HASH_MAX_BYTES:
                return None

            digest = hashlib.sha256()
            remaining = size
            total_read = 0
            while remaining:
                request = min(
                    _EXISTING_FILE_HASH_CHUNK_BYTES,
                    remaining,
                )
                chunk = os.read(fd, request)
                if not chunk:
                    return None
                if len(chunk) > request:
                    return None
                total_read += len(chunk)
                if total_read > _EXISTING_FILE_HASH_MAX_BYTES:
                    return None
                digest.update(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
        finally:
            os.close(fd)

        current = os.stat(str(path), follow_symlinks=False)

        def marker(info: os.stat_result) -> Tuple[int, int, int, int, int]:
            return (
                int(info.st_dev),
                int(info.st_ino),
                int(stat.S_IFMT(info.st_mode)),
                int(info.st_size),
                _stat_mtime_ns(info),
            )

        if marker(before) != marker(after) or marker(after) != marker(current):
            return None
        return digest.hexdigest()
    except MemoryError:
        raise
    except Exception:
        return None


def _fail_file_already_exists(path: Path) -> None:
    exc = SafeEditError(f"file already exists: {path}")
    setattr(exc, "_file_already_exists", True)
    existing = _existing_regular_file_sha256(path)
    if existing is not None:
        setattr(exc, "_actual_sha256", existing)
    raise exc


def classify_error_type(message: str) -> str:
    """Classify a SafeEditError message into a structured error type.
    
    Returns one of:
    - "match_not_found": old text / regex pattern not found in file
    - "match_ambiguous": multiple matches when one expected
    - "match_count_mismatch": expected_count doesn't match actual
    - "hash_mismatch": expectedSha256 no longer matches the target
    - "encoding_error": encoding/decoding failure
    - "file_error": file I/O or path issue
    - "validation_error": invalid arguments or constraint violation
    - "lock_error": file lock contention
    - "format_error": invalid diff-input or SEARCH/REPLACE format
    - "unknown": unclassified error
    """
    msg = message.lower()
    # Order matters: more specific checks first to avoid misclassification
    if ("sha-256 mismatch" in msg or "changed after" in msg
            or "changed while" in msg):
        return "hash_mismatch"
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
    *,
    closest_match: Any = _FUZZY_MATCH_UNSET,
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

    # Reuse a prior search only when it is bound to this exact expected
    # fragment. Diagnostic transports may truncate the fragment before this
    # function runs, in which case the cached selection is no longer valid.
    closest_match = _reuse_or_find_closest_match(
        text,
        old,
        closest_match,
    )
    similarity = 0.0

    if closest_match is None:
        result["failureClass"] = "USER_INPUT"
        result["rootCause"] = "content_not_found"
        confidence = _CONFIDENCE_SCORES.get("content_not_found", 0.70)
        result["recommendedAction"] = _build_recommended_action("ask_user", confidence)
        return result

    if isinstance(closest_match, FuzzyMatch):
        line_num = closest_match.line
        fragment = closest_match.fragment
    else:
        line_num, fragment = closest_match

    # Diagnose at character level after fuzzy boundary normalization.
    similarity = _fuzzy_diagnostic_similarity(old, fragment)

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


def _diagnostic_target_fragment(
    operation: Optional[Dict[str, Any]],
) -> Tuple[str, bool]:
    """Return a bounded user-supplied target fragment for error reporting."""
    if not isinstance(operation, dict):
        return ("", False)
    op_name = str(
        operation.get("op") or operation.get("command") or ""
    ).replace("_", "-")
    keys = ("old",) if op_name == "edit" else ("pattern", "anchor_pattern")
    for key in keys:
        value = operation.get(key)
        if value is None:
            continue
        fragment = str(value)
        if len(fragment) <= _DIAGNOSTIC_FRAGMENT_MAX_CHARS:
            return (fragment, False)
        return (
            fragment[:_DIAGNOSTIC_FRAGMENT_MAX_CHARS],
            True,
        )
    return ("", False)


def build_error_payload(
    exc: SafeEditError,
    file_path: str = "",
    command: str = "",
    *,
    old: str = "",
    text: str = "",
    lock_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a structured error object for CLI and MCP callers.

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

    For hash_mismatch errors, provides expectedSha256 plus, when known,
    actualSha256 and a retry strategy for non-deleting operations. Deletion
    mismatches require re-reading the changed file before retrying.

    For create-on-existing-file errors, provides actualSha256 of a safe regular
    file when available, but requires inspecting the collision before editing.
    """
    error_type = classify_error_type(str(exc))
    diagnostic_operation = getattr(exc, "_diagnostic_operation", None)
    diagnostic_text = getattr(exc, "_diagnostic_text", None)
    diagnostic_file = getattr(exc, "_diagnostic_file", None)
    diagnostic_command = getattr(exc, "_diagnostic_command", None)
    if diagnostic_file:
        file_path = str(diagnostic_file)
    if diagnostic_command:
        command = str(diagnostic_command)
    if not old and isinstance(diagnostic_operation, dict):
        old, _target_truncated = _diagnostic_target_fragment(
            diagnostic_operation
        )
    if not text and isinstance(diagnostic_text, str):
        text = diagnostic_text

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

    transaction_written = getattr(exc, "_transaction_written", None)
    transaction_rolled_back = getattr(
        exc, "_transaction_rolled_back", None
    )
    transaction_partial_write = getattr(
        exc, "_transaction_partial_write", None
    )
    transaction_rollback_conflict = getattr(
        exc, "_transaction_rollback_conflict", None
    )
    transaction_rollback_errors = getattr(
        exc, "_transaction_rollback_errors", None
    )
    if isinstance(transaction_written, bool):
        error_obj["written"] = transaction_written
    if isinstance(transaction_rolled_back, bool):
        error_obj["rolledBack"] = transaction_rolled_back
    if isinstance(transaction_partial_write, bool):
        error_obj["partialWrite"] = transaction_partial_write
    if isinstance(transaction_rollback_conflict, bool):
        error_obj["rollbackConflict"] = transaction_rollback_conflict
    if isinstance(transaction_rollback_errors, (list, tuple)):
        error_obj["rollbackErrors"] = [
            str(item) for item in transaction_rollback_errors
        ]

    operation_index = getattr(exc, "_diagnostic_operation_index", None)
    target_fragment, target_truncated = _diagnostic_target_fragment(
        diagnostic_operation
    )
    if operation_index is not None or isinstance(diagnostic_operation, dict):
        failed_operation: Dict[str, Any] = {
            "index": operation_index,
            "op": str(
                (diagnostic_operation or {}).get("op")
                or (diagnostic_operation or {}).get("command")
                or ""
            ).replace("_", "-"),
            "targetFragment": target_fragment,
        }
        if target_truncated:
            failed_operation["targetTruncated"] = True
        error_obj["failedOperation"] = failed_operation
        if operation_index is not None:
            error_obj["operationIndex"] = operation_index
        if target_fragment:
            error_obj["targetFragment"] = target_fragment

    file_index = getattr(exc, "_diagnostic_file_index", None)
    if file_index is not None:
        error_obj["failedFile"] = {
            "index": file_index,
            "file": file_path,
        }

    # Structured recovery info for match-related errors
    if error_type in ("match_not_found", "match_ambiguous", "match_count_mismatch"):
        if old and text:
            analysis = analyze_match_failure(
                old,
                text,
                error_type,
                closest_match=getattr(
                    exc,
                    "_diagnostic_closest_match",
                    _FUZZY_MATCH_UNSET,
                ),
            )
            error_obj["failureClass"] = analysis["failureClass"]
            error_obj["rootCause"] = analysis["rootCause"]
            error_obj["failureReason"] = analysis["rootCause"]
            error_obj["error"]["reason"] = analysis["rootCause"]
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

    # Structured recovery info for stale-hash errors
    if error_type == "hash_mismatch":
        is_remove = command == "remove-file"
        error_obj["failureClass"] = (
            "RE_READ_REQUIRED" if is_remove else "RETRYABLE"
        )
        expected_sha256 = getattr(exc, "_expected_sha256", None)
        actual_sha256 = getattr(exc, "_actual_sha256", None)
        if isinstance(expected_sha256, str) and expected_sha256:
            error_obj["expectedSha256"] = expected_sha256
        if isinstance(actual_sha256, str) and actual_sha256:
            error_obj["rootCause"] = "stale_expected_sha256"
            error_obj["failureReason"] = "stale_expected_sha256"
            error_obj["error"]["reason"] = "stale_expected_sha256"
            error_obj["actualSha256"] = actual_sha256
            if is_remove:
                error_obj["recommendedAction"] = {
                    "type": "re_read_file",
                    "confidence": 0.95,
                }
            else:
                error_obj["recommendedAction"] = {
                    "type": "retry_with_actual_sha256",
                    "confidence": 0.9,
                }
                error_obj["retryStrategy"] = {
                    "expectedSha256": actual_sha256
                }
        else:
            error_obj["rootCause"] = "target_changed_during_operation"
            error_obj["failureReason"] = "target_changed_during_operation"
            error_obj["error"]["reason"] = "target_changed_during_operation"
            error_obj["recommendedAction"] = {
                "type": "re_read_file" if is_remove else "re_stat_and_retry",
                "confidence": 0.9 if is_remove else 0.8,
            }

    # Structured recovery info when create finds an existing file. A create
    # collision must never be converted automatically into authority to edit.
    if error_type == "file_error" and getattr(exc, "_file_already_exists", False):
        error_obj["rootCause"] = "target_already_exists"
        error_obj["failureReason"] = "target_already_exists"
        error_obj["error"]["reason"] = "target_already_exists"
        error_obj["failureClass"] = "RE_READ_REQUIRED"
        existing_sha256 = getattr(exc, "_actual_sha256", None)
        if isinstance(existing_sha256, str) and existing_sha256:
            error_obj["actualSha256"] = existing_sha256
            error_obj["recommendedAction"] = {
                "type": "re_read_file",
                "confidence": 0.95,
            }
        else:
            error_obj["recommendedAction"] = {
                "type": "inspect_existing_path",
                "confidence": 0.9,
            }

    # Hint when an edit or stat target does not exist
    if error_type == "file_error" and getattr(exc, "_file_not_found", False):
        error_obj["rootCause"] = "target_not_found"
        error_obj["failureReason"] = "target_not_found"
        error_obj["error"]["reason"] = "target_not_found"
        error_obj["recommendedAction"] = {
            "type": "create_file_if_intended",
            "confidence": 0.6,
        }

    error_obj.setdefault("failureReason", error_type)
    error_obj["error"].setdefault("reason", error_obj["failureReason"])
    return error_obj


def emit_json_error(
    exc: SafeEditError,
    file_path: str = "",
    command: str = "",
    *,
    old: str = "",
    text: str = "",
    lock_info: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit the shared structured error payload as JSON."""
    error_obj = build_error_payload(
        exc,
        file_path=file_path,
        command=command,
        old=old,
        text=text,
        lock_info=lock_info,
    )
    print(json.dumps(error_obj, ensure_ascii=False, sort_keys=True))


def visualize_whitespace(text: str) -> str:
    """Convert whitespace characters to visible symbols for debugging."""
    return (
        text.replace("\t", "[TAB]")
        .replace(" ", "[SP]")
        .replace("\r", "[CR]")
        .replace("\n", "[LF]\n")
    )


def _normalize_fuzzy_lines(value: str) -> List[str]:
    """Normalize fuzzy input without changing meaningful inner whitespace."""
    return [line.strip() for line in value.splitlines()]


def _fuzzy_similarity(pattern: str, fragment: str) -> float:
    """Compare fuzzy fragments after per-line boundary normalization."""
    pattern_lines = _normalize_fuzzy_lines(pattern)
    fragment_lines = _normalize_fuzzy_lines(fragment)
    if not pattern_lines or not fragment_lines:
        return 0.0
    if len(pattern_lines) == 1 and len(fragment_lines) == 1:
        return difflib.SequenceMatcher(
            None, pattern_lines[0], fragment_lines[0]
        ).ratio()
    return difflib.SequenceMatcher(
        None, tuple(pattern_lines), tuple(fragment_lines)
    ).ratio()


def _fuzzy_diagnostic_similarity(pattern: str, fragment: str) -> float:
    """Return character similarity after fuzzy boundary normalization."""
    normalized_pattern = "\n".join(_normalize_fuzzy_lines(pattern))
    normalized_fragment = "\n".join(_normalize_fuzzy_lines(fragment))
    if not normalized_pattern or not normalized_fragment:
        return 0.0
    return difflib.SequenceMatcher(
        None, normalized_pattern, normalized_fragment
    ).ratio()


@dataclass(frozen=True)
class FuzzyMatch:
    """A fuzzy result that retains its exact source span."""

    line: int
    start: int
    length: int
    fragment: str
    similarity: float
    expected: str

    def as_tuple(self) -> Tuple[int, str]:
        return self.line, self.fragment


@dataclass(frozen=True)
class _CachedFuzzyMatch:
    """Bind a cached hit or miss to the exact expected fragment searched."""

    expected: str
    match: Optional[FuzzyMatch]


@dataclass
class _FuzzyTextIndex:
    """One normalized line table plus compact source offsets."""

    text: str
    lines: List[str]
    starts: Any

    def span(self, start: int, count: int) -> Tuple[int, int]:
        original_start = self.starts[start]
        last_start = self.starts[start + count - 1]
        next_line = start + count
        original_end = (
            self.starts[next_line]
            if next_line < len(self.starts)
            else len(self.text)
        )
        while (
            original_end > last_start
            and self.text[original_end - 1] in _FUZZY_LINE_BREAK_CHARS
        ):
            original_end -= 1
        return original_start, original_end - original_start


_FUZZY_LINE_BREAK_CHARS = frozenset(
    "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"
)


def _build_fuzzy_text_index(text: str) -> _FuzzyTextIndex:
    # Import locally so short non-fuzzy commands do not pay module startup
    # cost.  Offsets use four bytes until a text can exceed that range.
    from array import array

    lines = text.splitlines(keepends=True)
    starts = array("I" if len(text) <= 0xFFFFFFFF else "Q")
    offset = 0
    for index, record in enumerate(lines):
        content_length = len(record)
        while (
            content_length > 0
            and record[content_length - 1] in _FUZZY_LINE_BREAK_CHARS
        ):
            content_length -= 1
        starts.append(offset)
        lines[index] = record[:content_length].strip()
        offset += len(record)
    return _FuzzyTextIndex(text, lines, starts)


def _reuse_or_find_closest_match(
    text: str,
    expected: str,
    cached: Any,
) -> Optional[FuzzyMatch]:
    if isinstance(cached, _CachedFuzzyMatch):
        if cached.expected == expected:
            return cached.match
    elif isinstance(cached, FuzzyMatch) and cached.expected == expected:
        return cached
    return find_closest_match_result(text, expected)


def _line_fragment_span(
    text: str,
    line_number: int,
    fragment: str,
) -> Optional[Tuple[int, int]]:
    """Compatibility helper; fuzzy edit paths use FuzzyMatch directly."""
    index = _build_fuzzy_text_index(text)
    if line_number < 1 or line_number > len(index.lines):
        return None
    start = index.starts[line_number - 1]
    if text[start:start + len(fragment)] != fragment:
        return None
    return start, len(fragment)


def _candidate_start_intervals(
    text_lines: List[str],
    pattern_line_set: Set[str],
    pattern_len: int,
    max_start: int,
) -> Iterable[Tuple[int, int]]:
    """Yield merged ranges of windows sharing a line with the pattern."""
    current_start: Optional[int] = None
    current_end = -1
    for text_index, line in enumerate(text_lines):
        if line not in pattern_line_set:
            continue
        first_start = text_index - pattern_len + 1
        if first_start < 0:
            first_start = 0
        last_start = text_index
        if last_start > max_start:
            last_start = max_start
        if first_start > last_start:
            continue
        if current_start is None:
            current_start = first_start
            current_end = last_start
        elif first_start <= current_end + 1:
            current_end = max(current_end, last_start)
        else:
            yield current_start, current_end
            current_start = first_start
            current_end = last_start
    if current_start is not None:
        yield current_start, current_end


_FUZZY_AUTO_MIN_TEXT_BYTES = 8 * 1024 * 1024
_FUZZY_AUTO_MIN_COMPARISON_UNITS = 10_000_000
_FUZZY_AUTO_HIGH_WORK_UNITS = 40_000_000
_FUZZY_AUTO_MIN_SINGLE_CANDIDATES = 10_000
_FUZZY_AUTO_MIN_MULTILINE_CANDIDATES = 50_000
_FUZZY_AUTO_MAX_WORKERS = 4
_FUZZY_MAX_WORKERS = 8
_FUZZY_CPU_RESERVE = 2
_FUZZY_SINGLELINE_CANDIDATE_LIMIT = 256
_FUZZY_SEEN_LINE_CACHE_LIMIT = 4096
_FUZZY_SINGLELINE_MIN_SCORE = 0.3
_FUZZY_SINGLELINE_LCS_MIN_COMPARISON_UNITS = 64 * 1024
_INT_BIT_COUNT = getattr(int, "bit_count", None)


@dataclass(frozen=True)
class FuzzyWorkload:
    text_storage_bytes: int
    line_count: int
    pattern_line_count: int
    candidate_count: int
    comparison_units: int
    unique_line_count: int


@dataclass(frozen=True)
class FuzzyWorkerPlan:
    workers: int
    reason: str


def parse_fuzzy_workers(value: str) -> Any:
    """Parse auto or an explicit fuzzy process count."""
    normalized = str(value).strip().lower()
    if normalized == "auto":
        return "auto"
    try:
        workers = int(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--fuzzy-workers must be auto or an integer from 1 to 8"
        ) from exc
    if workers < 1 or workers > _FUZZY_MAX_WORKERS:
        raise argparse.ArgumentTypeError(
            "--fuzzy-workers must be auto or an integer from 1 to 8"
        )
    return workers


def _build_fuzzy_workload(
    text: str,
    text_lines: List[str],
    pattern_lines: Tuple[str, ...],
) -> FuzzyWorkload:
    pattern_len = len(pattern_lines)
    line_count = len(text_lines)
    if pattern_len == 1:
        unique_lines: Set[str] = set()
        unique_characters = 0
        for line in text_lines:
            if line in unique_lines:
                continue
            unique_lines.add(line)
            unique_characters += len(line)
            if len(unique_lines) > 4096:
                break
        estimated_characters = unique_characters
        if len(unique_lines) > 4096:
            average_characters = unique_characters / len(unique_lines)
            estimated_characters = max(
                unique_characters,
                int(average_characters * line_count),
            )
        comparison_units = max(1, len(pattern_lines[0])) * max(
            1, estimated_characters
        )
        candidate_count = line_count
        unique_line_count = len(unique_lines)
    else:
        max_start = line_count - pattern_len
        if max_start < 0:
            candidate_count = 0
        else:
            candidate_count = sum(
                end - start + 1
                for start, end in _candidate_start_intervals(
                    text_lines,
                    set(pattern_lines),
                    pattern_len,
                    max_start,
                )
            )
        comparison_units = candidate_count * pattern_len
        unique_line_count = 0

    return FuzzyWorkload(
        text_storage_bytes=text.__sizeof__(),
        line_count=line_count,
        pattern_line_count=pattern_len,
        candidate_count=candidate_count,
        comparison_units=comparison_units,
        unique_line_count=unique_line_count,
    )


def choose_fuzzy_worker_plan(
    requested: Any,
    workload: FuzzyWorkload,
    logical_cpus: Optional[int] = None,
) -> FuzzyWorkerPlan:
    """Choose a deterministic process count from task shape and CPU topology."""
    detected_cpus = os.cpu_count() or 1
    if logical_cpus is None and hasattr(os, "sched_getaffinity"):
        try:
            detected_cpus = len(os.sched_getaffinity(0))
        except OSError:
            pass
    cpus = max(1, int(logical_cpus or detected_cpus))
    if requested != "auto":
        requested_count = int(requested)
        if requested_count <= 1 or workload.candidate_count < 2:
            return FuzzyWorkerPlan(1, "explicit-serial")
        return FuzzyWorkerPlan(
            min(requested_count, cpus, workload.candidate_count),
            "explicit",
        )

    if workload.text_storage_bytes < _FUZZY_AUTO_MIN_TEXT_BYTES:
        return FuzzyWorkerPlan(1, "small-text")
    if workload.candidate_count < 2:
        return FuzzyWorkerPlan(1, "insufficient-candidates")

    minimum_candidates = (
        _FUZZY_AUTO_MIN_SINGLE_CANDIDATES
        if workload.pattern_line_count == 1
        else _FUZZY_AUTO_MIN_MULTILINE_CANDIDATES
    )
    if (
        workload.candidate_count < minimum_candidates
        and workload.comparison_units < _FUZZY_AUTO_HIGH_WORK_UNITS
    ):
        return FuzzyWorkerPlan(1, "light-candidate-set")
    if workload.comparison_units < _FUZZY_AUTO_MIN_COMPARISON_UNITS:
        return FuzzyWorkerPlan(1, "light-comparison-work")

    available = max(1, cpus - _FUZZY_CPU_RESERVE)
    workers = min(
        _FUZZY_AUTO_MAX_WORKERS,
        available,
        workload.candidate_count,
    )
    if workers < 2:
        return FuzzyWorkerPlan(1, "cpu-reserve")
    return FuzzyWorkerPlan(workers, "auto-heavy")


def _fuzzy_bit_count(value: int) -> int:
    if _INT_BIT_COUNT is not None:
        return _INT_BIT_COUNT(value)
    return bin(value).count("1")


def _fuzzy_character_masks(pattern: str) -> Dict[str, int]:
    masks: Dict[str, int] = {}
    bit = 1
    for character in pattern:
        masks[character] = masks.get(character, 0) | bit
        bit <<= 1
    return masks


def _fuzzy_lcs_ratio_upper(
    line: str,
    pattern_length: int,
    character_masks: Dict[str, int],
) -> float:
    """Return an exact LCS ratio, an upper bound for SequenceMatcher."""
    total_length = pattern_length + len(line)
    if total_length == 0:
        return 1.0
    state = 0
    for character in line:
        matches = character_masks.get(character, 0)
        combined = matches | state
        state = combined & ~(
            combined - ((state << 1) | 1)
        )
    return (2.0 * _fuzzy_bit_count(state)) / total_length


def _single_line_prefilter(
    text_lines: List[str],
    pattern_line: str,
    max_start: int,
) -> Optional[Tuple[int, float, int]]:
    """Select exactly while using a bounded top set only as a seed."""
    from array import array

    pattern_length = len(pattern_line)
    comparison_units = (max_start + 1) * max(1, pattern_length)
    use_lcs = False
    character_masks: Dict[str, int] = {}
    if comparison_units >= _FUZZY_SINGLELINE_LCS_MIN_COMPARISON_UNITS:
        character_masks = _fuzzy_character_masks(pattern_line)
        sample_count = min(8, max_start + 1)
        sample_step = max(1, (max_start + 1) // sample_count)
        sample_uppers = [
            _fuzzy_lcs_ratio_upper(
                text_lines[sample_index],
                pattern_length,
                character_masks,
            )
            for sample_index in itertools.islice(
                range(0, max_start + 1, sample_step),
                sample_count,
            )
        ]
        use_lcs = (
            sum(upper < _FUZZY_SINGLELINE_MIN_SCORE for upper in sample_uppers) * 2
            >= len(sample_uppers)
        )
        if not use_lcs:
            character_masks = {}
    index_code = "I" if max_start <= 0xFFFFFFFF else "Q"
    candidate_starts = array(index_code)
    upper_bounds = array("d")
    seed_heap: List[Tuple[float, int, int]] = []
    seen: Set[str] = set()
    best_score = 0.0
    best_start = 0
    saw_acceptable = False
    matcher = difflib.SequenceMatcher(None, pattern_line, "")

    for index in range(max_start + 1):
        line = text_lines[index]
        if line == pattern_line or (
            pattern_line and pattern_line in line
        ):
            return 1, 1.0, index
        if line in seen:
            continue
        if len(seen) < _FUZZY_SEEN_LINE_CACHE_LIMIT:
            seen.add(line)

        if line and line in pattern_line:
            score = len(line) / pattern_length if pattern_length else 0.0
            if score >= _FUZZY_SINGLELINE_MIN_SCORE and (
                not saw_acceptable
                or score > best_score
                or (score == best_score and index < best_start)
            ):
                best_score = score
                best_start = index
                saw_acceptable = True
            continue

        if not use_lcs:
            matcher.set_seq2(line)
            upper = matcher.real_quick_ratio()
            if upper < _FUZZY_SINGLELINE_MIN_SCORE or (saw_acceptable and upper <= best_score):
                continue
            upper = matcher.quick_ratio()
            if upper < _FUZZY_SINGLELINE_MIN_SCORE or (saw_acceptable and upper <= best_score):
                continue
            score = matcher.ratio()
            if score >= _FUZZY_SINGLELINE_MIN_SCORE and (
                not saw_acceptable
                or score > best_score
            ):
                best_score = score
                best_start = index
                saw_acceptable = True
            continue
        upper = _fuzzy_lcs_ratio_upper(
            line,
            pattern_length,
            character_masks,
        )
        if upper < _FUZZY_SINGLELINE_MIN_SCORE:
            continue
        position = len(candidate_starts)
        candidate_starts.append(index)
        upper_bounds.append(upper)
        item = (upper, -index, position)
        if len(seed_heap) < _FUZZY_SINGLELINE_CANDIDATE_LIMIT:
            heapq.heappush(seed_heap, item)
        elif item[:2] > seed_heap[0][:2]:
            heapq.heapreplace(seed_heap, item)


    def can_improve(upper: float, index: int) -> bool:
        if upper < _FUZZY_SINGLELINE_MIN_SCORE:
            return False
        if not saw_acceptable:
            return True
        return upper > best_score or (
            upper == best_score and index < best_start
        )

    def evaluate(position: int) -> None:
        nonlocal best_score, best_start, saw_acceptable
        index = candidate_starts[position]
        if not can_improve(upper_bounds[position], index):
            return
        matcher.set_seq2(text_lines[index])
        if not can_improve(matcher.real_quick_ratio(), index):
            return
        if not can_improve(matcher.quick_ratio(), index):
            return
        score = matcher.ratio()
        if score >= _FUZZY_SINGLELINE_MIN_SCORE and (
            not saw_acceptable
            or score > best_score
            or (score == best_score and index < best_start)
        ):
            best_score = score
            best_start = index
            saw_acceptable = True

    ordered_seed = sorted(
        seed_heap,
        key=lambda item: (-item[0], -item[1]),
    )
    seed_positions = {item[2] for item in ordered_seed}
    for _upper, _negative_index, position in ordered_seed:
        evaluate(position)
    for position in range(len(candidate_starts)):
        if position not in seed_positions:
            evaluate(position)

    if not saw_acceptable:
        return None
    return 0, best_score, best_start


def _find_closest_start(
    text_lines: List[str],
    pattern_lines: Tuple[str, ...],
    max_start: Optional[int] = None,
) -> Optional[Tuple[int, float, int]]:
    """Return priority, score, and zero-based start for one line slice."""
    pattern_len = len(pattern_lines)
    available_max_start = len(text_lines) - pattern_len
    if available_max_start < 0:
        return None
    if max_start is None or max_start > available_max_start:
        max_start = available_max_start
    if max_start < 0:
        return None

    if pattern_len == 1:
        return _single_line_prefilter(
            text_lines,
            pattern_lines[0],
            max_start,
        )

    pattern_counts = Counter(pattern_lines)
    best_matches = 0
    best_start = 0
    saw_candidate = False
    score_cache: Dict[Tuple[str, ...], int] = {}

    for interval_start, interval_end in _candidate_start_intervals(
        text_lines, set(pattern_lines), pattern_len, max_start
    ):
        saw_candidate = True
        window_counts = Counter(
            text_lines[interval_start:interval_start + pattern_len]
        )
        overlap = sum(
            min(count, pattern_counts.get(line, 0))
            for line, count in window_counts.items()
        )

        for start in range(interval_start, interval_end + 1):
            if overlap > best_matches:
                fragment = tuple(text_lines[start:start + pattern_len])
                if fragment == pattern_lines:
                    return 1, 1.0, start

                matched = score_cache.get(fragment)
                if matched is None:
                    line_matcher = difflib.SequenceMatcher(
                        None, pattern_lines, fragment
                    )
                    matched = sum(
                        block.size for block in line_matcher.get_matching_blocks()
                    )
                    if len(score_cache) < 4096:
                        score_cache[fragment] = matched
                if matched > best_matches:
                    best_matches = matched
                    best_start = start

            if start == interval_end:
                continue

            outgoing = text_lines[start]
            incoming = text_lines[start + pattern_len]
            if outgoing == incoming:
                continue

            outgoing_count = window_counts[outgoing]
            outgoing_cap = pattern_counts.get(outgoing, 0)
            if outgoing_cap and outgoing_count <= outgoing_cap:
                overlap -= 1
            if outgoing_count == 1:
                del window_counts[outgoing]
            else:
                window_counts[outgoing] = outgoing_count - 1

            incoming_count = window_counts.get(incoming, 0)
            incoming_cap = pattern_counts.get(incoming, 0)
            if incoming_cap and incoming_count < incoming_cap:
                overlap += 1
            window_counts[incoming] = incoming_count + 1

    if not saw_candidate:
        return None
    return 0, best_matches / pattern_len, best_start


def _fuzzy_outcome_is_acceptable(
    outcome: Optional[Tuple[int, float, int]],
    pattern_len: int,
) -> bool:
    if outcome is None:
        return False
    priority, score, _start = outcome
    if priority:
        return True
    return score >= (0.3 if pattern_len == 1 else 0.5)


def _fuzzy_outcome_is_better(
    candidate: Tuple[int, float, int],
    best: Optional[Tuple[int, float, int]],
) -> bool:
    if best is None:
        return True
    candidate_priority, candidate_score, candidate_start = candidate
    best_priority, best_score, best_start = best
    return (
        candidate_priority > best_priority
        or (
            candidate_priority == best_priority
            and (
                candidate_score > best_score
                or (
                    candidate_score == best_score
                    and candidate_start < best_start
                )
            )
        )
    )


def _configure_fuzzy_worker_priority() -> None:
    """Run fuzzy workers below normal priority so foreground work wins."""
    try:
        if os.name == "nt":
            import ctypes

            below_normal_priority_class = 0x00004000
            process = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.kernel32.SetPriorityClass(
                process, below_normal_priority_class
            )
        elif hasattr(os, "nice"):
            os.nice(5)
    except Exception:
        pass


def _fuzzy_chunk_worker(
    task: Tuple[List[str], Tuple[str, ...], int, int],
) -> Optional[Tuple[int, float, int]]:
    lines, pattern_lines, local_max_start, global_start = task
    outcome = _find_closest_start(lines, pattern_lines, local_max_start)
    if outcome is None:
        return None
    priority, score, local_start = outcome
    return priority, score, global_start + local_start


def _run_parallel_fuzzy_tasks(
    tasks: List[Tuple[List[str], Tuple[str, ...], int, int]],
    workers: int,
) -> List[Optional[Tuple[int, float, int]]]:
    # Importing multiprocessing support costs a measurable fraction of short
    # CLI invocations.  Keep it off the inspect/stat/edit startup path.
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_configure_fuzzy_worker_priority,
    ) as executor:
        return list(executor.map(_fuzzy_chunk_worker, tasks))


def _render_fuzzy_outcome(
    outcome: Optional[Tuple[int, float, int]],
    text_index: _FuzzyTextIndex,
    pattern_len: int,
    expected: str,
) -> Optional[FuzzyMatch]:
    if not _fuzzy_outcome_is_acceptable(outcome, pattern_len):
        return None
    assert outcome is not None
    start_line = outcome[2]
    start, length = text_index.span(start_line, pattern_len)
    fragment = text_index.text[start:start + length]
    return FuzzyMatch(
        line=start_line + 1,
        start=start,
        length=length,
        fragment=fragment,
        similarity=_fuzzy_similarity(expected, fragment),
        expected=expected,
    )


def find_closest_match_result(
    text: str,
    pattern: str,
    workers: Any = 1,
    max_lines: int = 10,
) -> Optional[FuzzyMatch]:
    """Find a closest match and retain its exact original-text span."""
    requested = parse_fuzzy_workers(str(workers))
    if not pattern or not text:
        return None

    pattern_raw_lines = pattern.splitlines()
    if not pattern_raw_lines:
        return None
    pattern_lines = tuple(line.strip() for line in pattern_raw_lines)
    pattern_len = len(pattern_lines)
    text_index = _build_fuzzy_text_index(text)
    text_lines = text_index.lines
    if len(text_lines) < pattern_len:
        return None

    if requested == 1:
        plan = FuzzyWorkerPlan(1, "explicit-serial")
    elif (
        requested == "auto"
        and text.__sizeof__() < _FUZZY_AUTO_MIN_TEXT_BYTES
    ):
        plan = FuzzyWorkerPlan(1, "small-text")
    else:
        low_diversity = False
        if requested == "auto" and text_lines:
            sample_step = max(1, len(text_lines) // 512)
            sampled = {
                text_lines[index]
                for index in range(0, len(text_lines), sample_step)
                if index // sample_step <= 512
            }
            low_diversity = len(sampled) <= 4
        if low_diversity:
            plan = FuzzyWorkerPlan(1, "low-diversity")
        else:
            workload = _build_fuzzy_workload(
                text,
                text_lines,
                pattern_lines,
            )
            plan = choose_fuzzy_worker_plan(requested, workload)

    if plan.workers <= 1:
        return _render_fuzzy_outcome(
            _find_closest_start(text_lines, pattern_lines),
            text_index,
            pattern_len,
            pattern,
        )

    total_starts = len(text_lines) - pattern_len + 1
    actual_workers = min(plan.workers, total_starts)
    max_payload_tasks = max(
        1,
        ((2 * len(text_lines)) // pattern_len + 1) // 2,
    )
    task_count = min(total_starts, actual_workers * 2, max_payload_tasks)
    actual_workers = min(actual_workers, task_count)
    if actual_workers <= 1:
        return _render_fuzzy_outcome(
            _find_closest_start(text_lines, pattern_lines),
            text_index,
            pattern_len,
            pattern,
        )

    chunk_size = (total_starts + task_count - 1) // task_count
    tasks: List[Tuple[List[str], Tuple[str, ...], int, int]] = []
    for global_start in range(0, total_starts, chunk_size):
        global_end = min(total_starts, global_start + chunk_size)
        tasks.append(
            (
                text_lines[global_start:global_end + pattern_len - 1],
                pattern_lines,
                global_end - global_start - 1,
                global_start,
            )
        )

    try:
        outcomes = _run_parallel_fuzzy_tasks(tasks, actual_workers)
    except (KeyboardInterrupt, MemoryError):
        raise
    except (OSError, RuntimeError):
        # BrokenProcessPool derives from RuntimeError.
        outcomes = []

    best: Optional[Tuple[int, float, int]] = None
    for outcome in outcomes:
        if outcome is not None and _fuzzy_outcome_is_better(outcome, best):
            best = outcome
    if not outcomes:
        best = _find_closest_start(text_lines, pattern_lines)
    return _render_fuzzy_outcome(
        best,
        text_index,
        pattern_len,
        pattern,
    )


def find_closest_match(
    text: str,
    pattern: str,
    max_lines: int = 10,
) -> Optional[Tuple[int, str]]:
    """Find a fuzzy fragment while retaining its original formatting."""
    match = find_closest_match_result(
        text,
        pattern,
        workers=1,
        max_lines=max_lines,
    )
    return None if match is None else match.as_tuple()


def find_closest_match_parallel(
    text: str,
    pattern: str,
    workers: Any = 1,
    max_lines: int = 10,
) -> Optional[Tuple[int, str]]:
    """Find a fuzzy match using conditional low-priority processes."""
    match = find_closest_match_result(
        text,
        pattern,
        workers=workers,
        max_lines=max_lines,
    )
    return None if match is None else match.as_tuple()

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
    
    # Report character similarity after fuzzy boundary normalization.
    pattern_lines = pattern.splitlines()
    similarity = _fuzzy_diagnostic_similarity(pattern, fragment)
    
    # Extract context around the match
    start = max(0, line_num - 1 - context_lines)
    end = min(len(text_lines), line_num - 1 + len(pattern_lines) + context_lines)
    context = "\n".join(text_lines[start:end])
    
    return {
        "line": line_num,
        "content": context,
        "similarity": round(similarity, 3),
    }


def explain_match_failure(
    expected: str,
    actual_text: str,
    context_lines: int = 3,
    *,
    closest_match: Any = _FUZZY_MATCH_UNSET,
) -> str:
    """Generate a detailed explanation of why a match failed."""
    lines = []
    lines.append("Match failed. Closest match found:")

    closest_match = _reuse_or_find_closest_match(
        actual_text,
        expected,
        closest_match,
    )
    if closest_match:
        if isinstance(closest_match, FuzzyMatch):
            line_num = closest_match.line
            fragment = closest_match.fragment
        else:
            line_num, fragment = closest_match
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


def _explained_match_error(
    message: str,
    expected: str,
    actual_text: str,
) -> SafeEditError:
    """Build one explained error and retain its closest-match computation."""
    closest_match = find_closest_match_result(actual_text, expected)
    cached = _CachedFuzzyMatch(expected, closest_match)
    explanation = explain_match_failure(
        expected,
        actual_text,
        closest_match=cached,
    )
    exc = SafeEditError(f"{message}\n\n{explanation}")
    setattr(exc, "_diagnostic_closest_match", cached)
    return exc


def find_context_anchor(
    text: str,
    context_pattern: str,
    occurrence: Optional[int] = None,
    records: Optional[List[Tuple[str, str]]] = None,
) -> int:
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
    matches = []
    if records is None:
        line_iter = enumerate(text.splitlines())
    else:
        line_iter = (
            (i, line)
            for i, (line, _separator) in enumerate(records)
        )

    for i, line in line_iter:
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


def _probe_temp_file(directory: str, prefix: str) -> bool:
    """Probe one uniquely named temporary file and clean it up safely."""
    fd: Optional[int] = None
    path: Optional[str] = None
    ok = False
    try:
        fd, path = tempfile.mkstemp(prefix=prefix, dir=directory)
        os.close(fd)
        fd = None
        ok = True
    except MemoryError:
        raise
    except Exception:
        ok = False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except MemoryError:
                raise
            except Exception:
                ok = False
        if path is not None:
            removed = False
            cleanup_failed = False
            for _ in range(2):
                try:
                    os.unlink(path)
                    removed = True
                    break
                except MemoryError:
                    raise
                except Exception:
                    cleanup_failed = True
            if not removed or cleanup_failed:
                ok = False
    return ok


@functools.lru_cache(maxsize=1)
def _get_tmp_dir() -> str:
    """Get the best available temporary directory for sandbox environments."""
    if os.name != "nt" and _probe_temp_file(
        "/tmp", ".safe-edit-probe-"
    ):
        return "/tmp"
    return tempfile.gettempdir()


def _validate_private_lock_directory(path: Path) -> None:
    try:
        info = os.lstat(str(path))
    except OSError as exc:
        fail(f"cannot inspect lock directory {path}: {exc}")
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(
        getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )
    if (
        stat.S_ISLNK(info.st_mode)
        or attributes & reparse_flag
        or not stat.S_ISDIR(info.st_mode)
    ):
        fail(f"unsafe lock directory: {path}")
    if os.name != "nt":
        if int(info.st_uid) != int(os.geteuid()):
            fail(f"lock directory is not owned by current user: {path}")
        if stat.S_IMODE(info.st_mode) != 0o700:
            try:
                os.chmod(str(path), 0o700)
            except OSError as exc:
                fail(f"cannot secure lock directory {path}: {exc}")
            secured = os.lstat(str(path))
            if (
                int(secured.st_uid) != int(os.geteuid())
                or stat.S_IMODE(secured.st_mode) != 0o700
            ):
                fail(f"cannot secure lock directory: {path}")


@functools.lru_cache(maxsize=1)
def _get_lock_dir() -> Path:
    """Return the hardened legacy marker namespace used by protocol 1."""
    root = Path(_get_tmp_dir()) / "safe-edit"
    lock_dir = root / "locks"
    for directory in (root, lock_dir):
        try:
            os.mkdir(str(directory), 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            fail(f"cannot create lock directory {directory}: {exc}")
        _validate_private_lock_directory(directory)
    return lock_dir


@functools.lru_cache(maxsize=1)
def _get_kernel_lock_dir() -> Path:
    """Return a private namespace for permanent protocol-2 stripes."""
    if os.name == "nt":
        root = Path(_get_tmp_dir()) / "safe-edit"
    else:
        root = Path(_get_tmp_dir()) / f"safe-edit-{os.geteuid()}"
    kernel_dir = root / "kernel-v2"
    for directory in (root, kernel_dir):
        try:
            os.mkdir(str(directory), 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            fail(
                f"cannot create kernel lock directory {directory}: {exc}"
            )
        _validate_private_lock_directory(directory)
    return kernel_dir


def _canonical_lock_path(file_path: str) -> Path:
    try:
        return Path(file_path).resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(os.path.abspath(file_path))


def _hash_lock_identity(kind: str, identity: str) -> str:
    value = f"{kind}\0{identity}".encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(value).hexdigest()[:32]


def _get_lock_key(file_path: str) -> str:
    """Return the stable canonical-path lock key for a target."""
    path = _canonical_lock_path(file_path)
    identity = os.path.normcase(os.path.abspath(str(path)))
    return _hash_lock_identity("path", identity)


def _get_inode_lock_key(file_path: str) -> Optional[str]:
    """Return a supplemental inode key used to serialize hardlink aliases."""
    path = _canonical_lock_path(file_path)
    try:
        stat_info = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(stat_info.st_mode) or not stat_info.st_ino:
        return None
    return _hash_lock_identity(
        "inode",
        f"{stat_info.st_dev}:{stat_info.st_ino}",
    )


def _get_lock_keys(file_path: str) -> Tuple[str, ...]:
    keys = {_get_lock_key(file_path)}
    inode_key = _get_inode_lock_key(file_path)
    if inode_key is not None:
        keys.add(inode_key)
    return tuple(sorted(keys))


def check_fs_capability(target_file: str) -> Dict[str, Any]:
    """Detect filesystem capability without modifying the target file; use unique temp probes in parent.

    Designed for sandbox environments where target dir is write-protected.
    Uses unique ephemeral probes in the target parent and temp dir, never modifying the target.
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
    if _probe_temp_file(target_dir, ".safe-edit-probe-"):
        result["directoryWritable"] = True
    else:
        result["suggestions"].append(
            f"Target directory not writable: {target_dir}"
        )

    # One temporary-file probe covers both ordinary staging and lock creation.
    if _probe_temp_file(tmp_dir, ".safe-edit-probe-"):
        result["canWriteTmp"] = True
        result["canCreateLock"] = True
    else:
        result["suggestions"].append(f"Cannot write to {tmp_dir}")
        result["suggestions"].append(f"Cannot create lock in {tmp_dir}")

    # 3. Derive mode
    if result["canWriteTmp"] and result["canCreateLock"]:
        result["executionMode"] = "sandbox-safe" if not result["directoryWritable"] else "full"
    elif result["canWriteTmp"]:
        result["executionMode"] = "no-lock-mode"
    else:
        result["executionMode"] = "readonly-fallback"
        result["suggestions"].append("Filesystem is effectively read-only")

    return result


_FS_CAPABILITY_TTL_SECONDS = 2.0
_FS_CAPABILITY_CACHE_MAX_ENTRIES = 128
_FS_CAPABILITY_CACHE: Dict[
    str,
    Tuple[float, Dict[str, Any]],
] = {}


def _fs_capability_cache_key(target_file: str) -> str:
    return os.path.normcase(
        os.path.abspath(str(Path(target_file).resolve().parent))
    )


def _copy_fs_capability(value: Dict[str, Any]) -> Dict[str, Any]:
    copied = dict(value)
    copied["suggestions"] = list(value.get("suggestions", []))
    return copied


def _invalidate_fs_capability(
    target_file: str,
    args: Optional[argparse.Namespace] = None,
) -> None:
    """Invalidate cached probes after a real filesystem write failure."""
    key = _fs_capability_cache_key(target_file)
    _FS_CAPABILITY_CACHE.pop(key, None)
    if args is not None:
        local = getattr(args, "_fs_capability_cache", None)
        if isinstance(local, dict):
            local.pop(key, None)


def clear_fs_capability_cache() -> None:
    """Clear process-wide filesystem capability observations."""
    _FS_CAPABILITY_CACHE.clear()


def _cached_fs_capability(
    args: argparse.Namespace,
    target_file: str,
) -> Dict[str, Any]:
    local_cache = getattr(args, "_fs_capability_cache", None)
    if local_cache is None:
        local_cache = {}
        args._fs_capability_cache = local_cache
    key = _fs_capability_cache_key(target_file)
    local_value = local_cache.get(key)
    if isinstance(local_value, dict):
        return _copy_fs_capability(local_value)

    now = time.monotonic()
    cached = _FS_CAPABILITY_CACHE.get(key)
    if cached is not None:
        expires_at, value = cached
        if expires_at > now:
            local_cache[key] = value
            return _copy_fs_capability(value)
        _FS_CAPABILITY_CACHE.pop(key, None)

    value = check_fs_capability(target_file)
    stored = _copy_fs_capability(value)
    if len(_FS_CAPABILITY_CACHE) >= _FS_CAPABILITY_CACHE_MAX_ENTRIES:
        oldest_key = min(
            _FS_CAPABILITY_CACHE,
            key=lambda item: _FS_CAPABILITY_CACHE[item][0],
        )
        _FS_CAPABILITY_CACHE.pop(oldest_key, None)
    _FS_CAPABILITY_CACHE[key] = (
        now + _FS_CAPABILITY_TTL_SECONDS,
        stored,
    )
    local_cache[key] = stored
    return _copy_fs_capability(stored)


def looks_like_utf16_without_bom(
    data: bytes,
    validate: bool = True,
) -> Optional[EncodingInfo]:
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
    if not validate:
        return info
    try:
        data.decode(info.codec, errors="strict")
        return info
    except UnicodeDecodeError:
        return None


def detect_and_decode(
    data: bytes,
    requested: str,
) -> Tuple[EncodingInfo, str]:
    requested = normalize_encoding(requested)
    if requested != "auto":
        info = make_encoding_info(requested, data)
        return info, strict_decode(data, info)

    if data.startswith(codecs.BOM_UTF8):
        info = EncodingInfo("utf-8-bom", "utf-8", codecs.BOM_UTF8)
        return info, strict_decode(data, info)
    if data.startswith(codecs.BOM_UTF16_LE):
        info = EncodingInfo("utf-16-le", "utf-16-le", codecs.BOM_UTF16_LE)
        return info, strict_decode(data, info)
    if data.startswith(codecs.BOM_UTF16_BE):
        info = EncodingInfo("utf-16-be", "utf-16-be", codecs.BOM_UTF16_BE)
        return info, strict_decode(data, info)
    if not data:
        return EncodingInfo("utf-8", "utf-8"), ""

    utf16 = looks_like_utf16_without_bom(data, validate=False)
    if utf16 is not None:
        try:
            return utf16, data.decode(utf16.codec, errors="strict")
        except UnicodeDecodeError:
            pass

    try:
        text = data.decode("utf-8", errors="strict")
        return EncodingInfo("utf-8", "utf-8"), text
    except UnicodeDecodeError:
        pass

    try:
        text = data.decode("gbk", errors="strict")
        return EncodingInfo("gbk", "gbk"), text
    except UnicodeDecodeError as exc:
        fail(
            "unable to auto-detect encoding as UTF-8, UTF-8 BOM, "
            "UTF-16 BOM/raw, or GBK; "
            f"use --encoding to override ({exc})"
        )


def detect_encoding(data: bytes, requested: str) -> EncodingInfo:
    encoding, _text = detect_and_decode(data, requested)
    return encoding


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


def _split_homogeneous_records(
    text: str,
    separator: str,
) -> List[Tuple[str, str]]:
    """Split text that is known to use one line-ending sequence."""
    records: List[Any] = text.split(separator)
    last_index = len(records) - 1
    for index in range(last_index):
        records[index] = (records[index], separator)
    if records[last_index]:
        records[last_index] = (records[last_index], "")
    else:
        records.pop()
    return records


def split_records(text: str) -> List[Tuple[str, str]]:
    """Split text into (line_content, line_ending) tuples."""
    if not text:
        return []

    # Homogeneous files are overwhelmingly common. str.split is substantially
    # faster and reuses one separator object instead of materializing a regex
    # capture for every line ending.
    if "\r" not in text:
        return _split_homogeneous_records(text, "\n")
    if "\n" not in text:
        return _split_homogeneous_records(text, "\r")
    crlf_count = text.count("\r\n")
    if crlf_count == text.count("\r") == text.count("\n"):
        return _split_homogeneous_records(text, "\r\n")

    # Mixed line endings require preserving the separator of every record.
    parts = _LINE_ENDING_RE.split(text)
    records: List[Tuple[str, str]] = []
    index = 0
    while index < len(parts):
        content = parts[index]
        if index + 1 < len(parts):
            records.append((content, parts[index + 1]))
            index += 2
        else:
            if content:
                records.append((content, ""))
            index += 1
    return records


def join_records(records: Iterable[Tuple[str, str]]) -> str:
    return "".join(itertools.chain.from_iterable(records))


def normalize_user_newlines(text: str, sep: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", sep)


def convert_line_endings(text: str, style: str) -> str:
    if style == "preserve":
        return text
    return normalize_user_newlines(text, line_sep(style))


def _find_trim_trailing_candidate(text: str) -> Optional[Tuple[int, str]]:
    for pattern in _TRIM_TRAILING_CANDIDATES:
        position = text.find(pattern)
        if position >= 0:
            return position, pattern
    return None


def trim_trailing_whitespace(text: str) -> str:
    trimmed = text.rstrip(" \t")
    if trimmed != text:
        text = trimmed

    candidate = _find_trim_trailing_candidate(text)
    if candidate is None:
        return text

    position, pattern = candidate
    if (
        position >= _TRIM_TRAILING_LONG_RUN_GUARD - 1
        and all(
            character in " \t"
            for character in text[
                position - (_TRIM_TRAILING_LONG_RUN_GUARD - 1) : position + 1
            ]
        )
    ):
        return _TRAILING_WHITESPACE_RE.sub("", text)

    character, line_end = pattern[0], pattern[-1]
    for run_length in _TRIM_TRAILING_SHORT_RUN_LENGTHS:
        text = text.replace(character * run_length + line_end, line_end)

    for _ in range(_TRIM_TRAILING_MIXED_ROUNDS):
        previous = text
        for pair in _TRIM_TRAILING_CANDIDATES:
            text = text.replace(pair, pair[-1])
        if text is previous:
            return text

    return (
        text
        if _find_trim_trailing_candidate(text) is None
        else _TRAILING_WHITESPACE_RE.sub("", text)
    )

def set_final_newline(text: str, mode: str, sep: str) -> str:
    if mode == "preserve":
        return text
    if mode == "ensure":
        return text if text.endswith(("\n", "\r")) else text + sep
    if mode == "strip":
        return text.rstrip("\r\n")
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


def _compile_whitespace_literal(pattern: str) -> re.Pattern[str]:
    """Compile a literal pattern whose whitespace runs may vary."""
    pieces: List[str] = []
    for part in re.split(r"(\s+)", pattern):
        if not part:
            continue
        pieces.append(r"\s+" if part[0].isspace() else re.escape(part))
    return re.compile("".join(pieces))


def _iter_whitespace_spans(
    text: str,
    pattern: str,
) -> Iterable[Tuple[int, int]]:
    matcher = _compile_whitespace_literal(pattern)
    for match in matcher.finditer(text):
        yield match.start(), match.end() - match.start()


def _find_whitespace_spans(text: str, pattern: str) -> List[Tuple[int, int]]:
    return list(_iter_whitespace_spans(text, pattern))


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
        text = _WHITESPACE_RE.sub(" ", text)
    
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
    fuzzy_workers: Any = 1,
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
    old = str(operation["old"])
    # Each relaxed strategy owns at most one normalized file copy.  Failed
    # strategy temporaries are released before the next strategy begins.
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
    
    # All normalization levels failed — try fuzzy if enabled.
    closest_match: Any = _FUZZY_MATCH_UNSET
    if fuzzy:
        closest_match = find_closest_match_result(
            text,
            old,
            workers=fuzzy_workers,
        )
        if closest_match is not None and closest_match.similarity >= 0.6:
            new = normalize_user_newlines(str(operation["new"]), newline)
            try:
                return _apply_edit_with_context(
                    text,
                    old,
                    new,
                    text,
                    old,
                    operation,
                    "fuzzy",
                    explain,
                    True,
                    True,
                    False,
                    context_before or "",
                    context_after or "",
                    positions_override=[
                        (closest_match.start, closest_match.length)
                    ],
                )
            except SafeEditError as exc:
                setattr(
                    exc,
                    "_diagnostic_closest_match",
                    _CachedFuzzyMatch(old, closest_match),
                )
                raise

    # Nothing worked.  Cache any fuzzy/diagnostic search on the exception so
    # JSON error analysis never scans the file a second time.
    if last_error is not None:
        message = (
            "old text was not found "
            "(auto-match exhausted all strategies); "
            "refusing a silent no-op"
        )
        if explain:
            if closest_match is _FUZZY_MATCH_UNSET:
                closest_match = find_closest_match_result(text, old)
            explanation = explain_match_failure(
                old,
                text,
                closest_match=_CachedFuzzyMatch(old, closest_match),
            )
            message += f"\n\n{explanation}"
        exc = SafeEditError(message)
        if closest_match is not _FUZZY_MATCH_UNSET:
            setattr(
                exc,
                "_diagnostic_closest_match",
                _CachedFuzzyMatch(old, closest_match),
            )
        raise exc
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


@dataclass(frozen=True)
class _LinePositionIndex:
    original_starts: Tuple[int, ...]
    normalized_starts: Tuple[int, ...]


def _build_line_position_index(
    original_text: str,
    ignore_indent: bool,
    ignore_eol: bool,
) -> _LinePositionIndex:
    original_starts: List[int] = []
    normalized_starts: List[int] = []
    original_offset = 0
    normalized_offset = 0
    lines = original_text.split("\n")

    for line in lines:
        original_starts.append(original_offset)
        normalized_starts.append(normalized_offset)
        original_offset += len(line) + 1

        normalized_line = line.lstrip(" \t") if ignore_indent else line
        if ignore_eol and normalized_line.endswith("\r"):
            normalized_line = normalized_line[:-1]
        normalized_offset += len(normalized_line) + 1

    return _LinePositionIndex(
        tuple(original_starts),
        tuple(normalized_starts),
    )


class _LazyLinePositionIndex:
    """Build the reusable line index only when position mapping needs it."""

    def __init__(
        self,
        original_text: str,
        ignore_indent: bool,
        ignore_eol: bool,
    ) -> None:
        self.original_text = original_text
        self.ignore_indent = ignore_indent
        self.ignore_eol = ignore_eol
        self._index: Optional[_LinePositionIndex] = None

    def get(self) -> _LinePositionIndex:
        if self._index is None:
            self._index = _build_line_position_index(
                self.original_text,
                self.ignore_indent,
                self.ignore_eol,
            )
        return self._index


class _EolPositionMapper:
    """Map monotonically increasing LF-normalized offsets to original offsets."""

    def __init__(self, original_text: str) -> None:
        self.original_text = original_text
        self.original_pos = 0
        self.normalized_pos = 0

    def _advance_to(self, boundary: int) -> int:
        if boundary < self.normalized_pos:
            self.original_pos = 0
            self.normalized_pos = 0
        remaining = boundary - self.normalized_pos
        if remaining <= 0:
            return self.original_pos

        # A normalized boundary differs from its original boundary only by the
        # number of CRLF pairs before it. Repeated C-level count() calls converge
        # quickly and avoid one Python loop per line ending.
        base_original = self.original_pos
        candidate = base_original + remaining
        text_size = len(self.original_text)
        while True:
            extra = self.original_text.count(
                "\r\n",
                base_original,
                min(text_size, candidate + 1),
            )
            updated = base_original + remaining + extra
            if updated == candidate:
                break
            candidate = updated

        self.original_pos = candidate
        self.normalized_pos = boundary
        return candidate

    def map_span(self, normalized_start: int, normalized_length: int) -> Tuple[int, int]:
        original_start = self._advance_to(normalized_start)
        original_end = self._advance_to(normalized_start + normalized_length)
        return original_start, original_end - original_start


class _WhitespacePositionMapper:
    r"""Map offsets after re.sub(r"\s+", " ", text) back to original text."""

    def __init__(self, original_text: str) -> None:
        self.original_text = original_text
        self.original_pos = 0
        self.normalized_pos = 0

    def _advance_to(self, boundary: int) -> int:
        if boundary < self.normalized_pos:
            self.original_pos = 0
            self.normalized_pos = 0

        text = self.original_text
        text_size = len(text)
        original_pos = self.original_pos
        normalized_pos = self.normalized_pos
        while normalized_pos < boundary and original_pos < text_size:
            if text[original_pos].isspace():
                original_pos += 1
                while original_pos < text_size and text[original_pos].isspace():
                    original_pos += 1
            else:
                original_pos += 1
            normalized_pos += 1

        self.original_pos = original_pos
        self.normalized_pos = normalized_pos
        return original_pos

    def map_span(self, normalized_start: int, normalized_length: int) -> Tuple[int, int]:
        original_start = self._advance_to(normalized_start)
        original_end = self._advance_to(normalized_start + normalized_length)
        return original_start, original_end - original_start


def _context_before_window(text: str, pos: int, line_count: int) -> str:
    search_end = pos
    start = 0
    for _ in range(line_count):
        newline_pos = text.rfind("\n", 0, search_end)
        if newline_pos < 0:
            return text[:pos]
        start = newline_pos + 1
        search_end = newline_pos
    return text[start:pos]


def _context_after_window(text: str, pos: int, line_count: int) -> str:
    start = pos
    search_start = start
    end = len(text)
    for _ in range(line_count):
        newline_pos = text.find("\n", search_start)
        if newline_pos < 0:
            return text[start:]
        end = newline_pos
        search_start = newline_pos + 1
    return text[start:end]


def _replace_spans(
    text: str,
    spans: List[Tuple[int, int]],
    new: str,
    ignore_indent: bool,
) -> str:
    chunks: List[str] = []
    cursor = 0
    for pos, length in spans:
        if pos < cursor:
            fail("internal error: overlapping replacement spans")
        chunks.append(text[cursor:pos])
        original_matched = text[pos:pos + length]
        chunks.append(
            adjust_replacement_for_indent(original_matched, new, ignore_indent)
        )
        cursor = pos + length
    chunks.append(text[cursor:])
    return "".join(chunks)


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
    line_index: Optional[Any] = None,
    position_mapper: Optional[Any] = None,
    positions_override: Optional[Iterable[Tuple[int, int]]] = None,
) -> Tuple[str, int, str]:
    def iter_positions() -> Iterable[Tuple[int, int]]:
        if positions_override is not None:
            yield from positions_override
            return

        if ignore_indent or ignore_eol or normalize_whitespace:
            search_start = 0
            search_start_orig = 0
            while True:
                norm_pos = normalized_text.find(normalized_old, search_start)
                if norm_pos < 0:
                    return
                original_pos, original_len = find_original_position(
                    text,
                    normalized_text,
                    norm_pos,
                    normalized_old,
                    old,
                    ignore_indent,
                    ignore_eol,
                    normalize_whitespace,
                    start_search_pos=search_start_orig,
                    line_index=line_index,
                    position_mapper=position_mapper,
                )
                search_start = norm_pos + len(normalized_old)
                if original_pos >= 0:
                    search_start_orig = original_pos + original_len
                    yield original_pos, original_len
            return

        start = 0
        while True:
            pos = text.find(old, start)
            if pos < 0:
                return
            start = pos + len(old)
            yield pos, len(old)

    old_line_count = max(1, old.count("\n") + 1)
    context_line_count = max(old_line_count, 2)
    expected = operation.get("expected_count")
    first_only = bool(operation.get("first", False))
    first_span: Optional[Tuple[int, int]] = None
    filtered: List[Tuple[int, int]] = []
    filtered_count = 0

    for pos, length in iter_positions():
        if context_before:
            window = _context_before_window(text, pos, context_line_count)
            if context_before not in window:
                continue
        if context_after:
            window = _context_after_window(
                text,
                pos + length,
                context_line_count,
            )
            if context_after not in window:
                continue

        filtered_count += 1
        if first_span is None:
            first_span = (pos, length)
        if first_only and expected is None:
            return (
                _replace_spans(text, [(pos, length)], new, ignore_indent),
                1,
                effective_strategy,
            )
        if not first_only:
            filtered.append((pos, length))

    if filtered_count == 0 and not bool(operation.get("no_op_ok", False)):
        message = (
            "old text was not found (after context filtering); "
            "refusing a silent no-op"
        )
        if explain:
            raise _explained_match_error(message, old, text)
        fail(message)
    if filtered_count == 0:
        return (text, 0, effective_strategy)
    if expected is not None and filtered_count != int(expected):
        fail(
            f"expected {expected} occurrence(s) after context filtering, "
            f"found {filtered_count}"
        )

    if first_only:
        assert first_span is not None
        matches = [first_span]
    else:
        matches = filtered
    return (
        _replace_spans(text, matches, new, ignore_indent),
        len(matches),
        effective_strategy,
    )


def apply_literal_edit(text: str, operation: Dict[str, Any], newline: str, explain: bool = False,
                       ignore_indent: bool = False, ignore_eol: bool = False, normalize_whitespace: bool = False,
                       match_strategy: Optional[str] = None, context_before: Optional[str] = None,
                       context_after: Optional[str] = None,
                       _normalized_pair: Optional[Tuple[str, str]] = None) -> Tuple[str, int, str]:
    """Apply a literal text replacement."""
    old = str(operation["old"])
    new = normalize_user_newlines(str(operation["new"]), newline)
    if old == "":
        fail("old text must not be empty")

    effective_strategy = match_strategy or _determine_match_strategy(
        ignore_indent,
        ignore_eol,
        normalize_whitespace,
    )
    if normalize_whitespace:
        if context_before or context_after:
            return _apply_edit_with_context(
                text,
                old,
                new,
                text,
                old,
                operation,
                effective_strategy,
                explain,
                ignore_indent,
                ignore_eol,
                normalize_whitespace,
                context_before or "",
                context_after or "",
                positions_override=_iter_whitespace_spans(text, old),
            )

        first_only = bool(operation.get("first", False))
        expected = operation.get("expected_count")
        span_iter = iter(_iter_whitespace_spans(text, old))

        # The common --first path consumes only the first match.  This keeps
        # memory bounded even when a small pattern occurs millions of times.
        if first_only and expected is None:
            first_span = next(span_iter, None)
            if first_span is None:
                if bool(operation.get("no_op_ok", False)):
                    return (text, 0, effective_strategy)
                message = (
                    "old text was not found; refusing a silent no-op"
                )
                if explain:
                    raise _explained_match_error(message, old, text)
                fail(message)
            return (
                _replace_spans(text, [first_span], new, ignore_indent),
                1,
                effective_strategy,
            )

        # An expected count must be validated before producing any output.
        # Count spans as a stream; do not retain one tuple per occurrence.
        actual: Optional[int] = None
        if expected is not None:
            actual = sum(1 for _ in span_iter)
            if actual == 0 and not bool(operation.get("no_op_ok", False)):
                message = (
                    "old text was not found; refusing a silent no-op"
                )
                if explain:
                    raise _explained_match_error(message, old, text)
                fail(message)
            if actual == 0:
                return (text, 0, effective_strategy)
            if actual != int(expected):
                fail(f"expected {expected} occurrence(s), found {actual}")

        matcher = _compile_whitespace_literal(old)

        def replace_match(match: re.Match[str]) -> str:
            return adjust_replacement_for_indent(
                match.group(0),
                new,
                ignore_indent,
            )

        replaced_text, replaced = matcher.subn(
            replace_match,
            text,
            count=1 if first_only else 0,
        )
        if replaced == 0 and not bool(operation.get("no_op_ok", False)):
            message = "old text was not found; refusing a silent no-op"
            if explain:
                raise _explained_match_error(message, old, text)
            fail(message)
        return (replaced_text, replaced, effective_strategy)

    if _normalized_pair is None:
        normalized_text = normalize_for_match(
            text, ignore_indent, ignore_eol, normalize_whitespace
        )
        normalized_old = normalize_for_match(
            old, ignore_indent, ignore_eol, normalize_whitespace
        )
    else:
        normalized_text, normalized_old = _normalized_pair

    line_index = None
    if ignore_indent and "\n" in text and not normalize_whitespace:
        line_index = _LazyLinePositionIndex(
            text,
            ignore_indent,
            ignore_eol,
        )
    position_mapper = None
    if ignore_eol and not ignore_indent and not normalize_whitespace:
        position_mapper = _EolPositionMapper(text)
    elif normalize_whitespace and not ignore_indent:
        position_mapper = _WhitespacePositionMapper(text)

    if context_before or context_after:
        return _apply_edit_with_context(
            text,
            old,
            new,
            normalized_text,
            normalized_old,
            operation,
            effective_strategy,
            explain,
            ignore_indent,
            ignore_eol,
            normalize_whitespace,
            context_before or "",
            context_after or "",
            line_index=line_index,
            position_mapper=position_mapper,
        )

    actual = normalized_text.count(normalized_old)
    expected = operation.get("expected_count")
    if actual == 0 and not bool(operation.get("no_op_ok", False)):
        message = "old text was not found; refusing a silent no-op"
        if explain:
            raise _explained_match_error(message, old, text)
        fail(message)
    if actual == 0:
        return (text, 0, effective_strategy)
    if expected is not None and actual != int(expected):
        fail(f"expected {expected} occurrence(s), found {actual}")

    if ignore_indent or ignore_eol or normalize_whitespace:
        if bool(operation.get("first", False)):
            norm_pos = normalized_text.find(normalized_old)
            if norm_pos < 0:
                return (text, 0, effective_strategy)
            original_pos, original_len = find_original_position(
                text,
                normalized_text,
                norm_pos,
                normalized_old,
                old,
                ignore_indent,
                ignore_eol,
                normalize_whitespace,
                line_index=line_index,
                position_mapper=position_mapper,
            )
            if original_pos < 0:
                return (text, 0, effective_strategy)
            spans = [(original_pos, original_len)]
        else:
            spans = []
            search_start = 0
            search_start_orig = 0
            while True:
                norm_pos = normalized_text.find(normalized_old, search_start)
                if norm_pos < 0:
                    break
                original_pos, original_len = find_original_position(
                    text,
                    normalized_text,
                    norm_pos,
                    normalized_old,
                    old,
                    ignore_indent,
                    ignore_eol,
                    normalize_whitespace,
                    start_search_pos=search_start_orig,
                    line_index=line_index,
                    position_mapper=position_mapper,
                )
                if original_pos >= 0:
                    spans.append((original_pos, original_len))
                    search_start_orig = original_pos + original_len
                search_start = norm_pos + len(normalized_old)

        return (
            _replace_spans(text, spans, new, ignore_indent),
            len(spans),
            effective_strategy,
        )

    count = 1 if bool(operation.get("first", False)) else -1
    replaced = min(actual, 1) if count == 1 else actual
    return (text.replace(old, new, count), replaced, effective_strategy)


def find_original_position(original_text: str, normalized_text: str, norm_pos: int,
                            normalized_old: str, original_old: str,
                            ignore_indent: bool, ignore_eol: bool, normalize_whitespace: bool,
                            start_search_pos: int = 0,
                            line_index: Optional[Any] = None,
                            position_mapper: Optional[Any] = None) -> Tuple[int, int]:
    """Map a normalized match position back to an original-text span."""
    if not ignore_indent and not ignore_eol and not normalize_whitespace:
        pos = original_text.find(original_old, start_search_pos)
        return (pos, len(original_old)) if pos >= 0 else (-1, 0)

    if position_mapper is not None:
        return position_mapper.map_span(norm_pos, len(normalized_old))

    candidate = original_text.find(original_old, start_search_pos)
    if candidate >= 0:
        normalized_candidate = normalize_for_match(
            original_text[candidate:candidate + len(original_old)],
            ignore_indent,
            ignore_eol,
            normalize_whitespace,
        )
        if normalized_candidate == normalized_old:
            return (candidate, len(original_old))

    if ignore_indent and not normalize_whitespace:
        result = _find_original_position_line_based(
            original_text,
            normalized_text,
            normalized_old,
            original_old,
            ignore_indent,
            ignore_eol,
            start_search_pos,
            norm_pos=norm_pos,
            line_index=line_index,
        )
        if result[0] >= 0:
            return result

    min_len = max(1, len(normalized_old))
    max_len = max(len(original_old) * 3, len(normalized_old) * 3, 200)
    search_start = start_search_pos
    while search_start < len(original_text):
        for length in range(
            min_len,
            min(max_len + 1, len(original_text) - search_start + 1),
        ):
            candidate_text = original_text[search_start:search_start + length]
            normalized_candidate = normalize_for_match(
                candidate_text,
                ignore_indent,
                ignore_eol,
                normalize_whitespace,
            )
            if normalized_candidate == normalized_old:
                if (
                    ignore_eol
                    and length < len(original_text) - search_start
                    and candidate_text.endswith("\r")
                    and original_text[search_start + length] == "\n"
                ):
                    length += 1
                return (search_start, length)
            if len(normalized_candidate) > len(normalized_old):
                break
        search_start += 1
    return (-1, 0)


def _find_original_position_line_based(
    original_text: str,
    normalized_text: str,
    normalized_old: str,
    original_old: str,
    ignore_indent: bool,
    ignore_eol: bool,
    start_search_pos: int,
    norm_pos: int = 0,
    line_index: Optional[Any] = None,
) -> Tuple[int, int]:
    if isinstance(line_index, _LazyLinePositionIndex):
        line_index = line_index.get()
    elif line_index is None:
        line_index = _build_line_position_index(
            original_text,
            ignore_indent,
            ignore_eol,
        )

    target_line = max(
        0,
        bisect_right(line_index.normalized_starts, norm_pos) - 1,
    )
    search_start = max(
        start_search_pos,
        line_index.original_starts[target_line],
    )
    search_end = min(
        len(original_text),
        line_index.original_starts[target_line] + len(original_old) * 3 + 200,
    )
    min_len = max(1, len(normalized_old))
    max_len = max(len(original_old) * 3, len(normalized_old) * 3, 200)

    pos = search_start
    while pos < search_end:
        for length in range(
            min_len,
            min(max_len + 1, len(original_text) - pos + 1),
        ):
            candidate = original_text[pos:pos + length]
            normalized_candidate = normalize_for_match(
                candidate,
                ignore_indent,
                ignore_eol,
                False,
            )
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

    expected = operation.get("expected_count")
    count = int(operation.get("count", 0) or 0)
    if bool(operation.get("first", False)):
        count = 1

    def substitute(
        limit: int,
        preserve_no_match_priority: bool = False,
    ) -> Tuple[str, int]:
        if bool(operation.get("literal_replacement", False)):
            return compiled.subn(lambda _match: replacement, text, count=limit)
        try:
            return compiled.subn(replacement, text, count=limit)
        except re.error as exc:
            if preserve_no_match_priority and compiled.search(text) is None:
                return (text, 0)
            fail(f"invalid regex replacement: {exc}")

    # Unlimited substitution returns the total match count, so counting first
    # would only duplicate the full scan.
    if count == 0:
        new_text, actual = substitute(0, preserve_no_match_priority=True)
        if actual == 0 and not bool(operation.get("no_op_ok", False)):
            message = (
                "regex pattern was not found; refusing a silent no-op"
            )
            if explain:
                raise _explained_match_error(message, pattern, text)
            fail(message)
        if actual == 0:
            return (text, 0, "regex")
        if expected is not None and actual != int(expected):
            fail(f"expected {expected} regex match(es), found {actual}")
        return (new_text, actual, "regex")

    if expected is None:
        new_text, replaced = substitute(
            count,
            preserve_no_match_priority=True,
        )
        if replaced == 0 and not bool(operation.get("no_op_ok", False)):
            message = (
                "regex pattern was not found; refusing a silent no-op"
            )
            if explain:
                raise _explained_match_error(message, pattern, text)
            fail(message)
        if replaced == 0:
            return (text, 0, "regex")
        return (new_text, replaced, "regex")

    actual = sum(1 for _ in compiled.finditer(text))
    if actual == 0 and not bool(operation.get("no_op_ok", False)):
        message = "regex pattern was not found; refusing a silent no-op"
        if explain:
            raise _explained_match_error(message, pattern, text)
        fail(message)
    if actual == 0:
        return (text, 0, "regex")
    if actual != int(expected):
        fail(f"expected {expected} regex match(es), found {actual}")

    new_text, replaced = substitute(count)
    return (new_text, replaced, "regex")


def range_bounds(operation: Dict[str, Any], records: List[Tuple[str, str]], text: str = "") -> Tuple[int, int]:
    """Calculate start and end bounds for line operations."""
    anchor_pattern = operation.get("anchor_pattern")
    if anchor_pattern:
        occurrence = operation.get("anchor_occurrence")
        anchor_line = find_context_anchor(
            text,
            anchor_pattern,
            occurrence,
            records=records,
        )
        offset_start = operation.get("offset_start", 0)
        offset_end = operation.get("offset_end", 0)

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
        start = int(operation["start"])
        end = int(operation["end"])

    if start < 1:
        fail(f"start must be >= 1, got {start}")
    if end < start:
        fail(f"end must be >= start, got start={start}, end={end}")
    if end > len(records):
        fail(f"end must be <= line count {len(records)}, got {end}")
    return (start - 1, end)


def _extract_indent(line: str) -> str:
    index = 0
    while index < len(line) and line[index] in " \t":
        index += 1
    return line[:index]


_LINE_BUFFER_OPERATIONS = frozenset(
    {"insert", "prepend", "append", "delete", "delete-lines", "replace-lines"}
)


def _split_keepends_records(text: str) -> List[str]:
    """Split common CR/LF text into strings that retain their line endings."""
    if not text:
        return []
    if _NON_CRLF_LINE_SEPARATOR_RE.search(text):
        return [
            content + separator
            for content, separator in split_records(text)
        ]
    return text.splitlines(keepends=True)


def _join_keepends_records(records: Iterable[str]) -> str:
    return "".join(records)


def _record_content(record: Any, keepends: bool) -> str:
    if not keepends:
        return record[0]
    if record.endswith("\r\n"):
        return record[:-2]
    if record.endswith(("\r", "\n")):
        return record[:-1]
    return record


def _record_separator(record: Any, keepends: bool) -> str:
    if not keepends:
        return record[1]
    if record.endswith("\r\n"):
        return "\r\n"
    if record.endswith("\r"):
        return "\r"
    if record.endswith("\n"):
        return "\n"
    return ""


def _make_record(content: str, separator: str, keepends: bool) -> Any:
    return content + separator if keepends else (content, separator)


def _block_records_for_mode(
    text: str,
    separator: str,
    final_separator: str,
    keepends: bool,
) -> List[Any]:
    records = block_records(text, separator, final_separator)
    if keepends:
        return [content + line_end for content, line_end in records]
    return records


def _apply_line_operation_to_records(
    records: List[Any],
    op_name: str,
    operation: Dict[str, Any],
    newline: str,
    *,
    text_for_anchor: str = "",
    keepends: bool = False,
) -> int:
    if op_name == "insert":
        line = int(operation["line"])
        line_count = len(records)
        if line < 1 or line > line_count + 1:
            fail(f"line must be between 1 and {line_count + 1}, got {line}")
        text_value = str(operation["text"])
        final_sep = newline
        if not records:
            final_sep = "" if not text_value.endswith(("\n", "\r")) else newline
        to_insert = _block_records_for_mode(
            text_value,
            newline,
            final_sep,
            keepends,
        )
        index = line - 1
        if (
            records
            and index == len(records)
            and _record_separator(records[-1], keepends) == ""
        ):
            records[-1] = _make_record(
                _record_content(records[-1], keepends),
                newline,
                keepends,
            )
        records[index:index] = to_insert
        return len(to_insert)

    if op_name == "prepend":
        text_value = str(operation["text"])
        final_sep = (
            newline
            if records
            else (newline if text_value.endswith(("\n", "\r")) else "")
        )
        to_insert = _block_records_for_mode(
            text_value,
            newline,
            final_sep,
            keepends,
        )
        records[0:0] = to_insert
        return len(to_insert)

    if op_name == "append":
        text_value = str(operation["text"])
        final_sep = newline if text_value.endswith(("\n", "\r")) else ""
        to_insert = _block_records_for_mode(
            text_value,
            newline,
            final_sep,
            keepends,
        )
        if records and _record_separator(records[-1], keepends) == "":
            records[-1] = _make_record(
                _record_content(records[-1], keepends),
                newline,
                keepends,
            )
        records.extend(to_insert)
        return len(to_insert)

    if op_name == "delete":
        line = int(operation["line"])
        if line < 1 or line > len(records):
            fail(f"line must be between 1 and {len(records)}, got {line}")
        del records[line - 1]
        return 1

    start, end = range_bounds(operation, records, text_for_anchor)
    if op_name == "delete-lines":
        del records[start:end]
        return end - start

    if op_name == "replace-lines":
        preserve_indent = bool(operation.get("preserve_indent", False))
        replacement_text = str(operation["text"])
        if preserve_indent and start < len(records):
            original_indent = _extract_indent(
                _record_content(records[start], keepends)
            )
            if original_indent:
                adjusted = []
                for line in replacement_text.split("\n"):
                    if line and line[0] not in " \t":
                        adjusted.append(original_indent + line)
                    else:
                        adjusted.append(line)
                replacement_text = "\n".join(adjusted)

        following_exists = end < len(records)
        original_final_sep = (
            _record_separator(records[end - 1], keepends)
            if end > start
            else newline
        )
        final_sep = newline if following_exists else original_final_sep
        replacement = _block_records_for_mode(
            replacement_text,
            newline,
            final_sep,
            keepends,
        )
        records[start:end] = replacement
        return end - start

    fail(f"unknown line operation: {op_name}")


def apply_insert(text: str, operation: Dict[str, Any], newline: str) -> Tuple[str, int, str]:
    records = split_records(text)
    changed = _apply_line_operation_to_records(
        records, "insert", operation, newline
    )
    return (join_records(records), changed, "line-based")


def _render_line_block(
    value: str,
    newline: str,
    final_separator: str,
) -> Tuple[str, int]:
    records = block_records(value, newline, final_separator)
    return join_records(records), len(records)


def apply_prepend(text: str, operation: Dict[str, Any], newline: str) -> Tuple[str, int, str]:
    text_value = str(operation["text"])
    final_separator = (
        newline
        if text
        else (newline if text_value.endswith(("\n", "\r")) else "")
    )
    block, changed = _render_line_block(text_value, newline, final_separator)
    return (block + text, changed, "line-based")


def apply_append(text: str, operation: Dict[str, Any], newline: str) -> Tuple[str, int, str]:
    text_value = str(operation["text"])
    final_separator = newline if text_value.endswith(("\n", "\r")) else ""
    block, changed = _render_line_block(text_value, newline, final_separator)
    separator = newline if text and not text.endswith(("\n", "\r")) else ""
    return (text + separator + block, changed, "line-based")


def apply_delete_line(text: str, operation: Dict[str, Any]) -> Tuple[str, int, str]:
    records = split_records(text)
    changed = _apply_line_operation_to_records(
        records, "delete", operation, "\n"
    )
    return (join_records(records), changed, "line-based")


def apply_delete_lines(text: str, operation: Dict[str, Any]) -> Tuple[str, int, str]:
    records = split_records(text)
    changed = _apply_line_operation_to_records(
        records,
        "delete-lines",
        operation,
        "\n",
        text_for_anchor=text,
    )
    return (join_records(records), changed, "line-based")


def apply_replace_lines(text: str, operation: Dict[str, Any], newline: str) -> Tuple[str, int, str]:
    records = split_records(text)
    changed = _apply_line_operation_to_records(
        records,
        "replace-lines",
        operation,
        newline,
        text_for_anchor=text,
    )
    return (join_records(records), changed, "line-based")


class _OperationBuffer:
    def __init__(self, text: str) -> None:
        self._text = text
        self._records: Optional[List[str]] = None

    def as_records(self) -> List[str]:
        if self._records is None:
            self._records = _split_keepends_records(self._text)
        return self._records

    def as_text(self) -> str:
        if self._records is not None:
            self._text = _join_keepends_records(self._records)
            self._records = None
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text
        self._records = None


def apply_operation(text: str, operation: Dict[str, Any], newline: str, explain: bool = False,
                    ignore_indent: bool = False, ignore_eol: bool = False, normalize_whitespace: bool = False,
                    auto_match: bool = False, fuzzy: bool = False,
                    context_before: Optional[str] = None, context_after: Optional[str] = None,
                    fuzzy_workers: Any = 1) -> Tuple[str, int, str, str]:
    """Apply a single operation and return text, count, name, and strategy."""
    op = str(operation.get("op") or operation.get("command") or "").replace("_", "-")
    match_strategy = "exact"
    if op == "edit":
        if auto_match:
            new_text, changed, match_strategy = apply_literal_edit_cascade(
                text,
                operation,
                newline,
                explain=explain,
                fuzzy=fuzzy,
                context_before=context_before,
                context_after=context_after,
                fuzzy_workers=fuzzy_workers,
            )
        else:
            new_text, changed, match_strategy = apply_literal_edit(
                text, operation, newline, explain,
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


def _operation_needs_eol_compat(
    operation: Dict[str, Any],
    newline: str,
) -> bool:
    """Return whether a literal target uses a different newline style."""
    op_name = str(
        operation.get("op") or operation.get("command") or ""
    ).replace("_", "-")
    if op_name != "edit":
        return False
    old = operation.get("old")
    if old is None:
        return False
    styles = set(_LINE_ENDING_RE.findall(str(old)))
    return bool(styles) and styles != {newline}


def _try_apply_exact_literal_batch(
    text: str,
    operations: List[Dict[str, Any]],
    newline: str,
    *,
    ignore_indent: bool = False,
    ignore_eol: bool = False,
    normalize_whitespace: bool = False,
    auto_match: bool = False,
    fuzzy: bool = False,
    context_before: Optional[str] = None,
    context_after: Optional[str] = None,
    auto_eol_match: bool = False,
) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    """Apply a proven-independent batch of exact literal edits in one pass."""
    if type(text) is not str or type(operations) is not list or type(newline) is not str:
        return None

    operation_count = len(operations)
    if not 4 <= operation_count <= 128:
        return None
    if len(text) * (operation_count - 1) < _EXACT_LITERAL_BATCH_MIN_SAVED_SCAN_CHARS:
        return None
    if (
        ignore_indent
        or ignore_eol
        or normalize_whitespace
        or auto_match
        or fuzzy
        or auto_eol_match
        or context_before not in (None, "")
        or context_after not in (None, "")
    ):
        return None

    allowed_keys = frozenset(
        {
            "op",
            "command",
            "old",
            "new",
            "expected_count",
            "first",
            "no_op_ok",
            "context_before",
            "context_after",
        }
    )
    olds: List[str] = []
    news: List[str] = []
    seen_olds = set()
    raw_literal_chars = 0
    normalized_literal_chars = 0

    for operation in operations:
        if type(operation) is not dict or not set(operation).issubset(allowed_keys):
            return None

        raw_op = operation.get("op") or operation.get("command") or ""
        if type(raw_op) is not str or raw_op.replace("_", "-") != "edit":
            return None
        if "old" not in operation or "new" not in operation:
            return None

        old = operation["old"]
        new = operation["new"]
        if type(old) is not str or type(new) is not str:
            return None
        if not old or old == new:
            return None
        if type(operation.get("expected_count")) is not int:
            return None
        if operation["expected_count"] != 1:
            return None
        if operation.get("no_op_ok", False) is not False:
            return None
        if "first" in operation and type(operation["first"]) is not bool:
            return None
        if operation.get("context_before") not in (None, ""):
            return None
        if operation.get("context_after") not in (None, ""):
            return None
        if old in seen_olds:
            return None
        if len(old) > 4 * 1024 or len(new) > 4 * 1024:
            return None

        normalized_new = normalize_user_newlines(new, newline)
        if len(normalized_new) > 4 * 1024:
            return None

        raw_literal_chars += len(old) + len(new)
        normalized_literal_chars += len(old) + len(normalized_new)
        if raw_literal_chars > 64 * 1024 or normalized_literal_chars > 64 * 1024:
            return None

        seen_olds.add(old)
        olds.append(old)
        news.append(normalized_new)

    overlap_work = 0
    for left_index, left in enumerate(olds):
        for right in olds[left_index + 1 :]:
            overlap_limit = min(len(left), len(right))
            overlap_work += 4 * overlap_limit * overlap_limit
            if overlap_work > _EXACT_LITERAL_BATCH_OVERLAP_WORK_BUDGET:
                return None

    for left_index, left in enumerate(olds):
        for right in olds[left_index + 1 :]:
            if left in right or right in left:
                return None
            for overlap_size in range(1, min(len(left), len(right))):
                if left.endswith(right[:overlap_size]):
                    return None
                if right.endswith(left[:overlap_size]):
                    return None

    try:
        literal_pattern = re.compile("|".join(re.escape(old) for old in olds))
    except re.error:
        return None

    old_to_index = {old: index for index, old in enumerate(olds)}
    spans_by_index: List[Optional[Tuple[int, int]]] = [None] * operation_count
    spans: List[Tuple[int, int, int]] = []
    for match in literal_pattern.finditer(text):
        operation_index = old_to_index[match.group(0)]
        if spans_by_index[operation_index] is not None:
            return None
        span = (match.start(), match.end())
        spans_by_index[operation_index] = span
        spans.append((span[0], span[1], operation_index))

    if len(spans) != operation_count or any(span is None for span in spans_by_index):
        return None

    spans.sort(key=lambda item: item[0])
    previous_end = -1
    required_gap = max(len(old) for old in olds) - 1
    for start, end, _operation_index in spans:
        if start < previous_end:
            return None
        if previous_end >= 0 and start - previous_end < required_gap:
            return None
        previous_end = end

    for operation_index, span in enumerate(spans_by_index):
        if span is None:
            return None
        start, end = span
        window_start = max(0, start - required_gap)
        window_end = min(len(text), end + required_gap)
        before = text[window_start:window_end]
        relative_start = start - window_start
        relative_end = end - window_start
        after = (
            before[:relative_start]
            + news[operation_index]
            + before[relative_end:]
        )
        for subsequent_old in olds[operation_index + 1 :]:
            if before.count(subsequent_old) != after.count(subsequent_old):
                return None

    chunks: List[str] = []
    cursor = 0
    for start, end, operation_index in spans:
        chunks.append(text[cursor:start])
        chunks.append(news[operation_index])
        cursor = end
    chunks.append(text[cursor:])

    results = [
        {
            "index": index,
            "op": "edit",
            "changed": 1,
            "matchStrategy": "exact",
        }
        for index in range(1, operation_count + 1)
    ]
    return ("".join(chunks), results)


def apply_operations(
    text: str,
    operations: List[Dict[str, Any]],
    newline: str,
    explain: bool = False,
    ignore_indent: bool = False,
    ignore_eol: bool = False,
    normalize_whitespace: bool = False,
    auto_match: bool = False,
    fuzzy: bool = False,
    context_before: Optional[str] = None,
    context_after: Optional[str] = None,
    fuzzy_workers: Any = 1,
    auto_eol_match: bool = False,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Apply operations with indexed diagnostics and reusable line records."""
    fast_result = _try_apply_exact_literal_batch(
        text,
        operations,
        newline,
        ignore_indent=ignore_indent,
        ignore_eol=ignore_eol,
        normalize_whitespace=normalize_whitespace,
        auto_match=auto_match,
        fuzzy=fuzzy,
        context_before=context_before,
        context_after=context_after,
        auto_eol_match=auto_eol_match,
    )
    if fast_result is not None:
        return fast_result

    buffer = _OperationBuffer(text)
    results: List[Dict[str, Any]] = []

    for index, operation in enumerate(operations, start=1):
        op_name = str(
            operation.get("op") or operation.get("command") or ""
        ).replace("_", "-")

        if (
            op_name == "edit"
            and "old" in operation
            and "new" in operation
            and str(operation["old"]) != ""
            and str(operation["old"]) == str(operation["new"])
        ):
            results.append(
                {
                    "index": index,
                    "op": op_name,
                    "changed": 0,
                    "matchStrategy": "no-op",
                    "skipped": True,
                    "reason": "old_equals_new",
                }
            )
            continue

        operation_ignore_eol = ignore_eol
        auto_eol_applied = False
        if (
            auto_eol_match
            and not auto_match
            and not ignore_eol
            and _operation_needs_eol_compat(operation, newline)
        ):
            operation_ignore_eol = True
            auto_eol_applied = True

        try:
            can_buffer = (
                op_name in _LINE_BUFFER_OPERATIONS
                and not operation.get("anchor_pattern")
                and not (
                    op_name in {"prepend", "append"}
                    and str(operation.get("text", "")) != ""
                )
            )
            if can_buffer:
                changed = _apply_line_operation_to_records(
                    buffer.as_records(),
                    op_name,
                    operation,
                    newline,
                    keepends=True,
                )
                op = op_name
                match_strategy = "line-based"
            else:
                op_ctx_before = operation.get("context_before", context_before)
                op_ctx_after = operation.get("context_after", context_after)
                updated, changed, op, match_strategy = apply_operation(
                    buffer.as_text(),
                    operation,
                    newline,
                    explain,
                    ignore_indent,
                    operation_ignore_eol,
                    normalize_whitespace,
                    auto_match=auto_match,
                    fuzzy=fuzzy,
                    context_before=op_ctx_before,
                    context_after=op_ctx_after,
                    fuzzy_workers=fuzzy_workers,
                )
                buffer.set_text(updated)
        except SafeEditError as exc:
            setattr(exc, "_diagnostic_operation_index", index)
            setattr(exc, "_diagnostic_operation", operation)
            setattr(exc, "_completed_operations", list(results))
            raise

        operation_result = {
            "index": index,
            "op": op,
            "changed": changed,
            "matchStrategy": match_strategy,
        }
        if auto_eol_applied:
            operation_result["autoEolMatch"] = True
        results.append(operation_result)

    return buffer.as_text(), results

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


def _common_prefix_length(left: str, right: str) -> int:
    """Return a common-prefix length with bounded temporary allocations."""
    limit = min(len(left), len(right))
    offset = 0
    chunk = _COMPACT_DIFF_SCAN_CHUNK
    while offset + chunk <= limit:
        end = offset + chunk
        if left[offset:end] != right[offset:end]:
            break
        offset = end
    while offset < limit and left[offset] == right[offset]:
        offset += 1
    return offset


def _common_suffix_length(
    left: str,
    right: str,
    prefix_length: int,
) -> int:
    """Return a non-overlapping common-suffix length."""
    limit = min(len(left), len(right)) - prefix_length
    matched = 0
    chunk = _COMPACT_DIFF_SCAN_CHUNK
    while matched + chunk <= limit:
        left_start = len(left) - matched - chunk
        right_start = len(right) - matched - chunk
        if (
            left[left_start:left_start + chunk]
            != right[right_start:right_start + chunk]
        ):
            break
        matched += chunk
    while (
        matched < limit
        and left[len(left) - matched - 1] == right[len(right) - matched - 1]
    ):
        matched += 1
    return matched


def _line_start_at(text: str, offset: int) -> int:
    """Return the start of the splitlines record containing offset."""
    if offset <= 0:
        return 0
    offset = min(offset, len(text))
    probe = offset
    # CRLF is one separator.  An offset on its LF still belongs to the record
    # ending at the preceding CR, not to a phantom empty record.
    if (
        probe < len(text)
        and text[probe] == "\n"
        and probe > 0
        and text[probe - 1] == "\r"
    ):
        probe -= 1

    separator = max(
        text.rfind(character, 0, probe)
        for character in _SPLITLINE_SEPARATOR_CHARS
    )
    if separator < 0:
        return 0
    if (
        text[separator] == "\r"
        and separator + 1 < len(text)
        and text[separator + 1] == "\n"
    ):
        return separator + 2
    return separator + 1


def _line_end_at(text: str, offset: int) -> int:
    """Return the exclusive end of the splitlines record at offset."""
    if offset >= len(text):
        return len(text)
    probe = max(0, offset)
    if (
        text[probe] == "\n"
        and probe > 0
        and text[probe - 1] == "\r"
    ):
        probe -= 1

    positions = [
        position
        for position in (
            text.find(character, probe)
            for character in _SPLITLINE_SEPARATOR_CHARS
        )
        if position >= 0
    ]
    if not positions:
        return len(text)
    separator = min(positions)
    if (
        text[separator] == "\r"
        and separator + 1 < len(text)
        and text[separator + 1] == "\n"
    ):
        return separator + 2
    return separator + 1


def _compact_window_bounds(
    text: str,
    change_start: int,
    change_end: int,
    context: int,
) -> Tuple[int, int, int, int]:
    text_size = len(text)
    change_start = max(0, min(change_start, text_size))
    change_end = max(change_start, min(change_end, text_size))
    zero_length = change_start == change_end
    at_line_boundary = _line_start_at(text, change_start) == change_start

    if zero_length and at_line_boundary:
        # A pure whole-line insertion/deletion has a zero-count side.
        core_start = change_start
        core_end = change_start
    else:
        core_start = _line_start_at(text, change_start)
        # change_end is exclusive.  Probing it directly would pull the next
        # unchanged line into the core whenever a change ends at a boundary.
        end_probe = change_start if zero_length else change_end - 1
        core_end = _line_end_at(text, end_probe)

    window_start = core_start
    window_end = core_end
    for _ in range(context):
        if window_start > 0:
            window_start = _line_start_at(text, window_start - 1)
        if window_end < text_size:
            window_end = _line_end_at(text, window_end)
    return window_start, core_start, core_end, window_end


def _count_line_breaks(text: str, start: int, end: int) -> int:
    if end <= start:
        return 0
    crlf = text.count("\r\n", start, end)
    count = (
        text.count("\n", start, end)
        + text.count("\r", start, end)
        - crlf
    )
    count += sum(
        1 for _ in _NON_CRLF_LINE_SEPARATOR_RE.finditer(text, start, end)
    )
    return count


def _line_number_at(text: str, offset: int) -> int:
    return _count_line_breaks(text, 0, offset) + 1


def _line_count_between(text: str, start: int, end: int) -> int:
    if end <= start:
        return 0
    count = _count_line_breaks(text, start, end)
    if text[end - 1] not in "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029":
        count += 1
    return count


def _adjust_unified_hunk_header(
    line: str,
    old_base: int,
    new_base: int,
) -> str:
    match = re.match(
        r"^@@ -(\d+)(,\d+)? \+(\d+)(,\d+)? @@(.*)$",
        line,
    )
    if match is None:
        return line
    old_line = int(match.group(1)) + old_base - 1
    new_line = int(match.group(3)) + new_base - 1
    return (
        f"@@ -{old_line}{match.group(2) or ''} "
        f"+{new_line}{match.group(4) or ''} @@{match.group(5)}"
    )


def _limit_compact_diff(lines: Iterable[str]) -> Tuple[str, bool]:
    kept: List[str] = []
    char_count = 0
    truncated = False
    for line in lines:
        separator_size = 1 if kept else 0
        available = _COMPACT_DIFF_MAX_CHARS - char_count - separator_size
        if len(kept) >= _COMPACT_DIFF_MAX_LINES or available <= 0:
            truncated = True
            break
        if len(line) > available:
            kept.append(line[:available])
            truncated = True
            break
        kept.append(line)
        char_count += len(line) + separator_size
    if truncated:
        marker = "... [compact diff truncated]"
        if len(kept) < _COMPACT_DIFF_MAX_LINES:
            kept.append(marker)
    return "\n".join(kept), truncated


def _line_content_end(text: str, start: int, line_end: int) -> int:
    if (
        line_end - start >= 2
        and text[line_end - 2:line_end] == "\r\n"
    ):
        return line_end - 2
    if (
        line_end > start
        and text[line_end - 1] in _SPLITLINE_SEPARATOR_CHARS
    ):
        return line_end - 1
    return line_end


def _take_compact_lines(
    text: str,
    start: int,
    end: int,
    limit: int,
    *,
    focus_start: Optional[int] = None,
    focus_end: Optional[int] = None,
) -> Tuple[List[str], bool]:
    lines: List[str] = []
    cursor = start
    truncated = False
    per_line_limit = max(256, _COMPACT_DIFF_MAX_CHARS // 4)
    while cursor < end and len(lines) < limit:
        line_end = min(end, _line_end_at(text, cursor))
        content_end = _line_content_end(text, cursor, line_end)
        content_length = content_end - cursor

        slice_start = cursor
        slice_end = content_end
        if content_length > per_line_limit:
            truncated = True
            usable = max(64, per_line_limit - 8)
            focused = (
                focus_start is not None
                and cursor <= focus_start <= content_end
            )
            if focused:
                bounded_focus_start = max(cursor, min(focus_start, content_end))
                bounded_focus_end = max(
                    bounded_focus_start,
                    min(
                        content_end,
                        focus_end
                        if focus_end is not None
                        else bounded_focus_start,
                    ),
                )
                focus_width = bounded_focus_end - bounded_focus_start
                if focus_width >= usable:
                    slice_start = bounded_focus_start
                    slice_end = min(content_end, slice_start + usable)
                else:
                    left_budget = (usable - focus_width) // 2
                    slice_start = max(cursor, bounded_focus_start - left_budget)
                    slice_end = min(content_end, slice_start + usable)
                    if slice_end - slice_start < usable:
                        slice_start = max(cursor, slice_end - usable)
            else:
                slice_end = cursor + usable

        rendered = text[slice_start:slice_end]
        if slice_start > cursor:
            rendered = "... " + rendered
        if slice_end < content_end:
            rendered += " ..."
        lines.append(rendered)
        cursor = line_end

    if cursor < end:
        truncated = True
    return lines, truncated


def _manual_compact_hunk(
    path: Path,
    before: str,
    after: str,
    old_bounds: Tuple[int, int, int, int],
    new_bounds: Tuple[int, int, int, int],
    old_change: Tuple[int, int],
    new_change: Tuple[int, int],
) -> Tuple[str, bool]:
    old_window_start, old_core_start, old_core_end, old_window_end = old_bounds
    new_window_start, new_core_start, new_core_end, new_window_end = new_bounds
    old_start_line = _line_number_at(before, old_window_start)
    new_start_line = _line_number_at(after, new_window_start)
    old_count = _line_count_between(before, old_window_start, old_window_end)
    new_count = _line_count_between(after, new_window_start, new_window_end)
    # Unified diff locates a zero-count range after the preceding line.
    old_header_start = old_start_line - 1 if old_count == 0 else old_start_line
    new_header_start = new_start_line - 1 if new_count == 0 else new_start_line

    rendered: List[str] = [
        f"--- {path} (before)",
        f"+++ {path} (after)",
        (
            f"@@ -{old_header_start},{old_count} "
            f"+{new_header_start},{new_count} @@"
        ),
    ]
    prefix, prefix_truncated = _take_compact_lines(
        before,
        old_window_start,
        old_core_start,
        4,
    )
    removed, removed_truncated = _take_compact_lines(
        before,
        old_core_start,
        old_core_end,
        24,
        focus_start=old_change[0],
        focus_end=old_change[1],
    )
    added, added_truncated = _take_compact_lines(
        after,
        new_core_start,
        new_core_end,
        24,
        focus_start=new_change[0],
        focus_end=new_change[1],
    )
    suffix, suffix_truncated = _take_compact_lines(
        after,
        new_core_end,
        new_window_end,
        4,
    )
    rendered.extend(" " + line for line in prefix)

    # Let difflib format only the already-bounded changed sample.  The custom
    # hunk header above retains the real file line numbers and full span sizes.
    sample_diff = difflib.unified_diff(
        removed,
        added,
        n=0,
        lineterm="",
    )
    in_hunk = False
    for line in sample_diff:
        if line.startswith("@@ "):
            in_hunk = True
            continue
        if in_hunk:
            rendered.append(line)

    if removed_truncated:
        rendered.append("-... [removed lines omitted]")
    if added_truncated:
        rendered.append("+... [added lines omitted]")
    rendered.extend(" " + line for line in suffix)

    diff_text, limit_truncated = _limit_compact_diff(rendered)
    return (
        diff_text,
        True
        or prefix_truncated
        or removed_truncated
        or added_truncated
        or suffix_truncated
        or limit_truncated,
    )


def generate_compact_diff(
    path: Path,
    before: str,
    after: str,
    context: int,
) -> Tuple[str, bool]:
    """Generate a bounded diff without running difflib over whole files."""
    if before == after:
        return "", False

    prefix = _common_prefix_length(before, after)
    suffix = _common_suffix_length(before, after, prefix)
    compact_context = max(0, min(context, 2))
    old_bounds = _compact_window_bounds(
        before,
        prefix,
        len(before) - suffix,
        compact_context,
    )
    new_bounds = _compact_window_bounds(
        after,
        prefix,
        len(after) - suffix,
        compact_context,
    )
    old_window_start, _old_core_start, _old_core_end, old_window_end = old_bounds
    new_window_start, _new_core_start, _new_core_end, new_window_end = new_bounds
    input_chars = (
        old_window_end - old_window_start
        + new_window_end - new_window_start
    )
    input_lines = (
        _line_count_between(before, old_window_start, old_window_end)
        + _line_count_between(after, new_window_start, new_window_end)
    )

    if (
        input_chars > _COMPACT_DIFF_MAX_INPUT_CHARS
        or input_lines > _COMPACT_DIFF_MAX_INPUT_LINES
    ):
        return _manual_compact_hunk(
            path,
            before,
            after,
            old_bounds,
            new_bounds,
            (prefix, len(before) - suffix),
            (prefix, len(after) - suffix),
        )

    old_lines = before[old_window_start:old_window_end].splitlines()
    new_lines = after[new_window_start:new_window_end].splitlines()
    old_base = _line_number_at(before, old_window_start)
    new_base = _line_number_at(after, new_window_start)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"{path} (before)",
        tofile=f"{path} (after)",
        n=compact_context,
        lineterm="",
    )
    adjusted = (
        _adjust_unified_hunk_header(line, old_base, new_base)
        if line.startswith("@@ ")
        else line
        for line in diff
    )
    return _limit_compact_diff(adjusted)


def build_diff_preview(
    path: Path,
    before: str,
    after: str,
    args: argparse.Namespace,
) -> Tuple[str, Optional[str], bool]:
    if not args.diff:
        return ("", None, False)
    if bool(getattr(args, "_compact_diff", False)):
        diff_text, truncated = generate_compact_diff(
            path, before, after, args.context
        )
        return (diff_text, "compact", truncated)
    return (generate_diff(path, before, after, args.context), "full", False)


_LOCK_CLEANUP_REMOVED = "removed"
_LOCK_CLEANUP_BUSY = "busy"
_LOCK_CLEANUP_RACED = "raced"
_LOCK_RELEASED = "released"
_LOCK_RELEASE_NOT_OWNER = "not-owner"
_LOCK_RELEASE_RACED = "release-raced"
_LOCK_PAYLOAD_MAX_BYTES = 8192
_LOCK_RETRY_DELAYS = (0.001, 0.002, 0.004, 0.008, 0.016, 0.032, 0.05)
_KERNEL_LOCK_STRIPE_COUNT = 1024
_KERNEL_STRIPE_MUTEXES = tuple(
    threading.Lock() for _index in range(_KERNEL_LOCK_STRIPE_COUNT)
)
_LOCK_OWNER_UNSET = object()


class _LockSnapshot(NamedTuple):
    device: int
    inode: int
    mtime_ns: int
    size: int
    payload: bytes
    pid: Optional[int]
    token: Optional[str]
    protocol: Optional[int]
    stable: bool
    complete: bool

    def owner_identity(self) -> Tuple[Any, ...]:
        return (
            self.device,
            self.inode,
            self.mtime_ns,
            self.size,
            self.token,
            self.pid,
            self.protocol,
        )


class _LockCleanupResult(NamedTuple):
    state: str
    owner: Optional[Tuple[Any, ...]]


def _parse_lock_metadata(
    payload: bytes,
) -> Tuple[Optional[int], Optional[str], Optional[int]]:
    try:
        content = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None, None, None
    pid: Optional[int] = None
    token: Optional[str] = None
    protocol: Optional[int] = None
    for field in content.split():
        if field.startswith("pid="):
            try:
                parsed_pid = int(field.split("=", 1)[1])
            except ValueError:
                parsed_pid = 0
            if 0 < parsed_pid <= 0x7FFFFFFF:
                pid = parsed_pid
        elif field.startswith("token="):
            parsed_token = field.split("=", 1)[1]
            if parsed_token and len(parsed_token) <= 128:
                token = parsed_token
        elif field.startswith("protocol="):
            try:
                parsed_protocol = int(field.split("=", 1)[1])
            except ValueError:
                parsed_protocol = 0
            if parsed_protocol > 0:
                protocol = parsed_protocol
    return pid, token, protocol


def _stat_mtime_ns(stat_info: os.stat_result) -> int:
    return int(
        getattr(
            stat_info,
            "st_mtime_ns",
            int(stat_info.st_mtime * 1_000_000_000),
        )
    )


def _snapshot_stat_identity(stat_info: os.stat_result) -> Tuple[int, ...]:
    return (
        int(stat_info.st_dev),
        int(stat_info.st_ino),
        _stat_mtime_ns(stat_info),
        int(stat_info.st_size),
    )


def _read_lock_snapshot(lock_path: Path) -> Optional[_LockSnapshot]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(lock_path), flags)
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(fd)
        chunks: List[bytes] = []
        remaining = _LOCK_PAYLOAD_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)

    payload = b"".join(chunks)
    pid, token, protocol = _parse_lock_metadata(payload)
    before_identity = _snapshot_stat_identity(before)
    after_identity = _snapshot_stat_identity(after)
    stable = before_identity == after_identity
    complete = (
        after.st_size <= _LOCK_PAYLOAD_MAX_BYTES
        and len(payload) == after.st_size
    )
    return _LockSnapshot(
        device=after_identity[0],
        inode=after_identity[1],
        mtime_ns=after_identity[2],
        size=after_identity[3],
        payload=payload,
        pid=pid,
        token=token,
        protocol=protocol,
        stable=stable,
        complete=complete,
    )


def _lock_snapshots_match(
    first: _LockSnapshot,
    second: _LockSnapshot,
    *,
    require_complete: bool = True,
) -> bool:
    return (
        first.stable
        and second.stable
        and (
            not require_complete
            or (first.complete and second.complete)
        )
        and first.device == second.device
        and first.inode == second.inode
        and first.mtime_ns == second.mtime_ns
        and first.size == second.size
        and first.token == second.token
        and first.payload == second.payload
    )


def _unlink_unchanged_lock(
    lock_path: Path,
    snapshot: _LockSnapshot,
    *,
    require_complete: bool = True,
) -> str:
    try:
        current = _read_lock_snapshot(lock_path)
    except OSError:
        return _LOCK_CLEANUP_BUSY
    if current is None or not _lock_snapshots_match(
        snapshot,
        current,
        require_complete=require_complete,
    ):
        return _LOCK_CLEANUP_RACED
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return _LOCK_CLEANUP_RACED
    except OSError:
        return _LOCK_CLEANUP_BUSY
    return _LOCK_CLEANUP_REMOVED

def _inspect_and_remove_stale_lock(
    lock_path: Path,
    stale_seconds: float,
) -> _LockCleanupResult:
    try:
        snapshot = _read_lock_snapshot(lock_path)
    except OSError:
        return _LockCleanupResult(_LOCK_CLEANUP_BUSY, None)
    if snapshot is None:
        return _LockCleanupResult(_LOCK_CLEANUP_RACED, None)
    owner = snapshot.owner_identity()
    if not snapshot.stable:
        return _LockCleanupResult(_LOCK_CLEANUP_RACED, owner)

    age = time.time() - (snapshot.mtime_ns / 1_000_000_000)
    if not snapshot.complete:
        if stale_seconds <= 0 or age <= stale_seconds:
            return _LockCleanupResult(_LOCK_CLEANUP_BUSY, owner)
        state = _unlink_unchanged_lock(
            lock_path,
            snapshot,
            require_complete=False,
        )
        return _LockCleanupResult(state, owner)

    # A well-formed protocol-2 owner cannot still hold this marker once the
    # caller owns its kernel stripe, so a residual marker is an orphan.
    # Protocol-1 markers keep conservative PID/age compatibility behavior.
    if (
        snapshot.protocol == 2
        and snapshot.pid is not None
        and snapshot.token is not None
    ):
        state = _unlink_unchanged_lock(lock_path, snapshot)
        return _LockCleanupResult(state, owner)

    if snapshot.pid is not None:
        liveness = _process_liveness(snapshot.pid)
        if liveness != _PROCESS_DEAD:
            return _LockCleanupResult(_LOCK_CLEANUP_BUSY, owner)
    elif stale_seconds <= 0 or age <= stale_seconds:
        return _LockCleanupResult(_LOCK_CLEANUP_BUSY, owner)

    state = _unlink_unchanged_lock(lock_path, snapshot)
    return _LockCleanupResult(state, owner)

def _new_lock_token() -> str:
    return os.urandom(16).hex()


def _build_lock_payload(target: Path, token: str, kind: str) -> bytes:
    identity = os.path.normcase(
        os.path.abspath(str(target))
    ).encode("utf-8", errors="surrogatepass")
    target_hash = hashlib.sha256(identity).hexdigest()
    payload = (
        f"protocol=2 pid={os.getpid()} token={token} "
        f"time={time.time()} kind={kind} fileHash={target_hash}\n"
    ).encode("utf-8")
    if len(payload) > _LOCK_PAYLOAD_MAX_BYTES:
        fail("internal error: lock payload exceeds bounded format")
    return payload


def _create_owned_lock_file(lock_path: Path, payload: bytes) -> bool:
    try:
        fd = os.open(
            str(lock_path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        return False
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("failed to write lock payload")
            view = view[written:]
    except BaseException:
        os.close(fd)
        try:
            lock_path.unlink()
        except OSError:
            pass
        raise
    os.close(fd)
    return True


def _kernel_stripe_for_lock(lock_path: Path) -> int:
    identity = os.path.normcase(
        os.path.abspath(str(lock_path))
    ).encode("utf-8", errors="surrogatepass")
    digest = hashlib.blake2s(identity, digest_size=4).digest()
    return int.from_bytes(digest, "big") % _KERNEL_LOCK_STRIPE_COUNT


def _kernel_stripe_path(stripe: int) -> Path:
    return (
        _get_kernel_lock_dir()
        / f"kernel-v2-{stripe:04x}.guard"
    )


def _kernel_guard_identity_matches(fd: int, path: Path) -> bool:
    try:
        opened = os.fstat(fd)
        current = os.stat(str(path), follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(opened.st_mode)
        and stat.S_ISREG(current.st_mode)
        and not (
            int(getattr(current, "st_file_attributes", 0))
            & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        )
        and int(opened.st_dev) == int(current.st_dev)
        and int(opened.st_ino) == int(current.st_ino)
    )


class _LockNativeBindings(NamedTuple):
    owner: Any
    overlapped_type: Any
    lock_file_ex: Any
    unlock_file_ex: Any


@functools.lru_cache(maxsize=1)
def _load_lock_native_bindings() -> _LockNativeBindings:
    import ctypes
    import ctypes.wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", ctypes.wintypes.DWORD),
            ("OffsetHigh", ctypes.wintypes.DWORD),
            ("hEvent", ctypes.wintypes.HANDLE),
        ]

    owner = ctypes.WinDLL("kernel32", use_last_error=True)
    lock_file_ex = owner.LockFileEx
    lock_file_ex.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    lock_file_ex.restype = ctypes.wintypes.BOOL
    unlock_file_ex = owner.UnlockFileEx
    unlock_file_ex.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    unlock_file_ex.restype = ctypes.wintypes.BOOL
    return _LockNativeBindings(
        owner,
        _Overlapped,
        lock_file_ex,
        unlock_file_ex,
    )


def _try_lock_kernel_fd(fd: int) -> Tuple[bool, Any]:
    """Acquire a one-byte/process kernel lock without blocking."""
    if os.name != "nt":
        try:
            import fcntl
        except ImportError:
            fail("kernel file locking is unavailable; refusing unsafe lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True, None
        except BlockingIOError:
            return False, None
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False, None
            fail(f"kernel file locking failed: {exc}")

    import ctypes
    import ctypes.wintypes
    import msvcrt

    bindings = _load_lock_native_bindings()
    overlapped = bindings.overlapped_type()
    handle = msvcrt.get_osfhandle(fd)
    _reset_thread_last_error()
    locked = bindings.lock_file_ex(
        ctypes.wintypes.HANDLE(handle),
        0x00000002 | 0x00000001,
        0,
        1,
        0,
        ctypes.byref(overlapped),
    )
    if locked:
        return True, overlapped
    last_error = _read_thread_last_error()
    if last_error == 33:
        return False, None
    fail(
        "kernel file locking failed: "
        f"{ctypes.WinError(last_error)}"
    )


def _unlock_kernel_fd(fd: int, platform_state: Any) -> None:
    if os.name != "nt":
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        return

    import ctypes
    import ctypes.wintypes
    import msvcrt

    if platform_state is None:
        return
    bindings = _load_lock_native_bindings()
    _reset_thread_last_error()
    bindings.unlock_file_ex(
        ctypes.wintypes.HANDLE(msvcrt.get_osfhandle(fd)),
        0,
        1,
        0,
        ctypes.byref(platform_state),
    )


class _KernelStripeGuard:
    def __init__(
        self,
        stripe: int,
        fd: int,
        mutex: Any,
        platform_state: Any,
    ) -> None:
        self.stripe = stripe
        self.fd = fd
        self.mutex = mutex
        self.platform_state = platform_state

    def close(self) -> None:
        fd = self.fd
        if fd < 0:
            return
        self.fd = -1
        try:
            _unlock_kernel_fd(fd, self.platform_state)
        finally:
            try:
                os.close(fd)
            finally:
                self.mutex.release()


def _try_acquire_kernel_stripe(
    stripe: int,
) -> Optional[_KernelStripeGuard]:
    mutex = _KERNEL_STRIPE_MUTEXES[stripe]
    if not mutex.acquire(blocking=False):
        return None

    fd = -1
    platform_state: Any = None
    locked = False
    path = _kernel_stripe_path(stripe)
    try:
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(path), flags, 0o600)
        locked, platform_state = _try_lock_kernel_fd(fd)
        if not locked:
            os.close(fd)
            fd = -1
            mutex.release()
            return None
        if not _kernel_guard_identity_matches(fd, path):
            fail(
                "kernel lock path identity changed; refusing unsafe lock: "
                f"{path}"
            )
        return _KernelStripeGuard(
            stripe,
            fd,
            mutex,
            platform_state,
        )
    except BaseException:
        if fd >= 0:
            if locked:
                _unlock_kernel_fd(fd, platform_state)
            try:
                os.close(fd)
            except OSError:
                pass
        mutex.release()
        raise


def _lock_retry_delay(attempt: int, token: str) -> float:
    base = _LOCK_RETRY_DELAYS[min(attempt, len(_LOCK_RETRY_DELAYS) - 1)]
    seed = int(token[:8], 16)
    jitter = ((seed + attempt * 17) % 21 - 10) / 100.0
    return base * (1.0 + jitter)


def _sleep_for_lock_retry(
    deadline: float,
    attempt: int,
    token: str,
) -> bool:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    time.sleep(min(_lock_retry_delay(attempt, token), remaining))
    return True


def _release_owned_lock(lock_path: Path, token: str) -> str:
    try:
        snapshot = _read_lock_snapshot(lock_path)
    except OSError:
        return _LOCK_RELEASE_RACED
    if snapshot is None or snapshot.token != token:
        return _LOCK_RELEASE_NOT_OWNER
    if not snapshot.stable or not snapshot.complete:
        return _LOCK_RELEASE_RACED
    state = _unlink_unchanged_lock(lock_path, snapshot)
    if state == _LOCK_CLEANUP_REMOVED:
        return _LOCK_RELEASED
    try:
        current = _read_lock_snapshot(lock_path)
    except OSError:
        return _LOCK_RELEASE_RACED
    if current is None or current.token != token:
        return _LOCK_RELEASE_NOT_OWNER
    return _LOCK_RELEASE_RACED


class FileLockSet:
    """Serialize lock-marker transitions with lifetime kernel stripe locks.

    Protocol-2 participants acquire sorted process-local and OS kernel stripe
    locks before touching legacy marker files and retain the whole stripe set
    until release. Permanent stripe files are never unlinked, so cooperating
    versions have no snapshot-to-pathname ABA window. Protocol-1 processes do
    not know about these stripes; their legacy marker races remain an explicit
    mixed-version compatibility boundary.
    """

    def __init__(
        self,
        paths: Iterable[Path],
        timeout: float,
        stale_seconds: float,
    ) -> None:
        canonical: Dict[str, Path] = {}
        for raw_path in paths:
            path = _canonical_lock_path(str(raw_path))
            identity = os.path.normcase(os.path.abspath(str(path)))
            canonical.setdefault(identity, path)
        if not canonical:
            fail("lock set requires at least one path")
        self.file_paths = tuple(canonical[key] for key in sorted(canonical))
        self.file_path = self.file_paths[0]
        self.timeout = timeout
        self.stale_seconds = stale_seconds
        self.acquired = False
        self.acquired_lock_paths: List[Path] = []
        self._acquired_lock_tokens: Dict[Path, str] = {}
        self._kernel_guards: List[_KernelStripeGuard] = []
        self.suppress_exit_errors = False
        self.exit_errors: List[str] = []
        self.late_failure_handler: Any = None
        self.lock_path = (
            _get_lock_dir()
            / f"{_get_lock_key(str(self.file_path))}.lock"
        )

    def _lock_specs(
        self,
        keyed_targets: Iterable[Tuple[str, Path]],
    ) -> List[Tuple[Path, Path]]:
        targets_by_key: Dict[str, Path] = {}
        for key, target in keyed_targets:
            targets_by_key.setdefault(key, target)
        lock_dir = _get_lock_dir()
        return [
            (lock_dir / f"{key}.lock", targets_by_key[key])
            for key in sorted(targets_by_key)
        ]

    def _desired_lock_specs(self) -> List[Tuple[Path, Path]]:
        keyed_targets: List[Tuple[str, Path]] = []
        for path in self.file_paths:
            keyed_targets.append((_get_lock_key(str(path)), path))
            inode_key = _get_inode_lock_key(str(path))
            if inode_key is not None:
                keyed_targets.append((inode_key, path))
        return self._lock_specs(keyed_targets)

    @staticmethod
    def _stripes_for_specs(
        specs: Iterable[Tuple[Path, Path]],
    ) -> Tuple[int, ...]:
        return tuple(sorted({
            _kernel_stripe_for_lock(lock_path)
            for lock_path, _target in specs
        }))

    @staticmethod
    def _close_kernel_guards_best_effort(
        guards: Iterable[_KernelStripeGuard],
    ) -> List[str]:
        errors: List[str] = []
        for guard in reversed(tuple(guards)):
            try:
                guard.close()
            except Exception as exc:
                errors.append(f"stripe {guard.stripe}: {exc}")
        return errors

    def _try_acquire_kernel_group(
        self,
        stripes: Iterable[int],
    ) -> bool:
        if self._kernel_guards:
            fail("kernel lock group is already held")
        guards: List[_KernelStripeGuard] = []
        try:
            for stripe in sorted(set(stripes)):
                guard = _try_acquire_kernel_stripe(stripe)
                if guard is None:
                    cleanup_errors = (
                        self._close_kernel_guards_best_effort(guards)
                    )
                    guards.clear()
                    if cleanup_errors:
                        fail(
                            "kernel lock acquisition cleanup failed: "
                            + "; ".join(cleanup_errors)
                        )
                    return False
                guards.append(guard)
        except BaseException as exc:
            cleanup_errors = self._close_kernel_guards_best_effort(
                guards
            )
            if cleanup_errors:
                message = (
                    f"kernel lock acquisition failed ({exc}); "
                    "cleanup also failed: "
                    + "; ".join(cleanup_errors)
                )
                if isinstance(exc, SafeEditError):
                    _fail_preserving_diagnostics(message, exc)
                fail(message)
            raise
        self._kernel_guards = guards
        return True

    def _release_kernel_group(
        self,
        suppress_errors: bool = False,
    ) -> None:
        guards = self._kernel_guards
        self._kernel_guards = []
        errors = self._close_kernel_guards_best_effort(guards)
        if errors and not suppress_errors:
            fail("failed to release kernel locks: " + "; ".join(errors))

    def _acquire_one_locked(
        self,
        lock_path: Path,
        target: Path,
    ) -> bool:
        token = _new_lock_token()
        payload = _build_lock_payload(target, token, "owner")
        for _attempt in range(4):
            if _create_owned_lock_file(lock_path, payload):
                self.acquired_lock_paths.append(lock_path)
                self._acquired_lock_tokens[lock_path] = token
                return True
            cleanup = _inspect_and_remove_stale_lock(
                lock_path,
                self.stale_seconds,
            )
            if cleanup.state in (
                _LOCK_CLEANUP_REMOVED,
                _LOCK_CLEANUP_RACED,
            ):
                continue
            return False
        return False

    def _release_one_locked(
        self,
        lock_path: Path,
        token: str,
    ) -> None:
        result = _release_owned_lock(lock_path, token)
        if result in (_LOCK_RELEASED, _LOCK_RELEASE_NOT_OWNER):
            return
        fail(f"failed to release owned lock {lock_path}")

    def _release_all(self, suppress_errors: bool = False) -> None:
        errors: List[str] = []
        pending = tuple(self.acquired_lock_paths)
        required_stripes = {
            _kernel_stripe_for_lock(lock_path)
            for lock_path in pending
        }
        held_stripes = {
            guard.stripe for guard in self._kernel_guards
        }
        try:
            if pending and not required_stripes.issubset(held_stripes):
                errors.append(
                    "refusing marker release without complete kernel "
                    "stripe coverage"
                )
            else:
                for lock_path in reversed(pending):
                    token = self._acquired_lock_tokens.get(lock_path)
                    if token is None:
                        errors.append(f"{lock_path}: missing owner token")
                        continue
                    try:
                        self._release_one_locked(lock_path, token)
                    except Exception as exc:
                        errors.append(f"{lock_path}: {exc}")
        finally:
            # Once the kernel group is gone this instance must never retry a
            # pathname marker deletion. Any failed protocol-2 marker is now an
            # orphan for the next owner of its stripe to reclaim.
            self.acquired_lock_paths = []
            self._acquired_lock_tokens.clear()
            self.acquired = False
            try:
                self._release_kernel_group(
                    suppress_errors=suppress_errors,
                )
            except Exception as exc:
                errors.append(str(exc))
        if errors and not suppress_errors:
            fail("failed to release locks: " + "; ".join(errors))

    def remove_stale_lock(self) -> None:
        deadline = time.monotonic() + max(0.0, self.timeout)
        retry_token = _new_lock_token()
        attempt = 0
        stripe = _kernel_stripe_for_lock(self.lock_path)
        while True:
            if self._try_acquire_kernel_group((stripe,)):
                try:
                    _inspect_and_remove_stale_lock(
                        self.lock_path,
                        self.stale_seconds,
                    )
                    return
                finally:
                    self._release_kernel_group()
            if not _sleep_for_lock_retry(
                deadline,
                attempt,
                retry_token,
            ):
                return
            attempt += 1

    def __enter__(self) -> "FileLockSet":
        deadline = time.monotonic() + max(0.0, self.timeout)
        retry_token = _new_lock_token()
        attempt = 0
        try:
            while True:
                specs = self._desired_lock_specs()
                stripes = self._stripes_for_specs(specs)
                if not self._try_acquire_kernel_group(stripes):
                    if not _sleep_for_lock_retry(
                        deadline,
                        attempt,
                        retry_token,
                    ):
                        fail(f"lock already exists: {self.lock_path}")
                    attempt += 1
                    continue

                confirmed_specs = self._desired_lock_specs()
                confirmed_stripes = self._stripes_for_specs(
                    confirmed_specs
                )
                if confirmed_stripes != stripes:
                    self._release_kernel_group()
                    if time.monotonic() >= deadline:
                        fail(
                            "target identity changed while acquiring locks: "
                            f"{self.file_path}"
                        )
                    attempt = 0
                    continue

                busy = False
                for lock_path, target in confirmed_specs:
                    if not self._acquire_one_locked(lock_path, target):
                        busy = True
                        break
                if busy:
                    # A live protocol-1 owner cannot make progress through our
                    # stripe, so release the entire ordered group before
                    # backing off. Protocol-2 residual markers were reclaimed
                    # as orphans while the stripe was held.
                    self._release_all()
                    if not _sleep_for_lock_retry(
                        deadline,
                        attempt,
                        retry_token,
                    ):
                        fail(f"lock already exists: {self.lock_path}")
                    attempt += 1
                    continue

                self.acquired = True
                return self
        except BaseException:
            self._release_all(suppress_errors=True)
            raise

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        try:
            self._release_all()
        except BaseException as release_exc:
            message = _best_effort_exception_text(release_exc)
            try:
                self.exit_errors.append(message)
                _attach_transaction_cleanup_errors(_exc, (message,))
            except BaseException:
                pass
            if _exc is None:
                handler = self.late_failure_handler
                if handler is not None:
                    try:
                        handler(release_exc)
                    except BaseException:
                        pass
                if (
                    _preserve_transaction_exception(release_exc)
                    or not self.suppress_exit_errors
                ):
                    raise


class FileLock(FileLockSet):
    def __init__(self, path: Path, timeout: float, stale_seconds: float) -> None:
        super().__init__([path], timeout, stale_seconds)

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
        exc = SafeEditError(f"file not found: {path}")
        setattr(exc, "_file_not_found", True)
        raise exc
    if path.is_symlink() and not follow_symlink:
        fail("refusing to edit a symlink without --follow-symlink")
    if follow_symlink:
        path = path.resolve()
    if not path.is_file():
        fail(f"not a regular file: {path}")
    return path


def read_target(path: Path, max_bytes: int) -> bytes:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        fail(f"cannot open target for stable read {path}: {exc}")
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            fail(f"not a regular file: {path}")
        if before.st_size > max_bytes:
            fail(
                f"file is {before.st_size} bytes, "
                f"exceeding --max-bytes {max_bytes}"
            )
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            fail(
                f"file exceeds --max-bytes {max_bytes} "
                "during stable read"
            )
        after = os.fstat(fd)
    finally:
        os.close(fd)

    try:
        current = os.stat(str(path), follow_symlinks=False)
    except OSError:
        fail(f"target changed during stable read: {path}")

    def marker(info: os.stat_result) -> Tuple[int, int, int, int, int]:
        return (
            int(info.st_dev),
            int(info.st_ino),
            int(stat.S_IFMT(info.st_mode)),
            int(info.st_size),
            _stat_mtime_ns(info),
        )

    if marker(before) != marker(after) or marker(after) != marker(current):
        fail(f"target changed during stable read: {path}")
    return data


_TRANSACTION_VERIFY_CHUNK_BYTES = 1024 * 1024


def _compare_file_bytes_strict(path: Path, expected: bytes) -> bool:
    """Compare a complete file using one bounded, exception-transparent read."""
    offset = 0
    with path.open("rb") as handle:
        while offset < len(expected):
            chunk = handle.read(
                min(_TRANSACTION_VERIFY_CHUNK_BYTES, len(expected) - offset)
            )
            if not chunk:
                return False
            end = offset + len(chunk)
            if not expected.startswith(chunk, offset):
                return False
            offset = end
        return handle.read(1) == b""


def _file_bytes_equal(path: Path, expected: bytes) -> bool:
    """Compare a file completely while allocating at most one chunk."""
    try:
        return _compare_file_bytes_strict(path, expected)
    except OSError:
        return False
def inspect_target(path: Path, original: bytes, encoding: EncodingInfo, text: str) -> Dict[str, Any]:
    newline_style, line_counts, mixed_line_endings = detect_line_ending(text)
    line_count = sum(line_counts.values())
    if text and not text.endswith(("\n", "\r")):
        line_count += 1
    edit_plan = _compute_edit_plan(
        encoding,
        text,
        path,
        newline_style=newline_style,
        mixed=mixed_line_endings,
        line_count=line_count,
    )
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
        "lineCount": line_count,
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


def stat_target(
    path: Path,
    original: bytes,
    encoding: EncodingInfo,
    text: str,
    capability: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a concise summary of file metadata with edit strategy for AI agents."""
    newline_style, line_counts, mixed_line_endings = detect_line_ending(text)
    line_count = sum(line_counts.values())
    if text and not text.endswith(("\n", "\r")):
        line_count += 1
    edit_plan = _compute_edit_plan(
        encoding,
        text,
        path,
        newline_style=newline_style,
        mixed=mixed_line_endings,
        line_count=line_count,
    )
    cap = capability if capability is not None else check_fs_capability(str(path))
    return {
        "ok": True,
        "file": str(path),
        "command": "stat",
        "encoding": encoding.name,
        "hasBom": bool(encoding.bom),
        "lineEnding": newline_style,
        "mixedLineEndings": mixed_line_endings,
        "sizeBytes": len(original),
        "lineCount": line_count,
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
        _fail_file_already_exists(path)
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
            _fail_file_already_exists(path)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(path.parent)
    except BaseException as exc:
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
        if isinstance(exc, OSError):
            _invalidate_fs_capability(str(path))
        raise


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
        # Prefer staging beside the target. This keeps the final replace atomic
        # and avoids writing the complete output twice when system temp is on a
        # different filesystem. Sandboxes that forbid target-dir staging still
        # fall back to the writable system temporary directory.
        try:
            fd, tmp_name = tempfile.mkstemp(
                prefix=prefix,
                suffix=".tmp",
                dir=str(directory),
            )
        except OSError:
            tmp_dir = _get_tmp_dir()
            if os.path.normcase(os.path.abspath(tmp_dir)) == os.path.normcase(
                os.path.abspath(str(directory))
            ):
                raise
            fd, tmp_name = tempfile.mkstemp(
                prefix=prefix,
                suffix=".tmp",
                dir=tmp_dir,
            )

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
    except OSError:
        _invalidate_fs_capability(str(path))
        raise
    finally:
        if fd != -1:
            os.close(fd)
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def load_batch_operations(args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Optional[Path]]:
    internal_operations = getattr(args, "_batch_operations", None)
    if internal_operations is not None:
        operations: List[Dict[str, Any]] = []
        for index, item in enumerate(internal_operations, start=1):
            if not isinstance(item, dict):
                fail(f"batch operation {index} must be an object")
            operations.append(dict(item))
        return operations, None
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


def normalize_request_payload(payload: Any, request_name: str) -> Dict[str, Any]:
    """Normalize an already-decoded structured request without serializing it."""
    if isinstance(payload, list):
        payload = {"files": payload}
    if not isinstance(payload, dict):
        fail(f"{request_name} JSON must be an object or a list of file requests")
    if "files" not in payload and "file" in payload:
        payload = {"files": [payload]}
    return payload


def load_request_payload(args: argparse.Namespace) -> Dict[str, Any]:
    request_name = args.command
    sources = [
        args.request_file is not None,
        args.request_base64 is not None,
        args.request_stdin,
    ].count(True)
    if sources != 1:
        fail(
            f"{request_name} requires exactly one of --request-file, "
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
        fail(f"invalid {request_name} JSON: {exc}")
    return normalize_request_payload(payload, request_name)


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
    explicit_diff = item.get("diff")
    child.diff = (
        bool(parent.dry_run)
        if explicit_diff is None
        else bool(explicit_diff)
    )
    child._compact_diff = explicit_diff is None and bool(parent.dry_run)
    parent_auto_eol = getattr(parent, "auto_eol_match", None)
    child.auto_eol_match = (
        True if parent_auto_eol is None else bool(parent_auto_eol)
    )

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
        child._batch_operations = operations
        child.ops = None
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


def run_stat_many(args: argparse.Namespace) -> Dict[str, Any]:
    return run_stat_many_payload(args, load_request_payload(args))


def run_stat_many_payload(
    args: argparse.Namespace, payload: Any
) -> Dict[str, Any]:
    payload = normalize_request_payload(payload, "stat-many")
    items = payload.get("files")
    if not isinstance(items, list) or not items:
        fail("stat-many request requires a non-empty files list")

    capability_cache: Dict[str, Dict[str, Any]] = {}
    results: List[Dict[str, Any]] = []
    identities: Set[str] = set()
    default_encoding = str(payload.get("encoding", args.encoding))

    for index, item in enumerate(items, start=1):
        if isinstance(item, str):
            file_value = item
            options: Dict[str, Any] = {}
        elif isinstance(item, dict):
            file_value = item.get("file")
            options = item
        else:
            fail(f"stat-many file request {index} must be a string or object")
        if not isinstance(file_value, str) or not file_value:
            fail(f"stat-many file request {index} requires a non-empty file")

        identity = os.path.normcase(os.path.abspath(file_value))
        if identity in identities:
            fail(f"stat-many contains duplicate file: {file_value}")
        identities.add(identity)

        child = argparse.Namespace(**vars(args))
        child.command = "stat"
        child.file = file_value
        child.encoding = str(
            options.get("inputEncoding", options.get("encoding", default_encoding))
        )
        child.expected_sha256 = options.get("expectedSha256")
        child.follow_symlink = bool(options.get("followSymlink", False))
        child.max_bytes = int(options.get("maxBytes", args.max_bytes))
        child._fs_capability_cache = capability_cache
        results.append(run(child))

    return {
        "ok": True,
        "command": "stat-many",
        "file": None,
        "files": results,
        "fileCount": len(results),
        "dryRun": True,
        "written": False,
        "skipped": True,
    }


def run_transaction(args: argparse.Namespace) -> Dict[str, Any]:
    return run_transaction_payload(args, load_request_payload(args))


TRANSACTION_MAX_FILES = 128
TRANSACTION_MAX_INPUT_BYTES = 128 * 1024 * 1024
TRANSACTION_MAX_OUTPUT_BYTES = 128 * 1024 * 1024
TRANSACTION_MAX_RETAINED_BYTES = 160 * 1024 * 1024


class _PreparedFrozenDict(NamedTuple):
    items: Tuple[Tuple[str, Any], ...]


class _PreparedFrozenList(NamedTuple):
    items: Tuple[Any, ...]


def _freeze_prepared_value(value: Any) -> Any:
    if isinstance(value, dict):
        items: List[Tuple[str, Any]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                fail("prepared transaction summary keys must be strings")
            items.append((key, _freeze_prepared_value(item)))
        return _PreparedFrozenDict(tuple(items))
    if isinstance(value, (list, tuple)):
        return _PreparedFrozenList(
            tuple(_freeze_prepared_value(item) for item in value)
        )
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    fail(
        "prepared transaction summary contains unsupported value: "
        f"{type(value).__name__}"
    )


def _thaw_prepared_value(value: Any) -> Any:
    if isinstance(value, _PreparedFrozenDict):
        return {
            key: _thaw_prepared_value(item)
            for key, item in value.items
        }
    if isinstance(value, _PreparedFrozenList):
        return [_thaw_prepared_value(item) for item in value.items]
    return value


class PreparedPathIdentity(NamedTuple):
    """Serializable, immutable identity for a filesystem object."""

    device: int
    inode: int
    file_type: int


class PreparedFilePlan(NamedTuple):
    action: str
    requested_path: str
    path: str
    canonical_parent: str
    basename: str
    prepared_parent_identity: PreparedPathIdentity
    prepared_target_identity: Optional[PreparedPathIdentity]
    follow_symlink: bool
    original_sha256: Optional[str]
    output: bytes
    output_sha256: str
    summary: _PreparedFrozenDict
    force_write: bool
    backup_suffix: str
    input_bytes: int
    max_bytes: int


class PreparedTransaction(NamedTuple):
    plans: Tuple[PreparedFilePlan, ...]
    lock_paths: Tuple[str, ...]
    no_lock: bool
    lock_timeout: float
    lock_stale_seconds: float
    file_count: int
    input_bytes: int
    output_bytes: int
    retained_bytes: int


def _transaction_total_limit(
    name: str,
    value: int,
    limit: int,
) -> None:
    if value > limit:
        fail(
            f"transaction {name} total {value} bytes exceeds "
            f"limit {limit} bytes"
        )


def _retained_object_bytes(
    value: Any,
    seen: Optional[Set[int]] = None,
) -> int:
    """Measure the immutable prepared object graph once for O(1) admission."""
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return 0
    seen.add(identity)
    size = sys.getsizeof(value)
    if isinstance(value, tuple):
        size += sum(_retained_object_bytes(item, seen) for item in value)
    return size


def _prepared_file_summary(plan: PreparedFilePlan) -> Dict[str, Any]:
    summary = _thaw_prepared_value(plan.summary)
    if not isinstance(summary, dict):
        fail("prepared transaction summary is not an object")
    return summary


def _prepared_preview_summary(
    prepared: PreparedTransaction,
) -> Dict[str, Any]:
    previews = [
        _prepared_file_summary(plan)
        for plan in prepared.plans
    ]
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


def _transaction_path_identity(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _prepared_path_identity(
    path: Path,
    *,
    follow_symlinks: bool = False,
) -> PreparedPathIdentity:
    try:
        info = os.stat(
            str(path),
            follow_symlinks=follow_symlinks,
        )
    except OSError as exc:
        fail(f"cannot inspect prepared path identity {path}: {exc}")
    return PreparedPathIdentity(
        device=int(info.st_dev),
        inode=int(info.st_ino),
        file_type=int(stat.S_IFMT(info.st_mode)),
    )


def prepare_transaction_payload(
    args: argparse.Namespace,
    payload: Any,
) -> PreparedTransaction:
    """Prepare outputs once without retaining mutable request objects."""
    payload = normalize_request_payload(payload, "transaction")
    items = payload.get("files")
    if not isinstance(items, list) or not items:
        fail("transaction request requires a non-empty files list")
    if len(items) > TRANSACTION_MAX_FILES:
        fail(
            f"transaction file count {len(items)} exceeds "
            f"limit {TRANSACTION_MAX_FILES}"
        )

    if getattr(args, "_fs_capability_cache", None) is None:
        args._fs_capability_cache = {}

    children: List[
        Tuple[
            argparse.Namespace,
            str,
            Path,
            str,
            PreparedPathIdentity,
            Optional[PreparedPathIdentity],
        ]
    ] = []
    identities: Set[str] = set()
    lock_paths: List[str] = []
    for file_index, item in enumerate(items, start=1):
        child = request_item_args(args, item, True)
        requested_path = os.path.abspath(child.file)
        try:
            if child.command == "create":
                target = resolve_create_path(
                    requested_path
                ).resolve(strict=False)
            else:
                target = resolve_target_path(
                    requested_path,
                    child.follow_symlink,
                ).resolve(strict=False)
            identity = _transaction_path_identity(target)
            if identity in identities:
                fail(
                    "transaction contains duplicate canonical target: "
                    f"{child.file}"
                )
            identities.add(identity)
            canonical_parent = target.parent.resolve(strict=True)
            basename = target.name
            if _transaction_path_identity(
                canonical_parent / basename
            ) != identity:
                fail(
                    "transaction target is not bound to its canonical "
                    f"parent: {target}"
                )
            prepared_parent_identity = _prepared_path_identity(
                canonical_parent
            )
            if prepared_parent_identity.file_type != stat.S_IFDIR:
                fail(
                    f"prepared parent is not a directory: "
                    f"{canonical_parent}"
                )
            prepared_target_identity: Optional[
                PreparedPathIdentity
            ] = None
            if child.command == "create":
                if os.path.lexists(str(target)):
                    _fail_file_already_exists(target)
            else:
                prepared_target_identity = _prepared_path_identity(
                    target
                )
                if prepared_target_identity.file_type != stat.S_IFREG:
                    fail(
                        f"prepared target is not a regular file: {target}"
                    )
            child.file = str(target)
            lock_paths.append(str(target))
            children.append(
                (
                    child,
                    requested_path,
                    canonical_parent,
                    basename,
                    prepared_parent_identity,
                    prepared_target_identity,
                )
            )
        except SafeEditError as exc:
            setattr(exc, "_diagnostic_file_index", file_index)
            setattr(exc, "_diagnostic_file", child.file)
            setattr(exc, "_diagnostic_command", "transaction")
            raise

    plans: List[PreparedFilePlan] = []
    input_bytes = 0
    output_bytes = 0
    for file_index, (
        child,
        requested_path,
        canonical_parent,
        basename,
        prepared_parent_identity,
        prepared_target_identity,
    ) in enumerate(
        children,
        start=1,
    ):
        try:
            preview = run(child)
        except SafeEditError as exc:
            setattr(exc, "_diagnostic_file_index", file_index)
            setattr(exc, "_diagnostic_file", child.file)
            setattr(exc, "_diagnostic_command", "transaction")
            raise

        raw_plan = getattr(child, "_transaction_plan", None)
        if not isinstance(raw_plan, dict):
            fail(f"transaction failed to prepare file: {child.file}")
        prepared_path = Path(str(raw_plan["path"]))
        if _transaction_path_identity(
            prepared_path
        ) != _transaction_path_identity(
            canonical_parent / basename
        ):
            fail(
                "transaction target canonical path changed during "
                f"prevalidation: {prepared_path}"
            )
        current_parent_identity = _prepared_path_identity(
            canonical_parent
        )
        if current_parent_identity != prepared_parent_identity:
            fail(
                "parent directory changed during transaction "
                f"prevalidation: {canonical_parent}"
            )
        if child.command == "create":
            if os.path.lexists(str(prepared_path)):
                _fail_file_already_exists(prepared_path)
        else:
            current_target_identity = _prepared_path_identity(
                prepared_path
            )
            if current_target_identity != prepared_target_identity:
                fail(
                    "target identity changed during transaction "
                    f"prevalidation: {prepared_path}"
                )

        output = raw_plan.get("output")
        output_sha256 = raw_plan.get("outputSha256")
        raw_input_bytes = raw_plan.get("inputSizeBytes")
        if not isinstance(output, bytes):
            fail(f"transaction prepared invalid output: {child.file}")
        if (
            not isinstance(output_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", output_sha256)
        ):
            fail(f"transaction prepared invalid output hash: {child.file}")
        if not isinstance(raw_input_bytes, int) or raw_input_bytes < 0:
            fail(f"transaction prepared invalid input size: {child.file}")

        action = str(raw_plan.get("action", ""))
        original_sha256 = raw_plan.get("originalSha256")
        if action == "edit":
            if (
                not isinstance(original_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", original_sha256)
            ):
                fail(
                    f"transaction prepared invalid original hash: "
                    f"{child.file}"
                )
        elif action == "create":
            original_sha256 = None
        else:
            fail(f"transaction prepared invalid action: {child.file}")

        summary = raw_plan.get("summary", preview)
        if not isinstance(summary, dict):
            fail(f"transaction prepared invalid summary: {child.file}")
        frozen_summary = _freeze_prepared_value(summary)
        if not isinstance(frozen_summary, _PreparedFrozenDict):
            fail(f"transaction prepared invalid summary: {child.file}")

        input_bytes += raw_input_bytes
        output_bytes += len(output)
        _transaction_total_limit(
            "input",
            input_bytes,
            TRANSACTION_MAX_INPUT_BYTES,
        )
        _transaction_total_limit(
            "output",
            output_bytes,
            TRANSACTION_MAX_OUTPUT_BYTES,
        )
        plans.append(
            PreparedFilePlan(
                action=action,
                requested_path=requested_path,
                path=str(prepared_path),
                canonical_parent=str(canonical_parent),
                basename=basename,
                prepared_parent_identity=prepared_parent_identity,
                prepared_target_identity=prepared_target_identity,
                follow_symlink=bool(child.follow_symlink),
                original_sha256=original_sha256,
                output=output,
                output_sha256=output_sha256,
                summary=frozen_summary,
                force_write=bool(child.force_write),
                backup_suffix=str(child.backup_suffix),
                input_bytes=raw_input_bytes,
                max_bytes=int(child.max_bytes),
            )
        )

    draft = PreparedTransaction(
        plans=tuple(plans),
        lock_paths=tuple(lock_paths),
        no_lock=bool(args.no_lock),
        lock_timeout=float(args.lock_timeout),
        lock_stale_seconds=float(args.lock_stale_seconds),
        file_count=len(plans),
        input_bytes=input_bytes,
        output_bytes=output_bytes,
        retained_bytes=0,
    )
    retained_bytes = _retained_object_bytes(draft)
    if retained_bytes < output_bytes:
        retained_bytes = output_bytes
    _transaction_total_limit(
        "retained",
        retained_bytes,
        TRANSACTION_MAX_RETAINED_BYTES,
    )
    return draft._replace(retained_bytes=retained_bytes)


def prepare_transaction(
    args: argparse.Namespace,
    payload: Any,
) -> PreparedTransaction:
    """Stable public preparation entry point used by long-lived transports."""
    return prepare_transaction_payload(args, payload)


def _resolve_prepared_target(plan: PreparedFilePlan) -> Path:
    if plan.action == "create":
        current = resolve_create_path(
            plan.requested_path
        ).resolve(strict=False)
    else:
        current = resolve_target_path(
            plan.requested_path,
            plan.follow_symlink,
        ).resolve(strict=False)
    if _transaction_path_identity(current) != _transaction_path_identity(
        Path(plan.path)
    ):
        fail(
            "transaction target canonical path changed after "
            f"prevalidation: {plan.requested_path}"
        )
    return current


def _identity_from_runtime_stat(
    info: os.stat_result,
) -> PreparedPathIdentity:
    return PreparedPathIdentity(
        device=int(info.st_dev),
        inode=int(info.st_ino),
        file_type=int(stat.S_IFMT(info.st_mode)),
    )


def _directory_stat_is_reparse(info: os.stat_result) -> bool:
    return bool(
        stat.S_ISLNK(info.st_mode)
        or (
            int(getattr(info, "st_file_attributes", 0))
            & int(
                getattr(
                    stat,
                    "FILE_ATTRIBUTE_REPARSE_POINT",
                    0x400,
                )
            )
        )
    )


def _windows_directory_chain(path: Path) -> Tuple[Path, ...]:
    absolute = Path(os.path.abspath(str(path)))
    parts = absolute.parts
    if not parts:
        fail(f"cannot determine directory ancestors: {path}")
    current = Path(parts[0])
    chain = [current]
    for part in parts[1:]:
        current = current / part
        chain.append(current)
    return tuple(chain)


class _DirectoryNativeBindings(NamedTuple):
    owner: Any
    create_file_w: Any
    get_file_information_by_handle: Any
    get_file_information_by_handle_ex: Optional[Any]
    close_handle: Any
    file_id_128_type: Any
    file_id_information_type: Any
    by_handle_file_information_type: Any


@functools.lru_cache(maxsize=1)
def _load_directory_native_bindings() -> _DirectoryNativeBindings:
    import ctypes
    import ctypes.wintypes

    class FileId128(ctypes.Structure):
        _fields_ = [
            ("identifier", ctypes.c_ubyte * 16),
        ]

    class FileIdInformation(ctypes.Structure):
        _fields_ = [
            ("volume_serial_number", ctypes.c_ulonglong),
            ("file_id", FileId128),
        ]

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", ctypes.wintypes.DWORD),
            ("creation_time", ctypes.wintypes.FILETIME),
            ("last_access_time", ctypes.wintypes.FILETIME),
            ("last_write_time", ctypes.wintypes.FILETIME),
            ("volume_serial_number", ctypes.wintypes.DWORD),
            ("file_size_high", ctypes.wintypes.DWORD),
            ("file_size_low", ctypes.wintypes.DWORD),
            ("number_of_links", ctypes.wintypes.DWORD),
            ("file_index_high", ctypes.wintypes.DWORD),
            ("file_index_low", ctypes.wintypes.DWORD),
        ]

    owner = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file_w = owner.CreateFileW
    create_file_w.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.HANDLE,
    ]
    create_file_w.restype = ctypes.wintypes.HANDLE
    get_file_information_by_handle = owner.GetFileInformationByHandle
    get_file_information_by_handle.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    ]
    get_file_information_by_handle.restype = ctypes.wintypes.BOOL
    get_file_information_by_handle_ex = getattr(
        owner,
        "GetFileInformationByHandleEx",
        None,
    )
    if get_file_information_by_handle_ex is not None:
        get_file_information_by_handle_ex.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
        ]
        get_file_information_by_handle_ex.restype = ctypes.wintypes.BOOL
    close_handle = owner.CloseHandle
    close_handle.argtypes = [ctypes.wintypes.HANDLE]
    close_handle.restype = ctypes.wintypes.BOOL
    return _DirectoryNativeBindings(
        owner,
        create_file_w,
        get_file_information_by_handle,
        get_file_information_by_handle_ex,
        close_handle,
        FileId128,
        FileIdInformation,
        ByHandleFileInformation,
    )


def _open_windows_directory_handle(path: Path) -> int:
    import ctypes
    import ctypes.wintypes

    bindings = _load_directory_native_bindings()
    _reset_thread_last_error()
    handle = bindings.create_file_w(
        str(path),
        0x00000080,
        0x00000001 | 0x00000002,
        None,
        3,
        0x02000000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        last_error = _read_thread_last_error()
        fail(
            f"cannot pin directory {path}: "
            f"{ctypes.WinError(last_error)}"
        )
    return int(handle)


def _windows_directory_handle_identity(
    handle: int,
    expected: PreparedPathIdentity,
) -> PreparedPathIdentity:
    import ctypes
    import ctypes.wintypes

    bindings = _load_directory_native_bindings()
    information = bindings.by_handle_file_information_type()
    _reset_thread_last_error()
    if not bindings.get_file_information_by_handle(
        ctypes.wintypes.HANDLE(handle),
        ctypes.byref(information),
    ):
        last_error = _read_thread_last_error()
        fail(
            "cannot inspect pinned directory handle: "
            f"{ctypes.WinError(last_error)}"
        )
    attributes = int(information.file_attributes)
    if attributes & 0x00000400 or not attributes & 0x00000010:
        fail("pinned directory handle is a reparse/non-directory object")
    legacy_identity = PreparedPathIdentity(
        device=int(information.volume_serial_number),
        inode=(
            int(information.file_index_high) << 32
            | int(information.file_index_low)
        ),
        file_type=stat.S_IFDIR,
    )

    full_identity: Optional[PreparedPathIdentity] = None
    inspect_file_id = bindings.get_file_information_by_handle_ex
    if inspect_file_id is not None:
        file_id = bindings.file_id_information_type()
        _reset_thread_last_error()
        if inspect_file_id(
            ctypes.wintypes.HANDLE(handle),
            18,
            ctypes.byref(file_id),
            ctypes.sizeof(file_id),
        ):
            full_identity = PreparedPathIdentity(
                device=int(file_id.volume_serial_number),
                inode=int.from_bytes(
                    bytes(file_id.file_id.identifier),
                    "little",
                ),
                file_type=stat.S_IFDIR,
            )

    if full_identity == expected:
        return full_identity
    if legacy_identity == expected:
        return legacy_identity
    if full_identity is not None:
        return full_identity
    return legacy_identity


def _close_windows_handle(handle: int) -> None:
    import ctypes
    import ctypes.wintypes

    bindings = _load_directory_native_bindings()
    _reset_thread_last_error()
    if not bindings.close_handle(ctypes.wintypes.HANDLE(handle)):
        last_error = _read_thread_last_error()
        raise OSError(last_error, str(ctypes.WinError(last_error)))


class _MoveNativeBindings(NamedTuple):
    owner: Any
    move_file_ex_w: Any


@functools.lru_cache(maxsize=1)
def _load_move_native_bindings() -> _MoveNativeBindings:
    import ctypes
    import ctypes.wintypes

    owner = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file_ex_w = owner.MoveFileExW
    move_file_ex_w.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
    ]
    move_file_ex_w.restype = ctypes.wintypes.BOOL
    return _MoveNativeBindings(owner, move_file_ex_w)


def _windows_move_noreplace(source: Path, destination: Path) -> None:
    import ctypes

    bindings = _load_move_native_bindings()
    _reset_thread_last_error()
    if bindings.move_file_ex_w(
        str(source),
        str(destination),
        0x00000008,
    ):
        return
    last_error = _read_thread_last_error()
    message = str(ctypes.WinError(last_error))
    if last_error in (2, 3):
        raise FileNotFoundError(
            errno.ENOENT,
            message,
            str(source),
        )
    if last_error in (80, 183):
        raise FileExistsError(
            errno.EEXIST,
            message,
            str(destination),
        )
    raise OSError(
        last_error,
        message,
        str(source),
    )


_RENAME_NOREPLACE = 1
_RENAME_MAC_EXCL = 4


class _PosixRenameNativeBindings(NamedTuple):
    owner: Any
    renameat2: Optional[Any]
    renameatx_np: Optional[Any]


@functools.lru_cache(maxsize=1)
def _load_posix_rename_native_bindings() -> _PosixRenameNativeBindings:
    import ctypes

    owner = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(owner, "renameat2", None)
    renameatx_np = None
    if renameat2 is not None:
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
    elif sys.platform == "darwin":
        renameatx_np = getattr(owner, "renameatx_np", None)
        if renameatx_np is not None:
            renameatx_np.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameatx_np.restype = ctypes.c_int
    return _PosixRenameNativeBindings(owner, renameat2, renameatx_np)


def _posix_rename_atomic(
    source_dir_fd: int,
    source: str,
    destination_dir_fd: int,
    destination: str,
    flags: int,
    source_path: Path,
    destination_path: Path,
) -> None:
    import ctypes

    bindings = _load_posix_rename_native_bindings()
    function = bindings.renameat2
    call_flags = flags
    if function is None:
        if sys.platform == "darwin":
            function = bindings.renameatx_np
            if function is None:
                fail(
                    "macOS renameatx_np is unavailable; refusing an "
                    "unsafe transaction pathname replacement"
                )
            call_flags = _RENAME_MAC_EXCL
        else:
            fail(
                "this POSIX platform lacks renameat2/renameatx_np; "
                "refusing an unsafe transaction pathname replacement"
            )
    ctypes.set_errno(0)
    result = function(
        source_dir_fd,
        os.fsencode(source),
        destination_dir_fd,
        os.fsencode(destination),
        call_flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            f"{source_path} -> {destination_path}",
        )


class _PreparedParentPin:
    def __init__(
        self,
        path: Path,
        expected_identity: PreparedPathIdentity,
    ) -> None:
        self.path = Path(path)
        self.expected_identity = expected_identity
        self.fd = -1
        self.windows_chain: List[
            Tuple[Path, PreparedPathIdentity, int]
        ] = []

    def open(self) -> None:
        if self.fd >= 0 or self.windows_chain:
            fail(f"prepared parent is already pinned: {self.path}")
        if os.name == "nt":
            try:
                for ancestor in _windows_directory_chain(self.path):
                    before = os.stat(
                        str(ancestor),
                        follow_symlinks=False,
                    )
                    if (
                        _directory_stat_is_reparse(before)
                        or not stat.S_ISDIR(before.st_mode)
                    ):
                        fail(
                            "refusing reparse/non-directory ancestor: "
                            f"{ancestor}"
                        )
                    handle = _open_windows_directory_handle(ancestor)
                    try:
                        after = os.stat(
                            str(ancestor),
                            follow_symlinks=False,
                        )
                        identity = _identity_from_runtime_stat(after)
                        handle_identity = (
                            _windows_directory_handle_identity(
                                handle,
                                identity,
                            )
                        )
                        if (
                            _directory_stat_is_reparse(after)
                            or not stat.S_ISDIR(after.st_mode)
                            or _identity_from_runtime_stat(before)
                            != identity
                            or handle_identity != identity
                        ):
                            fail(
                                "directory ancestor changed while pinning: "
                                f"{ancestor}"
                            )
                    except BaseException:
                        try:
                            _close_windows_handle(handle)
                        except OSError:
                            pass
                        raise
                    self.windows_chain.append(
                        (ancestor, handle_identity, handle)
                    )
                self.validate()
                return
            except BaseException:
                self.close(suppress_errors=True)
                raise

        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            self.fd = os.open(str(self.path), flags)
            self.validate()
        except BaseException:
            self.close(suppress_errors=True)
            raise

    def close(self, suppress_errors: bool = False) -> None:
        errors: List[str] = []
        if self.fd >= 0:
            fd = self.fd
            self.fd = -1
            try:
                os.close(fd)
            except OSError as exc:
                errors.append(str(exc))
        chain = self.windows_chain
        self.windows_chain = []
        for _path, _identity, handle in reversed(chain):
            try:
                _close_windows_handle(handle)
            except OSError as exc:
                errors.append(str(exc))
        if errors and not suppress_errors:
            fail(
                f"failed to close prepared parent pin {self.path}: "
                + "; ".join(errors)
            )

    def validate(self) -> None:
        if os.name == "nt":
            if not self.windows_chain:
                fail(f"prepared parent is not pinned: {self.path}")
            for ancestor, identity, handle in self.windows_chain:
                try:
                    current = os.stat(
                        str(ancestor),
                        follow_symlinks=False,
                    )
                    handle_identity = (
                        _windows_directory_handle_identity(handle, identity)
                    )
                except OSError:
                    fail(
                        "directory ancestor changed while transaction "
                        f"was pinned: {ancestor}"
                    )
                if (
                    _directory_stat_is_reparse(current)
                    or not stat.S_ISDIR(current.st_mode)
                    or _identity_from_runtime_stat(current) != identity
                    or handle_identity != identity
                ):
                    fail(
                        "directory ancestor changed while transaction "
                        f"was pinned: {ancestor}"
                    )
            if self.windows_chain[-1][1] != self.expected_identity:
                fail(
                    "parent directory identity changed after "
                    f"prevalidation: {self.path}"
                )
            return

        if self.fd < 0:
            fail(f"prepared parent is not pinned: {self.path}")
        try:
            opened = os.fstat(self.fd)
            current = os.stat(
                str(self.path),
                follow_symlinks=False,
            )
        except OSError:
            fail(
                "parent directory changed while transaction was pinned: "
                f"{self.path}"
            )
        opened_identity = _identity_from_runtime_stat(opened)
        current_identity = _identity_from_runtime_stat(current)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _directory_stat_is_reparse(current)
            or opened_identity != self.expected_identity
            or current_identity != self.expected_identity
        ):
            fail(
                "parent directory identity changed after prevalidation: "
                f"{self.path}"
            )

    @staticmethod
    def _check_basename(basename: str) -> None:
        if (
            not basename
            or basename in (".", "..")
            or "/" in basename
            or (os.name == "nt" and "\\" in basename)
        ):
            fail(f"unsafe prepared basename: {basename!r}")

    def entry_path(self, basename: str) -> Path:
        self._check_basename(basename)
        return self.path / basename

    def _validate_operation(self, recovery: bool) -> None:
        if not recovery or os.name == "nt":
            self.validate()
            return
        if self.fd < 0:
            fail(f"prepared parent is not pinned: {self.path}")
        try:
            opened = os.fstat(self.fd)
        except OSError:
            fail(
                "pinned parent handle became unavailable during recovery: "
                f"{self.path}"
            )
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _identity_from_runtime_stat(opened)
            != self.expected_identity
        ):
            fail(
                "pinned parent handle changed during recovery: "
                f"{self.path}"
            )

    def stat_entry(
        self,
        basename: str,
        *,
        recovery: bool = False,
    ) -> Optional[os.stat_result]:
        self._check_basename(basename)
        self._validate_operation(recovery)
        try:
            if os.name == "nt":
                return os.stat(
                    str(self.entry_path(basename)),
                    follow_symlinks=False,
                )
            return os.stat(
                basename,
                dir_fd=self.fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None

    def open_read(
        self,
        basename: str,
        *,
        recovery: bool = False,
    ) -> int:
        self._check_basename(basename)
        self._validate_operation(recovery)
        flags = os.O_RDONLY
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        if os.name == "nt":
            return os.open(str(self.entry_path(basename)), flags)
        return os.open(basename, flags, dir_fd=self.fd)

    def open_exclusive(self, basename: str, mode: int) -> int:
        self._check_basename(basename)
        self.validate()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        if os.name == "nt":
            return os.open(
                str(self.entry_path(basename)),
                flags,
                mode,
            )
        return os.open(
            basename,
            flags,
            mode,
            dir_fd=self.fd,
        )

    def move_entry_noreplace(
        self,
        source: str,
        destination: str,
        *,
        recovery: bool = False,
    ) -> None:
        self._check_basename(source)
        self._check_basename(destination)
        self._validate_operation(recovery)
        if os.name == "nt":
            _windows_move_noreplace(
                self.entry_path(source),
                self.entry_path(destination),
            )
            return
        _posix_rename_atomic(
            self.fd,
            source,
            self.fd,
            destination,
            _RENAME_NOREPLACE,
            self.entry_path(source),
            self.entry_path(destination),
        )

    def unlink_entry(
        self,
        basename: str,
        *,
        recovery: bool = False,
    ) -> None:
        self._check_basename(basename)
        self._validate_operation(recovery)
        if os.name == "nt":
            os.unlink(str(self.entry_path(basename)))
            return
        os.unlink(basename, dir_fd=self.fd)

    def fsync(self, *, recovery: bool = False) -> None:
        self._validate_operation(recovery)
        if os.name != "nt":
            os.fsync(self.fd)


class _PreparedParentPins:
    def __init__(
        self,
        plans: Tuple[PreparedFilePlan, ...],
        suppress_exit_errors: bool = False,
    ) -> None:
        self.plans = plans
        self.suppress_exit_errors = suppress_exit_errors
        self.cleanup_errors: List[str] = []
        self.late_failure_handler: Any = None
        self.by_parent: Dict[str, _PreparedParentPin] = {}
        self.opened: List[_PreparedParentPin] = []

    def __enter__(self) -> "_PreparedParentPins":
        expected: Dict[str, PreparedPathIdentity] = {}
        paths: Dict[str, Path] = {}
        for plan in self.plans:
            key = _transaction_path_identity(
                Path(plan.canonical_parent)
            )
            previous = expected.get(key)
            if (
                previous is not None
                and previous != plan.prepared_parent_identity
            ):
                fail(
                    "prepared plans disagree on parent identity: "
                    f"{plan.canonical_parent}"
                )
            expected[key] = plan.prepared_parent_identity
            paths[key] = Path(plan.canonical_parent)
        try:
            for key in sorted(paths):
                pin = _PreparedParentPin(paths[key], expected[key])
                pin.open()
                self.by_parent[key] = pin
                self.opened.append(pin)
        except BaseException as primary:
            self.__exit__(type(primary), primary, None)
            raise
        return self

    def for_plan(self, plan: PreparedFilePlan) -> _PreparedParentPin:
        key = _transaction_path_identity(
            Path(plan.canonical_parent)
        )
        pin = self.by_parent.get(key)
        if pin is None:
            fail(
                "prepared parent pin is missing: "
                f"{plan.canonical_parent}"
            )
        return pin

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        errors: List[str] = []
        cleanup_control: Optional[BaseException] = None
        cleanup_traceback: Any = None
        opened = self.opened
        self.opened = []
        self.by_parent = {}
        for pin in reversed(opened):
            try:
                pin.close()
            except BaseException as close_exc:
                errors.append(
                    _best_effort_exception_text(close_exc)
                )
                if (
                    _exc is None
                    and cleanup_control is None
                    and _preserve_transaction_exception(close_exc)
                ):
                    cleanup_control = close_exc
                    cleanup_traceback = close_exc.__traceback__
        if errors:
            try:
                self.cleanup_errors.extend(errors)
                _attach_transaction_cleanup_errors(_exc, errors)
            except BaseException:
                pass
        if cleanup_control is not None:
            handler = self.late_failure_handler
            if handler is not None:
                try:
                    handler(cleanup_control)
                except BaseException:
                    pass
            raise cleanup_control.with_traceback(cleanup_traceback)
        if (
            errors
            and _exc_type is None
            and not self.suppress_exit_errors
        ):
            fail(
                "failed to close prepared parent pins: "
                + "; ".join(errors)
            )


class _PinnedEntrySnapshot(NamedTuple):
    identity: PreparedPathIdentity
    data: Optional[bytes]
    sha256: str
    mode: int
    size: int
    mtime_ns: Optional[int]


class _PinnedPreparedPlan(NamedTuple):
    plan: PreparedFilePlan
    pin: _PreparedParentPin
    snapshot: Optional[_PinnedEntrySnapshot]


class _CommittedMutation(NamedTuple):
    runtime: _PinnedPreparedPlan
    committed_identity: PreparedPathIdentity
    committed_sha256: str
    committed_mode: int
    committed_size: int
    committed_mtime_ns: Optional[int]
    original_quarantine: Optional[str]


@dataclass
class _TransactionJournalEntry:
    runtime: _PinnedPreparedPlan
    stage_name: str
    original_quarantine: str
    rollback_quarantine: str
    intended_sha256: str
    intended_size: int
    intended_mode: int
    intended_mtime_ns: Optional[int]
    phase: str = "READY"
    stage_opened: bool = False
    stage_identity: Optional[PreparedPathIdentity] = None
    stage_marker: Optional[_PinnedEntrySnapshot] = None
    claimed_marker: Optional[_PinnedEntrySnapshot] = None
    committed_marker: Optional[_PinnedEntrySnapshot] = None
    mutation: Optional[_CommittedMutation] = None
    rolled_back: bool = False
    finalized: bool = False
    write_uncertain: bool = False
    uncertain: bool = False
    namespace_changed: bool = False
    cleanup_errors: List[str] = field(default_factory=list)


def _snapshot_matches_marker(
    snapshot: _PinnedEntrySnapshot,
    identity: PreparedPathIdentity,
    sha256: str,
    mode: int,
    size: int,
    mtime_ns: Optional[int],
) -> bool:
    return (
        snapshot.identity == identity
        and snapshot.identity.file_type == stat.S_IFREG
        and snapshot.sha256 == sha256
        and snapshot.mode == mode
        and snapshot.size == size
        and (
            mtime_ns is None
            or snapshot.mtime_ns == mtime_ns
        )
    )


def _mutation_matches_snapshot(
    mutation: _CommittedMutation,
    snapshot: _PinnedEntrySnapshot,
) -> bool:
    return _snapshot_matches_marker(
        snapshot,
        mutation.committed_identity,
        mutation.committed_sha256,
        mutation.committed_mode,
        mutation.committed_size,
        mutation.committed_mtime_ns,
    )


class _TransactionRollbackConflict(SafeEditError):
    pass


def _preserve_transaction_exception(exc: BaseException) -> bool:
    return (
        isinstance(
            exc,
            (KeyboardInterrupt, SystemExit, GeneratorExit, MemoryError),
        )
        or not isinstance(exc, Exception)
    )


def _read_pinned_entry(
    pin: _PreparedParentPin,
    basename: str,
    max_bytes: int,
    *,
    capture_data: bool = True,
    recovery: bool = False,
) -> _PinnedEntrySnapshot:
    entry = pin.stat_entry(basename, recovery=recovery)
    if entry is None:
        exc = SafeEditError(
            f"target disappeared during transaction: "
            f"{pin.entry_path(basename)}"
        )
        setattr(exc, "_file_not_found", True)
        raise exc
    entry_identity = _identity_from_runtime_stat(entry)
    if entry_identity.file_type != stat.S_IFREG:
        fail(
            "transaction target is not a regular file: "
            f"{pin.entry_path(basename)}"
        )

    fd = pin.open_read(basename, recovery=recovery)
    data: Optional[bytes] = None
    digest: Optional[Any] = None
    try:
        before = os.fstat(fd)
        before_identity = _identity_from_runtime_stat(before)
        if (
            before_identity != entry_identity
            or before_identity.file_type != stat.S_IFREG
        ):
            fail(
                "transaction target identity changed while opening: "
                f"{pin.entry_path(basename)}"
            )
        if before.st_size > max_bytes:
            fail(
                f"file is {before.st_size} bytes, "
                f"exceeding --max-bytes {max_bytes}"
            )
        if capture_data:
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = handle.read(max_bytes + 1)
            total = len(data)
        else:
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(
                    fd,
                    min(
                        _TRANSACTION_VERIFY_CHUNK_BYTES,
                        max_bytes + 1 - total,
                    ),
                )
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    break
                digest.update(chunk)
        if total > max_bytes:
            fail(
                f"file exceeds --max-bytes {max_bytes} "
                "during pinned read"
            )
        after = os.fstat(fd)
    finally:
        os.close(fd)

    final = pin.stat_entry(basename, recovery=recovery)
    if final is None:
        fail(
            "transaction target disappeared during pinned read: "
            f"{pin.entry_path(basename)}"
        )

    def marker(info: os.stat_result) -> Tuple[Any, ...]:
        return (
            _identity_from_runtime_stat(info),
            int(info.st_size),
            _stat_mtime_ns(info),
        )

    if marker(before) != marker(after) or marker(after) != marker(final):
        fail(
            "transaction target changed during pinned read: "
            f"{pin.entry_path(basename)}"
        )
    if capture_data:
        assert data is not None
        digest_hex = hashlib.sha256(data).hexdigest()
    else:
        assert digest is not None
        digest_hex = digest.hexdigest()
    return _PinnedEntrySnapshot(
        identity=_identity_from_runtime_stat(after),
        data=data,
        sha256=digest_hex,
        mode=stat.S_IMODE(after.st_mode),
        size=int(after.st_size),
        mtime_ns=_stat_mtime_ns(after),
    )


def _validate_prepared_plan(
    plan: PreparedFilePlan,
    pin: _PreparedParentPin,
) -> _PinnedPreparedPlan:
    expected_path = Path(plan.canonical_parent) / plan.basename
    if _transaction_path_identity(
        expected_path
    ) != _transaction_path_identity(Path(plan.path)):
        fail(
            "prepared target parent/basename binding is invalid: "
            f"{plan.path}"
        )
    pin.validate()
    current = _resolve_prepared_target(plan)
    if _transaction_path_identity(current) != _transaction_path_identity(
        expected_path
    ):
        fail(
            "transaction target binding changed after prevalidation: "
            f"{plan.requested_path}"
        )

    if plan.action == "create":
        if pin.stat_entry(plan.basename) is not None:
            _fail_file_already_exists(expected_path)
        return _PinnedPreparedPlan(plan, pin, None)

    snapshot = _read_pinned_entry(
        pin,
        plan.basename,
        plan.max_bytes,
        capture_data=False,
    )
    if snapshot.identity != plan.prepared_target_identity:
        fail(
            "target identity changed after transaction prevalidation: "
            f"{plan.path}"
        )
    if snapshot.sha256 != plan.original_sha256:
        assert plan.original_sha256 is not None
        _fail_sha256_mismatch(
            "target changed after transaction prevalidation: "
            f"{plan.path}",
            plan.original_sha256,
            snapshot.sha256,
        )
    return _PinnedPreparedPlan(plan, pin, snapshot)


def _revalidate_prepared_checkpoint(
    runtime: _PinnedPreparedPlan,
) -> _PinnedPreparedPlan:
    plan = runtime.plan
    pin = runtime.pin
    expected_path = Path(plan.canonical_parent) / plan.basename
    pin.validate()
    current = _resolve_prepared_target(plan)
    if _transaction_path_identity(current) != _transaction_path_identity(
        expected_path
    ):
        fail(
            "transaction target binding changed after prevalidation: "
            f"{plan.requested_path}"
        )
    current_info = pin.stat_entry(plan.basename)
    if plan.action == "create":
        if current_info is not None:
            _fail_file_already_exists(expected_path)
        return runtime

    original = runtime.snapshot
    assert original is not None
    if current_info is None:
        fail(
            "target disappeared after transaction prevalidation: "
            f"{plan.path}"
        )
    current_identity = _identity_from_runtime_stat(current_info)
    if (
        current_identity.file_type != stat.S_IFREG
        or current_identity != original.identity
    ):
        fail(
            "target identity changed after transaction prevalidation: "
            f"{plan.path}"
        )
    if (
        int(current_info.st_size) != original.size
        or _stat_mtime_ns(current_info) != original.mtime_ns
    ):
        fail(
            "target metadata changed after transaction prevalidation: "
            f"{plan.path}"
        )
    return runtime


def _transaction_before_mutations(
    _plans: Tuple[_PinnedPreparedPlan, ...],
) -> None:
    """Deterministic race-test hook after global validation."""


def _transaction_after_mutation(
    _mutation: _CommittedMutation,
    _file_index: int,
) -> None:
    """Deterministic race-test hook after a recorded mutation."""


def _transaction_before_response() -> None:
    """Deterministic fault hook after context cleanup and before response."""


def _write_transaction_fd(fd: int, data: bytes) -> None:
    view = memoryview(data)
    total = 0
    try:
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("failed to write transaction output")
            total += written
            view = view[written:]
        os.fsync(fd)
    except BaseException as exc:
        try:
            setattr(exc, "_transaction_bytes_written", total)
        except BaseException:
            pass
        raise


def _new_transaction_stage_name() -> str:
    return f".safe-edit-{os.urandom(16).hex()}.txn"


def _stage_pinned_bytes(
    journal: _TransactionJournalEntry,
    data: bytes,
    mode: int,
    output_sha256: str,
    output_mtime_ns: Optional[int],
    respect_umask: bool = False,
) -> Tuple[str, _PinnedEntrySnapshot]:
    pin = journal.runtime.pin
    for attempt in range(16):
        if attempt:
            journal.stage_name = _new_transaction_stage_name()
        name = journal.stage_name
        journal.phase = "ATTEMPT_STAGE_CREATE"
        try:
            fd = pin.open_exclusive(
                name,
                mode if respect_umask else 0o600,
            )
        except FileExistsError:
            journal.phase = "READY"
            continue
        journal.phase = "STAGE_OPEN"
        journal.stage_opened = True

        staged_info: Optional[os.stat_result] = None
        primary: Optional[BaseException] = None
        primary_traceback: Any = None
        try:
            created = os.fstat(fd)
            created_identity = _identity_from_runtime_stat(created)
            journal.stage_identity = created_identity
            if created_identity.file_type != stat.S_IFREG:
                fail("transaction staging entry is not a regular file")
            if not respect_umask and hasattr(os, "fchmod"):
                journal.phase = "ATTEMPT_STAGE_MODE"
                os.fchmod(fd, mode)
                journal.phase = "STAGE_MODE_SET"
            elif not respect_umask and os.name == "nt":
                journal.phase = "ATTEMPT_STAGE_MODE"
                os.chmod(str(pin.entry_path(name)), mode)
                journal.phase = "STAGE_MODE_SET"

            journal.phase = "ATTEMPT_STAGE_WRITE"
            _write_transaction_fd(fd, data)
            journal.phase = "STAGE_WRITTEN"
            if output_mtime_ns is not None:
                before_time = os.fstat(fd)
                journal.phase = "ATTEMPT_STAGE_MTIME"
                if os.utime in os.supports_fd:
                    os.utime(
                        fd,
                        ns=(
                            int(before_time.st_atime_ns),
                            output_mtime_ns,
                        ),
                    )
                else:
                    os.utime(
                        str(pin.entry_path(name)),
                        ns=(
                            int(before_time.st_atime_ns),
                            output_mtime_ns,
                        ),
                    )
                journal.phase = "STAGE_MTIME_SET"
                journal.phase = "ATTEMPT_STAGE_MTIME_SYNC"
                os.fsync(fd)
                journal.phase = "STAGE_MTIME_SYNCED"
            staged_info = os.fstat(fd)
            if (
                _identity_from_runtime_stat(staged_info)
                != created_identity
            ):
                fail("transaction staging identity changed while writing")
        except BaseException as exc:
            primary = exc
            primary_traceback = exc.__traceback__

        journal.phase = "ATTEMPT_STAGE_CLOSE"
        try:
            os.close(fd)
        except BaseException as close_exc:
            if primary is None:
                raise
            try:
                journal.cleanup_errors.append(
                    "staging descriptor close failed for "
                    f"{_pinned_artifact_label(pin, name)}: "
                    f"{_best_effort_exception_text(close_exc)}"
                )
            except BaseException:
                pass
        journal.phase = "STAGE_CLOSED"
        if primary is not None:
            raise primary.with_traceback(primary_traceback)

        assert staged_info is not None
        finalized = _read_pinned_entry(
            pin,
            name,
            len(data),
            capture_data=False,
        )
        journal.phase = "STAGE_INSPECTED"
        if (
            finalized.identity
            != _identity_from_runtime_stat(staged_info)
            or finalized.sha256 != output_sha256
        ):
            fail("transaction staging verification failed")
        journal.stage_marker = finalized
        journal.phase = "STAGED"
        return (name, finalized)
    fail("cannot allocate transaction staging file")
def _cleanup_pinned_stage(
    pin: _PreparedParentPin,
    name: str,
) -> None:
    if not name:
        return
    try:
        pin.unlink_entry(name)
    except FileNotFoundError:
        pass


def _expect_pinned_absent(runtime: _PinnedPreparedPlan) -> None:
    plan = runtime.plan
    if runtime.pin.stat_entry(plan.basename) is not None:
        _fail_file_already_exists(Path(plan.path))


def _assert_pinned_entry(
    runtime: _PinnedPreparedPlan,
    expected_identity: PreparedPathIdentity,
    expected_sha256: str,
) -> _PinnedEntrySnapshot:
    plan = runtime.plan
    snapshot = _read_pinned_entry(
        runtime.pin,
        plan.basename,
        max(plan.max_bytes, len(plan.output)),
        capture_data=False,
    )
    if (
        snapshot.identity != expected_identity
        or snapshot.identity.file_type != stat.S_IFREG
    ):
        fail(
            "transaction target identity changed before mutation: "
            f"{plan.path}"
        )
    if snapshot.sha256 != expected_sha256:
        _fail_sha256_mismatch(
            "SHA-256 mismatch before transaction mutation: "
            f"{plan.path}",
            expected_sha256,
            snapshot.sha256,
        )
    return snapshot


def _assert_pinned_snapshot_unchanged(
    runtime: _PinnedPreparedPlan,
) -> _PinnedEntrySnapshot:
    expected = runtime.snapshot
    assert expected is not None
    plan = runtime.plan
    current = _read_pinned_entry(
        runtime.pin,
        plan.basename,
        max(plan.max_bytes, len(plan.output), expected.size),
        capture_data=False,
    )
    if not _snapshot_matches_marker(
        current,
        expected.identity,
        expected.sha256,
        expected.mode,
        expected.size,
        expected.mtime_ns,
    ):
        conflict = _TransactionRollbackConflict(
            "transaction no-op target changed immediately before skip: "
            f"{plan.path}"
        )
        setattr(conflict, "_transaction_publish_conflict", True)
        raise conflict
    return current


def _record_transaction_cleanup_details(
    exc: BaseException,
    details: Iterable[str],
) -> None:
    additions = tuple(str(item) for item in details if str(item))
    if not additions:
        return
    existing = tuple(
        getattr(exc, "_transaction_cleanup_errors", ())
    )
    setattr(
        exc,
        "_transaction_cleanup_errors",
        existing + additions,
    )


def _cleanup_owned_pinned_entry(
    pin: _PreparedParentPin,
    name: str,
    expected: _PinnedEntrySnapshot,
    *,
    journal: Optional[_TransactionJournalEntry] = None,
    phase_prefix: str = "CLEANUP",
    propagate_control: bool = False,
) -> Tuple[str, ...]:
    if not name:
        return ()
    label = _pinned_artifact_label(pin, name)
    try:
        if pin.stat_entry(name, recovery=True) is None:
            if journal is not None:
                journal.phase = f"{phase_prefix}_ABSENT"
            return ()
        current = _read_pinned_entry(
            pin,
            name,
            expected.size,
            capture_data=False,
            recovery=True,
        )
    except BaseException as exc:
        if propagate_control and _preserve_transaction_exception(exc):
            raise
        if journal is not None:
            journal.phase = f"{phase_prefix}_INSPECTION_WARNING"
        return (
            "transaction cleanup retained unverified "
            f"{label}: {_best_effort_exception_text(exc)}",
        )
    if not _snapshot_matches_marker(
        current,
        expected.identity,
        expected.sha256,
        expected.mode,
        expected.size,
        expected.mtime_ns,
    ):
        if journal is not None:
            journal.phase = f"{phase_prefix}_RETAINED_CHANGED"
        return (
            "transaction cleanup retained externally changed "
            f"{label}",
        )
    if journal is not None:
        journal.phase = f"ATTEMPT_{phase_prefix}_UNLINK"
    try:
        pin.unlink_entry(name, recovery=True)
    except FileNotFoundError:
        if journal is not None:
            journal.phase = f"{phase_prefix}_ABSENT"
        return ()
    except BaseException as exc:
        if propagate_control and _preserve_transaction_exception(exc):
            raise
        try:
            final_info = pin.stat_entry(name, recovery=True)
        except BaseException as inspect_exc:
            if (
                propagate_control
                and _preserve_transaction_exception(inspect_exc)
            ):
                raise
            if journal is not None:
                journal.phase = f"{phase_prefix}_STATE_WARNING"
            return (
                "transaction cleanup state is uncertain for "
                f"{label}: {_best_effort_exception_text(inspect_exc)}; "
                "unlink error: "
                f"{_best_effort_exception_text(exc)}",
            )
        if final_info is None:
            if journal is not None:
                journal.phase = f"{phase_prefix}_ABSENT_AFTER_ERROR"
            return ()
        if journal is not None:
            journal.phase = f"{phase_prefix}_RETAINED"
        return (
            f"transaction cleanup retained {label}: "
            f"{_best_effort_exception_text(exc)}",
        )
    if journal is not None:
        journal.phase = f"{phase_prefix}_UNLINK_RETURNED"
    return ()


def _restore_claimed_entry(
    pin: _PreparedParentPin,
    quarantine: str,
    destination: str,
    expected: _PinnedEntrySnapshot,
    *,
    journal: Optional[_TransactionJournalEntry] = None,
    phase_prefix: str = "RESTORE",
) -> Tuple[bool, Tuple[str, ...]]:
    source_label = _pinned_artifact_label(pin, quarantine)
    destination_label = _pinned_artifact_label(pin, destination)
    details: List[str] = []
    if journal is not None:
        journal.phase = f"ATTEMPT_{phase_prefix}_MOVE"
    try:
        pin.move_entry_noreplace(
            quarantine,
            destination,
            recovery=True,
        )
    except BaseException as move_exc:
        try:
            source_info = pin.stat_entry(
                quarantine,
                recovery=True,
            )
            destination_info = pin.stat_entry(
                destination,
                recovery=True,
            )
        except BaseException as inspect_exc:
            if journal is not None:
                journal.phase = f"{phase_prefix}_STATE_UNKNOWN"
            return (
                False,
                (
                    "could not inspect no-replace restore state for "
                    f"{source_label} -> {destination_label}: "
                    f"{_best_effort_exception_text(inspect_exc)}; "
                    "original move error: "
                    f"{_best_effort_exception_text(move_exc)}",
                ),
            )
        if source_info is not None or destination_info is None:
            if journal is not None:
                journal.phase = f"{phase_prefix}_RETAINED"
            return (
                False,
                (
                    "could not restore quarantined generation without "
                    f"replacement; retained {source_label}: "
                    f"{_best_effort_exception_text(move_exc)}",
                ),
            )
        try:
            restored = _read_pinned_entry(
                pin,
                destination,
                expected.size,
                capture_data=False,
                recovery=True,
            )
        except BaseException as inspect_exc:
            if journal is not None:
                journal.phase = f"{phase_prefix}_STATE_UNKNOWN"
            return (
                False,
                (
                    "restore primitive may have completed but destination "
                    f"could not be verified at {destination_label}: "
                    f"{_best_effort_exception_text(inspect_exc)}; "
                    "original move error: "
                    f"{_best_effort_exception_text(move_exc)}",
                ),
            )
        if not _snapshot_matches_marker(
            restored,
            expected.identity,
            expected.sha256,
            expected.mode,
            expected.size,
            expected.mtime_ns,
        ):
            if journal is not None:
                journal.phase = f"{phase_prefix}_DESTINATION_CHANGED"
            return (
                False,
                (
                    "restore primitive failed after an unexpected "
                    f"generation appeared at {destination_label}; "
                    f"quarantine was {source_label}: "
                    f"{_best_effort_exception_text(move_exc)}",
                ),
            )
        if journal is not None:
            journal.phase = f"{phase_prefix}_MOVE_COMPLETED_AFTER_ERROR"
    else:
        if journal is not None:
            journal.phase = f"{phase_prefix}_MOVE_RETURNED"

    try:
        restored = _read_pinned_entry(
            pin,
            destination,
            expected.size,
            capture_data=False,
            recovery=True,
        )
    except BaseException as exc:
        if journal is not None:
            journal.phase = f"{phase_prefix}_STATE_UNKNOWN"
        return (
            False,
            (
                "restored generation could not be verified at "
                f"{destination_label}: "
                f"{_best_effort_exception_text(exc)}; quarantine was "
                f"{source_label}",
            ),
        )
    if not _snapshot_matches_marker(
        restored,
        expected.identity,
        expected.sha256,
        expected.mode,
        expected.size,
        expected.mtime_ns,
    ):
        if journal is not None:
            journal.phase = f"{phase_prefix}_DESTINATION_CHANGED"
        return (
            False,
            (
                "restored generation changed before verification at "
                f"{destination_label}; quarantine was {source_label}",
            ),
        )
    if journal is not None:
        journal.phase = f"{phase_prefix}_VERIFIED"
        journal.phase = f"ATTEMPT_{phase_prefix}_SYNC"
    try:
        pin.fsync(recovery=True)
    except BaseException as exc:
        if journal is not None:
            journal.phase = f"{phase_prefix}_SYNC_WARNING"
        details.append(
            "directory sync after no-replace restore failed for "
            f"{destination_label}: {_best_effort_exception_text(exc)}"
        )
    else:
        if journal is not None:
            journal.phase = f"{phase_prefix}_SYNCED"
    return (True, tuple(details))

def _replace_pinned_bytes(
    journal: _TransactionJournalEntry,
    data: bytes,
    expected_identity: PreparedPathIdentity,
    expected_sha256: str,
    expected_mode: int,
    expected_size: int,
    expected_mtime_ns: Optional[int],
    output_sha256: str,
    mode: int,
    output_mtime_ns: Optional[int],
) -> Tuple[_PinnedEntrySnapshot, str]:
    runtime = journal.runtime
    pin = runtime.pin
    plan = runtime.plan
    _stage, staged = _stage_pinned_bytes(
        journal,
        data,
        mode,
        output_sha256,
        output_mtime_ns,
    )
    expected = _PinnedEntrySnapshot(
        identity=expected_identity,
        data=None,
        sha256=expected_sha256,
        mode=expected_mode,
        size=expected_size,
        mtime_ns=expected_mtime_ns,
    )
    quarantine = journal.original_quarantine
    if not quarantine:
        fail(
            "transaction journal lacks original quarantine name for "
            f"{plan.path}"
        )

    journal.phase = "ATTEMPT_CLAIM"
    try:
        pin.move_entry_noreplace(plan.basename, quarantine)
    except BaseException as exc:
        if _preserve_transaction_exception(exc):
            raise
        conflict = _TransactionRollbackConflict(
            "atomic transaction target claim failed for "
            f"{plan.path}: {exc}"
        )
        setattr(conflict, "_transaction_publish_conflict", True)
        raise conflict from exc
    journal.phase = "CLAIMED"

    claimed = _read_pinned_entry(
        pin,
        quarantine,
        max(plan.max_bytes, expected_size),
        capture_data=False,
    )
    journal.phase = "CLAIM_INSPECTED"
    journal.claimed_marker = claimed
    if not _snapshot_matches_marker(
        claimed,
        expected.identity,
        expected.sha256,
        expected.mode,
        expected.size,
        expected.mtime_ns,
    ):
        hash_mismatch = claimed.sha256 != expected.sha256
        reason = (
            "SHA-256 mismatch after atomic target claim"
            if hash_mismatch
            else "claimed target generation did not match preparation"
        )
        conflict = _TransactionRollbackConflict(
            "atomic transaction publish conflict for "
            f"{plan.path}: {reason}"
        )
        setattr(conflict, "_transaction_publish_conflict", True)
        if hash_mismatch:
            setattr(conflict, "_expected_sha256", expected.sha256)
            setattr(conflict, "_actual_sha256", claimed.sha256)
        raise conflict
    journal.phase = "CLAIM_VERIFIED"

    journal.phase = "ATTEMPT_INSTALL"
    try:
        pin.move_entry_noreplace(_stage, plan.basename)
    except BaseException as exc:
        if _preserve_transaction_exception(exc):
            raise
        conflict = _TransactionRollbackConflict(
            "atomic transaction install conflict for "
            f"{plan.path}: {exc}"
        )
        setattr(conflict, "_transaction_publish_conflict", True)
        raise conflict from exc
    journal.phase = "INSTALLED"

    journal.phase = "ATTEMPT_INSTALL_SYNC"
    pin.fsync()
    journal.phase = "INSTALL_SYNCED"
    snapshot = _read_pinned_entry(
        pin,
        plan.basename,
        max(plan.max_bytes, len(data)),
        capture_data=False,
    )
    journal.phase = "TARGET_INSPECTED"
    if not _snapshot_matches_marker(
        snapshot,
        staged.identity,
        staged.sha256,
        staged.mode,
        staged.size,
        staged.mtime_ns,
    ):
        raise SafeEditError(
            "transaction replacement changed before verification: "
            f"{plan.path}"
        )
    journal.committed_marker = snapshot
    journal.mutation = _CommittedMutation(
        runtime=runtime,
        committed_identity=snapshot.identity,
        committed_sha256=snapshot.sha256,
        committed_mode=snapshot.mode,
        committed_size=snapshot.size,
        committed_mtime_ns=snapshot.mtime_ns,
        original_quarantine=quarantine,
    )
    journal.phase = "COMMITTED"
    return (snapshot, quarantine)


def _create_pinned_output(
    journal: _TransactionJournalEntry,
) -> _PinnedEntrySnapshot:
    runtime = journal.runtime
    plan = runtime.plan
    _expect_pinned_absent(runtime)
    stage, staged = _stage_pinned_bytes(
        journal,
        plan.output,
        0o666,
        plan.output_sha256,
        None,
        respect_umask=True,
    )

    journal.phase = "ATTEMPT_INSTALL"
    try:
        runtime.pin.move_entry_noreplace(
            stage,
            plan.basename,
        )
    except BaseException as exc:
        if _preserve_transaction_exception(exc):
            raise
        conflict = _TransactionRollbackConflict(
            "atomic transaction create conflict for "
            f"{plan.path}: {exc}"
        )
        setattr(conflict, "_transaction_publish_conflict", True)
        raise conflict from exc
    journal.phase = "INSTALLED"

    journal.phase = "ATTEMPT_INSTALL_SYNC"
    runtime.pin.fsync()
    journal.phase = "INSTALL_SYNCED"
    snapshot = _read_pinned_entry(
        runtime.pin,
        plan.basename,
        max(plan.max_bytes, len(plan.output)),
        capture_data=False,
    )
    journal.phase = "TARGET_INSPECTED"
    if not _snapshot_matches_marker(
        snapshot,
        staged.identity,
        staged.sha256,
        staged.mode,
        staged.size,
        staged.mtime_ns,
    ):
        raise SafeEditError(
            "created target changed before verification: "
            f"{plan.path}"
        )
    journal.committed_marker = snapshot
    journal.mutation = _CommittedMutation(
        runtime=runtime,
        committed_identity=snapshot.identity,
        committed_sha256=snapshot.sha256,
        committed_mode=snapshot.mode,
        committed_size=snapshot.size,
        committed_mtime_ns=snapshot.mtime_ns,
        original_quarantine=None,
    )
    journal.phase = "COMMITTED"
    return snapshot



class _JournalArtifactProbe(NamedTuple):
    status: str
    identity: Optional[PreparedPathIdentity]
    snapshot: Optional[_PinnedEntrySnapshot]
    detail: Optional[str]



def _best_effort_exception_text(exc: BaseException) -> str:
    try:
        return f"{type(exc).__name__}: {exc}"
    except BaseException:
        try:
            return type(exc).__name__
        except BaseException:
            return "unprintable exception"


def _pinned_artifact_label(
    pin: _PreparedParentPin,
    basename: str,
) -> str:
    try:
        basename_text = repr(basename)
    except BaseException as exc:
        basename_text = (
            "<unavailable:"
            + _best_effort_exception_text(exc)
            + ">"
        )
    try:
        parent = pin.expected_identity
        parent_text = (
            f"(device={parent.device}, inode={parent.inode}, "
            f"file_type={parent.file_type})"
        )
    except BaseException as exc:
        parent_text = (
            "<unavailable:"
            + _best_effort_exception_text(exc)
            + ">"
        )
    try:
        path = str(pin.entry_path(basename))
    except BaseException as exc:
        path = (
            "<unavailable:"
            + _best_effort_exception_text(exc)
            + ">"
        )
    return (
        f"artifact basename={basename_text}; "
        f"pinned parent identity={parent_text}; "
        f"best-effort path={path}"
    )


def _journal_artifact_label(
    journal: _TransactionJournalEntry,
    basename: str,
) -> str:
    return _pinned_artifact_label(journal.runtime.pin, basename)


def _probe_journal_artifact(
    journal: _TransactionJournalEntry,
    basename: str,
) -> _JournalArtifactProbe:
    if not basename:
        return _JournalArtifactProbe("ABSENT", None, None, None)
    pin = journal.runtime.pin
    label = _journal_artifact_label(journal, basename)
    try:
        info = pin.stat_entry(basename, recovery=True)
    except BaseException as exc:
        return _JournalArtifactProbe(
            "UNKNOWN",
            None,
            None,
            f"could not inspect {label}: {_best_effort_exception_text(exc)}",
        )
    if info is None:
        return _JournalArtifactProbe("ABSENT", None, None, None)

    identity = _identity_from_runtime_stat(info)
    if identity.file_type != stat.S_IFREG:
        return _JournalArtifactProbe(
            "PRESENT",
            identity,
            None,
            f"retained non-regular {label}",
        )
    plan = journal.runtime.plan
    original = journal.runtime.snapshot
    limit = max(
        plan.max_bytes,
        len(plan.output),
        original.size if original is not None else 0,
        int(info.st_size),
    )
    try:
        snapshot = _read_pinned_entry(
            pin,
            basename,
            limit,
            capture_data=False,
            recovery=True,
        )
    except BaseException as exc:
        return _JournalArtifactProbe(
            "PRESENT",
            identity,
            None,
            f"could not verify present {label}: "
            f"{_best_effort_exception_text(exc)}",
        )
    return _JournalArtifactProbe(
        "PRESENT",
        identity,
        snapshot,
        None,
    )


def _journal_probe_matches_marker(
    probe: _JournalArtifactProbe,
    marker: Optional[_PinnedEntrySnapshot],
) -> bool:
    return (
        marker is not None
        and probe.snapshot is not None
        and _snapshot_matches_marker(
            probe.snapshot,
            marker.identity,
            marker.sha256,
            marker.mode,
            marker.size,
            marker.mtime_ns,
        )
    )


def _journal_record_detail(
    journal: _TransactionJournalEntry,
    detail: str,
) -> None:
    try:
        journal.cleanup_errors.append(str(detail))
    except BaseException:
        pass


def _cleanup_journal_artifact(
    journal: _TransactionJournalEntry,
    basename: str,
    probe: _JournalArtifactProbe,
    *,
    marker: Optional[_PinnedEntrySnapshot],
    identity: Optional[PreparedPathIdentity],
    kind: str,
) -> bool:
    if probe.status == "ABSENT":
        return True
    label = _journal_artifact_label(journal, basename)
    possible_owned_stage = (
        kind == "stage"
        and (
            journal.stage_opened
            or journal.phase == "ATTEMPT_STAGE_CREATE"
        )
    )
    if probe.status == "UNKNOWN":
        if possible_owned_stage:
            journal.namespace_changed = True
        journal.uncertain = True
        _journal_record_detail(
            journal,
            probe.detail or f"retained unknown {label}",
        )
        return False

    owned = _journal_probe_matches_marker(probe, marker)
    if marker is None and identity is not None:
        owned = probe.identity == identity
    if owned and possible_owned_stage:
        journal.namespace_changed = True
    if not owned:
        if possible_owned_stage:
            journal.namespace_changed = True
        journal.uncertain = True
        detail = f"retained unowned or externally changed {label}"
        if probe.detail:
            detail += f": {probe.detail}"
        _journal_record_detail(journal, detail)
        return False

    journal.phase = f"ATTEMPT_CLEANUP_{kind.upper()}"
    try:
        journal.runtime.pin.unlink_entry(
            basename,
            recovery=True,
        )
    except BaseException as unlink_exc:
        try:
            final = _probe_journal_artifact(journal, basename)
        except BaseException as inspect_exc:
            final = _JournalArtifactProbe(
                "UNKNOWN",
                None,
                None,
                f"cleanup could not inspect {label}: "
                f"{_best_effort_exception_text(inspect_exc)}",
            )
        if final.status == "ABSENT":
            journal.phase = f"CLEANED_{kind.upper()}"
            return True
        journal.phase = f"CLEANUP_{kind.upper()}_UNCERTAIN"
        journal.uncertain = True
        detail = (
            f"cleanup retained or could not verify {label}: "
            f"{_best_effort_exception_text(unlink_exc)}"
        )
        if final.detail:
            detail += f"; {final.detail}"
        _journal_record_detail(journal, detail)
        return False
    journal.phase = f"CLEANED_{kind.upper()}"

    try:
        final_info = journal.runtime.pin.stat_entry(
            basename,
            recovery=True,
        )
    except BaseException as inspect_exc:
        journal.uncertain = True
        _journal_record_detail(
            journal,
            f"cleanup final state is unknown for {label}: "
            f"{_best_effort_exception_text(inspect_exc)}",
        )
        return False
    if final_info is not None:
        journal.uncertain = True
        _journal_record_detail(
            journal,
            f"cleanup path was recreated; retained replacement {label}",
        )
        return False
    return True



def _reconcile_transaction_journal(
    journal: _TransactionJournalEntry,
) -> Optional[_CommittedMutation]:
    if journal.mutation is not None:
        return journal.mutation

    phase = journal.phase
    plan = journal.runtime.plan
    stage = _probe_journal_artifact(
        journal,
        journal.stage_name,
    )
    target = _probe_journal_artifact(
        journal,
        plan.basename,
    )
    original_quarantine = _probe_journal_artifact(
        journal,
        journal.original_quarantine,
    )
    for probe in (stage, target, original_quarantine):
        if probe.status == "UNKNOWN" and probe.detail:
            journal.uncertain = True
            _journal_record_detail(journal, probe.detail)

    install_possible = phase in {
        "ATTEMPT_INSTALL",
        "INSTALLED",
        "ATTEMPT_INSTALL_SYNC",
        "INSTALL_SYNCED",
        "TARGET_INSPECTED",
        "COMMITTED",
    }
    stage_is_owned = _journal_probe_matches_marker(
        stage,
        journal.stage_marker,
    )
    target_matches_stage = _journal_probe_matches_marker(
        target,
        journal.stage_marker,
    )
    install_proven = (
        install_possible
        and stage.status == "ABSENT"
        and target_matches_stage
    )
    install_not_performed = (
        install_possible
        and stage.status == "PRESENT"
        and stage_is_owned
        and (
            target.status == "ABSENT"
            or (
                target.status == "PRESENT"
                and target.snapshot is not None
                and not target_matches_stage
            )
        )
    )
    if (
        install_possible
        and not install_proven
        and not install_not_performed
    ):
        journal.namespace_changed = True
        journal.write_uncertain = True
        journal.uncertain = True
        detail = (
            "install outcome is ambiguous; stage "
            f"{_journal_artifact_label(journal, journal.stage_name)} "
            f"is {stage.status}; target "
            f"{_journal_artifact_label(journal, plan.basename)} "
            f"is {target.status}"
        )
        if stage.detail:
            detail += f"; {stage.detail}"
        if target.detail:
            detail += f"; {target.detail}"
        _journal_record_detail(journal, detail)

    if install_proven:
        assert target.snapshot is not None
        snapshot = target.snapshot
        journal.committed_marker = snapshot
        journal.mutation = _CommittedMutation(
            runtime=journal.runtime,
            committed_identity=snapshot.identity,
            committed_sha256=snapshot.sha256,
            committed_mode=snapshot.mode,
            committed_size=snapshot.size,
            committed_mtime_ns=snapshot.mtime_ns,
            original_quarantine=(
                journal.original_quarantine or None
            ),
        )
        journal.namespace_changed = True
        journal.phase = "RECONCILED_COMMITTED"
        return journal.mutation

    claim_possible = (
        plan.action == "edit"
        and phase in {
            "ATTEMPT_CLAIM",
            "CLAIMED",
            "CLAIM_INSPECTED",
            "CLAIM_VERIFIED",
            "ATTEMPT_INSTALL",
            "INSTALLED",
            "ATTEMPT_INSTALL_SYNC",
            "INSTALL_SYNCED",
            "TARGET_INSPECTED",
            "COMMITTED",
        }
    )
    original = journal.runtime.snapshot
    target_is_original = _journal_probe_matches_marker(
        target,
        original,
    )
    quarantine_is_original = _journal_probe_matches_marker(
        original_quarantine,
        original,
    )
    quarantine_is_claimed = _journal_probe_matches_marker(
        original_quarantine,
        journal.claimed_marker,
    )
    restorable_marker = (
        journal.claimed_marker
        if quarantine_is_claimed
        else original if quarantine_is_original else None
    )

    if (
        claim_possible
        and restorable_marker is not None
        and target.status == "ABSENT"
    ):
        journal.namespace_changed = True
        restore_result = _restore_claimed_entry(
            journal.runtime.pin,
            journal.original_quarantine,
            plan.basename,
            restorable_marker,
            journal=journal,
            phase_prefix="RECONCILE_CLAIM_RESTORE",
        )
        restored, details = restore_result
        for detail in details:
            _journal_record_detail(journal, detail)
        if restored:
            journal.phase = "CLAIM_RESTORED"
            journal.rolled_back = True
        else:
            journal.uncertain = True
            _journal_record_detail(
                journal,
                "retained unresolved claimed generation "
                f"{_journal_artifact_label(journal, journal.original_quarantine)}; "
                "destination "
                f"{_journal_artifact_label(journal, plan.basename)}",
            )
    elif (
        claim_possible
        and restorable_marker is not None
        and target.status != "ABSENT"
    ):
        journal.namespace_changed = True
        journal.uncertain = True
        _journal_record_detail(
            journal,
            "retained claimed generation because destination is occupied; "
            f"{_journal_artifact_label(journal, journal.original_quarantine)}; "
            "destination "
            f"{_journal_artifact_label(journal, plan.basename)}",
        )
    elif claim_possible and target.status == "ABSENT":
        journal.namespace_changed = True
        journal.uncertain = True
        _journal_record_detail(
            journal,
            "claim endpoints are both absent or unverified; "
            f"{_journal_artifact_label(journal, plan.basename)}; "
            f"{_journal_artifact_label(journal, journal.original_quarantine)}",
        )
    elif claim_possible and original_quarantine.status == "UNKNOWN":
        journal.uncertain = True
    elif claim_possible and target_is_original:
        journal.rolled_back = True
    elif (
        claim_possible
        and target.status == "PRESENT"
        and not target_is_original
    ):
        _journal_record_detail(
            journal,
            "external destination generation preserved; "
            f"{_journal_artifact_label(journal, plan.basename)}; "
            "original quarantine "
            f"{_journal_artifact_label(journal, journal.original_quarantine)}",
        )

    if stage.status != "ABSENT":
        cleaned = _cleanup_journal_artifact(
            journal,
            journal.stage_name,
            stage,
            marker=journal.stage_marker,
            identity=journal.stage_identity,
            kind="stage",
        )
        if not cleaned:
            journal.uncertain = True
    return None


def _rollback_prepared_mutation(
    mutation: _CommittedMutation,
    journal: Optional[_TransactionJournalEntry] = None,
) -> None:
    runtime = mutation.runtime
    pin = runtime.pin
    plan = runtime.plan
    target_label = _pinned_artifact_label(pin, plan.basename)
    if journal is None:
        journal = _new_transaction_journal(runtime)
        journal.mutation = mutation
    if plan.action == "create":
        quarantine = journal.rollback_quarantine
        quarantine_label = _pinned_artifact_label(pin, quarantine)
        move_error: Optional[BaseException] = None
        journal.phase = "ATTEMPT_ROLLBACK_CREATE_CLAIM"
        try:
            pin.move_entry_noreplace(
                plan.basename,
                quarantine,
                recovery=True,
            )
            journal.phase = "ROLLBACK_CREATE_CLAIMED"
        except BaseException as exc:
            move_error = exc
            try:
                source_info = pin.stat_entry(
                    plan.basename,
                    recovery=True,
                )
                quarantine_info = pin.stat_entry(
                    quarantine,
                    recovery=True,
                )
            except BaseException as inspect_exc:
                raise _TransactionRollbackConflict(
                    "rollback could not inspect create claim state for "
                    f"{target_label}: "
                    f"{_best_effort_exception_text(inspect_exc)}; "
                    "move error: "
                    f"{_best_effort_exception_text(exc)}"
                )
            if source_info is not None or quarantine_info is None:
                raise _TransactionRollbackConflict(
                    "rollback could not claim created generation for "
                    f"{target_label}: {_best_effort_exception_text(exc)}; "
                    f"recovery {quarantine_label}"
                )

        try:
            current = _read_pinned_entry(
                pin,
                quarantine,
                max(plan.max_bytes, mutation.committed_size),
                capture_data=False,
                recovery=True,
            )
        except BaseException as exc:
            raise _TransactionRollbackConflict(
                "rollback could not inspect quarantined create "
                f"{quarantine_label}: {_best_effort_exception_text(exc)}; "
                "original move error: "
                f"{_best_effort_exception_text(move_error) if move_error is not None else 'none'}"
            )
        if not _mutation_matches_snapshot(mutation, current):
            recovered, details = _restore_claimed_entry(
                pin,
                quarantine,
                plan.basename,
                current,
                journal=journal,
                phase_prefix="ROLLBACK_CREATE_CONFLICT_RESTORE",
            )
            recovery = (
                "; ".join(details)
                if details
                else "external generation was restored"
            )
            if not recovered:
                recovery = (
                    "external generation retained as "
                    f"{quarantine_label}; {recovery}"
                )
            raise _TransactionRollbackConflict(
                f"rollback conflict for {plan.path}: {recovery}"
            )

        cleanup = _cleanup_owned_pinned_entry(
            pin,
            quarantine,
            current,
            journal=journal,
            phase_prefix="ROLLBACK_CREATE_CLEANUP",
        )
        if cleanup:
            raise SafeEditError("; ".join(cleanup))
        journal.phase = "ATTEMPT_ROLLBACK_CREATE_SYNC"
        try:
            pin.fsync(recovery=True)
            journal.phase = "ROLLBACK_CREATE_SYNCED"
        except BaseException as exc:
            raise SafeEditError(
                "directory sync after create rollback failed for "
                f"{target_label}: {_best_effort_exception_text(exc)}"
            )
        journal.phase = "ROLLED_BACK"
        journal.rolled_back = True
        return

    original = runtime.snapshot
    assert original is not None
    original_quarantine = mutation.original_quarantine
    if not original_quarantine:
        raise _TransactionRollbackConflict(
            "rollback lacks the original-generation quarantine for "
            f"{plan.path}"
        )
    original_quarantine_label = _pinned_artifact_label(
        pin,
        original_quarantine,
    )
    try:
        if pin.stat_entry(
            original_quarantine,
            recovery=True,
        ) is None:
            raise _TransactionRollbackConflict(
                "rollback original-generation quarantine disappeared: "
                f"{original_quarantine_label}"
            )
        quarantined_original = _read_pinned_entry(
            pin,
            original_quarantine,
            max(plan.max_bytes, original.size),
            capture_data=False,
            recovery=True,
        )
    except _TransactionRollbackConflict:
        raise
    except BaseException as exc:
        raise _TransactionRollbackConflict(
            "rollback could not inspect original-generation quarantine "
            f"{original_quarantine_label}: "
            f"{_best_effort_exception_text(exc)}"
        )
    if not _snapshot_matches_marker(
        quarantined_original,
        original.identity,
        original.sha256,
        original.mode,
        original.size,
        original.mtime_ns,
    ):
        raise _TransactionRollbackConflict(
            "rollback original-generation quarantine changed: "
            f"{original_quarantine_label}"
        )

    output_quarantine = journal.rollback_quarantine
    output_quarantine_label = _pinned_artifact_label(
        pin,
        output_quarantine,
    )
    journal.phase = "ATTEMPT_ROLLBACK_OUTPUT_CLAIM"
    try:
        pin.move_entry_noreplace(
            plan.basename,
            output_quarantine,
            recovery=True,
        )
        journal.phase = "ROLLBACK_OUTPUT_CLAIMED"
    except BaseException as exc:
        try:
            source_info = pin.stat_entry(
                plan.basename,
                recovery=True,
            )
            quarantine_info = pin.stat_entry(
                output_quarantine,
                recovery=True,
            )
        except BaseException as inspect_exc:
            raise _TransactionRollbackConflict(
                "rollback could not inspect committed-generation claim for "
                f"{target_label}: "
                f"{_best_effort_exception_text(inspect_exc)}; move error: "
                f"{_best_effort_exception_text(exc)}"
            )
        if source_info is not None or quarantine_info is None:
            raise _TransactionRollbackConflict(
                "rollback could not claim committed generation for "
                f"{target_label}: {_best_effort_exception_text(exc)}; "
                f"original retained as {original_quarantine_label}"
            )

    try:
        committed = _read_pinned_entry(
            pin,
            output_quarantine,
            max(plan.max_bytes, mutation.committed_size),
            capture_data=False,
            recovery=True,
        )
    except BaseException as exc:
        raise _TransactionRollbackConflict(
            "rollback could not inspect committed-generation quarantine "
            f"{output_quarantine_label}: "
            f"{_best_effort_exception_text(exc)}; original retained as "
            f"{original_quarantine_label}"
        )
    if not _mutation_matches_snapshot(mutation, committed):
        recovered, details = _restore_claimed_entry(
            pin,
            output_quarantine,
            plan.basename,
            committed,
            journal=journal,
            phase_prefix="ROLLBACK_OUTPUT_CONFLICT_RESTORE",
        )
        recovery = (
            "; ".join(details)
            if details
            else "external generation was restored"
        )
        if not recovered:
            recovery = (
                "external generation retained as "
                f"{output_quarantine_label}; {recovery}"
            )
        raise _TransactionRollbackConflict(
            f"rollback conflict for {target_label}: {recovery}; "
            f"original retained as {original_quarantine_label}"
        )

    install_error: Optional[BaseException] = None
    journal.phase = "ATTEMPT_ROLLBACK_RESTORE"
    try:
        pin.move_entry_noreplace(
            original_quarantine,
            plan.basename,
            recovery=True,
        )
        journal.phase = "ROLLBACK_RESTORED"
    except BaseException as exc:
        install_error = exc
        try:
            original_info = pin.stat_entry(
                original_quarantine,
                recovery=True,
            )
            target_info = pin.stat_entry(
                plan.basename,
                recovery=True,
            )
        except BaseException as inspect_exc:
            raise _TransactionRollbackConflict(
                "rollback could not inspect original restore state for "
                f"{target_label}: "
                f"{_best_effort_exception_text(inspect_exc)}; "
                f"original retained as {original_quarantine_label}; "
                "committed generation retained as "
                f"{output_quarantine_label}"
            )
        restored_despite_error = False
        if original_info is None and target_info is not None:
            try:
                restored = _read_pinned_entry(
                    pin,
                    plan.basename,
                    max(plan.max_bytes, original.size),
                    capture_data=False,
                    recovery=True,
                )
                restored_despite_error = _snapshot_matches_marker(
                    restored,
                    original.identity,
                    original.sha256,
                    original.mode,
                    original.size,
                    original.mtime_ns,
                )
            except BaseException:
                restored_despite_error = False
        if not restored_despite_error:
            recovered = False
            details: Tuple[str, ...] = ()
            if target_info is None:
                recovered, details = _restore_claimed_entry(
                    pin,
                    output_quarantine,
                    plan.basename,
                    committed,
                    journal=journal,
                    phase_prefix="ROLLBACK_COMMITTED_RECOVERY_RESTORE",
                )
            detail = "; ".join(details)
            if recovered:
                detail = (
                    "committed generation restored after original "
                    f"restore failed; {detail}"
                )
            else:
                detail = (
                    f"original retained as {original_quarantine_label}; "
                    "committed generation retained as "
                    f"{output_quarantine_label}; {detail}"
                )
            raise _TransactionRollbackConflict(
                "rollback could not restore original generation for "
                f"{target_label}: {_best_effort_exception_text(exc)}; "
                f"{detail}"
            )

    try:
        restored = _read_pinned_entry(
            pin,
            plan.basename,
            max(plan.max_bytes, original.size),
            capture_data=False,
            recovery=True,
        )
    except BaseException as exc:
        raise _TransactionRollbackConflict(
            "rollback restored original path but could not verify "
            f"{target_label}: {_best_effort_exception_text(exc)}; "
            "committed generation retained as "
            f"{output_quarantine_label}; move error: "
            f"{_best_effort_exception_text(install_error) if install_error is not None else 'none'}"
        )
    if not _snapshot_matches_marker(
        restored,
        original.identity,
        original.sha256,
        original.mode,
        original.size,
        original.mtime_ns,
    ):
        raise _TransactionRollbackConflict(
            "rollback original generation changed before verification for "
            f"{target_label}; committed generation retained as "
            f"{output_quarantine_label}"
        )

    cleanup = _cleanup_owned_pinned_entry(
        pin,
        output_quarantine,
        committed,
        journal=journal,
        phase_prefix="ROLLBACK_OUTPUT_CLEANUP",
    )
    if cleanup:
        raise SafeEditError("; ".join(cleanup))
    journal.phase = "ATTEMPT_ROLLBACK_EDIT_SYNC"
    try:
        pin.fsync(recovery=True)
        journal.phase = "ROLLBACK_EDIT_SYNCED"
    except BaseException as exc:
        raise SafeEditError(
            "directory sync after edit rollback failed for "
            f"{target_label}: {_best_effort_exception_text(exc)}"
        )
    journal.phase = "ROLLED_BACK"
    journal.rolled_back = True



def _mutation_still_present(
    mutation: _CommittedMutation,
) -> Optional[bool]:
    try:
        current = mutation.runtime.pin.stat_entry(
            mutation.runtime.plan.basename,
            recovery=True,
        )
        if current is None:
            return False
        snapshot = _read_pinned_entry(
            mutation.runtime.pin,
            mutation.runtime.plan.basename,
            max(
                mutation.runtime.plan.max_bytes,
                len(mutation.runtime.plan.output),
            ),
            capture_data=False,
            recovery=True,
        )
    except BaseException:
        return None
    return _mutation_matches_snapshot(mutation, snapshot)




def _set_transaction_error_state(
    exc: BaseException,
    *,
    written: bool,
    rolled_back: bool,
    partial_write: bool,
    rollback_conflict: bool,
    rollback_errors: Iterable[str],
) -> None:
    try:
        errors = tuple(rollback_errors)
    except BaseException:
        errors = ()
    values = (
        ("_transaction_written", written),
        ("_transaction_rolled_back", rolled_back),
        ("_transaction_partial_write", partial_write),
        ("_transaction_rollback_conflict", rollback_conflict),
        ("_transaction_rollback_errors", errors),
    )
    for name, value in values:
        try:
            setattr(exc, name, value)
        except BaseException:
            pass


def _new_transaction_journal(
    runtime: _PinnedPreparedPlan,
) -> _TransactionJournalEntry:
    plan = runtime.plan
    original = runtime.snapshot
    intended_mode = (
        original.mode
        if original is not None
        else 0o666
    )
    return _TransactionJournalEntry(
        runtime=runtime,
        stage_name=_new_transaction_stage_name(),
        original_quarantine=(
            _new_transaction_stage_name()
            if plan.action == "edit"
            else ""
        ),
        rollback_quarantine=_new_transaction_stage_name(),
        intended_sha256=plan.output_sha256,
        intended_size=len(plan.output),
        intended_mode=intended_mode,
        intended_mtime_ns=None,
    )


def commit_prepared_transaction(
    prepared: PreparedTransaction,
) -> Dict[str, Any]:
    """Commit an immutable plan through pinned parent directories."""
    if not isinstance(prepared, PreparedTransaction):
        fail("commit requires a PreparedTransaction")

    lock_context = (
        NullLock()
        if prepared.no_lock
        else FileLockSet(
            (Path(path) for path in prepared.lock_paths),
            prepared.lock_timeout,
            prepared.lock_stale_seconds,
        )
    )
    if isinstance(lock_context, FileLockSet):
        lock_context.suppress_exit_errors = True
    parent_context = _PreparedParentPins(
        prepared.plans,
        suppress_exit_errors=True,
    )
    results: List[Dict[str, Any]] = []
    mutations: List[_CommittedMutation] = []
    journals: List[_TransactionJournalEntry] = []
    file_index = 0
    current_plan: Optional[PreparedFilePlan] = None
    current_runtime: Optional[_PinnedPreparedPlan] = None
    mutation_uncertain = False
    commit_cleanup_warnings: List[str] = []


    def mark_late_failure(exc: BaseException) -> None:
        committed = any(
            journal.mutation is not None
            and not journal.rolled_back
            for journal in journals
        )
        namespace_changed = committed or any(
            journal.namespace_changed
            for journal in journals
        )
        _set_transaction_error_state(
            exc,
            written=committed,
            rolled_back=False,
            partial_write=namespace_changed,
            rollback_conflict=False,
            rollback_errors=(
                _best_effort_exception_text(exc),
            ),
        )
        try:
            details = [_best_effort_exception_text(exc)]
            details.extend(commit_cleanup_warnings)
            details.extend(parent_context.cleanup_errors)
            details.extend(
                getattr(lock_context, "exit_errors", ())
            )
            details.extend(
                detail
                for journal in journals
                for detail in journal.cleanup_errors
            )
            _set_transaction_error_state(
                exc,
                written=committed,
                rolled_back=False,
                partial_write=namespace_changed,
                rollback_conflict=False,
                rollback_errors=details,
            )
        except BaseException:
            pass
        try:
            setattr(exc, "_diagnostic_command", "transaction")
            if current_plan is not None:
                setattr(exc, "_diagnostic_file", current_plan.path)
                setattr(
                    exc,
                    "_diagnostic_file_index",
                    file_index,
                )
        except BaseException:
            pass

    parent_context.late_failure_handler = mark_late_failure
    if isinstance(lock_context, FileLockSet):
        lock_context.late_failure_handler = mark_late_failure

    with lock_context:
        with parent_context as parent_pins:
            try:
                first_validation: List[_PinnedPreparedPlan] = []
                for file_index, plan in enumerate(
                    prepared.plans,
                    start=1,
                ):
                    current_plan = plan
                    current_runtime = None
                    first_validation.append(
                        _validate_prepared_plan(
                            plan,
                            parent_pins.for_plan(plan),
                        )
                    )

                _transaction_before_mutations(
                    tuple(first_validation)
                )

                validated: List[_PinnedPreparedPlan] = []
                for file_index, runtime in enumerate(
                    first_validation,
                    start=1,
                ):
                    current_plan = runtime.plan
                    current_runtime = None
                    validated.append(
                        _revalidate_prepared_checkpoint(runtime)
                    )

                for file_index, runtime in enumerate(
                    validated,
                    start=1,
                ):
                    plan = runtime.plan
                    current_plan = plan
                    current_runtime = runtime
                    result = _prepared_file_summary(plan)
                    result["dryRun"] = False

                    if plan.action == "create":
                        journal = _new_transaction_journal(runtime)
                        journals.append(journal)
                        snapshot = _create_pinned_output(journal)
                        mutation = journal.mutation
                        if mutation is None:
                            fail(
                                "transaction helper returned without a "
                                f"committed journal marker: {plan.path}"
                            )
                        mutations.append(mutation)
                        if snapshot.sha256 != plan.output_sha256:
                            raise SafeEditError(
                                "post-write verification failed: "
                                "created bytes do not match intended output"
                            )
                        _transaction_after_mutation(
                            mutation,
                            file_index,
                        )
                        result["written"] = True
                        result["created"] = True
                    else:
                        original = runtime.snapshot
                        assert original is not None
                        if (
                            original.sha256 == plan.output_sha256
                            and not plan.force_write
                        ):
                            _assert_pinned_snapshot_unchanged(runtime)
                            result["skipped"] = True
                        else:
                            journal = _new_transaction_journal(runtime)
                            journals.append(journal)
                            (
                                snapshot,
                                _original_quarantine,
                            ) = _replace_pinned_bytes(
                                journal,
                                plan.output,
                                original.identity,
                                original.sha256,
                                original.mode,
                                original.size,
                                original.mtime_ns,
                                plan.output_sha256,
                                original.mode,
                                None,
                            )
                            mutation = journal.mutation
                            if mutation is None:
                                fail(
                                    "transaction helper returned without a "
                                    f"committed journal marker: {plan.path}"
                                )
                            mutations.append(mutation)
                            if snapshot.sha256 != plan.output_sha256:
                                raise SafeEditError(
                                    "post-write verification failed: "
                                    "edited bytes do not match intended "
                                    "output"
                                )
                            _transaction_after_mutation(
                                mutation,
                                file_index,
                            )
                            result["written"] = True

                    result["sha256"] = plan.output_sha256
                    results.append(result)

                for runtime in validated:
                    runtime.pin.validate()
                for mutation in mutations:
                    original_quarantine = (
                        mutation.original_quarantine
                    )
                    original = mutation.runtime.snapshot
                    if not original_quarantine or original is None:
                        continue
                    owner = next(
                        (
                            journal
                            for journal in journals
                            if journal.mutation is mutation
                        ),
                        None,
                    )
                    if owner is not None:
                        owner.phase = "ATTEMPT_FINALIZE"
                    cleanup = _cleanup_owned_pinned_entry(
                        mutation.runtime.pin,
                        original_quarantine,
                        original,
                        journal=owner,
                        phase_prefix="FINALIZE_CLEANUP",
                        propagate_control=True,
                    )
                    commit_cleanup_warnings.extend(cleanup)
                    try:
                        remaining_original = (
                            mutation.runtime.pin.stat_entry(
                                original_quarantine,
                                recovery=True,
                            )
                        )
                    except BaseException as inspect_exc:
                        if _preserve_transaction_exception(inspect_exc):
                            raise
                        if owner is not None:
                            owner.uncertain = True
                            owner.phase = "FINALIZE_INSPECTION_WARNING"
                        commit_cleanup_warnings.append(
                            "original quarantine final state is unknown; "
                            f"{_pinned_artifact_label(mutation.runtime.pin, original_quarantine)}: "
                            f"{_best_effort_exception_text(inspect_exc)}"
                        )
                    else:
                        if remaining_original is None and owner is not None:
                            owner.finalized = True
                            owner.phase = "FINALIZED"
                    if owner is not None:
                        owner.phase = "ATTEMPT_FINALIZE_SYNC"
                    try:
                        mutation.runtime.pin.fsync(recovery=True)
                    except BaseException as exc:
                        if _preserve_transaction_exception(exc):
                            raise
                        if owner is not None:
                            owner.phase = "FINALIZE_SYNC_WARNING"
                        commit_cleanup_warnings.append(
                            "directory sync after original quarantine "
                            f"cleanup failed for {_pinned_artifact_label(mutation.runtime.pin, mutation.runtime.plan.basename)}: "
                            f"{_best_effort_exception_text(exc)}"
                        )
                    else:
                        if owner is not None:
                            owner.phase = "FINALIZED_SYNCED"
            except BaseException as original_exc:
                propagate_original = _preserve_transaction_exception(
                    original_exc
                )
                if propagate_original:
                    cause = original_exc
                elif isinstance(original_exc, SafeEditError):
                    cause = original_exc
                else:
                    cause = SafeEditError(
                        "unexpected transaction failure: "
                        + _best_effort_exception_text(original_exc)
                    )

                try:
                    if not hasattr(cause, "_diagnostic_file_index"):
                        setattr(
                            cause,
                            "_diagnostic_file_index",
                            file_index,
                        )
                    if (
                        not hasattr(cause, "_diagnostic_file")
                        and current_plan is not None
                    ):
                        setattr(
                            cause,
                            "_diagnostic_file",
                            current_plan.path,
                        )
                    if not hasattr(cause, "_diagnostic_command"):
                        setattr(
                            cause,
                            "_diagnostic_command",
                            "transaction",
                        )
                except BaseException:
                    pass

                try:
                    publish_conflict = (
                        isinstance(
                            original_exc,
                            _TransactionRollbackConflict,
                        )
                        or bool(
                            getattr(
                                original_exc,
                                "_transaction_publish_conflict",
                                False,
                            )
                        )
                    )
                except BaseException:
                    publish_conflict = False

                rollback_failures: List[str] = []
                try:
                    rollback_failures.extend(
                        getattr(
                            original_exc,
                            "_transaction_cleanup_errors",
                            (),
                        )
                    )
                except BaseException:
                    pass

                rollback_order: List[_CommittedMutation] = []
                finalized_mutations: Set[int] = set()
                seen_mutations: Set[int] = set()
                reconciled_install = False
                finalization_phases = {
                    "ATTEMPT_FINALIZE",
                    "FINALIZE_CLEANUP_RETURNED",
                    "FINALIZE_INSPECTION_WARNING",
                    "FINALIZED",
                    "ATTEMPT_FINALIZE_SYNC",
                    "FINALIZE_SYNC_WARNING",
                    "FINALIZED_SYNCED",
                }
                for journal in reversed(journals):
                    prior_phase = journal.phase
                    mutation: Optional[_CommittedMutation]
                    is_finalization_phase = (
                        prior_phase in finalization_phases
                        or prior_phase.startswith(
                            "ATTEMPT_FINALIZE_CLEANUP_"
                        )
                        or prior_phase.startswith("FINALIZE_CLEANUP_")
                    )
                    if (
                        journal.mutation is not None
                        and is_finalization_phase
                    ):
                        try:
                            quarantine_probe = _probe_journal_artifact(
                                journal,
                                journal.original_quarantine,
                            )
                        except BaseException as probe_exc:
                            journal.finalized = True
                            journal.namespace_changed = True
                            journal.uncertain = True
                            journal.phase = "FINALIZED_AFTER_ERROR"
                            _journal_record_detail(
                                journal,
                                "finalization state inspection failed for "
                                f"{_journal_artifact_label(journal, journal.original_quarantine)}: "
                                f"{_best_effort_exception_text(probe_exc)}",
                            )
                            mutation = journal.mutation
                        else:
                            original = journal.runtime.snapshot
                            if quarantine_probe.status == "ABSENT":
                                journal.finalized = True
                            elif (
                                quarantine_probe.status == "UNKNOWN"
                                or not _journal_probe_matches_marker(
                                    quarantine_probe,
                                    original,
                                )
                            ):
                                journal.finalized = True
                                journal.uncertain = True
                                detail = (
                                    quarantine_probe.detail
                                    or "finalization retained an unverified "
                                    f"{_journal_artifact_label(journal, journal.original_quarantine)}"
                                )
                                _journal_record_detail(journal, detail)
                            if journal.finalized:
                                journal.namespace_changed = True
                                journal.uncertain = True
                                journal.phase = "FINALIZED_AFTER_ERROR"
                            mutation = journal.mutation
                    else:
                        try:
                            mutation = _reconcile_transaction_journal(
                                journal
                            )
                        except BaseException as reconciliation_exc:
                            journal.uncertain = True
                            detail = (
                                "journal reconciliation failed for "
                                f"{_journal_artifact_label(journal, journal.runtime.plan.basename)}: "
                                f"{_best_effort_exception_text(reconciliation_exc)}"
                            )
                            _journal_record_detail(journal, detail)
                            mutation = journal.mutation
                    if (
                        prior_phase == "ATTEMPT_INSTALL"
                        and mutation is not None
                    ):
                        reconciled_install = True
                    if mutation is None or id(mutation) in seen_mutations:
                        continue
                    seen_mutations.add(id(mutation))
                    if journal.finalized:
                        finalized_mutations.add(id(mutation))
                    else:
                        rollback_order.append(mutation)

                for mutation in reversed(mutations):
                    if id(mutation) in seen_mutations:
                        continue
                    seen_mutations.add(id(mutation))
                    rollback_order.append(mutation)

                if reconciled_install:
                    publish_conflict = False
                rollback_conflicts: List[str] = []
                if publish_conflict:
                    try:
                        rollback_conflicts.append(str(original_exc))
                    except BaseException:
                        rollback_conflicts.append(
                            "transaction publish conflict"
                        )

                journal_by_mutation = {
                    id(journal.mutation): journal
                    for journal in journals
                    if journal.mutation is not None
                }
                rolled_back_ids: Set[int] = set()
                rollback_incomplete = False
                for mutation in rollback_order:
                    owner = journal_by_mutation.get(id(mutation))
                    try:
                        _rollback_prepared_mutation(mutation, owner)
                    except _TransactionRollbackConflict as conflict:
                        rollback_incomplete = True
                        if owner is not None:
                            owner.uncertain = True
                            owner.phase = "ROLLBACK_CONFLICT"
                        rollback_conflicts.append(
                            _best_effort_exception_text(conflict)
                        )
                    except BaseException as rollback_exc:
                        rollback_incomplete = True
                        if owner is not None:
                            owner.uncertain = True
                            owner.phase = "ROLLBACK_FAILED"
                        rollback_failures.append(
                            f"{mutation.runtime.plan.path}: "
                            f"{_best_effort_exception_text(rollback_exc)}"
                        )
                    else:
                        rolled_back_ids.add(id(mutation))
                        if owner is not None:
                            owner.rolled_back = True
                            owner.phase = "ROLLED_BACK"

                remaining_written = any(
                    journal.write_uncertain for journal in journals
                )
                for mutation_id in finalized_mutations:
                    owner = journal_by_mutation.get(mutation_id)
                    if owner is None or owner.mutation is None:
                        remaining_written = True
                        continue
                    present = _mutation_still_present(owner.mutation)
                    if present is not False:
                        remaining_written = True
                    if present is None:
                        owner.uncertain = True
                for mutation in rollback_order:
                    if id(mutation) in rolled_back_ids:
                        continue
                    present = _mutation_still_present(mutation)
                    if present is not False:
                        remaining_written = True
                    if present is None:
                        owner = journal_by_mutation.get(id(mutation))
                        if owner is not None:
                            owner.uncertain = True

                for journal in journals:
                    rollback_failures.extend(journal.cleanup_errors)

                had_mutation = (
                    bool(rollback_order)
                    or bool(finalized_mutations)
                    or any(
                        journal.namespace_changed
                        for journal in journals
                    )
                )
                recovery_uncertain = (
                    rollback_incomplete
                    or any(journal.uncertain for journal in journals)
                )
                partial_write = (
                    had_mutation
                    and (
                        remaining_written
                        or recovery_uncertain
                    )
                )
                rolled_back = (
                    had_mutation
                    and not partial_write
                    and (
                        bool(rolled_back_ids)
                        or any(
                            journal.rolled_back
                            for journal in journals
                        )
                    )
                )
                rollback_details = (
                    rollback_conflicts + rollback_failures
                )
                _set_transaction_error_state(
                    cause,
                    written=remaining_written,
                    rolled_back=rolled_back,
                    partial_write=partial_write,
                    rollback_conflict=bool(rollback_conflicts),
                    rollback_errors=rollback_details,
                )

                if propagate_original:
                    raise
                if rolled_back:
                    message = f"transaction rolled back: {cause}"
                elif partial_write:
                    detail = (
                        "; ".join(rollback_details)
                        if rollback_details
                        else "mutation state could not be verified"
                    )
                    message = (
                        f"transaction failed ({cause}); "
                        f"rollback incomplete: {detail}"
                    )
                else:
                    message = (
                        "transaction prevalidation failed: "
                        f"{cause}"
                    )
                _fail_preserving_diagnostics(message, cause)

    try:
        _transaction_before_response()
        response: Dict[str, Any] = {
            "ok": True,
            "command": "transaction",
            "file": None,
            "files": results,
            "fileCount": len(results),
            "dryRun": False,
            "written": (
                any(
                    journal.mutation is not None
                    and not journal.rolled_back
                    for journal in journals
                )
                or any(item.get("written") for item in results)
            ),
            "rolledBack": False,
            "atomicity": "prevalidated-with-rollback",
            "crashAtomic": False,
        }
        cleanup_warnings = (
            commit_cleanup_warnings
            + list(parent_context.cleanup_errors)
            + list(getattr(lock_context, "exit_errors", ()))
        )
        if cleanup_warnings:
            response["cleanupWarnings"] = cleanup_warnings
        return response
    except BaseException as response_exc:
        mark_late_failure(response_exc)
        raise

def run_transaction_payload(
    args: argparse.Namespace,
    payload: Any,
) -> Dict[str, Any]:
    args._prepared_transaction = None
    prepared = prepare_transaction(args, payload)
    if args.dry_run:
        args._prepared_transaction = prepared
        return _prepared_preview_summary(prepared)
    return commit_prepared_transaction(prepared)
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely edit text files.")
    parser.add_argument(
        "command",
        choices=(
            "inspect",
            "stat",
            "stat-many",
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
    parser.add_argument(
        "--auto-eol-match",
        dest="auto_eol_match",
        action="store_true",
        default=None,
        help="automatically match multiline targets using the detected file EOL",
    )
    parser.add_argument(
        "--no-auto-eol-match",
        dest="auto_eol_match",
        action="store_false",
        help="disable transaction-default EOL-compatible matching",
    )
    parser.add_argument("--fuzzy", action="store_true",
                        help="enable fuzzy matching as last resort (requires --auto-match, similarity >= 0.6)")
    parser.add_argument(
        "--fuzzy-workers",
        type=parse_fuzzy_workers,
        default="auto",
        metavar="auto|N",
        help=(
            "fuzzy process limit: auto uses low-priority workers only for "
            "large CPU-heavy searches; 1 disables multiprocessing; N is 2-8"
        ),
    )
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
                        help='URL-safe or standard Base64 containing a UTF-8 structured request')
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
    cap = _cached_fs_capability(args, str(path))
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
            _fail_sha256_mismatch(
                "SHA-256 mismatch: "
                f"expected {expected}, actual {actual}; run stat again before removing",
                expected,
                actual,
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

    cap = _cached_fs_capability(args, str(path))
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
    result_sha256 = hashlib.sha256(output).hexdigest()
    diff_text, diff_mode, diff_truncated = build_diff_preview(
        path, "", new_text, args
    )

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
            "fuzzyWorkers": getattr(args, "fuzzy_workers", "auto"),
            "ignoreIndent": False,
            "ignoreEol": False,
            "normalizeWhitespace": False,
        },
        "dryRun": args.dry_run,
        "backup": None,
        "written": False,
        "created": False,
        "skipped": False,
        "sizeBytes": len(output),
        "resultSha256": result_sha256,
        "wouldChangeBytes": True,
        "wouldCreate": True,
    }
    if warnings:
        summary["warnings"] = warnings
    if args.diff:
        summary["diff"] = diff_text
        summary["diffMode"] = diff_mode
        summary["diffTruncated"] = diff_truncated

    if getattr(args, "_capture_transaction_plan", False):
        args._transaction_plan = {
            "action": "create",
            "path": path,
            "output": output,
            "outputSha256": result_sha256,
            "inputSizeBytes": len(output),
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
        if not _compare_file_bytes_strict(path, output):
            fail("post-write verification failed: bytes on disk do not match intended output")
        summary["written"] = True
        summary["created"] = True
        summary["sha256"] = result_sha256
    return summary


def run(args: argparse.Namespace) -> Dict[str, Any]:
    for option_name in ("lock_timeout", "lock_stale_seconds"):
        value = float(getattr(args, option_name, 0.0))
        if not math.isfinite(value) or value < 0:
            option = "--" + option_name.replace("_", "-")
            fail(f"{option} must be a finite non-negative number")

    if args.command == "preflight":
        return run_preflight(args)
    if args.command == "stat-many":
        return run_stat_many(args)
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
                _fail_sha256_mismatch(
                    "SHA-256 mismatch: "
                    f"expected {expected}, actual {actual}; run stat again",
                    expected,
                    actual,
                )
        encoding, text = detect_and_decode(original, args.encoding)
        return inspect_target(path, original, encoding, text)
    if args.command == "stat":
        original = read_target(path, args.max_bytes)
        if args.expected_sha256:
            expected = args.expected_sha256.lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                fail("--expected-sha256 must contain exactly 64 hexadecimal characters")
            actual = hashlib.sha256(original).hexdigest()
            if actual != expected:
                _fail_sha256_mismatch(
                    "SHA-256 mismatch: "
                    f"expected {expected}, actual {actual}; run stat again",
                    expected,
                    actual,
                )
        encoding, text = detect_and_decode(original, args.encoding)
        capability = _cached_fs_capability(args, str(path))
        return stat_target(path, original, encoding, text, capability)

    # Sandbox capability detection
    cap = _cached_fs_capability(args, str(path))
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
        original_sha256: Optional[str] = None
        if args.expected_sha256:
            expected = args.expected_sha256.lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                fail("--expected-sha256 must contain exactly 64 hexadecimal characters")
            actual = hashlib.sha256(original).hexdigest()
            original_sha256 = actual
            if actual != expected:
                _fail_sha256_mismatch(
                    "SHA-256 mismatch: "
                    f"expected {expected}, actual {actual}; run stat again",
                    expected,
                    actual,
                )
        encoding, text = detect_and_decode(original, args.encoding)
        if "\x00" in text and not args.allow_nul:
            fail("decoded text contains NUL bytes; refusing likely binary content")

        newline_style, line_counts, mixed_line_endings = detect_line_ending(text)
        newline = line_sep(newline_style)

        explain = getattr(args, "explain_match_failure", False)
        ignore_indent = getattr(args, "ignore_indent", False)
        ignore_eol = getattr(args, "ignore_eol", False)
        normalize_whitespace = getattr(args, "normalize_whitespace", False)
        auto_match = getattr(args, "auto_match", False)
        auto_eol_match = bool(getattr(args, "auto_eol_match", False))
        fuzzy = getattr(args, "fuzzy", False)
        fuzzy_workers = getattr(args, "fuzzy_workers", "auto")
        context_before = getattr(args, "context_before", None)
        context_after = getattr(args, "context_after", None)
        try:
            new_text, operation_results = apply_operations(
                text,
                operations,
                newline,
                explain,
                ignore_indent,
                ignore_eol,
                normalize_whitespace,
                auto_match=auto_match,
                fuzzy=fuzzy,
                context_before=context_before,
                context_after=context_after,
                fuzzy_workers=fuzzy_workers,
                auto_eol_match=auto_eol_match,
            )
        except SafeEditError as exc:
            setattr(exc, "_diagnostic_text", text)
            setattr(exc, "_diagnostic_file", str(path))
            setattr(exc, "_diagnostic_command", args.command)
            if (
                len(operations) == 1
                and not hasattr(exc, "_diagnostic_operation")
            ):
                setattr(exc, "_diagnostic_operation", operations[0])
                setattr(exc, "_diagnostic_operation_index", 1)
            raise

        for operation_result in operation_results:
            if operation_result.get("reason") == "old_equals_new":
                warnings.append(
                    "operation "
                    f"{operation_result['index']} skipped: old and new are identical"
                )

        new_text = apply_post_transforms(new_text, args, newline)
        output_encoding = encoding_for_output(args.to_encoding, encoding)
        output = encode_text(new_text, output_encoding)
        if original_sha256 is None:
            original_sha256 = hashlib.sha256(original).hexdigest()
        result_sha256 = hashlib.sha256(output).hexdigest()
        output_line_ending, output_line_counts, output_mixed_line_endings = detect_line_ending(new_text)
        diff_text, diff_mode, diff_truncated = build_diff_preview(
            path, text, new_text, args
        )
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
                "autoEolMatch": auto_eol_match,
                "fuzzy": fuzzy,
                "fuzzyWorkers": fuzzy_workers,
                "ignoreIndent": ignore_indent,
                "ignoreEol": ignore_eol,
                "normalizeWhitespace": normalize_whitespace,
            },
            "dryRun": args.dry_run,
            "backup": None,
            "written": False,
            "skipped": False,
            "originalSha256": original_sha256,
            "resultSha256": result_sha256,
            "wouldChangeBytes": output != original,
        }
        if warnings:
            summary["warnings"] = warnings
        if args.diff:
            summary["diff"] = diff_text
            summary["diffMode"] = diff_mode
            summary["diffTruncated"] = diff_truncated

        if getattr(args, "_capture_transaction_plan", False):
            args._transaction_plan = {
                "action": "edit",
                "path": path,
                "originalSha256": original_sha256,
                "output": output,
                "outputSha256": result_sha256,
                "inputSizeBytes": len(original),
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
                if not _compare_file_bytes_strict(path, output):
                    fail("post-write verification failed: bytes on disk do not match intended output")
                summary["backup"] = backup
                summary["written"] = True
            summary["sha256"] = result_sha256
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
    if summary.get("command") == "stat-many":
        print(f"Stat-many: files={summary['fileCount']}")
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


def _match_error_context(
    exc: SafeEditError,
    args: argparse.Namespace,
    file_path: str,
    command: str,
) -> Tuple[str, str]:
    diagnostic_text = getattr(exc, "_diagnostic_text", None)
    diagnostic_operation = getattr(exc, "_diagnostic_operation", None)
    if diagnostic_text is not None:
        old = ""
        if isinstance(diagnostic_operation, dict):
            operation_name = str(
                diagnostic_operation.get("op") or command
            ).replace("_", "-")
            key = "old" if operation_name == "edit" else "pattern"
            value = diagnostic_operation.get(key)
            old = "" if value is None else str(value)
        return old, diagnostic_text

    old = ""
    text = ""
    try:
        path = resolve_target_path(
            file_path, getattr(args, "follow_symlink", False)
        )
        original = read_target(
            path, getattr(args, "max_bytes", 50 * 1024 * 1024)
        )
        _encoding, text = detect_and_decode(
            original, getattr(args, "encoding", "auto")
        )

        if command == "edit":
            try:
                stdin_taken: List[str] = []
                old = resolve_cli_value(
                    args, "old", False, stdin_taken=stdin_taken
                ) or ""
            except Exception:
                old = getattr(args, "old", "") or ""
        elif command == "regex":
            try:
                stdin_taken = []
                old = resolve_cli_value(
                    args, "pattern", False, stdin_taken=stdin_taken
                ) or ""
            except Exception:
                old = getattr(args, "pattern", "") or ""
    except Exception:
        pass
    return old, text


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
                old, text = _match_error_context(
                    exc,
                    args,
                    file_path,
                    command,
                )

            elif error_type == "lock_error":
                # Try to read lock file for structured recovery info
                try:
                    target_name = Path(file_path).name if file_path else ""
                    lock_key = _get_lock_key(file_path)
                    lock_path = _get_lock_dir() / f"{lock_key}.lock"
                    snapshot = _read_lock_snapshot(lock_path)
                    pid = None
                    lock_time = None
                    if snapshot is not None and snapshot.complete:
                        pid = snapshot.pid
                        try:
                            content = snapshot.payload.decode(
                                "utf-8", errors="strict"
                            )
                        except UnicodeDecodeError:
                            content = ""
                        for field in content.split():
                            if field.startswith("time="):
                                try:
                                    candidate = float(
                                        field.split("=", 1)[1]
                                    )
                                except ValueError:
                                    continue
                                if math.isfinite(candidate):
                                    lock_time = candidate
                                break
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
