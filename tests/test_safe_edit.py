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


if __name__ == "__main__":
    unittest.main()
