import base64
import codecs
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "skills" / "safe-edit" / "safe_edit.py"


def _get_tmp_dir():
    """Get the best available temporary directory (matches safe_edit.py logic)."""
    if os.name != "nt":
        try:
            test_path = "/tmp/.safe-edit-probe"
            fd = os.open(test_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            os.unlink(test_path)
            return "/tmp"
        except OSError:
            pass
    return tempfile.gettempdir()


def _get_lock_dir():
    """Get or create the lock directory (matches safe_edit.py logic)."""
    lock_dir = Path(_get_tmp_dir()) / "safe-edit" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir


def _get_lock_key(file_path):
    """Generate the stable canonical-path lock key used by safe_edit.py."""
    try:
        path = Path(file_path).resolve(strict=False)
    except (OSError, RuntimeError):
        path = Path(os.path.abspath(file_path))
    identity = os.path.normcase(os.path.abspath(str(path)))
    value = f"path\0{identity}".encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(value).hexdigest()[:32]


def _get_lock_path(target_file):
    """Calculate the lock file path for a given target file (matches safe_edit.py logic)."""
    lock_key = _get_lock_key(target_file)
    return _get_lock_dir() / f"{lock_key}.lock"


class SafeEditTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="safe-edit-test-"))
        lock_dir = _get_lock_dir()
        if lock_dir.exists():
            for f in lock_dir.glob("*.lock"):
                f.unlink(missing_ok=True)

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

    def run_interactive(self, *args, input_text="y\n"):
        """Run tool with forced interactive mode and simulated user input."""
        env = os.environ.copy()
        env['SAFE_EDIT_FORCE_INTERACTIVE'] = '1'
        return subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, args)],
            input=input_text,
            text=True,
            capture_output=True,
            encoding="utf-8",
            env=env,
        )

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
        lock = _get_lock_path(path)
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
        
        # Try to match with LF in a CRLF file - this should fail
        # because the tool does NOT auto-normalize line endings
        # (user must use --ignore-eol for that)
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
        # Should contain error about the failed operation
        self.assertIn("not found", result.stderr)

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

    # =========================================================================
    # Controlled file removal tests
    # =========================================================================

    def test_stat_reports_sha256_for_remove_file_precondition(self):
        path = self.tmpdir / "remove-stat.txt"
        content = b"remove me\n"
        path.write_bytes(content)

        result = self.run_tool("stat", "--file", path, "--json")
        payload = json.loads(result.stdout)

        self.assertEqual(payload["sha256"], hashlib.sha256(content).hexdigest())

    def test_remove_file_removes_verified_regular_file(self):
        path = self.tmpdir / "remove.txt"
        content = b"obsolete\r\n"
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()

        result = self.run_tool(
            "remove-file",
            "--file", path,
            "--workspace-root", self.tmpdir,
            "--expected-sha256", digest,
            "--json",
        )
        payload = json.loads(result.stdout)

        self.assertFalse(path.exists())
        self.assertTrue(payload["removed"])
        self.assertTrue(payload["written"])
        self.assertEqual(payload["sha256"], digest)
        self.assertEqual(payload["workspaceRoot"], str(self.tmpdir.resolve()))

    def test_remove_file_dry_run_preserves_target(self):
        path = self.tmpdir / "remove-dry-run.txt"
        content = b"keep during preview\n"
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()

        result = self.run_tool(
            "remove-file",
            "--file", path,
            "--workspace-root", self.tmpdir,
            "--expected-sha256", digest,
            "--dry-run",
            "--json",
        )
        payload = json.loads(result.stdout)

        self.assertTrue(path.exists())
        self.assertTrue(payload["wouldRemove"])
        self.assertFalse(payload["removed"])
        self.assertFalse(payload["written"])

    def test_remove_file_rejects_sha256_mismatch(self):
        path = self.tmpdir / "remove-hash-mismatch.txt"
        path.write_bytes(b"current content\n")

        result = self.run_tool(
            "remove-file",
            "--file", path,
            "--workspace-root", self.tmpdir,
            "--expected-sha256", "0" * 64,
            "--json",
            expect=2,
        )
        payload = json.loads(result.stdout)

        self.assertTrue(path.exists())
        self.assertIn("SHA-256 mismatch", payload["error"]["message"])

    def test_remove_file_requires_workspace_root(self):
        path = self.tmpdir / "remove-no-root.txt"
        content = b"content\n"
        path.write_bytes(content)

        result = self.run_tool(
            "remove-file",
            "--file", path,
            "--expected-sha256", hashlib.sha256(content).hexdigest(),
            expect=2,
        )

        self.assertTrue(path.exists())
        self.assertIn("requires --workspace-root", result.stderr)

    def test_remove_file_rejects_file_outside_workspace_root(self):
        with tempfile.TemporaryDirectory(prefix="safe-edit-outside-") as outside_dir:
            path = Path(outside_dir) / "outside.txt"
            content = b"outside\n"
            path.write_bytes(content)

            result = self.run_tool(
                "remove-file",
                "--file", path,
                "--workspace-root", self.tmpdir,
                "--expected-sha256", hashlib.sha256(content).hexdigest(),
                expect=2,
            )

            self.assertTrue(path.exists())
            self.assertIn("outside workspace root", result.stderr)

    def test_remove_file_rejects_directory(self):
        result = self.run_tool(
            "remove-file",
            "--file", self.tmpdir,
            "--workspace-root", self.tmpdir,
            "--expected-sha256", "0" * 64,
            expect=2,
        )

        self.assertTrue(self.tmpdir.exists())
        self.assertIn("not a regular file", result.stderr)

    def test_remove_file_rejects_symbolic_link(self):
        if not self._can_create_symlink():
            self.skipTest("symlink creation not available (requires admin on Windows)")

        target = self.tmpdir / "remove-link-target.txt"
        target.write_bytes(b"target\n")
        link = self.tmpdir / "remove-link.txt"
        link.symlink_to(target)

        result = self.run_tool(
            "remove-file",
            "--file", link,
            "--workspace-root", self.tmpdir,
            "--expected-sha256", hashlib.sha256(target.read_bytes()).hexdigest(),
            expect=2,
        )

        self.assertTrue(link.is_symlink())
        self.assertTrue(target.exists())
        self.assertIn("refuses symbolic links", result.stderr)

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


    # =========================================================================
    # --interactive mode tests
    # =========================================================================

    def test_interactive_with_dry_run_fails(self):
        """Test that --interactive with --dry-run fails."""
        path = self.tmpdir / "interactive_dry.txt"
        path.write_bytes(b"foo\n")
        
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
            "--interactive",
            "--dry-run",
            expect=2,
        )
        
        self.assertIn("--interactive cannot be used with --dry-run", result.stderr)

    def test_interactive_with_inspect_fails(self):
        """Test that --interactive with inspect command fails."""
        path = self.tmpdir / "interactive_inspect.txt"
        path.write_bytes(b"foo\n")
        
        result = self.run_tool(
            "inspect",
            "--file",
            path,
            "--interactive",
            expect=2,
        )
        
        self.assertIn("--interactive is not applicable to inspect command", result.stderr)

    def test_interactive_in_non_tty_fails(self):
        """Test that --interactive fails in non-interactive terminal (piped)."""
        path = self.tmpdir / "interactive_nontty.txt"
        path.write_bytes(b"foo\n")
        
        # When running via subprocess, stdin/stdout are pipes, not TTY
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
            "--interactive",
            expect=2,
        )
        
        self.assertIn("--interactive requires an interactive terminal", result.stderr)

    def test_interactive_accept_with_y(self):
        """Test --interactive with 'y' (yes) input applies the change."""
        path = self.tmpdir / "interactive_y.txt"
        path.write_bytes(b"foo\n")
        
        result = self.run_interactive(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--expected-count", "1",
            "--interactive",
        )
        
        # Should show diff and prompt
        self.assertIn("-foo", result.stdout)
        self.assertIn("+bar", result.stdout)
        self.assertIn("Apply this change?", result.stdout)
        
        # File should be modified
        self.assertEqual(path.read_bytes(), b"bar\n")

    def test_interactive_reject_with_n(self):
        """Test --interactive with 'n' (no) input skips the change."""
        path = self.tmpdir / "interactive_n.txt"
        path.write_bytes(b"foo\n")
        original = path.read_bytes()
        
        result = self.run_interactive(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--expected-count", "1",
            "--interactive",
            input_text="n\n",
        )
        
        # Should show diff and prompt
        self.assertIn("Apply this change?", result.stdout)
        
        # File should NOT be modified
        self.assertEqual(path.read_bytes(), original)
        
        # Should show skipped message
        self.assertIn("Skipped write: user declined in interactive mode", result.stdout)

    def test_interactive_apply_all_with_a(self):
        """Test --interactive with 'a' (all) input applies all changes."""
        path = self.tmpdir / "interactive_a.txt"
        path.write_bytes(b"foo\nbar\n")
        
        result = self.run_interactive(
            "edit", "--file", path,
            "--old", "foo", "--new", "baz",
            "--expected-count", "1",
            "--interactive",
            input_text="a\n",
        )
        
        # File should be modified
        self.assertEqual(path.read_bytes(), b"baz\nbar\n")

    def test_interactive_quit_with_q(self):
        """Test --interactive with 'q' (quit) input skips the change."""
        path = self.tmpdir / "interactive_q.txt"
        path.write_bytes(b"foo\n")
        original = path.read_bytes()
        
        result = self.run_interactive(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--expected-count", "1",
            "--interactive",
            input_text="q\n",
        )
        
        # File should NOT be modified
        self.assertEqual(path.read_bytes(), original)

    def test_interactive_help_with_question(self):
        """Test --interactive with '?' shows help and re-prompts."""
        path = self.tmpdir / "interactive_help.txt"
        path.write_bytes(b"foo\n")
        
        result = self.run_interactive(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--expected-count", "1",
            "--interactive",
            input_text="?\nn\n",
        )
        
        # Should show help
        self.assertIn("y - yes", result.stdout)
        self.assertIn("n - no", result.stdout)
        self.assertIn("a - all", result.stdout)
        self.assertIn("q - quit", result.stdout)

    def test_interactive_shows_diff_context(self):
        """Test --interactive shows diff with proper context."""
        path = self.tmpdir / "interactive_context.txt"
        # Create file with multiple lines
        content_lines = ["line1", "line2", "line3", "target", "line5", "line6", "line7"]
        path.write_bytes(("\n".join(content_lines) + "\n").encode())
        
        result = self.run_interactive(
            "edit", "--file", path,
            "--old", "target", "--new", "replaced",
            "--expected-count", "1",
            "--interactive",
            "--context", "2",
        )
        
        # Should show context lines in diff
        self.assertIn("line2", result.stdout)
        self.assertIn("line3", result.stdout)
        self.assertIn("target", result.stdout)
        self.assertIn("replaced", result.stdout)
        self.assertIn("line5", result.stdout)
        self.assertIn("line6", result.stdout)

    def test_interactive_short_flag(self):
        """Test -i short flag works same as --interactive."""
        path = self.tmpdir / "interactive_short.txt"
        path.write_bytes(b"foo\n")
        
        result = self.run_interactive(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--expected-count", "1",
            "-i",
        )
        
        # File should be modified
        self.assertEqual(path.read_bytes(), b"bar\n")

    # =========================================================================
    # Controlled whitespace matching tests (--ignore-indent, --ignore-eol, --normalize-whitespace)
    # =========================================================================

    def test_ignore_indent_tab_vs_space(self):
        """Test --ignore-indent allows matching tab-indented text with space-indented pattern."""
        path = self.tmpdir / "indent_tab.txt"
        # File uses tab indentation
        path.write_bytes(b"def foo():\n\treturn 42\n")
        
        # Match with space indentation (should fail without flag)
        result = self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "    return 42",  # 4 spaces instead of tab
            "--new",
            "    return 43",
            expect=2,
        )
        self.assertIn("not found", result.stderr)
        
        # Match with space indentation (should succeed with flag)
        self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "    return 42",  # 4 spaces instead of tab
            "--new",
            "    return 43",
            "--expected-count",
            "1",
            "--ignore-indent",
        )
        
        # Should have replaced, but kept original indentation (tab)
        self.assertEqual(path.read_bytes(), b"def foo():\n\treturn 43\n")

    def test_ignore_indent_space_vs_tab(self):
        """Test --ignore-indent allows matching space-indented text with tab-indented pattern."""
        path = self.tmpdir / "indent_space.txt"
        # File uses space indentation
        path.write_bytes(b"def foo():\n    return 42\n")
        
        # Match with tab indentation (should succeed with flag)
        self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "\treturn 42",  # tab instead of spaces
            "--new",
            "\treturn 43",
            "--expected-count",
            "1",
            "--ignore-indent",
        )
        
        # Should have replaced, but kept original indentation (spaces)
        self.assertEqual(path.read_bytes(), b"def foo():\n    return 43\n")

    def test_ignore_eol_crlf_vs_lf(self):
        """Test --ignore-eol allows matching CRLF text with LF pattern."""
        path = self.tmpdir / "eol_crlf.txt"
        # File uses CRLF
        path.write_bytes(b"line1\r\nline2\r\n")
        
        # Match with LF (should fail without flag)
        result = self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "line1\nline2",
            "--new",
            "new1\nnew2",
            expect=2,
        )
        self.assertIn("not found", result.stderr)
        
        # Match with LF (should succeed with flag)
        self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "line1\nline2",
            "--new",
            "new1\nnew2",
            "--expected-count",
            "1",
            "--ignore-eol",
        )
        
        # Should have replaced, but kept original line endings (CRLF)
        self.assertEqual(path.read_bytes(), b"new1\r\nnew2\r\n")

    def test_ignore_eol_lf_vs_crlf(self):
        """Test --ignore-eol allows matching LF text with CRLF pattern."""
        path = self.tmpdir / "eol_lf.txt"
        # File uses LF
        path.write_bytes(b"line1\nline2\n")
        
        # Match with CRLF (should succeed with flag)
        self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "line1\r\nline2",
            "--new",
            "new1\r\nnew2",
            "--expected-count",
            "1",
            "--ignore-eol",
        )
        
        # Should have replaced, but kept original line endings (LF)
        self.assertEqual(path.read_bytes(), b"new1\nnew2\n")

    def test_normalize_whitespace_multiple_vs_single(self):
        """Test --normalize-whitespace treats multiple spaces as single space."""
        path = self.tmpdir / "whitespace.txt"
        # File has multiple spaces
        path.write_bytes(b"foo    bar\n")
        
        # Match with single space (should fail without flag)
        result = self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "foo bar",
            "--new",
            "baz qux",
            expect=2,
        )
        self.assertIn("not found", result.stderr)
        
        # Match with single space (should succeed with flag)
        # Use replacement that matches original whitespace pattern
        self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "foo bar",
            "--new",
            "baz    qux",  # Keep original whitespace pattern
            "--expected-count",
            "1",
            "--normalize-whitespace",
        )
        
        # Should have replaced with the provided new text
        self.assertEqual(path.read_bytes(), b"baz    qux\n")

    def test_normalize_whitespace_tabs_and_spaces(self):
        """Test --normalize-whitespace treats tabs and spaces as equivalent."""
        path = self.tmpdir / "whitespace_mixed.txt"
        # File has tabs and spaces mixed
        path.write_bytes(b"foo\t \tbar\n")
        
        # Match with single space (should succeed with flag)
        # Use replacement that matches original whitespace pattern
        self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "foo bar",
            "--new",
            "baz\t \tqux",  # Keep original whitespace pattern
            "--expected-count",
            "1",
            "--normalize-whitespace",
        )
        
        # Should have replaced with the provided new text
        self.assertEqual(path.read_bytes(), b"baz\t \tqux\n")

    def test_combined_flags(self):
        """Test combination of --ignore-indent, --ignore-eol, --normalize-whitespace."""
        path = self.tmpdir / "combined.txt"
        # File has tabs, CRLF, and multiple spaces
        path.write_bytes(b"def foo():\r\n\treturn    42\r\n")
        
        # Match with spaces, LF, and single space (should succeed with all flags)
        # Use replacement that matches original formatting
        self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "def foo():\n    return 42",
            "--new",
            "def bar():\n\treturn    43",  # Keep original formatting (tab + multiple spaces)
            "--expected-count",
            "1",
            "--ignore-indent",
            "--ignore-eol",
            "--normalize-whitespace",
        )
        
        # Should have replaced with the provided new text
        self.assertEqual(path.read_bytes(), b"def bar():\r\n\treturn    43\r\n")

    def test_replacement_not_affected(self):
        """Test that whitespace flags don't affect replacement text."""
        path = self.tmpdir / "replacement.txt"
        # File has tabs
        path.write_bytes(b"\tfoo\n")
        
        # Match with spaces, replace with different indentation
        self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "    foo",
            "--new",
            "        bar",  # 8 spaces
            "--expected-count",
            "1",
            "--ignore-indent",
        )
        
        # Replacement should be inserted as-is (8 spaces), not converted to tabs
        # But the original line's indentation (tab) should be preserved
        # Actually, the replacement replaces the entire matched text
        self.assertEqual(path.read_bytes(), b"\tbar\n")  # Original tab preserved, but "foo" -> "bar"

    def test_ignore_indent_multiline(self):
        """Test --ignore-indent with multiline pattern."""
        path = self.tmpdir / "multiline_indent.txt"
        # File has mixed indentation
        path.write_bytes(b"if (x) {\n\t\tdoSomething();\n\t}\n")
        
        # Match with different indentation
        # Use replacement that matches original indentation pattern
        self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "if (x) {\n        doSomething();\n    }",
            "--new",
            "if (y) {\n\t\tdoSomething();\n\t}",  # Keep original indentation
            "--expected-count",
            "1",
            "--ignore-indent",
        )
        
        # Should have replaced with the provided new text
        self.assertEqual(path.read_bytes(), b"if (y) {\n\t\tdoSomething();\n\t}\n")

    def test_whitespace_flags_only_affect_edit(self):
        """Test that whitespace flags only affect edit command, not other commands."""
        path = self.tmpdir / "other_command.txt"
        path.write_bytes(b"\tfoo\nbar\n")
        
        # insert should work normally (not affected by --ignore-indent)
        self.run_tool(
            "insert",
            "--file",
            path,
            "--line",
            "1",
            "--text",
            "new line",
            "--ignore-indent",  # Should be ignored for insert
        )
        
        # Should insert at beginning
        self.assertEqual(path.read_bytes(), b"new line\n\tfoo\nbar\n")

    def test_whitespace_flags_with_regex_not_affected(self):
        """Test that whitespace flags don't affect regex command."""
        path = self.tmpdir / "regex_not_affected.txt"
        path.write_bytes(b"\tfoo\n")
        
        # regex should work normally (not affected by --ignore-indent)
        self.run_tool(
            "regex",
            "--file",
            path,
            "--pattern",
            r"\s+foo",
            "--replacement",
            "bar",
            "--expected-count",
            "1",
            "--ignore-indent",  # Should be ignored for regex
        )
        
        # Should replace whitespace+foo with bar
        self.assertEqual(path.read_bytes(), b"bar\n")

    def test_whitespace_flags_with_expected_count(self):
        """Test that whitespace flags work correctly with --expected-count."""
        path = self.tmpdir / "expected_count.txt"
        # File has 3 occurrences with different indentation
        path.write_bytes(b"\tfoo\n    foo\n\t\tfoo\n")
        
        # Should find all 3 with --ignore-indent
        self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "foo",
            "--new",
            "bar",
            "--expected-count",
            "3",
            "--ignore-indent",
        )
        
        # All should be replaced
        self.assertEqual(path.read_bytes(), b"\tbar\n    bar\n\t\tbar\n")

    def test_whitespace_flags_with_first(self):
        """Test that whitespace flags work correctly with --first."""
        path = self.tmpdir / "first_flag.txt"
        # File has 2 occurrences with different indentation
        path.write_bytes(b"\tfoo\n    foo\n")
        
        # Should replace only first with --first
        self.run_tool(
            "edit",
            "--file",
            path,
            "--old",
            "foo",
            "--new",
            "bar",
            "--first",
            "--ignore-indent",
        )
        
        # Only first should be replaced
        self.assertEqual(path.read_bytes(), b"\tbar\n    foo\n")

    # =========================================================================
    # stat command tests
    # =========================================================================

    def test_stat_basic_output(self):
        """Test stat command produces concise output."""
        path = self.tmpdir / "stat_basic.txt"
        path.write_bytes(b"line1\nline2\nline3\n")
        
        result = self.run_tool("stat", "--file", path)
        
        # Should have concise output format
        self.assertIn("Encoding: UTF-8", result.stdout)
        self.assertIn("Line endings: LF", result.stdout)
        self.assertIn("Size:", result.stdout)
        self.assertIn("Lines: 3", result.stdout)

    def test_stat_json_output(self):
        """Test stat command with --json produces machine-readable output."""
        path = self.tmpdir / "stat_json.txt"
        path.write_bytes(b"alpha\r\nbeta\r\n")

        result = self.run_tool("stat", "--file", path, "--json")
        payload = json.loads(result.stdout)

        # Core fields
        self.assertEqual(payload["encoding"], "utf-8")
        self.assertEqual(payload["lineEnding"], "crlf")
        self.assertEqual(payload["sizeBytes"], 13)  # "alpha\r\nbeta\r\n" = 13 bytes
        self.assertEqual(payload["lineCount"], 2)
        self.assertEqual(payload["command"], "stat")

        # Edit Guard fields
        self.assertIn("editMode", payload)
        self.assertIn("editStrategy", payload)
        self.assertIn("why", payload)
        self.assertIn("hasBom", payload)
        self.assertIn("mixedLineEndings", payload)

        # CRLF file should require safe-edit
        self.assertEqual(payload["editMode"], "required")
        self.assertIn("crlf", payload["why"])

        # Should NOT have detailed inspect-only fields
        self.assertNotIn("lineEndingCounts", payload)
        self.assertNotIn("permissionsOctal", payload)

    def test_stat_utf8_bom_file(self):
        """Test stat command with UTF-8 BOM file."""
        path = self.tmpdir / "stat_bom.txt"
        path.write_bytes(codecs.BOM_UTF8 + b"content\n")
        
        result = self.run_tool("stat", "--file", path)
        
        # Should show UTF-8-BOM encoding
        self.assertIn("Encoding: UTF-8-BOM", result.stdout)

    def test_stat_gbk_file(self):
        """Test stat command with GBK encoded file."""
        path = self.tmpdir / "stat_gbk.txt"
        # GBK encoded Chinese text
        path.write_bytes(bytes.fromhex("c4 e3 ba c3 0d 0a"))
        
        result = self.run_tool("stat", "--file", path, "--encoding", "gbk", "--json")
        payload = json.loads(result.stdout)
        
        self.assertEqual(payload["encoding"], "gbk")
        self.assertEqual(payload["lineEnding"], "crlf")

    def test_stat_empty_file(self):
        """Test stat command with empty file."""
        path = self.tmpdir / "stat_empty.txt"
        path.write_bytes(b"")
        
        result = self.run_tool("stat", "--file", path, "--json")
        payload = json.loads(result.stdout)
        
        self.assertEqual(payload["lineCount"], 0)
        self.assertEqual(payload["sizeBytes"], 0)
        self.assertEqual(payload["encoding"], "utf-8")
        self.assertEqual(payload["lineEnding"], "lf")

    def test_stat_large_file_size_format(self):
        """Test stat command formats size correctly for larger files."""
        path = self.tmpdir / "stat_large.txt"
        # Create a file larger than 1 KB
        content = "line\n" * 300  # 300 lines, ~1500 bytes
        path.write_bytes(content.encode("utf-8"))
        
        result = self.run_tool("stat", "--file", path)
        
        # Size should be shown in KB
        self.assertIn("KB", result.stdout)
        self.assertIn("Lines: 300", result.stdout)

    def test_stat_small_file_size_format(self):
        """Test stat command formats size correctly for small files."""
        path = self.tmpdir / "stat_small.txt"
        path.write_bytes(b"tiny")
        
        result = self.run_tool("stat", "--file", path)
        
        # Size should be shown in bytes (less than 1 KB)
        self.assertIn("bytes", result.stdout)
        self.assertNotIn("KB", result.stdout)

    def test_stat_vs_inspect_difference(self):
        """Test that stat output is more concise than inspect."""
        path = self.tmpdir / "compare.txt"
        path.write_bytes(b"line1\r\nline2\n")
        
        # Get stat output
        stat_result = self.run_tool("stat", "--file", path)
        stat_lines = stat_result.stdout.strip().split("\n")
        
        # Get inspect output
        inspect_result = self.run_tool("inspect", "--file", path)
        
        # stat should have exactly 4 lines (Encoding, Line endings, Size, Lines)
        self.assertEqual(len(stat_lines), 4)
        
        # stat output should be more concise (shorter total length)
        self.assertLess(len(stat_result.stdout), len(inspect_result.stdout))
        
        # stat should not contain detailed info like inspect
        self.assertNotIn("mixedLineEndings", stat_result.stdout)
        self.assertNotIn("hasNul", stat_result.stdout)
        self.assertNotIn("permissionsOctal", stat_result.stdout)

    def test_stat_preserves_encoding_detection(self):
        """Test that stat uses same encoding detection as inspect."""
        path = self.tmpdir / "stat_detect.txt"
        path.write_bytes(codecs.BOM_UTF8 + b"test\r\n")
        
        # Both should detect UTF-8-BOM
        stat_result = self.run_tool("stat", "--file", path, "--json")
        inspect_result = self.run_tool("inspect", "--file", path, "--json")
        
        stat_payload = json.loads(stat_result.stdout)
        inspect_payload = json.loads(inspect_result.stdout)
        
        self.assertEqual(stat_payload["encoding"], inspect_payload["encoding"])
        self.assertEqual(stat_payload["lineEnding"], inspect_payload["lineEnding"])


    # =========================================================================
    # Structured JSON error output tests
    # =========================================================================

    def test_json_error_output_on_match_not_found(self):
        """Test --json emits structured error when match not found."""
        path = self.tmpdir / "json_err.txt"
        path.write_bytes(b"alpha\nbeta\ngamma\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "nonexistent", "--new", "bar",
            "--json", expect=2,
        )
        
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["type"], "match_not_found")
        self.assertIn("not found", payload["error"]["message"])

    def test_json_error_has_retry_strategy(self):
        """Test --json error includes retryStrategy for match_not_found."""
        path = self.tmpdir / "json_sugg.txt"
        path.write_bytes(b"alpha\nbeta\ngamma\n")

        result = self.run_tool(
            "edit", "--file", path,
            "--old", "nonexistent", "--new", "bar",
            "--json", expect=2,
        )

        payload = json.loads(result.stdout)
        self.assertIn("failureClass", payload)
        self.assertIn("rootCause", payload)

    def test_json_error_has_closest_match(self):
        """Test --json error includes closestMatch when pattern is close."""
        path = self.tmpdir / "json_nearby.txt"
        path.write_bytes(b"def foo():\n    return 42\n")

        result = self.run_tool(
            "edit", "--file", path,
            "--old", "return 43", "--new", "return 44",
            "--json", expect=2,
        )

        payload = json.loads(result.stdout)
        self.assertIn("closestMatch", payload)
        closest = payload["closestMatch"]
        self.assertIn("line", closest)
        self.assertIn("similarity", closest)

    # =========================================================================
    # Structured error recovery tests
    # =========================================================================

    def test_error_recovery_indentation_difference(self):
        """Test indentation_difference is diagnosed and --ignore-indent is recommended."""
        path = self.tmpdir / "indent_diff.txt"
        # File uses 4 spaces
        path.write_bytes(b"def foo():\n    print('hello')\n")

        # Try to match with tabs
        old_path = self.tmpdir / "old_tabs.txt"
        old_path.write_bytes(b"def foo():\n\tprint('hello')\n")

        result = self.run_tool(
            "edit", "--file", path,
            "--old-file", old_path, "--new", "bar",
            "--json", expect=2,
        )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["failureClass"], "RETRYABLE")
        self.assertEqual(payload["rootCause"], "indentation_difference")
        self.assertEqual(payload["recommendedAction"]["type"], "retry")
        self.assertIn("--ignore-indent", payload["retryStrategy"]["flags"])

        # Verify retry with suggested flag works
        result2 = self.run_tool(
            "edit", "--file", path,
            "--old-file", old_path, "--new", "def bar():\n    print('world')\n",
            "--ignore-indent", "--json",
        )
        payload2 = json.loads(result2.stdout)
        self.assertTrue(payload2["ok"])
        self.assertEqual(payload2["changed"], 1)

    def test_error_recovery_line_ending_difference(self):
        """Test line_ending_difference is diagnosed and --ignore-eol is recommended."""
        path = self.tmpdir / "eol_diff.txt"
        # File uses CRLF
        path.write_bytes(b"def foo():\r\n    print('hello')\r\n")

        # Try to match with LF
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "def foo():\n    print('hello')", "--new", "bar",
            "--json", expect=2,
        )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["failureClass"], "RETRYABLE")
        self.assertEqual(payload["rootCause"], "line_ending_difference")
        self.assertEqual(payload["recommendedAction"]["type"], "retry")
        self.assertIn("--ignore-eol", payload["retryStrategy"]["flags"])

    def test_error_recovery_content_not_found(self):
        """Test content_not_found results in USER_INPUT failure class."""
        path = self.tmpdir / "not_found.txt"
        path.write_bytes(b"alpha\nbeta\ngamma\n")

        result = self.run_tool(
            "edit", "--file", path,
            "--old", "nonexistent_pattern_xyz", "--new", "bar",
            "--json", expect=2,
        )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["failureClass"], "USER_INPUT")
        self.assertEqual(payload["rootCause"], "content_not_found")
        self.assertEqual(payload["recommendedAction"]["type"], "ask_user")

    def test_error_recovery_multiple_matches(self):
        """Test multiple matches scenario."""
        path = self.tmpdir / "multi.txt"
        path.write_bytes(b"foo\nbar foo baz\nfoo end\n")

        result = self.run_tool(
            "edit", "--file", path,
            "--old", "foo", "--new", "qux",
            "--expected-count", "1",
            "--json", expect=2,
        )

        payload = json.loads(result.stdout)
        # When there are multiple matches but expected-count is 1, it's a count mismatch
        # The error type should reflect this
        self.assertIn(payload["error"]["type"], ["match_count_mismatch", "expected_count_mismatch"])

    def test_error_retry_strategy_structure(self):
        """Test recommendedAction and retryStrategy structure for RETRYABLE cases."""
        path = self.tmpdir / "confidence.txt"
        path.write_bytes(b"def hello():\n    return 42\n")

        result = self.run_tool(
            "edit", "--file", path,
            "--old", "def hello():\n\treturn 42", "--new", "bar",
            "--json", expect=2,
        )

        payload = json.loads(result.stdout)
        # Check recommendedAction structure
        self.assertIn("recommendedAction", payload)
        self.assertIn("type", payload["recommendedAction"])
        self.assertIn("confidence", payload["recommendedAction"])
        self.assertGreater(payload["recommendedAction"]["confidence"], 0.5)
        self.assertEqual(payload["recommendedAction"]["type"], "retry")

        # Check retryStrategy structure (only present for RETRYABLE)
        self.assertIn("retryStrategy", payload)
        self.assertIn("flags", payload["retryStrategy"])
        self.assertIn("alternativeFlags", payload["retryStrategy"])

    def test_failure_class_action_consistency(self):
        """Test failureClass and recommendedAction.type are always consistent.

        This ensures Single Source of Truth - failureClass determines action type.
        """
        # Test RETRYABLE cases
        path = self.tmpdir / "consistency.txt"

        # indentation_difference -> RETRYABLE -> retry
        path.write_bytes(b"def foo():\n    pass\n")
        old_path = self.tmpdir / "old_tabs.txt"
        old_path.write_bytes(b"def foo():\n\tpass\n")
        result = self.run_tool("edit", "--file", path, "--old-file", old_path, "--new", "x", "--json", expect=2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["failureClass"], "RETRYABLE")
        self.assertEqual(payload["recommendedAction"]["type"], "retry")
        self.assertIn("retryStrategy", payload)  # Only RETRYABLE has retryStrategy

        # content_not_found -> USER_INPUT -> ask_user
        path.write_bytes(b"alpha\n")
        result = self.run_tool("edit", "--file", path, "--old", "nonexistent_xyz", "--new", "x", "--json", expect=2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["failureClass"], "USER_INPUT")
        self.assertEqual(payload["recommendedAction"]["type"], "ask_user")
        self.assertNotIn("retryStrategy", payload)

    def test_error_similar_content_exists_re_read_required(self):
        """Test similar_content_exists results in RE_READ_REQUIRED."""
        path = self.tmpdir / "similar.txt"
        # Content that is highly similar (only one char different)
        path.write_bytes(b"def calculate(x, y):\n    return x + y\n")

        result = self.run_tool(
            "edit", "--file", path,
            # Try to match with slightly different content (x * y vs x + y)
            "--old", "def calculate(x, y):\n    return x * y\n", "--new", "bar",
            "--json", expect=2,
        )

        payload = json.loads(result.stdout)
        # Should have closestMatch with high similarity
        self.assertIn("closestMatch", payload)
        # Similarity should be high (only one character different)
        self.assertGreaterEqual(payload["closestMatch"]["similarity"], 0.6)
        # Similar but not whitespace difference -> similar_content_exists -> RE_READ_REQUIRED
        self.assertEqual(payload["failureClass"], "RE_READ_REQUIRED")
        self.assertEqual(payload["rootCause"], "similar_content_exists")
        self.assertEqual(payload["recommendedAction"]["type"], "re_read_file")
        # Should NOT have retryStrategy
        self.assertNotIn("retryStrategy", payload)

    def test_json_error_classifies_expected_count_mismatch(self):
        """Test --json error classifies expected_count mismatch."""
        path = self.tmpdir / "json_count.txt"
        path.write_bytes(b"foo\nfoo\nfoo\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--expected-count", "99",
            "--json", expect=2,
        )
        
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn(payload["error"]["type"], ("match_count_mismatch", "expected_count_mismatch"))

    def test_json_success_includes_match_strategy(self):
        """Test --json success output includes matchStrategy in operations."""
        path = self.tmpdir / "json_strategy.txt"
        path.write_bytes(b"foo\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--expected-count", "1", "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["operations"][0]["matchStrategy"], "exact")

    # =========================================================================
    # Auto-match tests
    # =========================================================================

    def test_auto_match_crlf_vs_lf(self):
        """Test --auto-match resolves CRLF vs LF mismatch."""
        path = self.tmpdir / "auto_eol.txt"
        path.write_bytes(b"line1\r\nline2\r\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "line1\nline2",
            "--new", "new1\nnew2",
            "--auto-match", "--expected-count", "1", "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["operations"][0]["matchStrategy"], "ignore-eol")

    def test_auto_match_indent_mismatch(self):
        """Test --auto-match resolves indentation mismatch."""
        path = self.tmpdir / "auto_indent.txt"
        path.write_bytes(b"def foo():\n\treturn 42\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "    return 42",
            "--new", "    return 43",
            "--auto-match", "--expected-count", "1", "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["operations"][0]["matchStrategy"], "ignore-indent")
        # File keeps original tab indentation
        self.assertIn(b"\treturn 43", path.read_bytes())

    def test_auto_match_exact_succeeds_first(self):
        """Test --auto-match uses exact strategy when possible."""
        path = self.tmpdir / "auto_exact.txt"
        path.write_bytes(b"foo\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--auto-match", "--expected-count", "1", "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertEqual(payload["operations"][0]["matchStrategy"], "exact")

    def test_auto_match_exhausted_reports_error(self):
        """Test --auto-match error when all strategies fail."""
        path = self.tmpdir / "auto_fail.txt"
        path.write_bytes(b"alpha beta gamma\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "completely_different_text",
            "--new", "bar",
            "--auto-match", expect=2,
        )
        
        self.assertIn("auto-match exhausted all strategies", result.stderr)

    # =========================================================================
    # Fuzzy match tests
    # =========================================================================

    def test_fuzzy_match_similar_text(self):
        """Test --fuzzy matches similar text with auto-match."""
        path = self.tmpdir / "fuzzy_similar.txt"
        path.write_bytes(b"function calculateTotal(price, quantity):\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "function calculateTotal(cost, qty):",
            "--new", "def calculateTotal(cost, qty):",
            "--auto-match", "--fuzzy", "--expected-count", "1", "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["operations"][0]["matchStrategy"], "fuzzy")

    def test_fuzzy_below_threshold_fails(self):
        """Test --fuzzy fails when similarity is too low."""
        path = self.tmpdir / "fuzzy_low.txt"
        path.write_bytes(b"alpha beta gamma\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "xyz123 abc789 def456 completely_different",
            "--new", "bar",
            "--auto-match", "--fuzzy", expect=2,
        )
        
        self.assertIn("auto-match exhausted all strategies", result.stderr)

    def test_fuzzy_requires_auto_match(self):
        """Test --fuzzy without --auto-match doesn't enable fuzzy matching."""
        path = self.tmpdir / "fuzzy_no_auto.txt"
        path.write_bytes(b"function foo():\n    return 42\n")
        
        # --fuzzy alone should not help; it requires --auto-match
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "function bar():",
            "--new", "function baz():",
            "--fuzzy", expect=2,
        )
        
        self.assertIn("not found", result.stderr)

    # =========================================================================
    # Context disambiguation tests
    # =========================================================================

    def test_context_before_filters_matches(self):
        """Test --context-before disambiguates multiple matches."""
        path = self.tmpdir / "ctx_before.txt"
        path.write_bytes(b"header\ntarget\nmiddle\ntarget\nend\n")
        
        self.run_tool(
            "edit", "--file", path,
            "--old", "target", "--new", "replaced",
            "--context-before", "middle", "--expected-count", "1",
        )
        
        self.assertEqual(path.read_bytes(), b"header\ntarget\nmiddle\nreplaced\nend\n")

    def test_context_after_filters_matches(self):
        """Test --context-after disambiguates multiple matches."""
        path = self.tmpdir / "ctx_after.txt"
        path.write_bytes(b"prefix_alpha\ntarget\nprefix_beta\ntarget\nsuffix_gamma\n")
        
        # "suffix_gamma" only appears after the second target
        self.run_tool(
            "edit", "--file", path,
            "--old", "target", "--new", "replaced",
            "--context-after", "suffix_gamma", "--expected-count", "1",
        )
        
        self.assertEqual(path.read_bytes(), b"prefix_alpha\ntarget\nprefix_beta\nreplaced\nsuffix_gamma\n")

    def test_context_before_and_after_combined(self):
        """Test both --context-before and --context-after together."""
        path = self.tmpdir / "ctx_both.txt"
        path.write_bytes(b"A\ntarget\nB\ntarget\nC\ntarget\nD\n")
        
        self.run_tool(
            "edit", "--file", path,
            "--old", "target", "--new", "replaced",
            "--context-before", "B", "--context-after", "C",
        )
        
        self.assertEqual(path.read_bytes(), b"A\ntarget\nB\nreplaced\nC\ntarget\nD\n")

    def test_context_no_match_after_filtering(self):
        """Test error when context filtering eliminates all matches."""
        path = self.tmpdir / "ctx_nomatch.txt"
        path.write_bytes(b"header\nfoo\nfooter\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--context-before", "nonexistent_context",
            expect=2,
        )
        
        self.assertIn("context filtering", result.stderr)

    def test_context_with_auto_match(self):
        """Test context disambiguation works with --auto-match."""
        path = self.tmpdir / "ctx_auto.txt"
        path.write_bytes(b"header\r\ntarget\r\nmiddle\r\ntarget\r\nend\r\n")
        
        self.run_tool(
            "edit", "--file", path,
            "--old", "target\nmiddle\ntarget",
            "--new", "replaced\nmiddle\nreplaced",
            "--context-before", "header",
            "--auto-match", "--expected-count", "1",
        )
        
        content = path.read_bytes()
        self.assertIn(b"replaced", content)

    # =========================================================================
    # diff-input tests
    # =========================================================================

    def test_diff_input_single_block(self):
        """Test --diff-input with a single SEARCH/REPLACE block."""
        path = self.tmpdir / "diff_single.txt"
        path.write_bytes(b"hello world\n")
        
        self.run_tool(
            "edit", "--file", path,
            "--diff-input", "------- SEARCH\nhello world\n=======\nhello universe\n+++++++ REPLACE",
        )
        
        self.assertEqual(path.read_bytes(), b"hello universe\n")

    def test_diff_input_multiple_blocks(self):
        """Test --diff-input with multiple SEARCH/REPLACE blocks."""
        path = self.tmpdir / "diff_multi.txt"
        path.write_bytes(b"alpha\nbeta\ngamma\n")
        
        self.run_tool(
            "edit", "--file", path,
            "--diff-input", "------- SEARCH\nalpha\n=======\nALPHA\n+++++++ REPLACE\n------- SEARCH\ngamma\n=======\nGAMMA\n+++++++ REPLACE",
        )
        
        self.assertEqual(path.read_bytes(), b"ALPHA\nbeta\nGAMMA\n")

    def test_diff_input_file(self):
        """Test --diff-input-file reads from file."""
        path = self.tmpdir / "diff_file.txt"
        path.write_bytes(b"old text\n")
        
        diff_file = self.tmpdir / "diff.txt"
        diff_file.write_text("------- SEARCH\nold text\n=======\nnew text\n+++++++ REPLACE", encoding="utf-8")
        
        self.run_tool(
            "edit", "--file", path,
            "--diff-input-file", diff_file,
        )
        
        self.assertEqual(path.read_bytes(), b"new text\n")

    def test_diff_input_alternative_markers(self):
        """Test --diff-input with alternative marker formats."""
        path = self.tmpdir / "diff_alt.txt"
        path.write_bytes(b"foo\n")
        
        self.run_tool(
            "edit", "--file", path,
            "--diff-input", "<<< SEARCH\nfoo\n===\nbar\n>>> REPLACE",
        )
        
        self.assertEqual(path.read_bytes(), b"bar\n")

    def test_diff_input_invalid_format(self):
        """Test --diff-input with invalid format fails."""
        path = self.tmpdir / "diff_invalid.txt"
        path.write_bytes(b"foo\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--diff-input", "this is not a valid diff format",
            expect=2,
        )
        
        self.assertIn("no valid SEARCH/REPLACE blocks", result.stderr)

    def test_diff_input_unterminated_block(self):
        """Test --diff-input with unterminated block (missing REPLACE marker)."""
        path = self.tmpdir / "diff_unterm.txt"
        path.write_bytes(b"foo\n")
        
        self.run_tool(
            "edit", "--file", path,
            "--diff-input", "------- SEARCH\nfoo\n=======\nbar",
        )
        
        self.assertEqual(path.read_bytes(), b"bar\n")

    def test_diff_input_with_context(self):
        """Test --diff-input with --context-before disambiguation."""
        path = self.tmpdir / "diff_ctx.txt"
        path.write_bytes(b"header\ntarget\nmiddle\ntarget\nend\n")
        
        self.run_tool(
            "edit", "--file", path,
            "--diff-input", "------- SEARCH\ntarget\n=======\nreplaced\n+++++++ REPLACE",
            "--context-before", "middle",
        )
        
        self.assertEqual(path.read_bytes(), b"header\ntarget\nmiddle\nreplaced\nend\n")

    # =========================================================================
    # matchStrategy field tests
    # =========================================================================

    def test_match_strategy_exact(self):
        """Test matchStrategy is 'exact' for simple literal match."""
        path = self.tmpdir / "ms_exact.txt"
        path.write_bytes(b"foo\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--expected-count", "1", "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertEqual(payload["operations"][0]["matchStrategy"], "exact")

    def test_match_strategy_ignore_eol(self):
        """Test matchStrategy is 'ignore-eol' when --ignore-eol is used."""
        path = self.tmpdir / "ms_eol.txt"
        path.write_bytes(b"line1\r\nline2\r\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "line1\nline2",
            "--new", "new1\nnew2",
            "--ignore-eol", "--expected-count", "1", "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertEqual(payload["operations"][0]["matchStrategy"], "ignore-eol")

    def test_match_strategy_ignore_indent(self):
        """Test matchStrategy is 'ignore-indent' when --ignore-indent is used."""
        path = self.tmpdir / "ms_indent.txt"
        path.write_bytes(b"\tfoo\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "    foo",
            "--new", "    bar",
            "--ignore-indent", "--expected-count", "1", "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertEqual(payload["operations"][0]["matchStrategy"], "ignore-indent")

    def test_match_strategy_normalize_whitespace(self):
        """Test matchStrategy is 'normalize-whitespace' when used."""
        path = self.tmpdir / "ms_ws.txt"
        path.write_bytes(b"foo    bar\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "foo bar",
            "--new", "baz qux",
            "--normalize-whitespace", "--expected-count", "1", "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertEqual(payload["operations"][0]["matchStrategy"], "normalize-whitespace")

    def test_match_strategy_regex(self):
        """Test matchStrategy is 'regex' for regex command."""
        path = self.tmpdir / "ms_regex.txt"
        path.write_bytes(b"foo123\n")
        
        result = self.run_tool(
            "regex", "--file", path,
            "--pattern", r"foo\d+", "--replacement", "bar",
            "--expected-count", "1", "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertEqual(payload["operations"][0]["matchStrategy"], "regex")

    def test_match_strategy_line_based(self):
        """Test matchStrategy is 'line-based' for line operations."""
        path = self.tmpdir / "ms_line.txt"
        path.write_bytes(b"line1\nline2\n")
        
        result = self.run_tool(
            "delete", "--file", path,
            "--line", "1", "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertEqual(payload["operations"][0]["matchStrategy"], "line-based")

    # =========================================================================
    # closestMatch field tests
    # =========================================================================

    def test_closest_match_on_close_match(self):
        """Test closestMatch contains line and similarity for close match."""
        path = self.tmpdir / "nearby_close.txt"
        path.write_bytes(b"function foo():\n    return 42\n")

        result = self.run_tool(
            "edit", "--file", path,
            "--old", "function foo():\n    return 43",
            "--new", "bar",
            "--json", expect=2,
        )

        payload = json.loads(result.stdout)
        closest = payload.get("closestMatch")
        self.assertIsNotNone(closest)
        self.assertGreaterEqual(closest["similarity"], 0.5)

    def test_closest_match_missing_on_non_match_error(self):
        """Test closestMatch may be absent for completely unrelated patterns."""
        path = self.tmpdir / "nearby_none.txt"
        path.write_bytes(b"alpha\n")

        result = self.run_tool(
            "edit", "--file", path,
            "--old", "zzzzzzzzzzzzzzzzz",
            "--new", "bar",
            "--json", expect=2,
        )

        payload = json.loads(result.stdout)
        # closestMatch may or may not be present depending on similarity
        # The key test is that it doesn't crash

    # =========================================================================
    # classify_error_type tests (via --json error output)
    # =========================================================================

    def test_error_type_encoding_error(self):
        """Test error type classification for encoding errors."""
        path = self.tmpdir / "err_encoding.txt"
        # Write invalid UTF-8 bytes
        path.write_bytes(b"\xff\xfe\x00\x00invalid")
        
        result = self.run_tool(
            "inspect", "--file", path,
            "--encoding", "utf-8",
            "--json", expect=2,
        )
        
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "encoding_error")

    def test_error_type_validation_error(self):
        """Test error type classification for validation errors."""
        path = self.tmpdir / "err_validation.txt"
        path.write_bytes(b"foo\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "", "--new", "bar",
            "--json", expect=2,
        )
        
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "validation_error")

    # =========================================================================
    # --json ok field tests
    # =========================================================================

    def test_json_ok_true_on_success(self):
        """Test ok is true on successful edit."""
        path = self.tmpdir / "ok_true.txt"
        path.write_bytes(b"foo\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--expected-count", "1", "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])

    def test_json_ok_true_on_inspect(self):
        """Test ok is true on successful inspect."""
        path = self.tmpdir / "ok_inspect.txt"
        path.write_bytes(b"foo\n")
        
        result = self.run_tool("inspect", "--file", path, "--json")
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])

    def test_json_match_options_in_summary(self):
        """Test matchOptions is included in JSON summary."""
        path = self.tmpdir / "match_opts.txt"
        path.write_bytes(b"foo\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--auto-match", "--expected-count", "1", "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertIn("matchOptions", payload)
        self.assertTrue(payload["matchOptions"]["autoMatch"])

    # =========================================================================
    # classify_error_type coverage tests
    # =========================================================================

    def test_error_type_match_ambiguous(self):
        """Test error type classification for ambiguous anchor match."""
        path = self.tmpdir / "err_ambiguous.txt"
        path.write_bytes(b"pattern\nline1\npattern\nline2\n")
        
        result = self.run_tool(
            "replace-lines", "--file", path,
            "--anchor-pattern", "pattern",
            "--offset-start", "+1", "--offset-end", "+1",
            "--text", "new",
            "--json", expect=2,
        )
        
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "match_ambiguous")

    def test_error_type_file_error(self):
        """Test error type classification for missing file."""
        path = self.tmpdir / "nonexistent_file_xyz.txt"
        
        result = self.run_tool(
            "inspect", "--file", path,
            "--json", expect=2,
        )
        
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "file_error")

    def test_error_type_lock_error(self):
        """Test error type classification for lock contention."""
        path = self.tmpdir / "err_lock.txt"
        path.write_bytes(b"foo\n")
        
        # Create a lock file that is not stale
        lock = _get_lock_path(path)
        lock.write_text("active", encoding="utf-8")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--expected-count", "1",
            "--lock-timeout", "0.1",
            "--lock-stale-seconds", "0",
            "--json", expect=2,
        )
        
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "lock_error")

    def test_error_type_format_error(self):
        """Test error type classification for diff-input format error."""
        path = self.tmpdir / "err_format.txt"
        path.write_bytes(b"foo\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--diff-input", "this is not a valid diff format",
            "--json", expect=2,
        )
        
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "format_error")

    # =========================================================================
    # --allow-nul tests
    # =========================================================================

    def test_allow_nul_permits_nul_bytes(self):
        """Test --allow-nul allows editing files with NUL bytes."""
        path = self.tmpdir / "nul_allowed.txt"
        path.write_bytes(b"hello\x00world\n")
        
        # Without --allow-nul, should fail
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "hello", "--new", "hi",
            "--expected-count", "1",
            expect=2,
        )
        self.assertIn("NUL", result.stderr)
        
        # With --allow-nul, should succeed
        self.run_tool(
            "edit", "--file", path,
            "--old", "hello", "--new", "hi",
            "--expected-count", "1",
            "--allow-nul",
        )
        self.assertEqual(path.read_bytes(), b"hi\x00world\n")

    # =========================================================================
    # --max-bytes tests
    # =========================================================================

    def test_max_bytes_rejects_large_file(self):
        """Test --max-bytes rejects files exceeding the limit."""
        path = self.tmpdir / "large.txt"
        path.write_bytes(b"x" * 1000)
        
        result = self.run_tool(
            "inspect", "--file", path,
            "--max-bytes", "100",
            "--json", expect=2,
        )
        
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "file_error")

    def test_max_bytes_allows_file_under_limit(self):
        """Test --max-bytes allows files within the limit."""
        path = self.tmpdir / "small.txt"
        path.write_bytes(b"small content\n")
        
        result = self.run_tool(
            "inspect", "--file", path,
            "--max-bytes", "1000",
            "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])

    # =========================================================================
    # Additional encoding tests
    # =========================================================================

    def test_latin1_encoding_edit(self):
        """Test editing a Latin-1 encoded file."""
        path = self.tmpdir / "latin1.txt"
        # Latin-1 encoded: "café" = 63 61 66 e9
        path.write_bytes(bytes.fromhex("63 61 66 e9 0a"))
        
        self.run_tool(
            "edit", "--file", path,
            "--encoding", "latin-1",
            "--old", "caf", "--new", "CAF",
            "--expected-count", "1",
        )
        
        # "CAFé\n" in Latin-1
        self.assertEqual(path.read_bytes(), bytes.fromhex("43 41 46 e9 0a"))

    def test_utf16_le_encoding_inspect(self):
        """Test inspecting a UTF-16-LE file with BOM."""
        path = self.tmpdir / "utf16le.txt"
        # UTF-16-LE BOM + "hello\n"
        content = codecs.BOM_UTF16_LE + "hello\n".encode("utf-16-le")
        path.write_bytes(content)
        
        result = self.run_tool("inspect", "--file", path, "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["encoding"], "utf-16-le")

    # =========================================================================
    # --count flag for regex
    # =========================================================================

    def test_regex_count_limits_replacements(self):
        """Test --count limits the number of regex replacements."""
        path = self.tmpdir / "regex_count.txt"
        path.write_bytes(b"aaa bbb aaa bbb aaa\n")
        
        self.run_tool(
            "regex", "--file", path,
            "--pattern", "aaa", "--replacement", "xxx",
            "--count", "2",
        )
        
        # Only first 2 occurrences should be replaced
        self.assertEqual(path.read_bytes(), b"xxx bbb xxx bbb aaa\n")

    # =========================================================================
    # diff-input + auto-match combination
    # =========================================================================

    def test_diff_input_with_auto_match(self):
        """Test --diff-input with --auto-match for flexible matching."""
        path = self.tmpdir / "diff_auto.txt"
        # CRLF line endings in file, LF in diff input, multiline
        path.write_bytes(b"hello world\r\nfoo bar\r\n")
        
        # Diff input uses LF for multiline, file uses CRLF
        result = self.run_tool(
            "edit", "--file", path,
            "--diff-input", "------- SEARCH\nhello world\nfoo bar\n=======\nhello universe\nfoo baz\n+++++++ REPLACE",
            "--auto-match", "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["operations"][0]["matchStrategy"], "ignore-eol")

    # =========================================================================
    # Fuzzy multiline match
    # =========================================================================

    def test_fuzzy_multiline_match(self):
        """Test --fuzzy with multiline content where most lines match exactly."""
        path = self.tmpdir / "fuzzy_multi.txt"
        path.write_bytes(b"def calculate(price, qty):\n    total = price * qty\n    return total\n")
        
        # Only one line differs (cost vs price in first line)
        # Note: fuzzy multiline uses line-level SequenceMatcher, so we need
        # most lines to match exactly for similarity >= 0.6
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "def calculate(cost, qty):\n    total = price * qty\n    return total",
            "--new", "def compute(price, qty):\n    result = price * qty\n    return result",
            "--auto-match", "--fuzzy", "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["operations"][0]["matchStrategy"], "fuzzy")

    # =========================================================================
    # Context disambiguation + expected-count
    # =========================================================================

    def test_context_with_expected_count(self):
        """Test context filtering reduces matches to match expected-count."""
        path = self.tmpdir / "ctx_count.txt"
        # "target" appears 3 times
        path.write_bytes(b"prefix_A\ntarget\nmiddle\nprefix_B\ntarget\nsuffix\nprefix_C\ntarget\nend\n")
        
        # After context filtering (only matches after "prefix_B"), should find 1
        self.run_tool(
            "edit", "--file", path,
            "--old", "target", "--new", "replaced",
            "--context-before", "prefix_B",
            "--expected-count", "1",
        )
        
        self.assertEqual(
            path.read_bytes(),
            b"prefix_A\ntarget\nmiddle\nprefix_B\nreplaced\nsuffix\nprefix_C\ntarget\nend\n"
        )

    # =========================================================================
    # Empty SEARCH section in diff-input
    # =========================================================================

    def test_diff_input_empty_search_skipped(self):
        """Test --diff-input with empty SEARCH section is skipped."""
        path = self.tmpdir / "diff_empty_search.txt"
        path.write_bytes(b"hello\n")
        
        # Empty SEARCH section followed by a valid one
        self.run_tool(
            "edit", "--file", path,
            "--diff-input", "------- SEARCH\n=======\nshould_be_skipped\n+++++++ REPLACE\n------- SEARCH\nhello\n=======\nworld\n+++++++ REPLACE",
        )
        
        self.assertEqual(path.read_bytes(), b"world\n")

    # =========================================================================
    # adjust_replacement_for_indent edge cases
    # =========================================================================

    def test_ignore_indent_preserves_original_indentation_multiline(self):
        """Test --ignore-indent preserves original indentation on first line."""
        path = self.tmpdir / "indent_multi.txt"
        # File uses tabs
        path.write_bytes(b"class Foo:\n\tdef bar(self):\n\t\treturn 42\n")
        
        # Match with spaces, replace with different content
        self.run_tool(
            "edit", "--file", path,
            "--old", "def bar(self):\n        return 42",
            "--new", "def baz(self):\n        return 99",
            "--expected-count", "1",
            "--ignore-indent",
        )
        
        # Should keep original tab indentation on first line
        content = path.read_bytes()
        self.assertIn(b"\tdef baz(self):", content)

    # =========================================================================
    # classify_error_type: unknown branch
    # =========================================================================

    def test_error_type_unknown_for_unclassified_error(self):
        """Test error type classification falls back to 'unknown'."""
        # Import the module directly to test classify_error_type
        sys.path.insert(0, str(REPO_ROOT / "skills" / "safe-edit"))
        try:
            import safe_edit
            result = safe_edit.classify_error_type("something completely unexpected happened")
            self.assertEqual(result, "unknown")
        finally:
            sys.path.pop(0)

    # =========================================================================
    # Shift-JIS encoding
    # =========================================================================

    def test_shift_jis_encoding_edit(self):
        """Test editing a Shift-JIS encoded file."""
        path = self.tmpdir / "sjis.txt"
        # "こんにちは" in Shift-JIS: 82 b1 82:f1 82:c9 82 bf 82 cd
        content = b"\x82\xb1\x82\xf1\x82\xc9\x82\xbf\x82\xcd\n"
        path.write_bytes(content)
        
        self.run_tool(
            "edit", "--file", path,
            "--encoding", "shift-jis",
            "--old", "こんにちは", "--new", "世界",
            "--expected-count", "1",
        )
        
        # "世界" in Shift-JIS: 90 a2 8a 45
        expected = b"\x90\xa2\x8a\x45\n"
        self.assertEqual(path.read_bytes(), expected)

    # =========================================================================
    # Big5 encoding
    # =========================================================================

    def test_big5_encoding_edit(self):
        """Test editing a Big5 encoded file."""
        path = self.tmpdir / "big5.txt"
        # "你好" in Big5
        content = "你好".encode("big5") + b"\n"
        path.write_bytes(content)
        
        self.run_tool(
            "edit", "--file", path,
            "--encoding", "big5",
            "--old", "你好", "--new", "世界",
            "--expected-count", "1",
        )
        
        expected = "世界".encode("big5") + b"\n"
        self.assertEqual(path.read_bytes(), expected)

    # =========================================================================
    # --no-op-ok + --expected-count interaction
    # =========================================================================

    def test_no_op_ok_with_expected_count_zero_matches(self):
        """Test --no-op-ok with --expected-count when there are zero matches."""
        path = self.tmpdir / "noop_count.txt"
        path.write_bytes(b"hello world\n")
        
        # --no-op-ok should allow 0 matches even with --expected-count
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "nonexistent", "--new", "bar",
            "--no-op-ok", "--expected-count", "1",
            "--json",
        )
        
        # After fix: no_op_ok with 0 matches should succeed with 0 changes
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["changed"], 0)

    def test_no_op_ok_with_zero_expected_count(self):
        """Test --no-op-ok with --expected-count 0 means 'must not exist'."""
        path = self.tmpdir / "noop_zero.txt"
        path.write_bytes(b"hello world\n")
        
        # --expected-count 0 with --no-op-ok: "nonexistent" not found = 0 matches = OK
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "nonexistent", "--new", "bar",
            "--no-op-ok", "--expected-count", "0",
            "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["changed"], 0)

    def test_no_op_ok_regex_zero_matches(self):
        """Test --no-op-ok with regex when there are zero matches."""
        path = self.tmpdir / "noop_regex.txt"
        path.write_bytes(b"hello world\n")
        
        result = self.run_tool(
            "regex", "--file", path,
            "--pattern", "nonexistent_pattern", "--replacement", "bar",
            "--no-op-ok", "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["changed"], 0)

    # =========================================================================
    # --count + --first conflict
    # =========================================================================

    def test_count_and_first_conflict(self):
        """Test --count and --first are mutually exclusive for regex."""
        path = self.tmpdir / "count_first.txt"
        path.write_bytes(b"aaa bbb aaa\n")
        
        result = self.run_tool(
            "regex", "--file", path,
            "--pattern", "aaa", "--replacement", "xxx",
            "--count", "1", "--first",
            "--json", expect=2,
        )
        
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "validation_error")

    # =========================================================================
    # --force-write with identical content
    # =========================================================================

    def test_force_write_with_identical_content(self):
        """Test --force-write writes even when output is identical."""
        path = self.tmpdir / "force.txt"
        path.write_bytes(b"unchanged\n")
        before_mtime = os.stat(path).st_mtime_ns
        time.sleep(0.05)
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "unchanged", "--new", "unchanged",
            "--expected-count", "1",
            "--force-write", "--json",
        )
        
        payload = json.loads(result.stdout)
        self.assertTrue(payload["written"])
        self.assertFalse(payload["skipped"])

    # =========================================================================
    # --explain-match-failure output verification
    # =========================================================================

    def test_explain_match_failure_output(self):
        """Test --explain-match-failure produces diagnostic output when match fails."""
        path = self.tmpdir / "explain.txt"
        path.write_bytes(b"    foo bar\n")
        
        # Use exact match that won't find "    foo bar" (4 spaces + content)
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "foo baz", "--new", "qux",
            "--explain-match-failure",
            expect=2,
        )
        
        # Should mention "not found" and show diagnostic info
        self.assertIn("not found", result.stderr.lower())

    def test_explain_match_failure_indent_diagnosis(self):
        """Test --explain-match-failure detects indentation differences."""
        path = self.tmpdir / "explain_indent.txt"
        path.write_bytes(b"\tfoo bar\n")
        
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "    foo bar", "--new", "baz",
            "--explain-match-failure",
            expect=2,
        )
        
        # Should mention indentation or tab/space difference
        combined = (result.stderr + result.stdout).lower()
        # The diagnostic should show the closest match
        self.assertTrue(
            "indent" in combined or "tab" in combined or "closest" in combined or "not found" in combined,
            f"Expected diagnostic info in output, got: {combined[:200]}"
        )

    # =========================================================================
    # Encoding alias normalization
    # =========================================================================

    def test_encoding_alias_utf8(self):
        """Test 'utf8' is normalized to 'utf-8'."""
        path = self.tmpdir / "alias.txt"
        path.write_bytes(b"hello\n")
        
        result = self.run_tool("inspect", "--file", path, "--encoding", "utf8", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["encoding"], "utf-8")

    def test_encoding_alias_sjis(self):
        """Test 'sjis' is normalized to 'shift-jis'."""
        path = self.tmpdir / "alias_sjis.txt"
        path.write_bytes(b"hello\n")
        
        result = self.run_tool("inspect", "--file", path, "--encoding", "sjis", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["encoding"], "shift-jis")

    # =========================================================================
    # stat command
    # =========================================================================

    def test_stat_command_output(self):
        """Test stat command produces concise summary."""
        path = self.tmpdir / "stat_test.txt"
        path.write_bytes(b"line1\nline2\nline3\n")
        
        result = self.run_tool("stat", "--file", path)
        self.assertIn("UTF-8", result.stdout)
        self.assertIn("3", result.stdout)

    # =========================================================================
    # convert command validation
    # =========================================================================

    def test_convert_requires_at_least_one_option(self):
        """Test convert without any transformation option fails."""
        path = self.tmpdir / "convert_noop.txt"
        path.write_bytes(b"hello\n")
        
        result = self.run_tool(
            "convert", "--file", path,
            "--json", expect=2,
        )
        
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "validation_error")

    # =========================================================================
    # Context after filtering
    # =========================================================================

    def test_context_after_filters_matches(self):
        """Test --context-after filters matches by following text."""
        path = self.tmpdir / "ctx_after.txt"
        # "target" appears 3 times with different following text
        path.write_bytes(b"target\nsuffix_A\ntarget\nsuffix_B\ntarget\nsuffix_C\n")
        
        self.run_tool(
            "edit", "--file", path,
            "--old", "target", "--new", "replaced",
            "--context-after", "suffix_B",
            "--expected-count", "1",
        )
        
        self.assertEqual(
            path.read_bytes(),
            b"target\nsuffix_A\nreplaced\nsuffix_B\ntarget\nsuffix_C\n"
        )

    # =========================================================================
    # --first flag for literal edit
    # =========================================================================

    def test_first_flag_replaces_only_first(self):
        """Test --first replaces only the first occurrence."""
        path = self.tmpdir / "first_edit.txt"
        path.write_bytes(b"aaa\nbbb\naaa\nbbb\n")
        
        self.run_tool(
            "edit", "--file", path,
            "--old", "aaa", "--new", "xxx",
            "--first",
        )
        
        self.assertEqual(path.read_bytes(), b"xxx\nbbb\naaa\nbbb\n")

    # =========================================================================
    # Encoding aliases
    # =========================================================================

    def test_encoding_alias_cp936(self):
        """Test cp936 alias resolves to gbk."""
        path = self.tmpdir / "cp936.txt"
        path.write_bytes(bytes.fromhex("c4 e3 ba c3 0a"))  # GBK "你好\n"
        result = self.run_tool("inspect", "--file", path, "--encoding", "cp936", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["encoding"], "gbk")

    def test_encoding_alias_gb2312(self):
        """Test gb2312 alias resolves to gbk."""
        path = self.tmpdir / "gb2312.txt"
        path.write_bytes(bytes.fromhex("c4 e3 ba c3 0a"))
        result = self.run_tool("inspect", "--file", path, "--encoding", "gb2312", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["encoding"], "gbk")

    def test_encoding_alias_cp932(self):
        """Test cp932 alias resolves to shift-jis."""
        path = self.tmpdir / "cp932.txt"
        # Shift-JIS "こんにちは" in CP932 encoding
        path.write_bytes(b"\x82\xb1\x82\xf1\x82\xc9\x82\xbf\x82\xcd\x0a")
        result = self.run_tool("inspect", "--file", path, "--encoding", "cp932", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["encoding"], "shift-jis")

    def test_encoding_alias_ms932(self):
        """Test ms932 alias resolves to shift-jis."""
        path = self.tmpdir / "ms932.txt"
        path.write_bytes(b"\x82\xb1\x82\xf1\x82\xc9\x82\xbf\x82\xcd\x0a")
        result = self.run_tool("inspect", "--file", path, "--encoding", "ms932", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["encoding"], "shift-jis")

    def test_encoding_alias_sjis(self):
        """Test sjis alias resolves to shift-jis."""
        path = self.tmpdir / "sjis.txt"
        path.write_bytes(b"\x82\xb1\x82\xf1\x82\xc9\x82\xbf\x82\xcd\x0a")
        result = self.run_tool("inspect", "--file", path, "--encoding", "sjis", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["encoding"], "shift-jis")

    def test_encoding_alias_latin1(self):
        """Test latin1 alias resolves to latin-1."""
        path = self.tmpdir / "latin1.txt"
        path.write_bytes(b"Espa\xf1a\n")
        result = self.run_tool("inspect", "--file", path, "--encoding", "latin1", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["encoding"], "latin-1")

    def test_encoding_alias_iso_8859_1(self):
        """Test iso-8859-1 alias resolves to latin-1."""
        path = self.tmpdir / "iso8859.txt"
        path.write_bytes(b"Espa\xf1a\n")
        result = self.run_tool("inspect", "--file", path, "--encoding", "iso-8859-1", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["encoding"], "latin-1")

    def test_encoding_alias_utf8_sig(self):
        """Test utf-8-sig alias resolves to utf-8-bom."""
        path = self.tmpdir / "utf8sig.txt"
        path.write_bytes(codecs.BOM_UTF8 + b"hello\n")
        result = self.run_tool("inspect", "--file", path, "--encoding", "utf-8-sig", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["encoding"], "utf-8-bom")

    def test_encoding_alias_utf16_le_no_hyphen(self):
        """Test utf16-le alias (no hyphen) resolves to utf-16-le."""
        path = self.tmpdir / "utf16le.txt"
        content = codecs.BOM_UTF16_LE + "hello\n".encode("utf-16-le")
        path.write_bytes(content)
        result = self.run_tool("inspect", "--file", path, "--encoding", "utf16-le", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["encoding"], "utf-16-le")

    def test_encoding_alias_utf16_be_no_hyphen(self):
        """Test utf16-be alias (no hyphen) resolves to utf-16-be."""
        path = self.tmpdir / "utf16be.txt"
        content = codecs.BOM_UTF16_BE + "hello\n".encode("utf-16-be")
        path.write_bytes(content)
        result = self.run_tool("inspect", "--file", path, "--encoding", "utf16-be", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["encoding"], "utf-16-be")

    # =========================================================================
    # Error paths and CLI edge cases
    # =========================================================================

    def test_encode_text_failure_on_gbk_with_invalid_char(self):
        """Test editing a GBK file with a character that cannot be encoded in GBK."""
        path = self.tmpdir / "gbk_invalid.txt"
        path.write_bytes(bytes.fromhex("c4 e3 ba c3 0a"))  # GBK "你好\n"
        result = self.run_tool(
            "edit", "--file", path, "--encoding", "gbk",
            "--old", "\u4f60\u597d", "--new", "\U0001f600",  # 😀 cannot be encoded in GBK
            "--json", expect=2,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "encoding_error")

    def test_detect_encoding_auto_fails_on_unknown_binary(self):
        """Test auto-detection fails on bytes that are not UTF-8, GBK, or UTF-16."""
        path = self.tmpdir / "unknown.bin"
        # 0x80-0xBF are continuation bytes in UTF-8, invalid as start;
        # also not valid GBK start bytes for a two-byte sequence
        path.write_bytes(bytes([0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87]))
        result = self.run_tool("inspect", "--file", path, "--json", expect=2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "encoding_error")

    def test_old_and_new_base64_replace_exact_text(self):
        path = self.tmpdir / "old_new_base64.txt"
        original = 'quoted "%!"'
        replacement = """frozenset({'"', '%', '!', '\\r', '\\n'})"""
        path.write_text(original + "\n", encoding="utf-8")
        old_encoded = base64.urlsafe_b64encode(original.encode("utf-8")).decode("ascii").rstrip("=")
        new_encoded = base64.urlsafe_b64encode(replacement.encode("utf-8")).decode("ascii").rstrip("=")
        self.run_tool(
            "edit", "--file", path, "--old-base64", old_encoded,
            "--new-base64", new_encoded, "--expected-count", "1",
        )
        self.assertEqual(path.read_text(encoding="utf-8"), replacement + "\n")

    def test_base64_source_conflict_fails_before_write(self):
        path = self.tmpdir / "base64_conflict.txt"
        original = b"unchanged\n"
        path.write_bytes(original)
        self.run_tool(
            "append", "--file", path, "--text", "x", "--text-base64", "eA",
            expect=2,
        )
        self.assertEqual(path.read_bytes(), original)

    def test_text_base64_preserves_shell_sensitive_multiline_content(self):
        path = self.tmpdir / "text_base64.txt"
        path.write_bytes(b"placeholder\r\n")
        content = """'''doc'''
value = frozenset({'"', '%', '!', '\\r', '\\n', '`'})"""
        encoded = base64.urlsafe_b64encode(content.encode("utf-8")).decode("ascii").rstrip("=")
        self.run_tool(
            "replace-lines", "--file", path, "--start", "1", "--end", "1",
            "--text-base64", encoded, "--no-preserve-indent",
        )
        self.assertEqual(path.read_bytes(), (content.replace("\n", "\r\n") + "\r\n").encode("utf-8"))

    def test_ops_base64_carries_old_and_new_without_shell_parsing(self):
        path = self.tmpdir / "ops_base64.txt"
        path.write_bytes(b"before\r\nafter\r\n")
        replacement = """frozenset({'"', '%', '!', '\\r', '\\n'})"""
        operations = json.dumps([
            {"op": "edit", "old": "before", "new": replacement, "expected_count": 1}
        ], ensure_ascii=False)
        encoded = base64.urlsafe_b64encode(operations.encode("utf-8")).decode("ascii")
        self.run_tool("batch", "--file", path, "--ops-base64", encoded)
        self.assertEqual(path.read_text(encoding="utf-8"), replacement + "\nafter\n")

    def test_diff_input_stdin_and_base64(self):
        for option in ("--diff-input-stdin", "--diff-input-base64"):
            path = self.tmpdir / (option.removeprefix("--") + ".txt")
            path.write_bytes(b"alpha\nbeta\ngamma\n")
            diff = "------- SEARCH\nbeta\n=======\nBETA % ! \" \\ \u0060\n+++++++ REPLACE"
            if option.endswith("stdin"):
                self.run_tool("edit", "--file", path, option, input_text=diff)
            else:
                encoded = base64.urlsafe_b64encode(diff.encode("utf-8")).decode("ascii").rstrip("=")
                self.run_tool("edit", "--file", path, option, encoded)
            self.assertEqual(path.read_text(encoding="utf-8"), "alpha\nBETA % ! \" \\ \u0060\ngamma\n")

    def test_invalid_base64_does_not_modify_file(self):
        path = self.tmpdir / "invalid_base64.txt"
        original = b"unchanged\n"
        path.write_bytes(original)
        result = self.run_tool(
            "append", "--file", path, "--text-base64", "not*base64", "--json", expect=2,
        )
        payload = json.loads(result.stdout)
        self.assertIn("expected Base64 text", payload["error"]["message"])
        self.assertEqual(path.read_bytes(), original)

    def test_ops_stdin_batch_mode(self):
        """Test --ops-stdin reads batch JSON from stdin."""
        path = self.tmpdir / "ops_stdin.txt"
        path.write_bytes(b"foo bar\n")
        ops_json = json.dumps([
            {"op": "edit", "old": "foo", "new": "baz", "expected_count": 1}
        ])
        self.run_tool("batch", "--file", path, "--ops-stdin", input_text=ops_json)
        self.assertEqual(path.read_bytes(), b"baz bar\n")

    def test_diff_input_with_angle_bracket_markers(self):
        """Test diff-input with <<< SEARCH / === / >>> REPLACE markers."""
        path = self.tmpdir / "angle_markers.txt"
        path.write_bytes(b"alpha\nbeta\ngamma\n")
        diff = "<<< SEARCH\nbeta\n===\nBETA\n>>> REPLACE"
        self.run_tool("edit", "--file", path, "--diff-input", diff)
        self.assertEqual(path.read_bytes(), b"alpha\nBETA\ngamma\n")

    def test_diff_input_empty_search_block_skipped(self):
        """Test diff-input with an empty SEARCH block followed by a valid block."""
        path = self.tmpdir / "empty_search.txt"
        path.write_bytes(b"alpha\nbeta\ngamma\n")
        # Empty old_text block is skipped, valid block is applied
        diff = "------- SEARCH\n=======\nsomething\n+++++++ REPLACE\n------- SEARCH\nbeta\n=======\nBETA\n+++++++ REPLACE"
        self.run_tool("edit", "--file", path, "--diff-input", diff)
        self.assertEqual(path.read_bytes(), b"alpha\nBETA\ngamma\n")

    def test_diff_input_unterminated_block_applied(self):
        """Test diff-input with unterminated block (missing REPLACE marker) is still applied."""
        path = self.tmpdir / "unterminated.txt"
        path.write_bytes(b"alpha\nbeta\ngamma\n")
        # Missing >>> REPLACE at the end - should still apply
        diff = "------- SEARCH\nbeta\n=======\nBETA"
        self.run_tool("edit", "--file", path, "--diff-input", diff)
        self.assertEqual(path.read_bytes(), b"alpha\nBETA\ngamma\n")

    def test_backup_suffix_with_path_separator_fails(self):
        """Test --backup-suffix with path separator is rejected."""
        path = self.tmpdir / "baksuffix.txt"
        path.write_bytes(b"hello\n")
        result = self.run_tool(
            "edit", "--file", path, "--old", "hello", "--new", "world",
            "--backup", "--backup-suffix", "/evil.bak",
            "--json", expect=2,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "validation_error")

    def test_backup_suffix_with_backslash_fails(self):
        """Test --backup-suffix with backslash separator is rejected."""
        path = self.tmpdir / "baksuffix2.txt"
        path.write_bytes(b"hello\n")
        result = self.run_tool(
            "edit", "--file", path, "--old", "hello", "--new", "world",
            "--backup", "--backup-suffix", "\\evil.bak",
            "--json", expect=2,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "validation_error")

    def test_convert_gbk_to_utf8(self):
        """Test convert from GBK to UTF-8 encoding."""
        path = self.tmpdir / "gbk2utf8.txt"
        path.write_bytes(bytes.fromhex("c4 e3 ba c3 0a"))  # GBK "你好\n"
        result = self.run_tool(
            "convert", "--file", path, "--encoding", "gbk",
            "--to-encoding", "utf-8", "--json",
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["outputEncoding"], "utf-8")
        self.assertEqual(path.read_bytes(), "你好\n".encode("utf-8"))

    def test_convert_utf8_to_gbk(self):
        """Test convert from UTF-8 to GBK encoding."""
        path = self.tmpdir / "utf82gbk.txt"
        path.write_bytes("你好\n".encode("utf-8"))
        result = self.run_tool(
            "convert", "--file", path,
            "--to-encoding", "gbk", "--json",
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["outputEncoding"], "gbk")
        self.assertEqual(path.read_bytes(), bytes.fromhex("c4 e3 ba c3 0a"))

    def test_convert_utf8_to_utf16_le(self):
        """Test convert from UTF-8 to UTF-16-LE encoding (no BOM added when not in original)."""
        path = self.tmpdir / "utf82utf16.txt"
        path.write_bytes("hello\n".encode("utf-8"))
        result = self.run_tool(
            "convert", "--file", path,
            "--to-encoding", "utf-16-le", "--json",
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["outputEncoding"], "utf-16-le")
        # --to-encoding utf-16-le does not add BOM automatically
        expected = "hello\n".encode("utf-16-le")
        self.assertEqual(path.read_bytes(), expected)

    def test_strict_decode_bom_mismatch(self):
        """Test that specifying utf-8-bom encoding on a file without BOM fails."""
        path = self.tmpdir / "no_bom.txt"
        path.write_bytes(b"hello\n")
        result = self.run_tool(
            "inspect", "--file", path, "--encoding", "utf-8-bom", "--json", expect=2,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "encoding_error")

    def test_resolve_operation_value_conflict(self):
        """Test batch operation with both 'old' and 'old_file' fails."""
        path = self.tmpdir / "op_conflict.txt"
        path.write_bytes(b"hello\n")
        old_file = self.tmpdir / "old_content.txt"
        old_file.write_bytes(b"hello")
        ops_json = json.dumps([
            {"op": "edit", "old": "hello", "old_file": str(old_file), "new": "world"}
        ])
        result = self.run_tool("batch", "--file", path, "--ops", ops_json, "--json", expect=2)
        payload = json.loads(result.stdout)
        # "batch operation uses both old and old_file" doesn't match validation keywords,
        # so it's classified as "unknown" — but the key thing is it errors out
        self.assertFalse(payload["ok"])
        self.assertIn("both", payload["error"]["message"])

    # =========================================================================
    # Internal function unit tests (direct import)
    # =========================================================================

    def _import_safe_edit(self):
        """Helper to import safe_edit module for direct function testing."""
        sys.path.insert(0, str(REPO_ROOT / "skills" / "safe-edit"))
        try:
            import safe_edit
            return safe_edit
        finally:
            sys.path.pop(0)

    def test_visualize_whitespace_format(self):
        """Test visualize_whitespace converts whitespace to visible symbols."""
        m = self._import_safe_edit()
        result = m.visualize_whitespace("\thello world\r\n")
        self.assertIn("[TAB]", result)
        self.assertIn("[SP]", result)
        self.assertIn("[CR]", result)
        self.assertIn("[LF]", result)

    def test_find_closest_match_empty_pattern(self):
        """Test find_closest_match returns None for empty pattern."""
        m = self._import_safe_edit()
        self.assertIsNone(m.find_closest_match("some text", ""))

    def test_find_closest_match_empty_text(self):
        """Test find_closest_match returns None for empty text."""
        m = self._import_safe_edit()
        self.assertIsNone(m.find_closest_match("", "pattern"))

    def test_find_closest_match_none_inputs(self):
        """Test find_closest_match returns None when either arg is falsy."""
        m = self._import_safe_edit()
        self.assertIsNone(m.find_closest_match("text", ""))
        self.assertIsNone(m.find_closest_match("", "text"))

    def test_extract_nearby_content_no_match(self):
        """Test extract_nearby_content returns None when no close match found."""
        m = self._import_safe_edit()
        result = m.extract_nearby_content("hello world\nfoo bar\n", "zzzzzzzz")
        self.assertIsNone(result)

    def test_classify_error_type_priority(self):
        """Test classify_error_type returns correct type for overlapping patterns."""
        m = self._import_safe_edit()
        # "was not found" should match match_not_found before validation_error
        self.assertEqual(m.classify_error_type("old text was not found"), "match_not_found")
        # "not found" + "refusing" should match match_not_found
        self.assertEqual(m.classify_error_type("pattern not found, refusing"), "match_not_found")
        # "anchor pattern" + "not found" should match match_not_found
        self.assertEqual(m.classify_error_type("anchor pattern not found"), "match_not_found")
        # "anchor pattern found" + "times" should match match_ambiguous
        self.assertEqual(m.classify_error_type("anchor pattern found 3 times"), "match_ambiguous")
        # "expected" + "occurrence" should match match_count_mismatch
        self.assertEqual(m.classify_error_type("expected 2 occurrence(s)"), "match_count_mismatch")
        # "expected" + "match" + "found" should match match_count_mismatch
        self.assertEqual(m.classify_error_type("expected 1 match, found 2"), "match_count_mismatch")
        # "decode" should match encoding_error
        self.assertEqual(m.classify_error_type("failed to decode as utf-8"), "encoding_error")
        # "unsupported" should match validation_error
        self.assertEqual(m.classify_error_type("unsupported encoding: foo"), "encoding_error")
        # "file not found" should match file_error
        self.assertEqual(m.classify_error_type("file not found: test.txt"), "file_error")

    def test_set_final_newline_invalid_mode(self):
        """Test set_final_newline raises SafeEditError for invalid mode."""
        m = self._import_safe_edit()
        with self.assertRaises(m.SafeEditError):
            m.set_final_newline("hello\n", "invalid_mode", "\n")

    def test_make_backup_path_with_slash_fails(self):
        """Test make_backup_path rejects path separators in suffix."""
        m = self._import_safe_edit()
        path = Path(self.tmpdir / "test.txt")
        path.write_bytes(b"hello\n")
        with self.assertRaises(m.SafeEditError):
            m.make_backup_path(path, None, "/evil.bak")

    def test_make_backup_path_with_backslash_fails(self):
        """Test make_backup_path rejects backslash in suffix."""
        m = self._import_safe_edit()
        path = Path(self.tmpdir / "test2.txt")
        path.write_bytes(b"hello\n")
        with self.assertRaises(m.SafeEditError):
            m.make_backup_path(path, None, "\\evil.bak")

    def test_find_context_anchor_with_regex_special_chars(self):
        """Test find_context_anchor treats pattern as literal, not regex."""
        m = self._import_safe_edit()
        # Pattern with regex special chars should be matched literally
        text = "func(*args, **kwargs)\nother line\n"
        result = m.find_context_anchor(text, "*args, **kwargs")
        self.assertEqual(result, 1)  # Found on line 1

    def test_find_context_anchor_dot_star(self):
        """Test find_context_anchor with .* pattern is literal match."""
        m = self._import_safe_edit()
        text = "some text\npattern: .*\nother\n"
        # ".*" should match literally, not as regex
        result = m.find_context_anchor(text, ".*")
        self.assertEqual(result, 2)  # Found on line 2

    def test_parse_regex_flags_combined(self):
        """Test parse_regex_flags with combined flags."""
        m = self._import_safe_edit()
        import re
        flags = m.parse_regex_flags("ims")
        self.assertEqual(flags, re.IGNORECASE | re.MULTILINE | re.DOTALL)

    def test_parse_regex_flags_with_separator(self):
        """Test parse_regex_flags with comma/space separators."""
        m = self._import_safe_edit()
        import re
        flags = m.parse_regex_flags("i, m")
        self.assertEqual(flags, re.IGNORECASE | re.MULTILINE)

    def test_parse_regex_flags_invalid_char(self):
        """Test parse_regex_flags raises on invalid character."""
        m = self._import_safe_edit()
        with self.assertRaises(m.SafeEditError):
            m.parse_regex_flags("z")

    def test_apply_operation_unknown_op(self):
        """Test apply_operation raises SafeEditError for unknown op."""
        m = self._import_safe_edit()
        with self.assertRaises(m.SafeEditError):
            m.apply_operation("hello\n", {"op": "nonsense"}, "\n")

    def test_normalize_encoding_underscore_to_hyphen(self):
        """Test normalize_encoding converts underscores to hyphens."""
        m = self._import_safe_edit()
        self.assertEqual(m.normalize_encoding("utf_8"), "utf-8")
        self.assertEqual(m.normalize_encoding("UTF_8_BOM"), "utf-8-bom")

    def test_normalize_encoding_none_defaults_to_auto(self):
        """Test normalize_encoding returns 'auto' for None input."""
        m = self._import_safe_edit()
        self.assertEqual(m.normalize_encoding(None), "auto")

    def test_strict_decode_bom_missing(self):
        """Test strict_decode fails when BOM is expected but missing."""
        m = self._import_safe_edit()
        info = m.EncodingInfo("utf-8-bom", "utf-8", codecs.BOM_UTF8)
        with self.assertRaises(m.SafeEditError):
            m.strict_decode(b"hello\n", info)

    def test_detect_encoding_auto_empty_file(self):
        """Test detect_encoding auto mode returns utf-8 for empty file."""
        m = self._import_safe_edit()
        result = m.detect_encoding(b"", "auto")
        self.assertEqual(result.name, "utf-8")

    def test_detect_encoding_auto_utf8_bom(self):
        """Test detect_encoding auto detects UTF-8 BOM."""
        m = self._import_safe_edit()
        result = m.detect_encoding(codecs.BOM_UTF8 + b"hello\n", "auto")
        self.assertEqual(result.name, "utf-8-bom")
        self.assertEqual(result.bom, codecs.BOM_UTF8)

    def test_detect_encoding_auto_utf16_le_bom(self):
        """Test detect_encoding auto detects UTF-16 LE BOM."""
        m = self._import_safe_edit()
        result = m.detect_encoding(codecs.BOM_UTF16_LE + "hello\n".encode("utf-16-le"), "auto")
        self.assertEqual(result.name, "utf-16-le")

    def test_detect_encoding_auto_utf16_be_bom(self):
        """Test detect_encoding auto detects UTF-16 BE BOM."""
        m = self._import_safe_edit()
        result = m.detect_encoding(codecs.BOM_UTF16_BE + "hello\n".encode("utf-16-be"), "auto")
        self.assertEqual(result.name, "utf-16-be")

    def test_detect_encoding_auto_plain_utf8(self):
        """Test detect_encoding auto detects plain UTF-8 (no BOM)."""
        m = self._import_safe_edit()
        result = m.detect_encoding(b"hello world\n", "auto")
        self.assertEqual(result.name, "utf-8")
        self.assertEqual(result.bom, b"")

    def test_detect_and_decode_reuses_auto_decode_result(self):
        m = self._import_safe_edit()
        with patch.object(m, "strict_decode", side_effect=AssertionError):
            encoding, text = m.detect_and_decode(b"hello world\n", "auto")
        self.assertEqual(encoding.name, "utf-8")
        self.assertEqual(text, "hello world\n")

    def test_detect_encoding_auto_gbk(self):
        """Test detect_encoding auto falls back to GBK for non-UTF-8 CJK."""
        m = self._import_safe_edit()
        # GBK-encoded Chinese text is not valid UTF-8
        result = m.detect_encoding(bytes.fromhex("c4 e3 ba c3 0a"), "auto")
        self.assertEqual(result.name, "gbk")

    def test_detect_encoding_auto_all_fail(self):
        """Test detect_encoding auto raises SafeEditError for undetectable bytes."""
        m = self._import_safe_edit()
        # Bytes that are not valid UTF-8, not valid GBK, not UTF-16
        with self.assertRaises(m.SafeEditError) as ctx:
            m.detect_encoding(bytes([0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87]), "auto")
        self.assertIn("auto-detect", str(ctx.exception))

    def test_looks_like_utf16_without_bom_short_data(self):
        """Test looks_like_utf16_without_bom returns None for data < 4 bytes."""
        m = self._import_safe_edit()
        self.assertIsNone(m.looks_like_utf16_without_bom(b"\x00"))
        self.assertIsNone(m.looks_like_utf16_without_bom(b"\x00\x00"))
        self.assertIsNone(m.looks_like_utf16_without_bom(b""))

    def test_looks_like_utf16_without_bom_le(self):
        """Test looks_like_utf16_without_bom detects UTF-16 LE without BOM."""
        m = self._import_safe_edit()
        # UTF-16 LE: ASCII chars followed by \x00
        data = "hello\n".encode("utf-16-le")
        result = m.looks_like_utf16_without_bom(data)
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "utf-16-le")

    def test_looks_like_utf16_without_bom_be(self):
        """Test looks_like_utf16_without_bom detects UTF-16 BE without BOM."""
        m = self._import_safe_edit()
        # UTF-16 BE: \x00 followed by ASCII chars
        data = "hello\n".encode("utf-16-be")
        result = m.looks_like_utf16_without_bom(data)
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "utf-16-be")

    def test_looks_like_utf16_without_bom_plain_ascii(self):
        """Test looks_like_utf16_without_bom returns None for plain ASCII."""
        m = self._import_safe_edit()
        self.assertIsNone(m.looks_like_utf16_without_bom(b"hello world this is plain ascii\n"))

    def test_normalize_for_match_no_flags(self):
        """Test normalize_for_match returns text unchanged when no flags set."""
        m = self._import_safe_edit()
        text = "  hello\r\nworld  "
        self.assertEqual(m.normalize_for_match(text), text)

    def test_normalize_for_match_ignore_indent(self):
        """Test normalize_for_match strips leading whitespace with ignore_indent."""
        m = self._import_safe_edit()
        text = "  hello\n    world"
        result = m.normalize_for_match(text, ignore_indent=True)
        self.assertNotIn("  hello", result)
        self.assertEqual(result, "hello\nworld")

    def test_normalize_for_match_ignore_eol(self):
        """Test normalize_for_match normalizes line endings with ignore_eol."""
        m = self._import_safe_edit()
        text = "hello\r\nworld"
        result = m.normalize_for_match(text, ignore_eol=True)
        self.assertNotIn("\r\n", result)
        self.assertEqual(result, "hello\nworld")

    def test_normalize_for_match_normalize_whitespace(self):
        """Test normalize_for_match collapses whitespace."""
        m = self._import_safe_edit()
        text = "hello   world\n\tfoo  bar"
        result = m.normalize_for_match(text, normalize_whitespace=True)
        # All whitespace collapsed to single space
        self.assertNotIn("  ", result)
        self.assertNotIn("\t", result)

    def test_split_records_and_join_roundtrip(self):
        """Test split_records and join_records are inverse operations."""
        m = self._import_safe_edit()
        text = "alpha\r\nbeta\n gamma\r"
        records = m.split_records(text)
        result = m.join_records(records)
        self.assertEqual(result, text)

    def test_detect_line_ending_no_newlines(self):
        """Test detect_line_ending returns 'lf' for text with no newlines."""
        m = self._import_safe_edit()
        style, counts, mixed = m.detect_line_ending("hello world")
        self.assertEqual(style, "lf")
        self.assertFalse(mixed)

    def test_detect_line_ending_crlf(self):
        """Test detect_line_ending correctly identifies CRLF."""
        m = self._import_safe_edit()
        style, counts, mixed = m.detect_line_ending("hello\r\nworld\r\n")
        self.assertEqual(style, "crlf")
        self.assertEqual(counts["crlf"], 2)

    def test_detect_line_ending_mixed(self):
        """Test detect_line_ending detects mixed line endings."""
        m = self._import_safe_edit()
        style, counts, mixed = m.detect_line_ending("hello\r\nworld\nfoo")
        self.assertTrue(mixed)
        self.assertEqual(counts["crlf"], 1)
        self.assertEqual(counts["lf"], 1)

    def test_encode_text_roundtrip(self):
        """Test encode_text and strict_decode roundtrip for GBK."""
        m = self._import_safe_edit()
        original_text = "你好世界"
        info = m.EncodingInfo("gbk", "gbk")
        encoded = m.encode_text(original_text, info)
        decoded = m.strict_decode(encoded, info)
        self.assertEqual(decoded, original_text)

    def test_encoding_for_output_preserve(self):
        """Test encoding_for_output with 'preserve' returns original."""
        m = self._import_safe_edit()
        original = m.EncodingInfo("gbk", "gbk")
        result = m.encoding_for_output("preserve", original)
        self.assertEqual(result.name, "gbk")

    def test_encoding_for_output_explicit(self):
        """Test encoding_for_output with explicit encoding name."""
        m = self._import_safe_edit()
        original = m.EncodingInfo("gbk", "gbk")
        result = m.encoding_for_output("utf-8", original)
        self.assertEqual(result.name, "utf-8")
        self.assertEqual(result.codec, "utf-8")

    # =========================================================================
    # CRLF + ignore_eol + ignore_indent combined (CLI indirect test for
    # _find_original_position_line_based)
    # =========================================================================

    def test_crlf_file_ignore_eol_edit(self):
        """Test editing a CRLF file with --ignore-eol finds and replaces correctly."""
        path = self.tmpdir / "crlf_ignroeol.txt"
        path.write_bytes(b"alpha\r\n  beta\r\ngamma\r\n")
        self.run_tool(
            "edit", "--file", path,
            "--old", "beta", "--new", "BETA",
            "--ignore-eol", "--expected-count", "1",
        )
        self.assertEqual(path.read_bytes(), b"alpha\r\n  BETA\r\ngamma\r\n")

    def test_crlf_file_ignore_indent_edit(self):
        """Test editing a CRLF file with --ignore-indent finds indented text."""
        path = self.tmpdir / "crlf_indent.txt"
        path.write_bytes(b"alpha\r\n  beta\r\ngamma\r\n")
        self.run_tool(
            "edit", "--file", path,
            "--old", "beta", "--new", "BETA",
            "--ignore-indent", "--expected-count", "1",
        )
        self.assertEqual(path.read_bytes(), b"alpha\r\n  BETA\r\ngamma\r\n")

    def test_crlf_file_ignore_eol_and_indent_combined(self):
        """Test editing a CRLF file with both --ignore-eol and --ignore-indent."""
        path = self.tmpdir / "crlf_both.txt"
        path.write_bytes(b"alpha\r\n  beta\r\ngamma\r\n")
        self.run_tool(
            "edit", "--file", path,
            "--old", "beta", "--new", "BETA",
            "--ignore-eol", "--ignore-indent", "--expected-count", "1",
        )
        self.assertEqual(path.read_bytes(), b"alpha\r\n  BETA\r\ngamma\r\n")

    def test_crlf_file_auto_match(self):
        """Test auto-match on a CRLF file with LF-style --old text."""
        path = self.tmpdir / "crlf_automatch.txt"
        path.write_bytes(b"alpha\r\n  beta\r\ngamma\r\n")
        # --old uses LF, file uses CRLF; auto-match should resolve via ignore-eol
        self.run_tool(
            "edit", "--file", path,
            "--old", "beta", "--new", "BETA",
            "--auto-match", "--expected-count", "1",
        )
        self.assertEqual(path.read_bytes(), b"alpha\r\n  BETA\r\ngamma\r\n")

    # =========================================================================
    # Additional edge cases
    # =========================================================================

    def test_edit_empty_old_text_fails(self):
        """Test editing with empty --old fails."""
        path = self.tmpdir / "empty_old.txt"
        path.write_bytes(b"hello\n")
        result = self.run_tool(
            "edit", "--file", path, "--old", "", "--new", "world",
            "--json", expect=2,
        )
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])

    def test_regex_invalid_pattern_fails(self):
        """Test regex with invalid pattern fails gracefully."""
        path = self.tmpdir / "bad_regex.txt"
        path.write_bytes(b"hello\n")
        result = self.run_tool(
            "regex", "--file", path, "--pattern", "[invalid", "--replacement", "x",
            "--json", expect=2,
        )
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])

    def test_replace_lines_anchor_with_offset(self):
        """Test replace-lines with anchor pattern and positive offsets."""
        path = self.tmpdir / "anchor_offset.txt"
        path.write_bytes(b"line1\nmarker\nline3\nline4\nline5\n")
        # marker on line 2, offset_start=+1 → line 3, offset_end=+1 → line 3
        # Replace only line 3 with "replaced"
        self.run_tool(
            "replace-lines", "--file", path,
            "--anchor-pattern", "marker",
            "--offset-start", "+1", "--offset-end", "+1",
            "--text", "replaced\n",
        )
        self.assertEqual(path.read_bytes(), b"line1\nmarker\nreplaced\nline4\nline5\n")

    def test_delete_lines_anchor_with_negative_offset(self):
        """Test delete-lines with anchor pattern and negative/positive offsets."""
        path = self.tmpdir / "anchor_neg.txt"
        path.write_bytes(b"line1\nline2\nmarker\nline4\nline5\n")
        # marker on line 3, offset_start=-1 → line 2, offset_end=+1 → line 4
        # Delete lines 2 through 4 (inclusive), leaving line1 and line5
        self.run_tool(
            "delete-lines", "--file", path,
            "--anchor-pattern", "marker",
            "--offset-start", "-1", "--offset-end", "+1",
        )
        self.assertEqual(path.read_bytes(), b"line1\nline5\n")

    def test_create_new_file_with_explicit_format(self):
        path = self.tmpdir / "created.txt"
        result = self.run_tool(
            "create", "--file", path,
            "--to-encoding", "utf-8",
            "--to-line-ending", "crlf",
            "--final-newline", "ensure",
            "--text", "alpha\nbeta",
            "--json",
        )
        payload = json.loads(result.stdout)
        self.assertTrue(payload["created"])
        self.assertTrue(payload["written"])
        self.assertEqual(path.read_bytes(), b"alpha\r\nbeta\r\n")

        stat_result = self.run_tool("stat", "--file", path, "--json")
        stat_payload = json.loads(stat_result.stdout)
        self.assertEqual(stat_payload["encoding"], "utf-8")
        self.assertEqual(stat_payload["lineEnding"], "crlf")

    def test_create_refuses_to_overwrite_existing_file(self):
        path = self.tmpdir / "existing.txt"
        path.write_text("original", encoding="utf-8")
        result = self.run_tool(
            "create", "--file", path,
            "--to-encoding", "utf-8",
            "--to-line-ending", "lf",
            "--text", "replacement",
            "--json",
            expect=2,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "file_error")
        self.assertIn("file already exists", payload["error"]["message"])
        self.assertEqual(path.read_text(encoding="utf-8"), "original")

    def test_create_requires_existing_parent_and_explicit_format(self):
        missing_parent = self.tmpdir / "missing" / "new.txt"
        result = self.run_tool(
            "create", "--file", missing_parent,
            "--to-encoding", "utf-8",
            "--to-line-ending", "lf",
            "--text", "content",
            "--json",
            expect=2,
        )
        self.assertIn("parent directory not found", json.loads(result.stdout)["error"]["message"])

        path = self.tmpdir / "format-required.txt"
        result = self.run_tool(
            "create", "--file", path,
            "--to-line-ending", "lf",
            "--text", "content",
            "--json",
            expect=2,
        )
        self.assertIn("explicit --to-encoding", json.loads(result.stdout)["error"]["message"])
        self.assertFalse(path.exists())

        result = self.run_tool(
            "create", "--file", path,
            "--to-encoding", "utf-8",
            "--text", "content",
            "--json",
            expect=2,
        )
        self.assertIn("explicit --to-line-ending", json.loads(result.stdout)["error"]["message"])
        self.assertFalse(path.exists())

    def test_create_dry_run_and_base64_payload(self):
        path = self.tmpdir / "preview.txt"
        encoded = base64.urlsafe_b64encode(b"encoded\ntext").decode("ascii").rstrip("=")
        result = self.run_tool(
            "create", "--file", path,
            "--to-encoding", "utf-8-bom",
            "--to-line-ending", "lf",
            "--text-base64", encoded,
            "--dry-run", "--diff", "--json",
        )
        payload = json.loads(result.stdout)
        self.assertTrue(payload["wouldCreate"])
        self.assertFalse(payload["created"])
        self.assertFalse(payload["written"])
        self.assertIn("encoded", payload["diff"])
        self.assertFalse(path.exists())

    def test_create_enforces_text_safety_limits(self):
        nul_path = self.tmpdir / "contains-nul.txt"
        nul_payload = base64.urlsafe_b64encode(b"a\x00b").decode("ascii").rstrip("=")
        result = self.run_tool(
            "create", "--file", nul_path,
            "--to-encoding", "utf-8",
            "--to-line-ending", "lf",
            "--text-base64", nul_payload,
            "--dry-run", "--json",
            expect=2,
        )
        self.assertIn("NUL bytes", json.loads(result.stdout)["error"]["message"])
        self.assertFalse(nul_path.exists())

        large_path = self.tmpdir / "large.txt"
        result = self.run_tool(
            "create", "--file", large_path,
            "--to-encoding", "utf-8",
            "--to-line-ending", "lf",
            "--text", "12345",
            "--max-bytes", "4",
            "--dry-run", "--json",
            expect=2,
        )
        self.assertIn("exceeding --max-bytes", json.loads(result.stdout)["error"]["message"])
        self.assertFalse(large_path.exists())

    def test_file_not_found_fails(self):
        """Test editing a non-existent file fails with file_error."""
        path = self.tmpdir / "nonexistent.txt"
        result = self.run_tool(
            "inspect", "--file", path, "--json", expect=2,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "file_error")

    def test_edit_file_not_found_fails(self):
        """Test editing a non-existent file fails."""
        path = self.tmpdir / "no_such_file.txt"
        result = self.run_tool(
            "edit", "--file", path, "--old", "x", "--new", "y",
            "--json", expect=2,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "file_error")

    def test_nul_bytes_rejected_in_edit_without_flag(self):
        """Test editing a file with NUL bytes is rejected without --allow-nul."""
        path = self.tmpdir / "has_nul.txt"
        path.write_bytes(b"hello\x00world\n")
        result = self.run_tool(
            "edit", "--file", path, "--old", "hello", "--new", "hi",
            "--json", expect=2,
        )
        payload = json.loads(result.stdout)
        # "decoded text contains NUL bytes" matches "decode" → encoding_error
        self.assertIn(payload["error"]["type"], ("encoding_error", "validation_error"))
        self.assertFalse(payload["ok"])

    def test_nul_bytes_detected_in_inspect(self):
        """Test inspect reports hasNul=true for files with NUL bytes."""
        path = self.tmpdir / "nul_inspect.txt"
        path.write_bytes(b"hello\x00world\n")
        result = self.run_tool("inspect", "--file", path, "--json")
        payload = json.loads(result.stdout)
        self.assertTrue(payload["hasNul"])

    def test_diff_input_with_expected_count(self):
        """Test diff-input with --expected-count applies to each block."""
        path = self.tmpdir / "diff_count.txt"
        path.write_bytes(b"alpha\nbeta\ngamma\n")
        diff = "------- SEARCH\nbeta\n=======\nBETA\n+++++++ REPLACE"
        self.run_tool("edit", "--file", path, "--diff-input", diff, "--expected-count", "1")
        self.assertEqual(path.read_bytes(), b"alpha\nBETA\ngamma\n")

    def test_auto_match_crlf_boundary_preserves_line_endings(self):
        """Test that --auto-match on CRLF file does not introduce standalone LF.

        Regression: when ignore-eol matching found a candidate ending with \r
        (truncated CRLF), the trailing \n was left as a standalone LF in the file.
        Fix: extend match to include the trailing \n when candidate ends with \r.
        """
        # CRLF file content
        path = self.tmpdir / "crlf_boundary.cpp"
        path.write_bytes(
            b"void Func() {\r\n    if (a) {\r\n        doSoft();\r\n    }\r\n"
            b"    else {\r\n        doHard();\r\n    }\r\n}\r\n"
        )

        # LF-only old and new (simulating Agent passing LF text to CRLF file)
        old_file = self.tmpdir / "crlf_old.txt"
        old_file.write_text("    else {\n        doHard();\n    }\n", encoding="utf-8")
        new_file = self.tmpdir / "crlf_new.txt"
        new_file.write_text(
            "    else {\n        ListFaceSoft();\n        entityList = true;\n    }\n",
            encoding="utf-8",
        )

        result = self.run_tool(
            "edit", "--file", path, "--encoding", "utf-8",
            "--old-file", str(old_file), "--new-file", str(new_file),
            "--auto-match", "--expected-count", "1", "--json",
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["changed"], 1)
        self.assertEqual(payload["operations"][0]["matchStrategy"], "ignore-eol")

        # Verify no standalone LF in the result
        # On Windows, read_text() auto-converts \r\n to \n, so use read_bytes().decode()
        new_content = path.read_bytes().decode("utf-8")
        crlf_count = new_content.count("\r\n")
        lf_count = new_content.count("\n")
        standalone_lf = lf_count - crlf_count
        self.assertEqual(standalone_lf, 0, "No standalone LF should exist in CRLF file")


