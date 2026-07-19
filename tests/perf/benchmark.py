#!/usr/bin/env python3
"""Deterministic performance benchmark for safe_edit.py."""

from __future__ import annotations

import argparse
import gc
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
from typing import Any, Callable, Dict, List

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
) -> Dict[str, Any]:
    for _ in range(warmups):
        validate(fn())

    samples: List[float] = []
    output: Any = None
    for _ in range(iterations):
        gc.collect()
        started = time.perf_counter_ns()
        output = fn()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)

    validate(output)
    median_ms = statistics.median(samples)
    result: Dict[str, Any] = {
        "name": name,
        "inputBytes": input_bytes,
        "iterations": iterations,
        "minMs": min(samples),
        "medianMs": median_ms,
        "p95Ms": percentile(samples, 0.95),
        "maxMs": max(samples),
        "throughputMiBPerSecond": (
            input_bytes / 1024 / 1024 / (median_ms / 1000)
            if input_bytes and median_ms
            else None
        ),
    }

    if trace_memory:
        gc.collect()
        tracemalloc.start()
        validate(fn())
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
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

    trailing_whitespace_text = repeat_to_size("value   \r\n", size)
    results.append(
        measure(
            "core.trim-trailing-whitespace",
            lambda: se.trim_trailing_whitespace(trailing_whitespace_text),
            validate=lambda value: (
                None
                if "   \r\n" not in value
                else (_ for _ in ()).throw(AssertionError("whitespace remains"))
            ),
            **common,
        )
    )

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes-mib", default="1,10")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--context-matches", type=int, default=1000)
    parser.add_argument("--trace-memory", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.iterations < 1 or args.warmups < 0 or args.context_matches < 1:
        parser.error("iterations/context-matches must be positive and warmups non-negative")

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
            if path.read_bytes() != before:
                raise AssertionError("benchmark modified its input fixture")

    document = {
        "schemaVersion": 1,
        "python": sys.version.split()[0],
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
