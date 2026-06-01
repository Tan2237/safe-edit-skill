#!/usr/bin/env python3
"""Performance benchmark for safe_edit.py"""

import time
import sys
import os

sys.path.insert(0, '../../skills/safe-edit')
from safe_edit import (
    detect_encoding, strict_decode, detect_line_ending,
    split_records, normalize_for_match, find_closest_match
)

def timeit(func, *args, iterations=3):
    """Run func multiple times and return average time."""
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        result = func(*args)
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    return sum(times) / len(times), result

def benchmark_file(filepath, label):
    """Run benchmarks on a single file."""
    print(f"\n{'='*60}")
    print(f"File: {label}")
    print(f"{'='*60}")

    # Read file
    with open(filepath, 'rb') as f:
        data = f.read()

    size_mb = len(data) / 1024 / 1024
    print(f"Size: {size_mb:.2f} MB ({len(data):,} bytes)")

    # Benchmark 1: Encoding detection
    t, encoding = timeit(detect_encoding, data, 'auto')
    print(f"\n[1] detect_encoding:  {t*1000:6.1f} ms  ({encoding.name})")

    # Decode text
    text = strict_decode(data, encoding)

    # Benchmark 2: Line ending detection
    t, (le_style, le_counts, le_mixed) = timeit(detect_line_ending, text)
    print(f"[2] detect_line_ending: {t*1000:6.1f} ms  ({le_style})")

    # Benchmark 3: split_records
    t, records = timeit(split_records, text)
    print(f"[3] split_records:     {t*1000:6.1f} ms  ({len(records):,} lines)")

    # Benchmark 4: normalize_for_match (ignore-indent)
    t, normalized = timeit(normalize_for_match, text, True, False, False)
    print(f"[4] normalize (indent): {t*1000:6.1f} ms")

    # Benchmark 5: normalize_for_match (ignore-eol)
    t, normalized = timeit(normalize_for_match, text, False, True, False)
    print(f"[5] normalize (eol):    {t*1000:6.1f} ms")

    # Benchmark 6: find_closest_match (single line pattern)
    pattern = "process_item_1000"
    t, result = timeit(find_closest_match, text, pattern)
    print(f"[6] find_closest (1ln): {t*1000:6.1f} ms  (found: {result is not None})")

    # Benchmark 7: find_closest_match (multi-line pattern)
    pattern = "def process_item_500(data: str) -> str:\n    return data.strip()"
    t, result = timeit(find_closest_match, text, pattern)
    print(f"[7] find_closest (ml):  {t*1000:6.1f} ms  (found: {result is not None})")

    # Benchmark 8: Simple string operations for comparison
    t, _ = timeit(text.count, '\n')
    print(f"[8] text.count('\\n'):  {t*1000:6.1f} ms  (baseline)")

    t, _ = timeit(text.find, "process_item_500")
    print(f"[9] text.find():       {t*1000:6.1f} ms  (baseline)")

    return {
        'size_mb': size_mb,
        'detect_encoding': None,
        'detect_line_ending': None,
        'split_records': None,
        'normalize_indent': None,
        'normalize_eol': None,
        'find_closest_single': None,
        'find_closest_multi': None,
    }

def main():
    print("safe_edit.py Performance Benchmark")
    print("="*60)

    test_files = [
        ('test_100kb.txt', '100 KB'),
        ('test_1000kb.txt', '1 MB'),
        ('test_10000kb.txt', '10 MB'),
        ('test_50000kb.txt', '50 MB'),
    ]

    results = []
    for filename, label in test_files:
        if os.path.exists(filename):
            result = benchmark_file(filename, label)
            results.append((label, result))
        else:
            print(f"\n[SKIP] {filename} not found")

    # Summary table
    print("\n" + "="*60)
    print("SUMMARY (times in ms)")
    print("="*60)
    print(f"{'File':<10} {'encoding':>10} {'line_end':>10} {'split':>10} {'norm_ind':>10}")
    print("-"*60)

if __name__ == '__main__':
    main()