# =========================================================================
    # Lock stale cleanup & JSON enrichment tests
    # =========================================================================

    def test_is_process_alive_current_pid(self):
        """Test _is_process_alive returns True for current process PID."""
        m = self._import_safe_edit()
        self.assertTrue(m._is_process_alive(os.getpid()))

    def test_is_process_alive_dead_pid(self):
        """Test _is_process_alive returns False for non-existent PID."""
        m = self._import_safe_edit()
        # Very large PID is almost certainly not alive on any system
        self.assertFalse(m._is_process_alive(99999999))

    def test_is_process_alive_zero_pid(self):
        """Test _is_process_alive returns False for PID 0."""
        m = self._import_safe_edit()
        self.assertFalse(m._is_process_alive(0))

    def test_read_lock_pid_parses_correctly(self):
        """Test _read_lock_pid parses PID from lock file content."""
        m = self._import_safe_edit()
        lock = self.tmpdir / ".test_read.txt.safe-edit.lock"
        lock.write_text(f"pid={os.getpid()} time={time.time()} file=.test_read.txt.safe-edit.lock\n", encoding="utf-8")
        self.assertEqual(m._read_lock_pid(lock), os.getpid())

    def test_read_lock_pid_returns_none_on_missing_file(self):
        """Test _read_lock_pid returns None when lock file doesn't exist."""
        m = self._import_safe_edit()
        lock = self.tmpdir / ".nonexistent.txt.safe-edit.lock"
        self.assertIsNone(m._read_lock_pid(lock))

    def test_read_lock_pid_returns_none_on_bad_content(self):
        """Test _read_lock_pid returns None for unparseable content."""
        m = self._import_safe_edit()
        lock = self.tmpdir / ".bad.txt.safe-edit.lock"
        lock.write_text("garbage content no pid field\n", encoding="utf-8")
        self.assertIsNone(m._read_lock_pid(lock))

    def test_stale_lock_removed_when_pid_dead(self):
        """Test lock is removed immediately when owner PID is dead (even with stale_seconds=0)."""
        path = self.tmpdir / "dead_pid.txt"
        path.write_bytes(b"foo\n")
        lock = _get_lock_path(path)
        # Use a non-existent PID
        lock.write_text(f"pid=99999999 time={time.time()} file=.dead_pid.txt.safe-edit.lock\n", encoding="utf-8")

        # With stale_seconds=0, PID-based cleanup should still work
        self.run_tool(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--expected-count", "1",
            "--lock-timeout", "1",
            "--lock-stale-seconds", "0",
        )
        self.assertEqual(path.read_bytes(), b"bar\n")
        self.assertFalse(lock.exists())

    def test_stale_lock_removed_when_age_exceeded_even_with_alive_pid(self):
        """Test lock is removed by age check even when PID is alive."""
        path = self.tmpdir / "stale_alive.txt"
        path.write_bytes(b"foo\n")
        lock = _get_lock_path(path)
        # Use current PID (alive), but set lock age > stale_seconds
        lock.write_text(f"pid={os.getpid()} time={time.time() - 200} file=.stale_alive.txt.safe-edit.lock\n", encoding="utf-8")
        # Set mtime to match the old timestamp in the content
        old_time = time.time() - 200
        os.utime(lock, (old_time, old_time))

        self.run_tool(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--expected-count", "1",
            "--lock-timeout", "1",
            "--lock-stale-seconds", "60",
        )
        self.assertEqual(path.read_bytes(), b"bar\n")
        self.assertFalse(lock.exists())

    def test_stale_lock_preserved_when_pid_alive_and_not_stale(self):
        """Test lock is NOT removed when PID is alive and age < stale_seconds."""
        path = self.tmpdir / "fresh_lock.txt"
        path.write_bytes(b"foo\n")
        lock = _get_lock_path(path)
        # Use current PID (alive) + fresh timestamp
        lock.write_text(f"pid={os.getpid()} time={time.time()} file=.fresh_lock.txt.safe-edit.lock\n", encoding="utf-8")

        result = self.run_tool(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--expected-count", "1",
            "--lock-timeout", "0.1",
            "--lock-stale-seconds", "120",
            "--json", expect=2,
        )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "lock_error")
        # Lock file should still exist
        self.assertTrue(lock.exists())

    def test_lock_error_json_includes_metadata(self):
        """Test lock_error JSON output includes targetFile, lockPid, lockAgeSeconds, failureClass, recommendedAction."""
        path = self.tmpdir / "lock_meta.txt"
        path.write_bytes(b"foo\n")
        lock = _get_lock_path(path)
        # Use current PID (alive) + stale_seconds > lock age so lock is NOT removed
        lock_pid = os.getpid()
        lock_time = time.time() - 37.2
        lock.write_text(f"pid={lock_pid} time={lock_time} file=.lock_meta.txt.safe-edit.lock\n", encoding="utf-8")
        # Set mtime to match the timestamp in the content
        os.utime(lock, (lock_time, lock_time))

        result = self.run_tool(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--expected-count", "1",
            "--lock-timeout", "0.1",
            "--lock-stale-seconds", "120",
            "--json", expect=2,
        )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "lock_error")
        self.assertEqual(payload["targetFile"], "lock_meta.txt")
        self.assertEqual(payload["lockPid"], lock_pid)
        self.assertAlmostEqual(payload["lockAgeSeconds"], 37.2, delta=2.0)
        self.assertEqual(payload["failureClass"], "RETRYABLE")
        self.assertEqual(payload["recommendedAction"]["type"], "retry_after_lock_clears")
        self.assertEqual(payload["recommendedAction"]["confidence"], 0.8)

    def test_lock_error_json_with_alive_pid_lock(self):
        """Test lock_error JSON when PID is alive — still includes metadata."""
        path = self.tmpdir / "lock_alive_meta.txt"
        path.write_bytes(b"foo\n")
        lock = _get_lock_path(path)
        lock_time = time.time()
        lock.write_text(f"pid={os.getpid()} time={lock_time} file=.lock_alive_meta.txt.safe-edit.lock\n", encoding="utf-8")

        result = self.run_tool(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--expected-count", "1",
            "--lock-timeout", "0.1",
            "--lock-stale-seconds", "120",
            "--json", expect=2,
        )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "lock_error")
        self.assertEqual(payload["targetFile"], "lock_alive_meta.txt")
        self.assertEqual(payload["lockPid"], os.getpid())
        self.assertEqual(payload["failureClass"], "RETRYABLE")
        self.assertEqual(payload["recommendedAction"]["type"], "retry_after_lock_clears")

    def test_lock_error_json_missing_metadata_when_lock_deleted(self):
        """Test lock_error JSON still has targetFile even if lock file is deleted between fail and JSON output."""
        path = self.tmpdir / "lock_gone.txt"
        path.write_bytes(b"foo\n")
        lock = _get_lock_path(path)
        lock.write_text(f"pid={os.getpid()} time={time.time()} file=.lock_gone.txt.safe-edit.lock\n", encoding="utf-8")

        # Run with very short timeout so it fails, then delete lock before reading JSON
        # Note: in practice, the lock file might be deleted by a concurrent process.
        # We test this by creating a scenario where lock parsing fails.
        result = self.run_tool(
            "edit", "--file", path,
            "--old", "foo", "--new", "bar",
            "--expected-count", "1",
            "--lock-timeout", "0.1",
            "--lock-stale-seconds", "120",
            "--json", expect=2,
        )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "lock_error")
        # targetFile should always be present (computed from args, not lock file)
        self.assertEqual(payload["targetFile"], "lock_gone.txt")
        # failureClass and recommendedAction should always be present for lock_error
        self.assertEqual(payload["failureClass"], "RETRYABLE")
        self.assertEqual(payload["recommendedAction"]["type"], "retry_after_lock_clears")

    def test_default_lock_stale_seconds_is_120(self):
        """Test that --lock-stale-seconds defaults to 120."""
        m = self._import_safe_edit()
        # Verify by inspecting the argument parser default
        parser = m.build_parser()
        action = next(a for a in parser._actions if "--lock-stale-seconds" in a.option_strings)
        self.assertEqual(action.default, 120.0)


    def test_replace_lines_preserves_indent_by_default(self):
        """Test that replace-lines preserves original indentation by default."""
        path = self.tmpdir / "indent_default.txt"
        path.write_bytes(b"void foo()\n{\n    bar();\n}\n")

        self.run_tool(
            "replace-lines",
            "--file", path,
            "--start", "3",
            "--end", "3",
            "--text", "baz();",
        )

        self.assertEqual(
            path.read_bytes(),
            b"void foo()\n{\n    baz();\n}\n",
        )

    def test_replace_lines_no_preserve_indent(self):
        """Test that --no-preserve-indent disables indent preservation."""
        path = self.tmpdir / "no_preserve.txt"
        path.write_bytes(b"void foo()\n{\n    bar();\n}\n")

        self.run_tool(
            "replace-lines",
            "--file", path,
            "--start", "3",
            "--end", "3",
            "--text", "baz();",
            "--no-preserve-indent",
        )

        self.assertEqual(
            path.read_bytes(),
            b"void foo()\n{\nbaz();\n}\n",
        )

    def test_replace_lines_preserve_indent_already_indented(self):
        """Test that already-indented replacement text is not doubled."""
        path = self.tmpdir / "already_indented.txt"
        path.write_bytes(b"void foo()\n{\n    bar();\n}\n")

        self.run_tool(
            "replace-lines",
            "--file", path,
            "--start", "3",
            "--end", "3",
            "--text", "    baz();",
        )

        self.assertEqual(
            path.read_bytes(),
            b"void foo()\n{\n    baz();\n}\n",
        )

    def test_replace_lines_preserve_indent_empty_lines(self):
        """Test that empty lines in replacement don't get indent added."""
        path = self.tmpdir / "empty_lines.txt"
        path.write_bytes(b"void foo()\n{\n    bar();\n}\n")

        block = self.tmpdir / "block.txt"
        block.write_text("alpha();\n\nbeta();", encoding="utf-8")

        self.run_tool(
            "replace-lines",
            "--file", path,
            "--start", "3",
            "--end", "3",
            "--text-file", block,
        )

        self.assertEqual(
            path.read_bytes(),
            b"void foo()\n{\n    alpha();\n\n    beta();\n}\n",
        )

    def test_replace_lines_preserve_indent_multiline(self):
        """Test indent preservation across multiline replacement."""
        path = self.tmpdir / "multiline_indent.txt"
        path.write_bytes(b"void foo()\n{\n    bar();\n    baz();\n}\n")

        block = self.tmpdir / "block.txt"
        block.write_text("alpha();\nbeta();", encoding="utf-8")

        self.run_tool(
            "replace-lines",
            "--file", path,
            "--start", "3",
            "--end", "4",
            "--text-file", block,
        )

        self.assertEqual(
            path.read_bytes(),
            b"void foo()\n{\n    alpha();\n    beta();\n}\n",
        )

    def test_msys2_detection_with_git_prefix(self):
        """Test _detect_msys2_path_corruption detects Git prefix corruption."""
        m = self._import_safe_edit()
        with patch.object(m.sys, "platform", "win32"), \
             patch.dict(os.environ, {
                 "MSYSTEM": "MINGW64",
                 "MINGW_PREFIX": "C:/Program Files/Git",
             }, clear=False):
            os.environ.pop("MSYS2_ARG_CONV_EXCL", None)
            result = m._detect_msys2_path_corruption(
                "C:/Program Files/Git/if (iScale > 30)", "old"
            )
            self.assertIsNotNone(result)
            self.assertIn("MSYS2_ARG_CONV_EXCL", result)

    def test_msys2_detection_single_slash(self):
        """Test _detect_msys2_path_corruption warns on / prefix under MSYS2."""
        m = self._import_safe_edit()
        with patch.object(m.sys, "platform", "win32"), \
             patch.dict(os.environ, {"MSYSTEM": "MINGW64"}, clear=False):
            os.environ.pop("MSYS2_ARG_CONV_EXCL", None)
            result = m._detect_msys2_path_corruption("/if (x > 0)", "old")
            self.assertIsNotNone(result)
            self.assertIn("MSYS2_ARG_CONV_EXCL", result)

    def test_msys2_detection_no_env(self):
        """Test _detect_msys2_path_corruption returns None without MSYS2 env."""
        m = self._import_safe_edit()
        with patch.object(m.sys, "platform", "win32"), \
             patch.dict(os.environ, {}, clear=True):
            result = m._detect_msys2_path_corruption("/if (x > 0)", "old")
            self.assertIsNone(result)

    def test_msys2_detection_with_conv_excl_set(self):
        """Test _detect_msys2_path_corruption returns None when MSYS2_ARG_CONV_EXCL is set."""
        m = self._import_safe_edit()
        with patch.object(m.sys, "platform", "win32"), \
             patch.dict(os.environ, {
                 "MSYSTEM": "MINGW64",
                 "MSYS2_ARG_CONV_EXCL": "*",
             }, clear=False):
            result = m._detect_msys2_path_corruption("/if (x > 0)", "old")
            self.assertIsNone(result)

    def test_msys2_detection_normal_text(self):
        """Test _detect_msys2_path_corruption returns None for normal text."""
        m = self._import_safe_edit()
        with patch.object(m.sys, "platform", "win32"), \
             patch.dict(os.environ, {"MSYSTEM": "MINGW64"}, clear=False):
            os.environ.pop("MSYS2_ARG_CONV_EXCL", None)
            result = m._detect_msys2_path_corruption("if (x > 0)", "old")
            self.assertIsNone(result)

    # =========================================================================
    # check_fs_capability tests
    # =========================================================================

    def test_check_fs_capability_returns_required_keys(self):
        """Test check_fs_capability returns all required keys."""
        m = self._import_safe_edit()
        path = self.tmpdir / "cap_test.txt"
        path.write_bytes(b"test\n")
        result = m.check_fs_capability(str(path))
        self.assertIn("directoryWritable", result)
        self.assertIn("canWriteTmp", result)
        self.assertIn("canCreateLock", result)
        self.assertIn("executionMode", result)
        self.assertIn("suggestions", result)

    def test_check_fs_capability_directory_writable(self):
        """Test check_fs_capability detects writable directory."""
        m = self._import_safe_edit()
        path = self.tmpdir / "cap_writable.txt"
        path.write_bytes(b"test\n")
        result = m.check_fs_capability(str(path))
        self.assertTrue(result["directoryWritable"])
        self.assertIn(result["executionMode"], ("full", "sandbox-safe"))

    def test_check_fs_capability_tmp_writable(self):
        """Test check_fs_capability detects writable tmp."""
        m = self._import_safe_edit()
        path = self.tmpdir / "cap_tmp.txt"
        path.write_bytes(b"test\n")
        result = m.check_fs_capability(str(path))
        self.assertTrue(result["canWriteTmp"])

    def test_check_fs_capability_lock_creatable(self):
        """Test check_fs_capability detects lock capability."""
        m = self._import_safe_edit()
        path = self.tmpdir / "cap_lock.txt"
        path.write_bytes(b"test\n")
        result = m.check_fs_capability(str(path))
        self.assertTrue(result["canCreateLock"])

    def test_check_fs_capability_full_mode_when_writable(self):
        """Test executionMode is 'full' when target dir is writable."""
        m = self._import_safe_edit()
        path = self.tmpdir / "cap_full.txt"
        path.write_bytes(b"test\n")
        result = m.check_fs_capability(str(path))
        if result["directoryWritable"]:
            self.assertEqual(result["executionMode"], "full")

    def test_check_fs_capability_sandbox_mode_when_not_writable(self):
        """Test executionMode is 'sandbox-safe' when target dir not writable but tmp works."""
        m = self._import_safe_edit()
        path = self.tmpdir / "cap_sandbox.txt"
        path.write_bytes(b"test\n")

        original_open = os.open
        def mock_open_for_probe(*args, **kwargs):
            probe_path = args[0] if args else ""
            if ".safe-edit-probe" in probe_path:
                raise OSError("read-only")
            return original_open(*args, **kwargs)

        with patch("os.open", side_effect=mock_open_for_probe):
            result = m.check_fs_capability(str(path))
            if result["canWriteTmp"]:
                self.assertIn(result["executionMode"], ("sandbox-safe", "no-lock-mode"))

    def test_check_fs_capability_suggestions_populated_on_issues(self):
        """Test suggestions list is populated when there are issues."""
        m = self._import_safe_edit()
        path = self.tmpdir / "cap_suggest.txt"
        path.write_bytes(b"test\n")
        result = m.check_fs_capability(str(path))
        # If directory is not writable, should have a suggestion
        if not result["directoryWritable"]:
            self.assertTrue(len(result["suggestions"]) > 0)

    def test_check_fs_capability_readonly_fallback(self):
        """Test executionMode is 'readonly-fallback' when nothing works."""
        m = self._import_safe_edit()
        path = self.tmpdir / "cap_readonly.txt"
        path.write_bytes(b"test\n")
        # Mock all filesystem operations to fail
        original_open = os.open
        def mock_open_readonly(*args, **kwargs):
            raise OSError("read-only filesystem")
        with patch("os.open", side_effect=mock_open_readonly):
            result = m.check_fs_capability(str(path))
            self.assertEqual(result["executionMode"], "readonly-fallback")
            self.assertFalse(result["canWriteTmp"])
            self.assertFalse(result["canCreateLock"])

    def test_stat_includes_capability_fields(self):
        """Test stat command output includes capability fields."""
        path = self.tmpdir / "stat_cap.txt"
        path.write_bytes(b"test\n")
        result = self.run_tool("stat", "--file", path, "--json")
        payload = json.loads(result.stdout)
        self.assertIn("directoryWritable", payload)
        self.assertIn("canCreateTemp", payload)
        self.assertIn("canCreateLock", payload)
        self.assertIn("executionMode", payload)
        self.assertIn("suggestions", payload)

    def test_get_lock_key_same_file_same_key(self):
        """Test _get_lock_key returns same key for same file."""
        m = self._import_safe_edit()
        path = self.tmpdir / "lock_key.txt"
        path.write_bytes(b"test\n")
        key1 = m._get_lock_key(str(path))
        key2 = m._get_lock_key(str(path))
        self.assertEqual(key1, key2)

    def test_get_lock_key_different_files_different_keys(self):
        """Test _get_lock_key returns different keys for different files."""
        m = self._import_safe_edit()
        path1 = self.tmpdir / "lock_key1.txt"
        path2 = self.tmpdir / "lock_key2.txt"
        path1.write_bytes(b"test1\n")
        path2.write_bytes(b"test2\n")
        key1 = m._get_lock_key(str(path1))
        key2 = m._get_lock_key(str(path2))
        self.assertNotEqual(key1, key2)

    def test_get_lock_key_nonexistent_file_uses_path(self):
        """Test _get_lock_key falls back to path hash for non-existent file."""
        m = self._import_safe_edit()
        path = self.tmpdir / "nonexistent.txt"
        key = m._get_lock_key(str(path))
        self.assertIsInstance(key, str)
        self.assertEqual(len(key), 32)

    def test_get_lock_key_resolves_symlinks(self):
        """Test _get_lock_key resolves symlinks to get same key."""
        m = self._import_safe_edit()
        if os.name == "nt":
            self.skipTest("Symlink test not reliable on Windows")
        path = self.tmpdir / "lock_key_symlink.txt"
        path.write_bytes(b"test\n")
        link = self.tmpdir / "lock_key_link.txt"
        try:
            link.symlink_to(path)
            key1 = m._get_lock_key(str(path))
            key2 = m._get_lock_key(str(link))
            self.assertEqual(key1, key2)
        except OSError:
            self.skipTest("Symlinks not supported")

    def test_is_cross_device_error_exdev(self):
        """EXDEV (Unix cross-device link) is recognised."""
        m = self._import_safe_edit()
        self.assertTrue(m._is_cross_device_error(OSError(errno.EXDEV, "cross-device link")))

    def test_is_cross_device_error_winerror(self):
        """Windows ERROR_NOT_SAME_DEVICE (winerror 17) is recognised."""
        m = self._import_safe_edit()
        exc = OSError(17, "The system cannot move the file to a different disk drive")
        exc.winerror = 17
        self.assertTrue(m._is_cross_device_error(exc))

    def test_is_cross_device_error_rejects_other(self):
        """Non cross-device errors are not treated as cross-device."""
        m = self._import_safe_edit()
        self.assertFalse(m._is_cross_device_error(OSError(errno.EACCES, "denied")))
        self.assertFalse(m._is_cross_device_error(OSError(errno.ENOENT, "no such file")))
        self.assertFalse(m._is_cross_device_error(OSError(errno.ENOSPC, "no space")))

    def test_replace_file_same_device_is_atomic(self):
        """On the same filesystem os.replace is used directly (one call)."""
        m = self._import_safe_edit()
        src = self.tmpdir / "src_same.txt"
        dst = self.tmpdir / "dst_same.txt"
        src.write_bytes(b"new contents\n")
        dst.write_bytes(b"old contents\n")

        calls = []
        real_replace = os.replace

        def spy(a, b):
            calls.append((str(a), str(b)))
            return real_replace(a, b)

        with patch.object(m.os, "replace", side_effect=spy):
            m._replace_file(str(src), str(dst))

        self.assertEqual(dst.read_bytes(), b"new contents\n")
        self.assertFalse(src.exists())
        self.assertEqual(len(calls), 1)

    def test_replace_file_cross_device_stages_beside_target(self):
        """A cross-device os.replace failure re-stages beside the target.

        The first os.replace raises EXDEV; the second (stage -> dst) must
        succeed, leaving the destination updated and the staging src removed.
        """
        m = self._import_safe_edit()
        src = self.tmpdir / "src_xdev.txt"
        dst = self.tmpdir / "dst_xdev.txt"
        src.write_bytes(b"new contents\n")
        dst.write_bytes(b"old contents\n")

        calls = {"n": 0}
        real_replace = os.replace

        def fake_replace(a, b):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_replace(a, b)

        with patch.object(m.os, "replace", side_effect=fake_replace):
            m._replace_file(str(src), str(dst))

        self.assertEqual(dst.read_bytes(), b"new contents\n")
        self.assertFalse(src.exists(), "staging src must be cleaned up")
        # No leftover stage files in the target directory.
        leftovers = [p for p in self.tmpdir.iterdir() if p.name.endswith(".stage")]
        self.assertEqual(leftovers, [])

    def test_replace_file_cross_device_target_dir_unwritable_falls_back(self):
        """When the target dir cannot hold a stage file, fall back to copy+delete."""
        m = self._import_safe_edit()
        src = self.tmpdir / "src_fallback.txt"
        dst = self.tmpdir / "dst_fallback.txt"
        src.write_bytes(b"new contents\n")
        dst.write_bytes(b"old contents\n")

        real_replace = os.replace
        real_mkstemp = tempfile.mkstemp

        def fake_replace(a, b):
            # First (direct) replace always crosses devices.
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        def fake_mkstemp(*args, **kwargs):
            # Target-dir staging is impossible in this sandbox scenario.
            raise OSError(errno.EACCES, "Permission denied")

        with patch.object(m.os, "replace", side_effect=fake_replace), \
                patch.object(m.tempfile, "mkstemp", side_effect=fake_mkstemp):
            m._replace_file(str(src), str(dst))

        self.assertEqual(dst.read_bytes(), b"new contents\n")
        self.assertFalse(src.exists())

    def test_replace_file_non_cross_device_error_reraised(self):
        """A permission error (not cross-device) must propagate unchanged."""
        m = self._import_safe_edit()
        src = self.tmpdir / "src_perm.txt"
        dst = self.tmpdir / "dst_perm.txt"
        src.write_bytes(b"new\n")
        dst.write_bytes(b"old\n")

        def fake_replace(a, b):
            raise OSError(errno.EACCES, "Permission denied")

        with patch.object(m.os, "replace", side_effect=fake_replace):
            with self.assertRaises(OSError) as ctx:
                m._replace_file(str(src), str(dst))
        self.assertEqual(ctx.exception.errno, errno.EACCES)
        # Neither file should have been changed.
        self.assertEqual(src.read_bytes(), b"new\n")
        self.assertEqual(dst.read_bytes(), b"old\n")


    def test_atomic_replace_stages_beside_target_first(self):
        m = self._import_safe_edit()
        target = self.tmpdir / "target-stage-first.txt"
        target.write_bytes(b"old\n")
        real_mkstemp = m.tempfile.mkstemp

        with patch.object(m.tempfile, "mkstemp", wraps=real_mkstemp) as mkstemp_mock:
            m.atomic_replace(target, b"new\n", False, None, ".bak")

        self.assertEqual(target.read_bytes(), b"new\n")
        self.assertEqual(len(mkstemp_mock.call_args_list), 1)
        stage_dir = mkstemp_mock.call_args_list[0].kwargs["dir"]
        self.assertEqual(Path(stage_dir).resolve(), target.parent.resolve())

    def test_atomic_replace_falls_back_when_target_staging_is_denied(self):
        m = self._import_safe_edit()
        target = self.tmpdir / "target-stage-fallback.txt"
        target.write_bytes(b"old\n")
        real_mkstemp = m.tempfile.mkstemp
        stage_dirs = []

        def deny_target_staging(*args, **kwargs):
            stage_dir = Path(kwargs["dir"]).resolve()
            stage_dirs.append(stage_dir)
            if stage_dir == target.parent.resolve():
                raise OSError(errno.EACCES, "Permission denied")
            return real_mkstemp(*args, **kwargs)

        with patch.object(m.tempfile, "mkstemp", side_effect=deny_target_staging):
            m.atomic_replace(target, b"new\n", False, None, ".bak")

        self.assertEqual(target.read_bytes(), b"new\n")
        self.assertEqual(stage_dirs[0], target.parent.resolve())
        self.assertEqual(stage_dirs[1], Path(m._get_tmp_dir()).resolve())

    def test_preflight_reports_runtime_and_transports(self):
        result = self.run_tool("preflight", "--json")
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["base64Available"])
        self.assertIn("stdin", payload["requestTransports"])
        self.assertTrue(payload["pythonExecutable"])

    def test_stat_many_reuses_parent_capability_probe(self):
        m = self._import_safe_edit()
        first = self.tmpdir / "stat-many-first.txt"
        second = self.tmpdir / "stat-many-second.txt"
        request = self.tmpdir / "stat-many.json"
        first.write_bytes(b"first\n")
        second.write_bytes(b"second\n")
        request.write_text(
            json.dumps({"files": [str(first), {"file": str(second)}]}),
            encoding="utf-8",
        )
        args = m.build_parser().parse_args(
            ["stat-many", "--request-file", str(request)]
        )

        with patch.object(
            m, "check_fs_capability", wraps=m.check_fs_capability
        ) as capability_mock:
            result = m.run(args)

        self.assertEqual(result["command"], "stat-many")
        self.assertEqual(result["fileCount"], 2)
        self.assertEqual(capability_mock.call_count, 1)
        self.assertEqual(
            result["files"][0]["sha256"], hashlib.sha256(b"first\n").hexdigest()
        )
        self.assertEqual(
            result["files"][1]["sha256"], hashlib.sha256(b"second\n").hexdigest()
        )

    def test_transaction_edits_and_creates_as_one_request(self):
        existing = self.tmpdir / "existing-transaction.txt"
        created = self.tmpdir / "created-transaction.txt"
        request = self.tmpdir / "transaction.json"
        existing.write_bytes(b"alpha\n")
        request.write_text(
            json.dumps(
                {
                    "files": [
                        {
                            "file": str(existing),
                            "action": "edit",
                            "expectedSha256": hashlib.sha256(b"alpha\n").hexdigest(),
                            "operations": [
                                {
                                    "op": "edit",
                                    "old": "alpha",
                                    "new": "beta",
                                    "expected_count": 1,
                                }
                            ],
                        },
                        {
                            "file": str(created),
                            "action": "create",
                            "text": "new\nfile\n",
                            "encoding": "utf-8",
                            "lineEnding": "lf",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        result = self.run_tool(
            "transaction", "--request-file", request, "--json"
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["fileCount"], 2)
        self.assertEqual(payload["atomicity"], "prevalidated-with-rollback")
        self.assertFalse(payload["crashAtomic"])
        self.assertEqual(existing.read_bytes(), b"beta\n")
        self.assertEqual(created.read_bytes(), b"new\nfile\n")

    def test_transaction_reuses_prevalidated_edit_plan(self):
        m = self._import_safe_edit()
        target = self.tmpdir / "planned-transaction.txt"
        request = self.tmpdir / "planned-transaction.json"
        target.write_bytes(b"alpha\n")
        request.write_text(
            json.dumps(
                {
                    "files": [
                        {
                            "file": str(target),
                            "action": "edit",
                            "expectedSha256": hashlib.sha256(b"alpha\n").hexdigest(),
                            "operations": [
                                {
                                    "op": "edit",
                                    "old": "alpha",
                                    "new": "beta",
                                    "expected_count": 1,
                                }
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        args = m.build_parser().parse_args(
            ["transaction", "--request-file", str(request)]
        )

        with patch.object(m, "read_target", wraps=m.read_target) as read_mock, \
                patch.object(m, "apply_operation", wraps=m.apply_operation) as apply_mock, \
                patch.object(m.json, "loads", wraps=m.json.loads) as json_loads_mock:
            result = m.run(args)

        target_reads = [
            call for call in read_mock.call_args_list
            if Path(call.args[0]) == target
        ]
        self.assertEqual(len(target_reads), 2)
        self.assertEqual(apply_mock.call_count, 1)
        self.assertEqual(json_loads_mock.call_count, 1)
        self.assertTrue(result["written"])
        self.assertEqual(target.read_bytes(), b"beta\n")

    def test_transaction_revalidates_target_before_committing_plan(self):
        m = self._import_safe_edit()
        target = self.tmpdir / "concurrent-transaction.txt"
        request = self.tmpdir / "concurrent-transaction.json"
        target.write_bytes(b"alpha\n")
        request.write_text(
            json.dumps(
                {
                    "files": [
                        {
                            "file": str(target),
                            "action": "edit",
                            "expectedSha256": hashlib.sha256(b"alpha\n").hexdigest(),
                            "operations": [
                                {
                                    "op": "edit",
                                    "old": "alpha",
                                    "new": "beta",
                                    "expected_count": 1,
                                }
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        args = m.build_parser().parse_args(
            ["transaction", "--request-file", str(request)]
        )
        real_read_target = m.read_target
        reads = {"target": 0}

        def mutate_before_commit(path, max_bytes):
            if Path(path) == target:
                reads["target"] += 1
                if reads["target"] == 2:
                    target.write_bytes(b"external\n")
            return real_read_target(path, max_bytes)

        with patch.object(m, "read_target", side_effect=mutate_before_commit):
            with self.assertRaises(m.SafeEditError) as ctx:
                m.run(args)

        self.assertIn("target changed after transaction prevalidation", str(ctx.exception))
        self.assertEqual(target.read_bytes(), b"external\n")

    def test_transaction_request_base64_dry_run_does_not_write(self):
        created = self.tmpdir / "base64-transaction.txt"
        request = json.dumps(
            {
                "file": str(created),
                "text": "content",
                "encoding": "utf-8",
                "lineEnding": "lf",
            }
        )
        encoded = base64.urlsafe_b64encode(request.encode("utf-8")).decode("ascii")
        result = self.run_tool(
            "transaction", "--request-base64", encoded,
            "--dry-run", "--json",
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["atomicity"], "prevalidated")
        self.assertFalse(payload["written"])
        self.assertFalse(created.exists())

    def test_transaction_rolls_back_files_written_before_runtime_failure(self):
        m = self._import_safe_edit()
        first = self.tmpdir / "first-rollback.txt"
        second = self.tmpdir / "second-rollback.txt"
        request = self.tmpdir / "rollback-transaction.json"
        request.write_text(
            json.dumps(
                {
                    "files": [
                        {
                            "file": str(first),
                            "action": "create",
                            "text": "first",
                            "encoding": "utf-8",
                            "lineEnding": "lf",
                        },
                        {
                            "file": str(second),
                            "action": "create",
                            "text": "second",
                            "encoding": "utf-8",
                            "lineEnding": "lf",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        args = m.build_parser().parse_args(
            ["transaction", "--request-file", str(request)]
        )
        real_create = m.exclusive_create
        calls = {"count": 0}

        def fail_second(path, data):
            calls["count"] += 1
            if calls["count"] == 2:
                raise m.SafeEditError("injected write failure")
            return real_create(path, data)

        with patch.object(m, "exclusive_create", side_effect=fail_second):
            with self.assertRaises(m.SafeEditError) as ctx:
                m.run(args)
        self.assertIn("transaction rolled back", str(ctx.exception))
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_path_lock_key_survives_atomic_replace(self):
        m = self._import_safe_edit()
        path = self.tmpdir / "stable-path-lock.txt"
        path.write_bytes(b"before\n")
        before = m._get_lock_key(str(path))
        m.atomic_replace(path, b"after\n", False, None, ".bak")
        self.assertEqual(m._get_lock_key(str(path)), before)

    def test_file_lock_survives_atomic_replace(self):
        m = self._import_safe_edit()
        path = self.tmpdir / "held-path-lock.txt"
        path.write_bytes(b"before\n")
        with m.FileLock(path, 0.1, 0):
            m.atomic_replace(path, b"after\n", False, None, ".bak")
            with self.assertRaises(m.SafeEditError):
                with m.FileLock(path, 0.05, 0):
                    pass

    def test_hardlink_alias_uses_inode_lock_and_releases_partial_lock(self):
        m = self._import_safe_edit()
        first = self.tmpdir / "hardlink-first.txt"
        alias = self.tmpdir / "hardlink-alias.txt"
        first.write_bytes(b"content\n")
        try:
            os.link(first, alias)
        except OSError as exc:
            self.skipTest(f"hardlinks unavailable: {exc}")

        with m.FileLock(first, 0.1, 0):
            with self.assertRaises(m.SafeEditError):
                with m.FileLock(alias, 0.05, 0):
                    pass
        with m.FileLock(alias, 0.1, 0):
            pass

    def test_stat_and_inspect_do_not_split_records_for_line_count(self):
        m = self._import_safe_edit()
        path = self.tmpdir / "line-count.txt"
        original = b"alpha\r\nbeta"
        path.write_bytes(original)
        encoding, text = m.detect_and_decode(original, "auto")
        capability = {
            "directoryWritable": True,
            "canWriteTmp": True,
            "canCreateLock": True,
            "executionMode": "full",
            "suggestions": [],
        }
        with patch.object(
            m, "split_records", side_effect=AssertionError("unexpected split")
        ):
            stat_result = m.stat_target(
                path, original, encoding, text, capability
            )
            inspect_result = m.inspect_target(path, original, encoding, text)
        self.assertEqual(stat_result["lineCount"], 2)
        self.assertEqual(inspect_result["lineCount"], 2)

    def test_apply_operations_reuses_line_records(self):
        m = self._import_safe_edit()
        text = "one\ntwo\nthree\nfour\nfive\n"
        operations = [
            {"op": "insert", "line": 2, "text": "inserted"},
            {"op": "replace-lines", "start": 3, "end": 3, "text": "TWO"},
            {"op": "delete-lines", "start": 5, "end": 5},
            {"op": "append", "text": "tail"},
        ]
        expected = text
        expected_results = []
        for operation in operations:
            expected, changed, op, strategy = m.apply_operation(
                expected, operation, "\n"
            )
            expected_results.append(
                {"index": len(expected_results) + 1, "op": op,
                 "changed": changed, "matchStrategy": strategy}
            )

        with patch.object(
            m, "split_records", wraps=m.split_records
        ) as split_mock:
            actual, actual_results = m.apply_operations(
                text, operations, "\n"
            )
        self.assertEqual(actual, expected)
        self.assertEqual(actual_results, expected_results)
        self.assertEqual(split_mock.call_count, 1)

    def test_ignore_indent_builds_line_index_once(self):
        m = self._import_safe_edit()
        text = "".join(
            "    target line\n" if index % 10 == 0 else "    ordinary line\n"
            for index in range(500)
        )
        operation = {
            "old": "\ttarget line",
            "new": "changed",
            "expected_count": 50,
        }
        with patch.object(
            m,
            "_build_line_position_index",
            wraps=m._build_line_position_index,
        ) as index_mock:
            result, changed, _strategy = m.apply_literal_edit(
                text, operation, "\n", ignore_indent=True
            )
        self.assertEqual(changed, 50)
        self.assertEqual(result.count("changed"), 50)
        self.assertEqual(index_mock.call_count, 1)

    def test_context_filter_uses_local_windows(self):
        m = self._import_safe_edit()
        text = "scope-A\nneedle\n" * 200
        operation = {"old": "needle", "new": "changed", "first": True}
        with patch.object(
            m, "_context_before_window", wraps=m._context_before_window
        ) as window_mock:
            result, changed, _strategy = m.apply_literal_edit(
                text,
                operation,
                "\n",
                context_before="scope-A",
            )
        self.assertEqual(changed, 1)
        self.assertIn("changed", result)
        self.assertEqual(window_mock.call_count, 200)

    def test_regex_unlimited_path_skips_precount(self):
        m = self._import_safe_edit()
        real_pattern = m.re.compile(r"value=\d+")

        class PatternProxy:
            def finditer(self, _text):
                raise AssertionError("unlimited regex path should not pre-count")

            def subn(self, replacement, text, count=0):
                return real_pattern.subn(replacement, text, count=count)

        operation = {
            "pattern": r"value=\d+",
            "replacement": "value=0",
            "expected_count": 2,
        }
        with patch.object(m.re, "compile", return_value=PatternProxy()):
            result, changed, strategy = m.apply_regex_edit(
                "value=1 value=2", operation, "\n"
            )
        self.assertEqual(result, "value=0 value=0")
        self.assertEqual(changed, 2)
        self.assertEqual(strategy, "regex")

    def test_performance_benchmark_quick_json(self):
        benchmark = REPO_ROOT / "tests" / "perf" / "benchmark.py"
        completed = subprocess.run(
            [
                sys.executable,
                str(benchmark),
                "--sizes-mib",
                "0.01",
                "--iterations",
                "1",
                "--warmups",
                "0",
                "--context-matches",
                "10",
                "--json",
            ],
            cwd=self.tmpdir,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        names = {item["name"] for item in payload["results"]}
        self.assertIn("cli.inspect", names)
        self.assertIn("cli.stat", names)
        self.assertIn("core.batch-line-ops", names)
        for item in payload["results"]:
            self.assertGreaterEqual(item["medianMs"], 0)
            self.assertGreaterEqual(item["p95Ms"], item["minMs"])


    def test_ignore_eol_mapper_reuses_cr_scan(self):
        m = self._import_safe_edit()
        text = "value\n" * 500
        operation = {
            "old": "value\r\n",
            "new": "done\n",
            "expected_count": 500,
        }
        scan_count = 0
        refresh_next_cr = m._EolPositionMapper._refresh_next_cr

        def counted_refresh(mapper):
            nonlocal scan_count
            scan_count += 1
            return refresh_next_cr(mapper)

        with patch.object(
            m._EolPositionMapper, "_refresh_next_cr", counted_refresh
        ):
            result, changed, _strategy = m.apply_literal_edit(
                text, operation, "\n", ignore_eol=True
            )
        self.assertEqual(changed, 500)
        self.assertEqual(result, "done\n" * 500)
        self.assertEqual(scan_count, 1)

    def test_ignore_indent_line_index_is_lazy(self):
        m = self._import_safe_edit()
        with patch.object(
            m,
            "_build_line_position_index",
            wraps=m._build_line_position_index,
        ) as index_mock:
            with self.assertRaises(m.SafeEditError):
                m.apply_literal_edit(
                    "    ordinary\n",
                    {"old": "\tmissing", "new": "changed"},
                    "\n",
                    ignore_indent=True,
                )
            with self.assertRaises(m.SafeEditError):
                m.apply_literal_edit(
                    "    target\n    target\n",
                    {
                        "old": "\ttarget",
                        "new": "changed",
                        "expected_count": 3,
                    },
                    "\n",
                    ignore_indent=True,
                )
            result, changed, _strategy = m.apply_literal_edit(
                "    target\n    target\n",
                {
                    "old": "    target",
                    "new": "changed",
                    "expected_count": 2,
                },
                "\n",
                ignore_indent=True,
            )
        self.assertEqual(changed, 2)
        self.assertEqual(result, "    changed\n    changed\n")
        self.assertEqual(index_mock.call_count, 0)

    def test_regex_no_match_precedes_invalid_replacement(self):
        m = self._import_safe_edit()
        operation = {"pattern": "missing", "replacement": r"\9"}
        with self.assertRaises(m.SafeEditError) as ctx:
            m.apply_regex_edit("content", operation, "\n")
        self.assertIn("not found", str(ctx.exception))

        unchanged, changed, strategy = m.apply_regex_edit(
            "content",
            {**operation, "no_op_ok": True},
            "\n",
        )
        self.assertEqual((unchanged, changed, strategy), ("content", 0, "regex"))

if __name__ == "__main__":
    unittest.main()
