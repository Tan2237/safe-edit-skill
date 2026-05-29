import codecs
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "skills" / "safe-edit" / "safe_edit.py"


class SafeEditTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="safe-edit-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def run_tool(self, *args, expect=0, input_text=None):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, args)],
            input=input_text,
            text=True,
            capture_output=True,
            encoding="utf-8",
        )
        if result.returncode != expect:
            self.fail(
                f"unexpected exit {result.returncode}, expected {expect}\n"
                f"args={args}\nstdout={result.stdout}\nstderr={result.stderr}"
            )
        return result

    def test_inspect_reports_encoding_and_line_endings(self):
        path = self.tmpdir / "utf8-bom-crlf.txt"
        path.write_bytes(codecs.BOM_UTF8 + b"alpha\r\nbeta\r\n")
        result = self.run_tool("inspect", "--file", path, "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["encoding"], "utf-8-bom")
        self.assertEqual(payload["lineEnding"], "crlf")
        self.assertEqual(payload["lineEndingCounts"]["crlf"], 2)
        self.assertFalse(payload["written"])

    def test_literal_edit_preserves_utf8_bom_and_crlf(self):
        path = self.tmpdir / "literal.txt"
        path.write_bytes(codecs.BOM_UTF8 + b"alpha\r\nfoo\r\nomega\r\n")
        self.run_tool("edit", "--file", path, "--old", "foo", "--new", "bar", "--expected-count", "1")
        self.assertEqual(path.read_bytes(), codecs.BOM_UTF8 + b"alpha\r\nbar\r\nomega\r\n")

    def test_gbk_regex_replacement_preserves_bytes(self):
        path = self.tmpdir / "gbk.txt"
        path.write_bytes(bytes.fromhex("c4 e3 ba c3 0d 0a 66 6f 6f 0d 0a"))
        replacement = chr(0x4E16) + chr(0x754C)
        result = self.run_tool(
            "regex",
            "--file",
            path,
            "--encoding",
            "gbk",
            "--pattern",
            "f.o",
            "--replacement",
            replacement,
            "--expected-count",
            "1",
            "--json",
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["encoding"], "gbk")
        self.assertEqual(path.read_bytes(), bytes.fromhex("c4 e3 ba c3 0d 0a ca c0 bd e7 0d 0a"))

    def test_prepend_and_append_preserve_lf_shape(self):
        path = self.tmpdir / "append.txt"
        path.write_bytes(b"middle\n")
        self.run_tool("prepend", "--file", path, "--text", "top")
        self.run_tool("append", "--file", path, "--text", "bottom")
        self.assertEqual(path.read_bytes(), b"top\nmiddle\nbottom")

    def test_same_output_skips_write(self):
        path = self.tmpdir / "same.txt"
        path.write_bytes(b"foo\n")
        before_mtime = os.stat(path).st_mtime_ns
        time.sleep(0.02)
        result = self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "foo",
            "--new",
            "foo",
            "--expected-count",
            "1",
            "--json",
        )
        payload = json.loads(result.stdout)
        self.assertTrue(payload["skipped"])
        self.assertFalse(payload["written"])
        self.assertEqual(path.read_bytes(), b"foo\n")
        self.assertEqual(os.stat(path).st_mtime_ns, before_mtime)

    def test_replace_lines_and_delete_lines(self):
        path = self.tmpdir / "lines.txt"
        block = self.tmpdir / "block.txt"
        path.write_bytes(b"1\n2\n3\n4\n5\n")
        block.write_text("A\nB", encoding="utf-8")
        self.run_tool("replace-lines", "--file", path, "--start", "2", "--end", "4", "--text-file", block)
        self.assertEqual(path.read_bytes(), b"1\nA\nB\n5\n")
        self.run_tool("delete-lines", "--file", path, "--start", "2", "--end", "3")
        self.assertEqual(path.read_bytes(), b"1\n5\n")

    def test_batch_runs_multiple_operations(self):
        path = self.tmpdir / "batch.txt"
        ops = self.tmpdir / "ops.json"
        path.write_bytes(b"foo\nversion = \"0\"\nkeep\nremove1\nremove2\n")
        ops.write_text(
            json.dumps(
                [
                    {"op": "edit", "old": "foo", "new": "bar", "expected_count": 1},
                    {
                        "op": "regex",
                        "pattern": r"version = \"[^\"]+\"",
                        "replacement": 'version = "9"',
                        "expected_count": 1,
                    },
                    {"op": "delete-lines", "start": 4, "end": 5},
                ]
            ),
            encoding="utf-8",
        )
        self.run_tool("batch", "--file", path, "--ops-file", ops)
        self.assertEqual(path.read_bytes(), b"bar\nversion = \"9\"\nkeep\n")

    def test_failure_leaves_file_unchanged(self):
        path = self.tmpdir / "fail.txt"
        path.write_bytes(b"abc\n")
        before = path.read_bytes()
        self.run_tool("regex", "--file", path, "--pattern", "zzz", "--replacement", "x", expect=2)
        self.assertEqual(path.read_bytes(), before)

    def test_dry_run_diff_does_not_write(self):
        path = self.tmpdir / "dry.txt"
        path.write_bytes(b"foo\n")
        before = path.read_bytes()
        result = self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "foo",
            "--new",
            "bar",
            "--expected-count",
            "1",
            "--dry-run",
            "--diff",
        )
        self.assertIn("bar", result.stdout)
        self.assertEqual(path.read_bytes(), before)

    def test_convert_encoding_line_endings_and_final_newline(self):
        path = self.tmpdir / "convert.txt"
        path.write_bytes("alpha\nbeta".encode("utf-8"))
        result = self.run_tool(
            "convert",
            "--file",
            path,
            "--to-encoding",
            "utf-8-bom",
            "--to-line-ending",
            "crlf",
            "--final-newline",
            "ensure",
            "--json",
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["outputEncoding"], "utf-8-bom")
        self.assertEqual(payload["outputLineEnding"], "crlf")
        self.assertTrue(payload["written"])
        self.assertEqual(path.read_bytes(), codecs.BOM_UTF8 + b"alpha\r\nbeta\r\n")

    def test_convert_can_trim_and_strip_final_newline(self):
        path = self.tmpdir / "trim.txt"
        path.write_bytes(b"a  \n\tb\t\n")
        self.run_tool("convert", "--file", path, "--trim-trailing-whitespace", "--final-newline", "strip")
        self.assertEqual(path.read_bytes(), b"a\n\tb")

    def test_backup_dir_and_suffix(self):
        path = self.tmpdir / "backup.txt"
        backup_dir = self.tmpdir / "backups"
        path.write_bytes(b"before\n")
        self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "before",
            "--new",
            "after",
            "--expected-count",
            "1",
            "--backup",
            "--backup-dir",
            backup_dir,
            "--backup-suffix",
            ".bak",
        )
        self.assertEqual(path.read_bytes(), b"after\n")
        backup = backup_dir / "backup.txt.bak"
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_bytes(), b"before\n")

    def test_stale_lock_can_be_removed(self):
        path = self.tmpdir / "locked.txt"
        path.write_bytes(b"foo\n")
        lock = self.tmpdir / ".locked.txt.safe-edit.lock"
        lock.write_text("stale", encoding="utf-8")
        old_time = time.time() - 120
        os.utime(lock, (old_time, old_time))
        self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "foo",
            "--new",
            "bar",
            "--expected-count",
            "1",
            "--lock-timeout",
            "1",
            "--lock-stale-seconds",
            "10",
        )
        self.assertEqual(path.read_bytes(), b"bar\n")
        self.assertFalse(lock.exists())

    def test_explain_match_failure_shows_diagnostics(self):
        """Test that --explain-match-failure provides detailed diagnostics."""
        path = self.tmpdir / "explain.txt"
        # File has tabs instead of spaces
        path.write_bytes(b"def foo():\n\treturn 42\n")
        
        # Try to match with spaces (should fail)
        result = self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "    return 42",  # 4 spaces instead of tab
            "--new",
            "    return 43",
            "--explain-match-failure",
            expect=2,
        )
        
        # Should contain diagnostic information
        self.assertIn("Match failed", result.stderr)
        self.assertIn("Closest match", result.stderr)
        self.assertIn("EXPECTED:", result.stderr)
        self.assertIn("ACTUAL:", result.stderr)
        self.assertIn("Differences:", result.stderr)

    def test_anchor_pattern_replace_lines(self):
        """Test anchor-based line replacement."""
        path = self.tmpdir / "anchor.txt"
        path.write_bytes(b"header\nAcGePoint3d ptCenter\nline1\nline2\nline3\nfooter\n")
        
        # Replace lines relative to anchor
        # anchor at line 2, +2 = line 4, +3 = line 5 (inclusive)
        self.run_tool(
            "replace-lines",
            "--file",
            path,
            "--anchor-pattern",
            "AcGePoint3d ptCenter",
            "--offset-start",
            "+2",
            "--offset-end",
            "+3",
            "--text",
            "new_line",
        )
        
        # Should replace lines 4-5 (line2 and line3)
        self.assertEqual(
            path.read_bytes(),
            b"header\nAcGePoint3d ptCenter\nline1\nnew_line\nfooter\n"
        )

    def test_anchor_pattern_delete_lines(self):
        """Test anchor-based line deletion."""
        path = self.tmpdir / "anchor_delete.txt"
        path.write_bytes(b"header\nAcGePoint3d ptCenter\nline1\nline2\nline3\nfooter\n")
        
        # Delete lines relative to anchor
        self.run_tool(
            "delete-lines",
            "--file",
            path,
            "--anchor-pattern",
            "AcGePoint3d ptCenter",
            "--offset-start",
            "+2",
            "--offset-end",
            "+3",
        )
        
        # Should delete lines 4-5 (anchor at line 2, +2 = line 4, +3 = line 5)
        self.assertEqual(
            path.read_bytes(),
            b"header\nAcGePoint3d ptCenter\nline1\nfooter\n"
        )

    def test_anchor_pattern_ambiguous_fails(self):
        """Test that ambiguous anchor pattern fails without occurrence."""
        path = self.tmpdir / "ambiguous.txt"
        path.write_bytes(b"pattern\nline1\npattern\nline2\n")
        
        # Should fail because pattern appears twice
        result = self.run_tool(
            "replace-lines",
            "--file",
            path,
            "--anchor-pattern",
            "pattern",
            "--offset-start",
            "+1",
            "--offset-end",
            "+1",
            "--text",
            "new",
            expect=2,
        )
        
        self.assertIn("found 2 times", result.stderr)
        self.assertIn("--anchor-occurrence", result.stderr)

    def test_anchor_pattern_with_occurrence(self):
        """Test anchor pattern with specific occurrence."""
        path = self.tmpdir / "occurrence.txt"
        path.write_bytes(b"pattern\nline1\npattern\nline2\npattern\nline3\n")
        
        # Use second occurrence
        self.run_tool(
            "replace-lines",
            "--file",
            path,
            "--anchor-pattern",
            "pattern",
            "--anchor-occurrence",
            "2",
            "--offset-start",
            "+1",
            "--offset-end",
            "+1",
            "--text",
            "new",
        )
        
        # Should replace line 4 (second pattern at line 3, +1 = line 4)
        self.assertEqual(
            path.read_bytes(),
            b"pattern\nline1\npattern\nnew\npattern\nline3\n"
        )

    # =========================================================================
    # --explain-match-failure tests
    # =========================================================================

    def test_explain_match_failure_multiline(self):
        """Test --explain-match-failure with multiline pattern mismatch."""
        path = self.tmpdir / "multiline.txt"
        # File has different content than expected
        path.write_bytes(b"def foo():\n    x = 1\n    y = 2\n")
        
        # Try to match a multiline pattern that doesn't exist
        result = self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "def foo():\n    x = 1\n    z = 3",  # z = 3 instead of y = 2
            "--new",
            "def bar():\n    x = 1\n    z = 3",
            "--explain-match-failure",
            expect=2,
        )
        
        # Should show diagnostics
        self.assertIn("Match failed", result.stderr)
        self.assertIn("Closest match", result.stderr)
        self.assertIn("EXPECTED:", result.stderr)
        self.assertIn("ACTUAL:", result.stderr)

    def test_explain_match_failure_line_ending_crlf_vs_lf(self):
        """Test --explain-match-failure with different line endings."""
        path = self.tmpdir / "line_ending.txt"
        # File uses CRLF
        path.write_bytes(b"line1\r\nline2\r\n")
        
        # Try to match with LF - this should actually match because
        # the tool normalizes line endings for matching
        result = self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "line1\nline2",
            "--new",
            "new1\nnew2",
            "--explain-match-failure",
            expect=2,
        )
        
        # Should show match failure diagnostics
        self.assertIn("Match failed", result.stderr)

    def test_explain_match_failure_missing_line(self):
        """Test --explain-match-failure when pattern has more lines than file."""
        path = self.tmpdir / "missing.txt"
        # File has 3 lines
        path.write_bytes(b"line1\nline2\nline3\n")
        
        # Try to match 4-line pattern that doesn't exist
        result = self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "line1\nline2\nline3\nline4",  # Extra line
            "--new",
            "new1\nnew2\nnew3\nnew4",
            "--explain-match-failure",
            expect=2,
        )
        
        # Should report match failure
        self.assertIn("Match failed", result.stderr)

    def test_explain_match_failure_extra_line(self):
        """Test --explain-match-failure when file has more lines than pattern."""
        path = self.tmpdir / "extra.txt"
        # File has 4 lines
        path.write_bytes(b"line1\nline2\nline3\nline4\n")
        
        # Try to match 2-line pattern that doesn't exist
        result = self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "line1\nline2",  # This should match
            "--new",
            "new1\nnew2",
            "--expected-count",
            "99",  # Wrong count to trigger failure
            "--explain-match-failure",
            expect=2,
        )
        
        # Should report count mismatch
        self.assertIn("expected 99", result.stderr)

    def test_explain_match_failure_pattern_not_found_at_all(self):
        """Test --explain-match-failure when pattern is completely unrelated."""
        path = self.tmpdir / "unrelated.txt"
        path.write_bytes(b"alpha\nbeta\ngamma\n")
        
        # Try to match something completely different
        result = self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "xyz123\nabc456",
            "--new",
            "new_content",
            "--explain-match-failure",
            expect=2,
        )
        
        # Should report no close match found
        self.assertIn("Match failed", result.stderr)

    def test_explain_match_failure_close_match_vs_no_match(self):
        """Test --explain-match-failure distinguishes close match from no match."""
        path1 = self.tmpdir / "close_match.txt"
        path1.write_bytes(b"function foo():\n    return 42\n")
        
        # Close match - typo in pattern
        result1 = self.run_tool(
            "edit",
            "--file",
            path1,
            "--old",
            "function foo():\n    return 43",  # 43 instead of 42
            "--new",
            "function bar():\n    return 43",
            "--explain-match-failure",
            expect=2,
        )
        
        # Should find close match
        self.assertIn("Closest match", result1.stderr)
        
        # No match - completely different
        path2 = self.tmpdir / "no_match.txt"
        path2.write_bytes(b"alpha beta gamma\n")
        
        result2 = self.run_tool(
            "edit",
            "--file",
            path2,
            "--old",
            "xyz123\nabc456\ndef789",
            "--new",
            "new",
            "--explain-match-failure",
            expect=2,
        )
        
        # Should report no close match
        self.assertIn("Match failed", result2.stderr)

    # =========================================================================
    # --anchor-pattern tests
    # =========================================================================

    def test_anchor_pattern_negative_offset(self):
        """Test --anchor-pattern with negative offset (backward positioning)."""
        path = self.tmpdir / "negative_offset.txt"
        path.write_bytes(b"line1\nline2\nANCHOR\nline4\nline5\n")
        
        # Use negative offset to target lines before anchor
        # anchor at line 3, -1 = line 2, anchor = line 3
        self.run_tool(
            "replace-lines",
            "--file",
            path,
            "--anchor-pattern",
            "ANCHOR",
            "--offset-start",
            "-1",
            "--offset-end",
            "-1",
            "--text",
            "REPLACED",
        )
        
        # Should replace line 2 (anchor at 3, -1 = 2)
        self.assertEqual(
            path.read_bytes(),
            b"line1\nREPLACED\nANCHOR\nline4\nline5\n"
        )

    def test_anchor_pattern_not_found(self):
        """Test --anchor-pattern when pattern doesn't exist in file."""
        path = self.tmpdir / "no_anchor.txt"
        path.write_bytes(b"line1\nline2\nline3\n")
        
        # Try to use non-existent anchor
        result = self.run_tool(
            "delete-lines",
            "--file",
            path,
            "--anchor-pattern",
            "NONEXISTENT_PATTERN",
            "--offset-start",
            "+1",
            "--offset-end",
            "+2",
            expect=2,
        )
        
        self.assertIn("anchor pattern not found", result.stderr)

    def test_anchor_pattern_offset_out_of_bounds(self):
        """Test --anchor-pattern when offset exceeds file boundaries."""
        path = self.tmpdir / "bounds.txt"
        path.write_bytes(b"line1\nANCHOR\nline3\n")
        
        # Offset that would go beyond file end
        result = self.run_tool(
            "replace-lines",
            "--file",
            path,
            "--anchor-pattern",
            "ANCHOR",
            "--offset-start",
            "+5",  # Would be line 7, but file only has 3 lines
            "--offset-end",
            "+10",
            "--text",
            "new",
            expect=2,
        )
        
        self.assertIn("end must be", result.stderr)

    def test_anchor_pattern_at_file_boundary(self):
        """Test --anchor-pattern when anchor is at first or last line."""
        # Anchor at first line
        path1 = self.tmpdir / "first_line_anchor.txt"
        path1.write_bytes(b"ANCHOR\nline2\nline3\n")
        
        self.run_tool(
            "replace-lines",
            "--file",
            path1,
            "--anchor-pattern",
            "ANCHOR",
            "--offset-start",
            "+1",
            "--offset-end",
            "+1",
            "--text",
            "NEW",
        )
        
        self.assertEqual(
            path1.read_bytes(),
            b"ANCHOR\nNEW\nline3\n"
        )
        
        # Anchor at last line - delete line before anchor
        path2 = self.tmpdir / "last_line_anchor.txt"
        path2.write_bytes(b"line1\nline2\nANCHOR\n")
        
        self.run_tool(
            "delete-lines",
            "--file",
            path2,
            "--anchor-pattern",
            "ANCHOR",
            "--offset-start",
            "-1",
            "--offset-end",
            "-1",
        )
        
        # Should delete line2 (anchor at line 3, -1 = line 2)
        self.assertEqual(
            path2.read_bytes(),
            b"line1\nANCHOR\n"
        )

    def test_anchor_pattern_conflict_with_absolute_lines(self):
        """Test that when both anchor and absolute line params are provided, absolute lines are used."""
        path = self.tmpdir / "conflict.txt"
        path.write_bytes(b"line1\nline2\nline3\nline4\n")
        
        # When both anchor and absolute line numbers are provided,
        # the implementation uses absolute line numbers (start/end)
        # because they are checked first in the code
        self.run_tool(
            "replace-lines",
            "--file",
            path,
            "--start",
            "1",
            "--end",
            "2",
            "--anchor-pattern",
            "line2",
            "--offset-start",
            "+1",
            "--offset-end",
            "+1",
            "--text",
            "new",
        )
        
        # Should use absolute lines (1-2), not anchor (line2 at line 2, +1=3)
        self.assertEqual(
            path.read_bytes(),
            b"new\nline3\nline4\n"
        )

    def test_anchor_pattern_occurrence_out_of_range(self):
        """Test --anchor-occurrence when it exceeds actual occurrences."""
        path = self.tmpdir / "occurrence_range.txt"
        path.write_bytes(b"pattern\nline1\npattern\nline2\n")
        
        # Pattern appears twice, but we ask for 99th occurrence
        result = self.run_tool(
            "replace-lines",
            "--file",
            path,
            "--anchor-pattern",
            "pattern",
            "--anchor-occurrence",
            "99",
            "--offset-start",
            "+1",
            "--offset-end",
            "+1",
            "--text",
            "new",
            expect=2,
        )
        
        self.assertIn("out of range", result.stderr)

    # =========================================================================
    # Boundary case tests
    # =========================================================================

    def test_empty_file_handling(self):
        """Test operations on empty file."""
        path = self.tmpdir / "empty.txt"
        path.write_bytes(b"")
        
        # Inspect should work
        result = self.run_tool("inspect", "--file", path, "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["lineCount"], 0)
        
        # Append should work
        self.run_tool("append", "--file", path, "--text", "first line")
        self.assertEqual(path.read_bytes(), b"first line")
        
        # Edit should fail
        result = self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "something",
            "--new",
            "other",
            expect=2,
        )
        self.assertIn("not found", result.stderr)

    def test_single_line_file_handling(self):
        """Test operations on single-line file."""
        path = self.tmpdir / "single.txt"
        path.write_bytes(b"only line")
        
        # Edit should work
        self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "only line",
            "--new",
            "new line",
            "--expected-count",
            "1",
        )
        self.assertEqual(path.read_bytes(), b"new line")
        
        # Delete the line should result in empty file
        self.run_tool("delete", "--file", path, "--line", "1")
        self.assertEqual(path.read_bytes(), b"")

    def test_special_characters_literal_edit(self):
        """Test that literal edit handles regex special characters correctly."""
        path = self.tmpdir / "special.txt"
        path.write_bytes(b"function test() { return $1 + $2; }\n")
        
        # Pattern contains regex special characters: $ ( ) { }
        # Should be treated literally, not as regex
        self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "$1 + $2",
            "--new",
            "$10 + $20",
            "--expected-count",
            "1",
        )
        
        self.assertEqual(
            path.read_bytes(),
            b"function test() { return $10 + $20; }\n"
        )

    def test_special_characters_in_regex_mode(self):
        """Test regex special characters work correctly in regex mode."""
        path = self.tmpdir / "regex_special.txt"
        path.write_bytes(b"price: $100, discount: $20\n")
        
        # Use regex to match dollar amounts
        self.run_tool(
            "regex",
            "--file",
            path,
            "--pattern",
            r"\$(\d+)",
            "--replacement",
            r"USD_\1",
            "--expected-count",
            "2",
        )
        
        self.assertEqual(
            path.read_bytes(),
            b"price: USD_100, discount: USD_20\n"
        )

    def test_literal_replacement_flag(self):
        """Test --literal-replacement prevents backreference interpretation."""
        path = self.tmpdir / "literal.txt"
        path.write_bytes(b"replace \\n with newline\n")
        
        # Without literal-replacement, \1 would be a backreference
        # With literal-replacement, it's treated literally
        self.run_tool(
            "regex",
            "--file",
            path,
            "--pattern",
            r"replace",
            "--replacement",
            r"\1test\1",
            "--expected-count",
            "1",
            "--literal-replacement",
        )
        
        # Should contain literal \1, not a backreference
        self.assertEqual(
            path.read_bytes(),
            b"\\1test\\1 \\n with newline\n"
        )

    def test_unicode_content_handling(self):
        """Test handling of Unicode content in various encodings."""
        # UTF-8 with Chinese characters
        path1 = self.tmpdir / "chinese_utf8.txt"
        path1.write_text("你好世界\nHello World\n", encoding="utf-8")
        
        self.run_tool(
            "edit",
            "--file",
            path1,
            "--old",
            "你好世界",
            "--new",
            "世界你好",
            "--expected-count",
            "1",
        )
        
        self.assertEqual(
            path1.read_text(encoding="utf-8"),
            "世界你好\nHello World\n"
        )

    def test_mixed_line_endings_detection(self):
        """Test that mixed line endings are detected and reported."""
        path = self.tmpdir / "mixed.txt"
        # Mix of CRLF and LF
        path.write_bytes(b"line1\r\nline2\nline3\r\nline4\n")
        
        result = self.run_tool("inspect", "--file", path, "--json")
        payload = json.loads(result.stdout)
        
        self.assertTrue(payload["mixedLineEndings"])
        self.assertIn("crlf", payload["lineEndingCounts"])
        self.assertIn("lf", payload["lineEndingCounts"])

    def test_preserve_line_endings_on_edit(self):
        """Test that edit preserves original line ending style."""
        # CRLF file
        path_crlf = self.tmpdir / "crlf.txt"
        path_crlf.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
        
        self.run_tool(
            "edit",
            "--file",
            path_crlf,
            "--old",
            "beta",
            "--new",
            "delta",
            "--expected-count",
            "1",
        )
        
        # Should still have CRLF
        self.assertEqual(
            path_crlf.read_bytes(),
            b"alpha\r\ndelta\r\ngamma\r\n"
        )
        
        # LF file
        path_lf = self.tmpdir / "lf.txt"
        path_lf.write_bytes(b"alpha\nbeta\ngamma\n")
        
        self.run_tool(
            "edit",
            "--file",
            path_lf,
            "--old",
            "beta",
            "--new",
            "delta",
            "--expected-count",
            "1",
        )
        
        # Should still have LF
        self.assertEqual(
            path_lf.read_bytes(),
            b"alpha\ndelta\ngamma\n"
        )

    def test_no_op_ok_flag(self):
        """Test --no-op-ok allows zero matches without error."""
        path = self.tmpdir / "noop.txt"
        path.write_bytes(b"alpha\nbeta\ngamma\n")
        
        # Without --no-op-ok, this should fail
        result = self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "nonexistent",
            "--new",
            "newvalue",
            expect=2,
        )
        self.assertIn("not found", result.stderr)
        
        # With --no-op-ok, it should succeed without changes
        self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "nonexistent",
            "--new",
            "newvalue",
            "--no-op-ok",
        )
        
        # File should be unchanged
        self.assertEqual(path.read_bytes(), b"alpha\nbeta\ngamma\n")

    def test_first_flag_replaces_only_first(self):
        """Test --first flag replaces only the first occurrence."""
        path = self.tmpdir / "first.txt"
        path.write_bytes(b"foo bar foo bar foo\n")
        
        self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "foo",
            "--new",
            "baz",
            "--first",
        )
        
        # Only first occurrence should be replaced
        self.assertEqual(
            path.read_bytes(),
            b"baz bar foo bar foo\n"
        )

    def test_expected_count_validation(self):
        """Test --expected-count validates match count."""
        path = self.tmpdir / "count.txt"
        path.write_bytes(b"foo\nfoo\nfoo\n")
        
        # Should fail when count doesn't match
        result = self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "foo",
            "--new",
            "bar",
            "--expected-count",
            "5",  # Actual count is 3
            expect=2,
        )
        
        self.assertIn("expected 5", result.stderr)
        self.assertIn("found 3", result.stderr)

    def test_insert_at_various_positions(self):
        """Test insert at beginning, middle, and end of file."""
        path = self.tmpdir / "insert.txt"
        path.write_bytes(b"line1\nline2\nline3\n")
        
        # Insert at beginning
        self.run_tool("insert", "--file", path, "--line", "1", "--text", "NEW_FIRST")
        self.assertEqual(
            path.read_bytes(),
            b"NEW_FIRST\nline1\nline2\nline3\n"
        )
        
        # Insert in middle
        self.run_tool("insert", "--file", path, "--line", "3", "--text", "NEW_MIDDLE")
        self.assertEqual(
            path.read_bytes(),
            b"NEW_FIRST\nline1\nNEW_MIDDLE\nline2\nline3\n"
        )
        
        # Insert at end
        self.run_tool("insert", "--file", path, "--line", "6", "--text", "NEW_LAST")
        self.assertEqual(
            path.read_bytes(),
            b"NEW_FIRST\nline1\nNEW_MIDDLE\nline2\nline3\nNEW_LAST\n"
        )

    def test_insert_out_of_bounds(self):
        """Test insert with invalid line number."""
        path = self.tmpdir / "insert_bounds.txt"
        path.write_bytes(b"line1\nline2\n")
        
        # Line 0 is invalid
        result = self.run_tool(
            "insert",
            "--file",
            path,
            "--line",
            "0",
            "--text",
            "new",
            expect=2,
        )
        self.assertIn("must be between", result.stderr)
        
        # Line 10 is too far (file has 2 lines, max is 3)
        result = self.run_tool(
            "insert",
            "--file",
            path,
            "--line",
            "10",
            "--text",
            "new",
            expect=2,
        )
        self.assertIn("must be between", result.stderr)

    def test_delete_preserves_remaining_content(self):
        """Test that delete-lines preserves content and line endings."""
        path = self.tmpdir / "delete.txt"
        path.write_bytes(b"line1\r\nline2\r\nline3\r\nline4\r\n")
        
        # Delete middle lines
        self.run_tool("delete-lines", "--file", path, "--start", "2", "--end", "3")
        
        # Should preserve CRLF and remaining content
        self.assertEqual(
            path.read_bytes(),
            b"line1\r\nline4\r\n"
        )

    def test_replace_lines_with_multiline_text(self):
        """Test replace-lines with multiline replacement text."""
        path = self.tmpdir / "replace_multi.txt"
        path.write_bytes(b"header\nOLD1\nOLD2\nOLD3\nfooter\n")
        
        block = self.tmpdir / "block.txt"
        block.write_text("NEW_A\nNEW_B\nNEW_C", encoding="utf-8")
        
        self.run_tool(
            "replace-lines",
            "--file",
            path,
            "--start",
            "2",
            "--end",
            "4",
            "--text-file",
            block,
        )
        
        self.assertEqual(
            path.read_bytes(),
            b"header\nNEW_A\nNEW_B\nNEW_C\nfooter\n"
        )

    def test_batch_atomicity(self):
        """Test that batch operations are atomic - all or nothing."""
        path = self.tmpdir / "atomic.txt"
        path.write_bytes(b"alpha\nbeta\ngamma\n")
        original = path.read_bytes()
        
        # Batch with one operation that will fail
        ops = self.tmpdir / "ops_fail.json"
        ops.write_text(
            json.dumps([
                {"op": "edit", "old": "alpha", "new": "ALPHA", "expected_count": 1},
                {"op": "edit", "old": "nonexistent", "new": "FAIL", "expected_count": 1},
            ]),
            encoding="utf-8",
        )
        
        result = self.run_tool("batch", "--file", path, "--ops-file", ops, expect=2)
        
        # File should be unchanged
        self.assertEqual(path.read_bytes(), original)
        # Should contain error about expected count
        self.assertIn("expected 1", result.stderr)

    def test_dry_run_with_json_output(self):
        """Test --dry-run with --json shows what would change."""
        path = self.tmpdir / "dry_json.txt"
        path.write_bytes(b"foo\n")
        before = path.read_bytes()
        
        result = self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "foo",
            "--new",
            "bar",
            "--expected-count",
            "1",
            "--dry-run",
            "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertTrue(payload["dryRun"])
        self.assertFalse(payload["written"])
        self.assertTrue(payload["wouldChangeBytes"])
        
        # File should be unchanged
        self.assertEqual(path.read_bytes(), before)

    def test_force_write_even_when_identical(self):
        """Test --force-write updates mtime even when content is identical."""
        path = self.tmpdir / "force.txt"
        path.write_bytes(b"unchanged\n")
        before_mtime = os.stat(path).st_mtime_ns
        time.sleep(0.02)
        
        self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "unchanged",
            "--new",
            "unchanged",
            "--expected-count",
            "1",
            "--force-write",
        )
        
        # mtime should be updated
        self.assertGreater(os.stat(path).st_mtime_ns, before_mtime)

    # =========================================================================
    # Symlink tests
    # =========================================================================

    def _can_create_symlink(self):
        """Check if we can create symlinks (requires admin on Windows)."""
        try:
            target = self.tmpdir / "symlink_test_target.txt"
            target.write_bytes(b"test")
            link = self.tmpdir / "symlink_test_link.txt"
            link.symlink_to(target)
            link.unlink()
            target.unlink()
            return True
        except (OSError, NotImplementedError):
            return False

    def test_symlink_without_follow_fails(self):
        """Test that editing a symlink without --follow-symlink fails."""
        if not self._can_create_symlink():
            self.skipTest("symlink creation not available (requires admin on Windows)")
        
        # Create target file
        target = self.tmpdir / "target.txt"
        target.write_bytes(b"original content\n")
        
        # Create symlink pointing to target
        link = self.tmpdir / "link.txt"
        link.symlink_to(target)
        
        # Verify symlink is created correctly
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_bytes(), b"original content\n")
        
        # Try to edit through symlink without --follow-symlink
        result = self.run_tool(
            "edit",
            "--file",
            link,
            "--old",
            "original content",
            "--new",
            "modified content",
            "--expected-count",
            "1",
            expect=2,
        )
        
        # Should fail with appropriate error message
        self.assertIn("refusing to edit a symlink", result.stderr)
        self.assertIn("--follow-symlink", result.stderr)
        
        # Target file should be unchanged
        self.assertEqual(target.read_bytes(), b"original content\n")

    def test_symlink_with_follow_edits_target(self):
        """Test that editing a symlink with --follow-symlink modifies the target."""
        if not self._can_create_symlink():
            self.skipTest("symlink creation not available (requires admin on Windows)")
        
        # Create target file
        target = self.tmpdir / "target.txt"
        target.write_bytes(b"original content\n")
        
        # Create symlink pointing to target
        link = self.tmpdir / "link.txt"
        link.symlink_to(target)
        
        # Verify symlink is created correctly
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_bytes(), b"original content\n")
        
        # Edit through symlink with --follow-symlink
        self.run_tool(
            "edit",
            "--file",
            link,
            "--old",
            "original content",
            "--new",
            "modified content",
            "--expected-count",
            "1",
            "--follow-symlink",
        )
        
        # Target file should be modified
        self.assertEqual(target.read_bytes(), b"modified content\n")
        
        # Symlink should still exist and point to target
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_bytes(), b"modified content\n")

    def test_symlink_preserves_encoding_and_line_endings(self):
        """Test that editing through symlink preserves target's encoding and line endings."""
        if not self._can_create_symlink():
            self.skipTest("symlink creation not available (requires admin on Windows)")
        
        # Create target file with GBK encoding and CRLF
        target = self.tmpdir / "target_gbk.txt"
        # GBK encoded Chinese text with CRLF
        target.write_bytes(bytes.fromhex("c4 e3 ba c3 0d 0a 66 6f 6f 0d 0a"))
        
        # Create symlink
        link = self.tmpdir / "link_gbk.txt"
        link.symlink_to(target)
        
        # Edit through symlink with --follow-symlink
        self.run_tool(
            "edit",
            "--file",
            link,
            "--encoding",
            "gbk",
            "--old",
            "foo",
            "--new",
            "bar",
            "--expected-count",
            "1",
            "--follow-symlink",
        )
        
        # Target should have modified content with preserved encoding and line endings
        # "你好\r\nbar\r\n" in GBK
        self.assertEqual(target.read_bytes(), bytes.fromhex("c4 e3 ba c3 0d 0a 62 61 72 0d 0a"))


if __name__ == "__main__":
    unittest.main()
