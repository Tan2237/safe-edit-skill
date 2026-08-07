#!/usr/bin/env python3
"""Deterministic performance benchmark for safe_edit.py."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "skills" / "safe-edit" / "safe_edit.py"
sys.path.insert(0, str(SCRIPT.parent))

import safe_edit as se  # noqa: E402


def repeat_to_size(unit: str, size: int, suffix: str = "") -> str:
    if size < len(suffix):
        raise ValueError("size is smaller than suffix")
    body_size = size - len(suffix)
    count, remainder = divmod(body_size, len(unit))
    return unit * count + unit[:remainder] + suffix


def percentile(values: List[float], ratio: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * ratio) - 1))
    return ordered[index]


def measure(
    name: str,
    fn: Callable[[], Any],
    *,
    input_bytes: int,
    iterations: int,
    warmups: int,
    validate: Callable[[Any], None],
    trace_memory: bool,
    setup: Optional[Callable[[], None]] = None,
) -> Dict[str, Any]:
    for _ in range(warmups):
        if setup is not None:
            setup()
        output = fn()
        validate(output)
        del output

    samples: List[float] = []
    for _ in range(iterations):
        gc.collect()
        if setup is not None:
            setup()
        started = time.perf_counter_ns()
        output = fn()
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        samples.append(elapsed_ms)
        validate(output)
        del output
    median_ms = statistics.median(samples)
    result: Dict[str, Any] = {
        "name": name,
        "inputBytes": input_bytes,
        "iterations": iterations,
        "minMs": min(samples),
        "medianMs": median_ms,
        "p95Ms": percentile(samples, 0.95),
        "p95Method": "nearest-rank-sample",
        "maxMs": max(samples),
        "throughputMiBPerSecond": (
            input_bytes / 1024 / 1024 / (median_ms / 1000)
            if input_bytes and median_ms
            else None
        ),
    }

    if trace_memory:
        gc.collect()
        if setup is not None:
            setup()
        tracemalloc.start()
        try:
            output = fn()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        validate(output)
        del output
        result["peakTracedMiB"] = peak / 1024 / 1024
    return result


def run_cli(command: str, path: Path) -> Dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            command,
            "--file",
            str(path),
            "--json",
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{command} failed ({completed.returncode}): "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        )
    return json.loads(completed.stdout)


def validate_cli(value: Dict[str, Any]) -> None:
    if not value.get("ok") or value.get("written"):
        raise AssertionError(value)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def add_trim_size_cases(
    results: List[Dict[str, Any]],
    size: int,
    args: argparse.Namespace,
) -> None:
    measure_options = {
        "iterations": args.iterations,
        "warmups": args.warmups,
        "trace_memory": args.trace_memory,
    }

    short_line = "value   \r\n"
    short_line_count = size // len(short_line)
    short_remainder = size - short_line_count * len(short_line)
    short_tail_text = short_line * short_line_count + "x" * short_remainder
    short_expected_size = (
        short_line_count * len("value\r\n") + short_remainder
    )

    def validate_short_tails(value: str) -> None:
        if (
            len(value) != short_expected_size
            or value.count("value\r\n") != short_line_count
            or "   \r\n" in value
        ):
            raise AssertionError("short trailing whitespace was not trimmed")

    results.append(
        measure(
            "core.trim-trailing-whitespace",
            lambda: se.trim_trailing_whitespace(short_tail_text),
            input_bytes=len(short_tail_text),
            validate=validate_short_tails,
            **measure_options,
        )
    )
    del short_tail_text

    interior_suffix = "right\ntrim me  \n"
    interior_run_size = size - len("left") - len(interior_suffix)
    if interior_run_size < 1:
        raise AssertionError("trim benchmark size is too small")
    interior_text = (
        "left" + (" " * interior_run_size) + interior_suffix
    )

    def validate_long_interior(value: str) -> None:
        if (
            len(value) != size - 2
            or not value.startswith("left")
            or not value.endswith("right\ntrim me\n")
            or value.find("right\n") != len("left") + interior_run_size
            or value.count(" ") != interior_run_size + 1
        ):
            raise AssertionError("interior whitespace changed during trim")

    results.append(
        measure(
            "core.trim-long-interior-short-tail",
            lambda: se.trim_trailing_whitespace(interior_text),
            input_bytes=len(interior_text),
            validate=validate_long_interior,
            **measure_options,
        )
    )
    del interior_text

    long_tail_size = size - len("value\n")
    long_tail_text = "value" + (" " * long_tail_size) + "\n"
    results.append(
        measure(
            "core.trim-single-long-tail-run",
            lambda: se.trim_trailing_whitespace(long_tail_text),
            input_bytes=len(long_tail_text),
            validate=lambda value: (
                None
                if value == "value\n"
                else (_ for _ in ()).throw(AssertionError(value[-32:]))
            ),
            **measure_options,
        )
    )


def add_prepared_transaction_cases(
    results: List[Dict[str, Any]],
    size: int,
    path: Path,
    text: str,
    args: argparse.Namespace,
) -> None:
    original = text.encode("ascii")
    original_sha256 = hashlib.sha256(original).hexdigest()
    path.write_bytes(original)
    prepare_args = se.build_parser().parse_args(
        ["transaction", "--request-stdin", "--dry-run", "--json"]
    )
    payload = {
        "files": [
            {
                "file": str(path),
                "action": "edit",
                "expectedSha256": original_sha256,
                "diff": False,
                "operations": [
                    {
                        "op": "edit",
                        "old": "scope-A",
                        "new": "scope-B",
                        "first": True,
                    }
                ],
            }
        ]
    }

    def prepare_once() -> Any:
        summary = se.run_transaction_payload(prepare_args, payload)
        return summary, getattr(
            prepare_args, "_prepared_transaction", None
        )

    def validate_prepare(value: Any) -> None:
        summary, prepared = value
        files = summary.get("files", [])
        if (
            not summary.get("dryRun")
            or summary.get("written")
            or len(files) != 1
            or files[0].get("diff")
            or not isinstance(prepared, se.PreparedTransaction)
            or len(prepared.plans) != 1
            or not prepared.plans[0].output.startswith(b"scope-B")
        ):
            raise AssertionError(value)

    prepare_result = measure(
        "core.transaction-prepare-dry-run",
        prepare_once,
        input_bytes=size,
        iterations=args.iterations,
        warmups=args.warmups,
        validate=validate_prepare,
        trace_memory=args.trace_memory,
    )
    prepare_result["stage"] = "dry-run-prepare"
    results.append(prepare_result)
    if sha256_file(path) != original_sha256:
        raise AssertionError("dry-run preparation modified its fixture")

    prepared_holder: Dict[str, Any] = {}

    def prepare_confirmation() -> None:
        path.write_bytes(original)
        prepared = se.prepare_transaction_payload(prepare_args, payload)
        if not isinstance(prepared, se.PreparedTransaction):
            raise AssertionError("confirmation setup did not prepare a plan")
        prepared_holder["value"] = prepared

    prepare_confirmation()
    output_sha256 = prepared_holder["value"].plans[0].output_sha256

    def validate_confirm(value: Dict[str, Any]) -> None:
        files = value.get("files", [])
        if (
            value.get("dryRun")
            or not value.get("written")
            or len(files) != 1
            or files[0].get("sha256") != output_sha256
        ):
            raise AssertionError(value)

    try:
        with patch.object(
            se,
            "prepare_transaction",
            side_effect=AssertionError("confirmation replanned transaction"),
        ):
            confirm_result = measure(
                "core.transaction-confirm-revalidate",
                lambda: se.commit_prepared_transaction(
                    prepared_holder["value"]
                ),
                input_bytes=size,
                iterations=args.iterations,
                warmups=args.warmups,
                validate=validate_confirm,
                trace_memory=args.trace_memory,
                setup=prepare_confirmation,
            )
        if sha256_file(path) != output_sha256:
            raise AssertionError("confirmation wrote unexpected bytes")
        confirm_result["stage"] = "confirm-revalidate"
        confirm_result["replanned"] = False
        results.append(confirm_result)
    finally:
        path.write_bytes(original)

    if sha256_file(path) != original_sha256:
        raise AssertionError("transaction benchmark failed to restore fixture")


def add_size_cases(
    results: List[Dict[str, Any]],
    size: int,
    path: Path,
    text: str,
    args: argparse.Namespace,
) -> None:
    common = {
        "input_bytes": size,
        "iterations": args.iterations,
        "warmups": args.warmups,
        "trace_memory": args.trace_memory,
    }
    expected_bytes = path.read_bytes()
    common_trace = {**common, "input_bytes": len(expected_bytes)}
    results.append(
        measure(
            "core.verify-file-bytes-streaming",
            lambda: se._compare_file_bytes_strict(path, expected_bytes),
            validate=lambda value: (
                None
                if value is True
                else (_ for _ in ()).throw(AssertionError(value))
            ),
            **common_trace,
        )
    )

    results.append(
        measure(
            "cli.inspect",
            lambda: run_cli("inspect", path),
            validate=validate_cli,
            **{**common, "trace_memory": False},
        )
    )
    results.append(
        measure(
            "cli.stat",
            lambda: run_cli("stat", path),
            validate=lambda value: (
                validate_cli(value)
                if len(value.get("sha256", "")) == 64
                else (_ for _ in ()).throw(AssertionError(value))
            ),
            **{**common, "trace_memory": False},
        )
    )

    line_operations = [
        {"op": "replace-lines", "start": 10, "end": 10, "text": "replacement"},
        {"op": "insert", "line": 20, "text": "inserted"},
        {"op": "delete-lines", "start": 30, "end": 31},
    ]

    results.append(
        measure(
            "core.batch-line-ops",
            lambda: se.apply_operations(text, line_operations, "\r\n"),
            validate=lambda value: (
                None
                if sum(item["changed"] for item in value[1]) == 4
                else (_ for _ in ()).throw(AssertionError(value))
            ),
            **common,
        )
    )

    batch_operation_counts = [args.batch_operations]
    if args.batch_operations != 64:
        batch_operation_counts.append(64)
    for operation_count in batch_operation_counts:
        literal_markers = [
            f"batch-target-{index:03d}"
            for index in range(operation_count)
        ]
        literal_chunk_size = max(64, size // len(literal_markers))
        literal_text = "".join(
            repeat_to_size(
                "ordinary batch content ",
                max(1, literal_chunk_size - len(marker) - 1),
            )
            + marker
            + "\n"
            for marker in literal_markers
        )
        literal_operations = [
            {
                "op": "edit",
                "old": marker,
                "new": marker.replace("target", "done"),
                "expected_count": 1,
            }
            for marker in literal_markers
        ]
        literal_expected_text = literal_text
        literal_expected_results: List[Dict[str, Any]] = []
        for index, operation in enumerate(literal_operations, start=1):
            replacement = se.normalize_user_newlines(operation["new"], "\n")
            literal_expected_text = literal_expected_text.replace(
                operation["old"], replacement, 1
            )
            literal_expected_results.append(
                {
                    "index": index,
                    "op": "edit",
                    "changed": 1,
                    "matchStrategy": "exact",
                }
            )
        literal_expected = (
            literal_expected_text,
            literal_expected_results,
        )

        fast_path_probe = se._try_apply_exact_literal_batch(
            literal_text, literal_operations, "\n"
        )
        fast_path_eligible = fast_path_probe is not None
        if fast_path_eligible and fast_path_probe != literal_expected:
            raise AssertionError("exact literal fast-path probe mismatch")
        del fast_path_probe

        def validate_literal(
            value: Any,
            expected: Any = literal_expected,
        ) -> None:
            if value != expected:
                raise AssertionError(value)
        case_name = (
            "core.batch-literal-ops"
            if operation_count == args.batch_operations
            else f"core.batch-literal-ops-{operation_count}"
        )
        literal_result = measure(
            case_name,
            lambda: se.apply_operations(
                literal_text, literal_operations, "\n"
            ),
            input_bytes=len(literal_text),
            iterations=args.iterations,
            warmups=args.warmups,
            validate=validate_literal,
            trace_memory=args.trace_memory,
        )
        literal_result["operationCount"] = operation_count
        literal_result["fastPathEligible"] = fast_path_eligible
        if not fast_path_eligible:
            literal_result["fallbackReason"] = (
                "exact-literal helper rejected proof"
            )
        results.append(literal_result)

    fallback_suffix = (
        "\nbatch-fallback-seed\nindependent-a\nindependent-c\n"
    )
    fallback_text = repeat_to_size(
        "ordinary fallback content ",
        size,
        fallback_suffix,
    )
    fallback_operations = [
        {
            "op": "edit",
            "old": "batch-fallback-seed",
            "new": "batch-fallback-created",
            "expected_count": 1,
        },
        {
            "op": "edit",
            "old": "batch-fallback-created",
            "new": "batch-fallback-done",
            "expected_count": 1,
        },
        {
            "op": "edit",
            "old": "independent-a",
            "new": "independent-b",
            "expected_count": 1,
        },
        {
            "op": "edit",
            "old": "independent-c",
            "new": "independent-d",
            "expected_count": 1,
        },
    ]
    fallback_expected_text = fallback_text
    fallback_expected_results: List[Dict[str, Any]] = []
    for index, operation in enumerate(fallback_operations, start=1):
        replacement = se.normalize_user_newlines(operation["new"], "\n")
        fallback_expected_text = fallback_expected_text.replace(
            operation["old"], replacement, 1
        )
        fallback_expected_results.append(
            {
                "index": index,
                "op": "edit",
                "changed": 1,
                "matchStrategy": "exact",
            }
        )
    fallback_expected = (
        fallback_expected_text,
        fallback_expected_results,
    )

    def validate_fallback(
        value: Any,
        expected: Any = fallback_expected,
    ) -> None:
        if value != expected:
            raise AssertionError(value)

    fallback_result = measure(
        "core.batch-literal-fallback-dependent",
        lambda: se.apply_operations(
            fallback_text, fallback_operations, "\n"
        ),
        input_bytes=len(fallback_text),
        iterations=args.iterations,
        warmups=args.warmups,
        validate=validate_fallback,
        trace_memory=args.trace_memory,
    )
    fallback_result["operationCount"] = len(fallback_operations)
    fallback_result["fastPathEligible"] = False
    fallback_result["fallbackReason"] = (
        "prior replacement creates a later target"
    )
    results.append(fallback_result)
    append_operations = [{"op": "append", "text": "tail"}]
    append_separator = "" if text.endswith(("\r", "\n")) else "\r\n"
    expected_append = text + append_separator + "tail"
    results.append(
        measure(
            "core.append",
            lambda: se.apply_operations(text, append_operations, "\r\n"),
            validate=lambda value: (
                None
                if value[0] == expected_append and value[1][0]["changed"] == 1
                else (_ for _ in ()).throw(AssertionError(value[1]))
            ),
            **common,
        )
    )

    regex_text = repeat_to_size("value=123456 other=text\n", size)
    regex_matches = regex_text.count("value=123456")
    regex_operation = {
        "pattern": r"value=\d+",
        "replacement": "value=0",
        "expected_count": regex_matches,
    }
    results.append(
        measure(
            "core.regex-all",
            lambda: se.apply_regex_edit(regex_text, regex_operation, "\n"),
            validate=lambda value: (
                None
                if value[1] == regex_matches
                else (_ for _ in ()).throw(AssertionError(value))
            ),
            **common,
        )
    )

    normalized = repeat_to_size(
        "ordinary = 123456789\r\n",
        size,
        "\r\nneedle_one\r\nneedle_two\r\n",
    )
    normalize_operation = {
        "old": "needle_one\nneedle_two",
        "new": "done",
        "first": True,
    }
    results.append(
        measure(
            "core.normalize-ignore-eol-tail",
            lambda: se.apply_literal_edit(
                normalized,
                normalize_operation,
                "\r\n",
                ignore_eol=True,
            ),
            validate=lambda value: (
                None
                if value[1] == 1
                else (_ for _ in ()).throw(AssertionError(value))
            ),
            **common,
        )
    )

    whitespace_text = repeat_to_size(
        "ordinary value ",
        size,
        "needle   one",
    )
    whitespace_operation = {
        "old": "needle one",
        "new": "done",
        "first": True,
    }
    results.append(
        measure(
            "core.normalize-whitespace-tail",
            lambda: se.apply_literal_edit(
                whitespace_text,
                whitespace_operation,
                "\n",
                normalize_whitespace=True,
            ),
            validate=lambda value: (
                None
                if value[1] == 1
                else (_ for _ in ()).throw(AssertionError(value))
            ),
            **common,
        )
    )

    whitespace_many_text = repeat_to_size(
        "alpha   beta / ",
        size,
        "alpha   beta",
    )
    whitespace_many_operation = {
        "old": "alpha beta",
        "new": "done",
        "first": True,
    }
    results.append(
        measure(
            "core.normalize-whitespace-first-many",
            lambda: se.apply_literal_edit(
                whitespace_many_text,
                whitespace_many_operation,
                "\n",
                normalize_whitespace=True,
            ),
            validate=lambda value: (
                None
                if value[1] == 1 and value[0].startswith("done")
                else (_ for _ in ()).throw(AssertionError(value))
            ),
            **common,
        )
    )

    whitespace_context_text = repeat_to_size(
        "ordinary value\n",
        size,
        "scope-B\nneedle   one\n",
    )
    results.append(
        measure(
            "core.normalize-whitespace-context-tail",
            lambda: se.apply_literal_edit(
                whitespace_context_text,
                whitespace_operation,
                "\n",
                normalize_whitespace=True,
                context_before="scope-B",
            ),
            validate=lambda value: (
                None
                if value[1] == 1
                else (_ for _ in ()).throw(AssertionError(value))
            ),
            **common,
        )
    )

    add_trim_size_cases(results, size, args)

    repeated_lines = repeat_to_size(
        "ordinary generated line\n",
        size,
    )
    results.append(
        measure(
            "core.closest-match-repeated-miss",
            lambda: se.find_closest_match(repeated_lines, "zzzzzzzzzz"),
            validate=lambda value: (
                None
                if value is None
                else (_ for _ in ()).throw(AssertionError(value))
            ),
            **common,
        )
    )
    repeated_match_lines = repeat_to_size("same\n", size)
    results.append(
        measure(
            "core.closest-match-repeated-window",
            lambda: se.find_closest_match(
                repeated_match_lines,
                "changed\nsame\nsame",
            ),
            validate=lambda value: (
                None
                if value == (1, "same\nsame\nsame")
                else (_ for _ in ()).throw(AssertionError(value))
            ),
            **common,
        )
    )
    results.append(
        measure(
            "core.closest-match-multiline-miss",
            lambda: se.find_closest_match(
                repeated_lines,
                "missing one\nmissing two\nmissing three",
            ),
            validate=lambda value: (
                None
                if value is None
                else (_ for _ in ()).throw(AssertionError(value))
            ),
            **common,
        )
    )

    compact_before = repeat_to_size("left\nright\n", size)
    compact_after = repeat_to_size("right\nleft\n", size)
    results.append(
        measure(
            "core.compact-diff-reordered-repetitions",
            lambda: se.generate_compact_diff(
                Path("benchmark.txt"),
                compact_before,
                compact_after,
                3,
            ),
            input_bytes=len(compact_before) + len(compact_after),
            iterations=args.iterations,
            warmups=args.warmups,
            validate=lambda value: (
                None
                if (
                    value[1]
                    and len(value[0])
                    <= se._COMPACT_DIFF_MAX_CHARS
                    + len("\n... [compact diff truncated]")
                )
                else (_ for _ in ()).throw(AssertionError(value))
            ),
            trace_memory=args.trace_memory,
        )
    )

    forward_blocks = "".join(
        chr(ord("a") + index) * 16 for index in range(15)
    )
    reverse_blocks = "".join(
        chr(ord("a") + index) * 16 for index in reversed(range(15))
    )
    high_diversity_pattern = forward_blocks + ("z" * 16)
    high_diversity_line_count = max(16, size // 257)
    high_diversity_text = "\n".join(
        reverse_blocks + f"{index:016x}"
        for index in range(high_diversity_line_count)
    )
    results.append(
        measure(
            "core.closest-match-high-diversity-miss",
            lambda: se.find_closest_match(
                high_diversity_text,
                high_diversity_pattern,
            ),
            input_bytes=len(high_diversity_text),
            iterations=args.iterations,
            warmups=args.warmups,
            validate=lambda value: (
                None
                if value is None
                else (_ for _ in ()).throw(AssertionError(value))
            ),
            trace_memory=args.trace_memory,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes-mib", default="1,10")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--context-matches", type=int, default=1000)
    parser.add_argument("--batch-operations", type=int, default=16)
    parser.add_argument("--trace-memory", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if (
        args.iterations < 1
        or args.warmups < 0
        or args.context_matches < 1
        or args.batch_operations < 1
    ):
        parser.error(
            "iterations/context-matches/batch-operations must be positive "
            "and warmups non-negative"
        )

    sizes = [
        max(4096, int(float(value) * 1024 * 1024))
        for value in args.sizes_mib.split(",")
    ]
    results: List[Dict[str, Any]] = []

    context_text = "scope-A\nneedle\n" * args.context_matches
    context_operation = {"old": "needle", "new": "changed", "first": True}
    results.append(
        measure(
            "core.context-before",
            lambda: se.apply_literal_edit(
                context_text,
                context_operation,
                "\n",
                context_before="scope-A",
            ),
            input_bytes=len(context_text),
            iterations=args.iterations,
            warmups=args.warmups,
            validate=lambda value: (
                None
                if value[1] == 1
                else (_ for _ in ()).throw(AssertionError(value))
            ),
            trace_memory=args.trace_memory,
        )
    )

    with tempfile.TemporaryDirectory(prefix="safe-edit-benchmark-") as temp_name:
        temp_dir = Path(temp_name)
        for size in sizes:
            text = repeat_to_size(
                "scope-A\r\n    value = 1234567890\r\n",
                size,
            )
            path = temp_dir / f"input-{size}.txt"
            path.write_bytes(text.encode("ascii"))
            before = path.read_bytes()
            add_size_cases(results, size, path, text, args)
            add_prepared_transaction_cases(
                results,
                size,
                temp_dir / f"prepared-{size}.txt",
                text,
                args,
            )
            if path.read_bytes() != before:
                raise AssertionError("benchmark modified its input fixture")

    document = {
        "schemaVersion": 1,
        "python": sys.version.split()[0],
        "throughputBasis": "input-bytes-per-median-wall-time",
        "platform": platform.platform(),
        "script": str(SCRIPT),
        "results": results,
    }
    if args.json:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True))
    else:
        print(f"{'case':36} {'bytes':>10} {'median':>11} {'p95':>11} {'MiB/s':>10}")
        for row in results:
            throughput = row["throughputMiBPerSecond"] or 0.0
            print(
                f"{row['name']:36} {row['inputBytes']:10d} "
                f"{row['medianMs']:10.2f}ms {row['p95Ms']:10.2f}ms "
                f"{throughput:10.2f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
