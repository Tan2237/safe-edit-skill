import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = REPO_ROOT / "mcp" / "safe_edit_server.py"


def _load_server():
    spec = importlib.util.spec_from_file_location(
        "safe_edit_mcp_server_test", SERVER_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load MCP server")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


server = _load_server()
ToolInputError = server.execute_tool.__globals__["ToolInputError"]
ToolExecutionError = server.execute_tool.__globals__["ToolExecutionError"]
implementation = sys.modules[server.execute_tool.__module__]


def _initialize_params(protocol_version="2025-11-25"):
    return {
        "protocolVersion": protocol_version,
        "capabilities": {},
        "clientInfo": {"name": "safe-edit-test", "version": "1.0"},
    }


class SafeEditMcpTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(
            prefix="safe-edit-mcp-test-"
        )
        self.root = Path(self.tempdir.name)
        server.execute_tool.__globals__[
            "_PENDING_TRANSACTIONS"
        ].clear()
        fs_cache = getattr(server.core, "_FS_CAPABILITY_CACHE", None)
        if fs_cache is not None:
            fs_cache.clear()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_tools_list_exposes_structured_fast_path(self):
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        names = [tool["name"] for tool in response["result"]["tools"]]
        self.assertEqual(
            names,
            [
                "safe_edit_preflight",
                "safe_edit_stat",
                "safe_edit_transaction",
            ],
        )

    def test_raw_structured_transaction_needs_no_base64(self):
        target = self.root / "raw-payload.txt"
        target.write_bytes(b"alpha\n")
        stat_summary = server.execute_tool(
            "safe_edit_stat", {"files": [str(target)]}
        )
        expected = stat_summary["files"][0]["sha256"]
        self.assertEqual(expected, hashlib.sha256(b"alpha\n").hexdigest())

        replacement = (
            "<script>\n"
            'const path = "C:\\\\temp\\\\quoted";\n'
            "</script>"
        )
        result = server.execute_tool(
            "safe_edit_transaction",
            {
                "files": [
                    {
                        "file": str(target),
                        "action": "edit",
                        "expectedSha256": expected,
                        "operations": [
                            {
                                "op": "edit",
                                "old": "alpha",
                                "new": replacement,
                                "expected_count": 1,
                            }
                        ],
                    }
                ]
            },
        )

        self.assertTrue(result["written"])
        self.assertEqual(
            target.read_text(encoding="utf-8"), replacement + "\n"
        )
        self.assertEqual(result["transport"], "mcp-structured")

    def test_dry_run_can_be_confirmed_without_resending_payload(self):
        target = self.root / "confirmable.txt"
        target.write_bytes(b"before\n")
        expected = server.execute_tool(
            "safe_edit_stat", {"files": [str(target)]}
        )["files"][0]["sha256"]

        preview = server.execute_tool(
            "safe_edit_transaction",
            {
                "dryRun": True,
                "files": [
                    {
                        "file": str(target),
                        "expectedSha256": expected,
                        "operations": [
                            {
                                "op": "edit",
                                "old": "before",
                                "new": "after",
                                "expected_count": 1,
                            }
                        ],
                    }
                ],
            },
        )

        self.assertEqual(target.read_bytes(), b"before\n")
        self.assertTrue(preview["transactionId"].startswith("tx_"))
        self.assertEqual(preview["files"][0]["diffMode"], "compact")
        self.assertIn("-before", preview["files"][0]["diff"])
        planned_sha = hashlib.sha256(b"after\n").hexdigest()
        self.assertEqual(
            preview["files"][0]["resultSha256"], planned_sha
        )

        applied = server.execute_tool(
            "safe_edit_transaction",
            {"transactionId": preview["transactionId"]},
        )
        self.assertTrue(applied["confirmed"])
        self.assertTrue(applied["written"])
        self.assertEqual(applied["files"][0]["sha256"], planned_sha)
        self.assertEqual(target.read_bytes(), b"after\n")

        with self.assertRaisesRegex(
            ToolExecutionError, "unknown or expired transactionId"
        ):
            server.execute_tool(
                "safe_edit_transaction",
                {"transactionId": preview["transactionId"]},
            )

    def test_transaction_auto_matches_detected_crlf(self):
        target = self.root / "auto-eol.txt"
        target.write_bytes(b"alpha\r\nbeta\r\n")
        expected = server.execute_tool(
            "safe_edit_stat", {"files": [str(target)]}
        )["files"][0]["sha256"]

        result = server.execute_tool(
            "safe_edit_transaction",
            {
                "files": [
                    {
                        "file": str(target),
                        "expectedSha256": expected,
                        "operations": [
                            {
                                "op": "edit",
                                "old": "alpha\nbeta",
                                "new": "ALPHA\nBETA",
                                "expected_count": 1,
                            }
                        ],
                    }
                ]
            },
        )

        operation = result["files"][0]["operations"][0]
        self.assertEqual(operation["matchStrategy"], "ignore-eol")
        self.assertTrue(operation["autoEolMatch"])
        self.assertEqual(target.read_bytes(), b"ALPHA\r\nBETA\r\n")

    def test_transaction_auto_eol_prefers_exact_mixed_match(self):
        target = self.root / "auto-eol-exact-first.txt"
        target.write_bytes(b"same\r\nblock\r\nsame\nblock\n")
        expected = server.execute_tool(
            "safe_edit_stat", {"files": [str(target)]}
        )["files"][0]["sha256"]

        result = server.execute_tool(
            "safe_edit_transaction",
            {
                "files": [
                    {
                        "file": str(target),
                        "expectedSha256": expected,
                        "operations": [
                            {
                                "op": "edit",
                                "old": "same\nblock",
                                "new": "changed",
                                "expected_count": 1,
                            }
                        ],
                    }
                ]
            },
        )

        operation = result["files"][0]["operations"][0]
        self.assertEqual(operation["matchStrategy"], "exact")
        self.assertNotIn("autoEolMatch", operation)
        self.assertEqual(
            target.read_bytes(),
            b"same\r\nblock\r\nchanged\n",
        )

    def test_transaction_auto_eol_matches_mixed_segment(self):
        target = self.root / "auto-eol-mixed-segment.txt"
        target.write_bytes(
            b"head\none\ntwo\nsame\r\nblock\r\ntail\n"
        )
        expected = server.execute_tool(
            "safe_edit_stat", {"files": [str(target)]}
        )["files"][0]["sha256"]

        result = server.execute_tool(
            "safe_edit_transaction",
            {
                "files": [
                    {
                        "file": str(target),
                        "expectedSha256": expected,
                        "operations": [
                            {
                                "op": "edit",
                                "old": "same\nblock",
                                "new": "SAME\nBLOCK",
                                "expected_count": 1,
                            }
                        ],
                    }
                ]
            },
        )

        operation = result["files"][0]["operations"][0]
        self.assertEqual(operation["matchStrategy"], "ignore-eol")
        self.assertTrue(operation["autoEolMatch"])
        self.assertEqual(
            target.read_bytes(),
            b"head\none\ntwo\nSAME\r\nBLOCK\r\ntail\n",
        )

    def test_transaction_auto_eol_normalizes_multiline_context(self):
        target = self.root / "auto-eol-context.txt"
        target.write_bytes(
            b"scope-a\r\nscope-b\r\ntarget\r\n"
            b"other-a\r\nother-b\r\ntarget\r\n"
        )
        expected = server.execute_tool(
            "safe_edit_stat", {"files": [str(target)]}
        )["files"][0]["sha256"]

        result = server.execute_tool(
            "safe_edit_transaction",
            {
                "files": [
                    {
                        "file": str(target),
                        "expectedSha256": expected,
                        "operations": [
                            {
                                "op": "edit",
                                "old": "target",
                                "new": "done",
                                "context_before": "other-a\nother-b",
                                "expected_count": 1,
                            }
                        ],
                    }
                ]
            },
        )

        operation = result["files"][0]["operations"][0]
        self.assertEqual(operation["matchStrategy"], "ignore-eol")
        self.assertTrue(operation["autoEolMatch"])
        self.assertEqual(
            target.read_bytes(),
            b"scope-a\r\nscope-b\r\ntarget\r\n"
            b"other-a\r\nother-b\r\ndone\r\n",
        )

    def test_transaction_auto_eol_uses_local_cr_only_context_window(self):
        target = self.root / "auto-eol-cr-context.txt"
        target.write_bytes(
            b"wanted-a\rwanted-b\rtarget\r"
            b"other-a\rother-b\rtarget\r"
        )
        expected = server.execute_tool(
            "safe_edit_stat", {"files": [str(target)]}
        )["files"][0]["sha256"]

        result = server.execute_tool(
            "safe_edit_transaction",
            {
                "files": [
                    {
                        "file": str(target),
                        "expectedSha256": expected,
                        "operations": [
                            {
                                "op": "edit",
                                "old": "target",
                                "new": "done",
                                "context_before": "wanted-a\nwanted-b",
                                "expected_count": 1,
                            }
                        ],
                    }
                ]
            },
        )

        operation = result["files"][0]["operations"][0]
        self.assertEqual(operation["matchStrategy"], "ignore-eol")
        self.assertTrue(operation["autoEolMatch"])
        self.assertEqual(
            target.read_bytes(),
            b"wanted-a\rwanted-b\rdone\r"
            b"other-a\rother-b\rtarget\r",
        )

    def test_transaction_auto_eol_uses_local_cr_only_after_context(self):
        target = self.root / "auto-eol-cr-after-context.txt"
        target.write_bytes(
            b"target\rother-a\rother-b\r"
            b"target\rwanted-a\rwanted-b\r"
        )
        expected = server.execute_tool(
            "safe_edit_stat", {"files": [str(target)]}
        )["files"][0]["sha256"]

        result = server.execute_tool(
            "safe_edit_transaction",
            {
                "files": [
                    {
                        "file": str(target),
                        "expectedSha256": expected,
                        "operations": [
                            {
                                "op": "edit",
                                "old": "target",
                                "new": "done",
                                "context_after": "wanted-a\nwanted-b",
                                "expected_count": 1,
                            }
                        ],
                    }
                ]
            },
        )

        operation = result["files"][0]["operations"][0]
        self.assertEqual(operation["matchStrategy"], "ignore-eol")
        self.assertTrue(operation["autoEolMatch"])
        self.assertEqual(
            target.read_bytes(),
            b"target\rother-a\rother-b\r"
            b"done\rwanted-a\rwanted-b\r",
        )

    def test_transaction_auto_eol_runs_after_no_op_ok_exact_miss(self):
        target = self.root / "auto-eol-no-op-ok.txt"
        target.write_bytes(b"alpha\r\nbeta\r\n")
        expected = server.execute_tool(
            "safe_edit_stat", {"files": [str(target)]}
        )["files"][0]["sha256"]

        result = server.execute_tool(
            "safe_edit_transaction",
            {
                "files": [
                    {
                        "file": str(target),
                        "expectedSha256": expected,
                        "operations": [
                            {
                                "op": "edit",
                                "old": "alpha\nbeta",
                                "new": "done",
                                "expected_count": 1,
                                "no_op_ok": True,
                            }
                        ],
                    }
                ]
            },
        )

        operation = result["files"][0]["operations"][0]
        self.assertEqual(operation["changed"], 1)
        self.assertEqual(operation["matchStrategy"], "ignore-eol")
        self.assertTrue(operation["autoEolMatch"])
        self.assertEqual(target.read_bytes(), b"done\r\n")

    def test_transaction_auto_eol_can_be_disabled(self):
        target = self.root / "auto-eol-disabled.txt"
        target.write_bytes(b"alpha\r\nbeta\r\n")
        expected = server.execute_tool(
            "safe_edit_stat", {"files": [str(target)]}
        )["files"][0]["sha256"]

        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "safe_edit_transaction",
                    "arguments": {
                        "dryRun": True,
                        "autoEolMatch": False,
                        "files": [
                            {
                                "file": str(target),
                                "expectedSha256": expected,
                                "operations": [
                                    {
                                        "op": "edit",
                                        "old": "alpha\nbeta",
                                        "new": "done",
                                        "expected_count": 1,
                                    }
                                ],
                            }
                        ],
                    },
                },
            }
        )
        payload = response["result"]["structuredContent"]

        self.assertTrue(response["result"]["isError"])
        self.assertEqual(payload["rootCause"], "line_ending_difference")
        self.assertEqual(payload["failureStage"], "target")
        self.assertEqual(
            payload["retryStrategy"]["argumentsPatch"],
            {"autoEolMatch": True},
        )
        self.assertEqual(target.read_bytes(), b"alpha\r\nbeta\r\n")

    def test_transaction_auto_match_handles_line_wrap_both_directions(self):
        cases = (
            ("space-to-line", b"alpha\nbeta\n", "alpha beta"),
            ("line-to-space", b"alpha beta\n", "alpha\nbeta"),
        )
        for name, original, old in cases:
            with self.subTest(name=name):
                target = self.root / f"auto-match-{name}.txt"
                target.write_bytes(original)
                expected = server.execute_tool(
                    "safe_edit_stat", {"files": [str(target)]}
                )["files"][0]["sha256"]

                result = server.execute_tool(
                    "safe_edit_transaction",
                    {
                        "autoMatch": True,
                        "files": [
                            {
                                "file": str(target),
                                "expectedSha256": expected,
                                "operations": [
                                    {
                                        "op": "edit",
                                        "old": old,
                                        "new": "done",
                                        "expected_count": 1,
                                    }
                                ],
                            }
                        ],
                    },
                )

                operation = result["files"][0]["operations"][0]
                self.assertEqual(
                    operation["matchStrategy"],
                    "normalize-whitespace",
                )
                self.assertEqual(target.read_bytes(), b"done\n")

    def test_transaction_auto_match_count_mismatch_fails_closed(self):
        target = self.root / "auto-match-count-mismatch.txt"
        target.write_bytes(b"alpha\tbeta\nalpha\nbeta\n")
        expected = server.execute_tool(
            "safe_edit_stat", {"files": [str(target)]}
        )["files"][0]["sha256"]

        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "safe_edit_transaction",
                    "arguments": {
                        "dryRun": True,
                        "autoMatch": True,
                        "files": [
                            {
                                "file": str(target),
                                "expectedSha256": expected,
                                "operations": [
                                    {
                                        "op": "edit",
                                        "old": "alpha  beta",
                                        "new": "done",
                                        "expected_count": 1,
                                    }
                                ],
                            }
                        ],
                    },
                },
            }
        )
        payload = response["result"]["structuredContent"]

        self.assertTrue(response["result"]["isError"])
        self.assertEqual(payload["error"]["type"], "match_count_mismatch")
        self.assertEqual(payload["rootCause"], "multiple_matches")
        self.assertEqual(payload["failureClass"], "RE_READ_REQUIRED")
        self.assertEqual(payload["expectedCount"], 1)
        self.assertEqual(payload["actualCount"], 2)
        self.assertEqual(payload["failureStage"], "target")
        self.assertFalse(payload["writeAttempted"])
        self.assertNotIn("retryStrategy", payload)
        self.assertEqual(
            target.read_bytes(),
            b"alpha\tbeta\nalpha\nbeta\n",
        )

    def test_transaction_context_count_mismatch_is_not_retryable(self):
        target = self.root / "context-count-mismatch.txt"
        target.write_bytes(
            b"scope-a\r\nscope-b\r\ntarget\r\n"
            b"scope-a\r\nscope-b\r\ntarget\r\n"
        )
        original = target.read_bytes()
        expected = server.execute_tool(
            "safe_edit_stat", {"files": [str(target)]}
        )["files"][0]["sha256"]

        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "safe_edit_transaction",
                    "arguments": {
                        "dryRun": True,
                        "files": [
                            {
                                "file": str(target),
                                "expectedSha256": expected,
                                "operations": [
                                    {
                                        "op": "edit",
                                        "old": "target",
                                        "new": "done",
                                        "context_before": "scope-a\nscope-b",
                                        "expected_count": 1,
                                    }
                                ],
                            }
                        ],
                    },
                },
            }
        )
        payload = response["result"]["structuredContent"]

        self.assertTrue(response["result"]["isError"])
        self.assertEqual(payload["error"]["type"], "match_count_mismatch")
        self.assertEqual(payload["rootCause"], "multiple_matches")
        self.assertEqual(payload["failureClass"], "RE_READ_REQUIRED")
        self.assertEqual(payload["expectedCount"], 1)
        self.assertEqual(payload["actualCount"], 2)
        self.assertEqual(payload["failureStage"], "context_filter")
        self.assertEqual(payload["contextField"], "context_before")
        self.assertEqual(payload["matchesBeforeContext"], 2)
        self.assertEqual(payload["matchesAfterContext"], 2)
        self.assertNotIn("retryStrategy", payload)
        self.assertEqual(target.read_bytes(), original)

    def test_transaction_count_shortfall_is_not_multiple_matches(self):
        target = self.root / "count-shortfall.txt"
        target.write_bytes(b"alpha\n")
        expected = server.execute_tool(
            "safe_edit_stat", {"files": [str(target)]}
        )["files"][0]["sha256"]

        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "safe_edit_transaction",
                    "arguments": {
                        "dryRun": True,
                        "files": [
                            {
                                "file": str(target),
                                "expectedSha256": expected,
                                "operations": [
                                    {
                                        "op": "edit",
                                        "old": "alpha",
                                        "new": "done",
                                        "expected_count": 2,
                                    }
                                ],
                            }
                        ],
                    },
                },
            }
        )
        payload = response["result"]["structuredContent"]

        self.assertTrue(response["result"]["isError"])
        self.assertEqual(payload["error"]["type"], "match_count_mismatch")
        self.assertEqual(payload["rootCause"], "count_mismatch")
        self.assertEqual(payload["failureClass"], "RE_READ_REQUIRED")
        self.assertEqual(payload["expectedCount"], 2)
        self.assertEqual(payload["actualCount"], 1)
        self.assertNotIn("retryStrategy", payload)
        self.assertEqual(target.read_bytes(), b"alpha\n")

    def test_transaction_regex_count_details_cover_excess_and_shortfall(self):
        cases = (
            ("excess", 1, 2, "multiple_matches"),
            ("shortfall", 3, 2, "count_mismatch"),
        )
        for name, expected_count, actual_count, root_cause in cases:
            with self.subTest(name=name):
                target = self.root / f"regex-count-{name}.txt"
                target.write_bytes(b"alpha alpha\n")
                expected_sha = server.execute_tool(
                    "safe_edit_stat", {"files": [str(target)]}
                )["files"][0]["sha256"]

                response = server.handle_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "safe_edit_transaction",
                            "arguments": {
                                "dryRun": True,
                                "files": [
                                    {
                                        "file": str(target),
                                        "expectedSha256": expected_sha,
                                        "operations": [
                                            {
                                                "op": "regex",
                                                "pattern": "alpha",
                                                "replacement": "done",
                                                "expected_count": expected_count,
                                            }
                                        ],
                                    }
                                ],
                            },
                        },
                    }
                )
                payload = response["result"]["structuredContent"]

                self.assertTrue(response["result"]["isError"])
                self.assertEqual(
                    payload["error"]["type"],
                    "match_count_mismatch",
                )
                self.assertEqual(payload["rootCause"], root_cause)
                self.assertEqual(payload["expectedCount"], expected_count)
                self.assertEqual(payload["actualCount"], actual_count)
                self.assertNotIn("retryStrategy", payload)
                self.assertEqual(target.read_bytes(), b"alpha alpha\n")

    def test_transaction_missing_target_is_not_reported_as_context_failure(self):
        target = self.root / "missing-target-with-context.txt"
        target.write_bytes(b"header\ncontext\n")
        expected = server.execute_tool(
            "safe_edit_stat", {"files": [str(target)]}
        )["files"][0]["sha256"]

        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "safe_edit_transaction",
                    "arguments": {
                        "dryRun": True,
                        "files": [
                            {
                                "file": str(target),
                                "expectedSha256": expected,
                                "operations": [
                                    {
                                        "op": "edit",
                                        "old": "missing",
                                        "new": "done",
                                        "context_before": "header\ncontext",
                                        "expected_count": 1,
                                    }
                                ],
                            }
                        ],
                    },
                },
            }
        )
        payload = response["result"]["structuredContent"]

        self.assertTrue(response["result"]["isError"])
        self.assertEqual(payload["failureStage"], "target")
        self.assertEqual(payload["rootCause"], "content_not_found")
        self.assertNotIn("contextField", payload)
        self.assertEqual(target.read_bytes(), b"header\ncontext\n")

    def test_transaction_failure_identifies_operation_and_target(self):
        target = self.root / "diagnostic.txt"
        target.write_bytes(b"alpha\n")
        expected = server.execute_tool(
            "safe_edit_stat", {"files": [str(target)]}
        )["files"][0]["sha256"]

        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "safe_edit_transaction",
                    "arguments": {
                        "dryRun": True,
                        "files": [
                            {
                                "file": str(target),
                                "expectedSha256": expected,
                                "operations": [
                                    {
                                        "op": "edit",
                                        "old": "alpha",
                                        "new": "beta",
                                        "expected_count": 1,
                                    },
                                    {
                                        "op": "edit",
                                        "old": "missing target",
                                        "new": "replacement",
                                        "expected_count": 1,
                                    },
                                ],
                            }
                        ],
                    },
                },
            }
        )
        payload = response["result"]["structuredContent"]

        self.assertTrue(response["result"]["isError"])
        self.assertEqual(payload["failedFile"]["index"], 1)
        self.assertEqual(payload["operationIndex"], 2)
        self.assertEqual(
            payload["failedOperation"]["targetFragment"],
            "missing target",
        )
        self.assertEqual(payload["failureReason"], "content_not_found")
        self.assertTrue(payload["dryRun"])
        self.assertEqual(payload["phase"], "prepare")
        self.assertEqual(payload["failureStage"], "target")
        self.assertFalse(payload["writeAttempted"])
        self.assertFalse(payload["statRequired"])
        self.assertEqual(target.read_bytes(), b"alpha\n")

    def test_multi_file_line_wrap_failure_retries_without_restat(self):
        first = self.root / "line-wrap-first.txt"
        second = self.root / "line-wrap-second.txt"
        first.write_bytes(b"before\n")
        second.write_bytes(b"alpha\nbeta\n")
        stats = server.execute_tool(
            "safe_edit_stat", {"files": [str(first), str(second)]}
        )["files"]
        first_sha = stats[0]["sha256"]
        second_sha = stats[1]["sha256"]

        files = [
            {
                "file": str(first),
                "expectedSha256": first_sha,
                "operations": [
                    {
                        "op": "edit",
                        "old": "before",
                        "new": "after",
                        "expected_count": 1,
                    }
                ],
            },
            {
                "file": str(second),
                "expectedSha256": second_sha,
                "operations": [
                    {
                        "op": "edit",
                        "old": "alpha beta",
                        "new": "ALPHA BETA",
                        "expected_count": 1,
                    }
                ],
            },
        ]
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "safe_edit_transaction",
                    "arguments": {"dryRun": True, "files": files},
                },
            }
        )
        payload = response["result"]["structuredContent"]

        self.assertTrue(response["result"]["isError"])
        self.assertEqual(payload["failedFile"]["index"], 2)
        self.assertEqual(payload["rootCause"], "whitespace_difference")
        self.assertEqual(payload["failureClass"], "RETRYABLE")
        self.assertEqual(
            payload["retryStrategy"]["argumentsPatch"],
            {"autoMatch": True},
        )
        self.assertTrue(payload["dryRun"])
        self.assertEqual(payload["phase"], "prepare")
        self.assertFalse(payload["writeAttempted"])
        self.assertFalse(payload["statRequired"])
        self.assertEqual(first.read_bytes(), b"before\n")
        self.assertEqual(second.read_bytes(), b"alpha\nbeta\n")

        preview = server.execute_tool(
            "safe_edit_transaction",
            {"dryRun": True, "files": files, "autoMatch": True},
        )
        self.assertFalse(preview["written"])
        self.assertEqual(first.read_bytes(), b"before\n")
        self.assertEqual(second.read_bytes(), b"alpha\nbeta\n")

        applied = server.execute_tool(
            "safe_edit_transaction",
            {"transactionId": preview["transactionId"]},
        )
        self.assertTrue(applied["written"])
        self.assertEqual(first.read_bytes(), b"after\n")
        self.assertEqual(second.read_bytes(), b"ALPHA BETA\n")

    def test_hash_mismatch_returns_retryable_actual_sha256(self):
        target = self.root / "stale-hash.txt"
        target.write_bytes(b"alpha\n")

        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "safe_edit_transaction",
                    "arguments": {
                        "files": [
                            {
                                "file": str(target),
                                "expectedSha256": "0" * 64,
                                "operations": [
                                    {
                                        "op": "edit",
                                        "old": "alpha",
                                        "new": "beta",
                                        "expected_count": 1,
                                    }
                                ],
                            }
                        ],
                    },
                },
            }
        )
        payload = response["result"]["structuredContent"]

        self.assertTrue(response["result"]["isError"])
        self.assertEqual(payload["error"]["type"], "hash_mismatch")
        self.assertEqual(payload["failureClass"], "RETRYABLE")
        self.assertEqual(payload["expectedSha256"], "0" * 64)
        self.assertEqual(
            payload["actualSha256"], hashlib.sha256(b"alpha\n").hexdigest()
        )
        self.assertEqual(
            payload["recommendedAction"]["type"], "retry_with_actual_sha256"
        )
        self.assertEqual(
            payload["retryStrategy"]["expectedSha256"],
            payload["actualSha256"],
        )
        self.assertEqual(payload["failedFile"]["index"], 1)
        self.assertEqual(target.read_bytes(), b"alpha\n")

    def test_identical_edit_is_explicitly_skipped(self):
        target = self.root / "no-op.txt"
        target.write_bytes(b"same\n")
        expected = server.execute_tool(
            "safe_edit_stat", {"files": [str(target)]}
        )["files"][0]["sha256"]

        result = server.execute_tool(
            "safe_edit_transaction",
            {
                "files": [
                    {
                        "file": str(target),
                        "expectedSha256": expected,
                        "operations": [
                            {
                                "op": "edit",
                                "old": "same",
                                "new": "same",
                                "expected_count": 1,
                            }
                        ],
                    }
                ]
            },
        )

        operation = result["files"][0]["operations"][0]
        self.assertTrue(operation["skipped"])
        self.assertEqual(operation["reason"], "old_equals_new")
        self.assertFalse(result["written"])
        self.assertEqual(result["files"][0]["sha256"], expected)

    def test_transaction_exposes_fuzzy_worker_options(self):
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        transaction = next(
            tool
            for tool in response["result"]["tools"]
            if tool["name"] == "safe_edit_transaction"
        )
        properties = transaction["inputSchema"]["properties"]
        self.assertIn("autoMatch", properties)
        self.assertIn("autoEolMatch", properties)
        self.assertIn("transactionId", properties)
        self.assertIn("fuzzy", properties)
        self.assertIn("fuzzyWorkers", properties)
        self.assertEqual(
            properties["lockTimeout"]["maximum"],
            implementation.MAX_LOCK_TIMEOUT_SECONDS,
        )
        self.assertEqual(properties["lockTimeout"]["minimum"], 0)
        self.assertEqual(properties["lockStaleSeconds"]["minimum"], 0)
        self.assertTrue(transaction["annotations"]["destructiveHint"])

        stat_tool = next(
            tool
            for tool in response["result"]["tools"]
            if tool["name"] == "safe_edit_stat"
        )
        stat_properties = stat_tool["inputSchema"]["properties"]
        self.assertEqual(stat_properties["lockTimeout"]["minimum"], 0)
        self.assertEqual(
            stat_properties["lockStaleSeconds"]["minimum"], 0
        )

    def test_resource_limits_match_runtime_and_tool_schemas(self):
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        tools = {
            tool["name"]: tool for tool in response["result"]["tools"]
        }
        stat_schema = tools["safe_edit_stat"]["inputSchema"]
        transaction_schema = tools["safe_edit_transaction"]["inputSchema"]
        stat_files = stat_schema["properties"]["files"]
        transaction_files = transaction_schema["properties"]["files"]

        self.assertEqual(stat_files["maxItems"], implementation.MAX_FILES)
        self.assertEqual(
            transaction_files["maxItems"], implementation.MAX_FILES
        )
        self.assertEqual(
            stat_schema["properties"]["maxBytes"]["maximum"],
            implementation.MAX_FILE_BYTES,
        )
        self.assertEqual(
            transaction_schema["properties"]["maxBytes"]["maximum"],
            implementation.MAX_FILE_BYTES,
        )
        stat_item_max = stat_files["items"]["oneOf"][1]["properties"][
            "maxBytes"
        ]["maximum"]
        self.assertEqual(stat_item_max, implementation.MAX_FILE_BYTES)
        operation_schema = transaction_files["items"]["properties"][
            "operations"
        ]
        self.assertEqual(
            operation_schema["maxItems"],
            implementation.MAX_OPERATIONS_PER_FILE,
        )
        self.assertEqual(
            transaction_schema["oneOf"][1],
            {"required": ["transactionId"], "maxProperties": 1},
        )

        implementation._validate_stat_arguments(
            {
                "files": ["unused"] * implementation.MAX_FILES,
                "maxBytes": implementation.MAX_FILE_BYTES,
            }
        )
        with self.assertRaisesRegex(ToolInputError, "at most 128 items"):
            implementation._validate_stat_arguments(
                {"files": ["unused"] * (implementation.MAX_FILES + 1)}
            )
        with self.assertRaisesRegex(ToolInputError, "maxBytes must be at most"):
            implementation._validate_stat_arguments(
                {
                    "files": ["unused"],
                    "maxBytes": implementation.MAX_FILE_BYTES + 1,
                }
            )
        with self.assertRaisesRegex(ToolInputError, "maxBytes must be at most"):
            implementation._validate_stat_arguments(
                {
                    "files": [
                        {
                            "file": "unused",
                            "maxBytes": implementation.MAX_FILE_BYTES + 1,
                        }
                    ]
                }
            )

        def edit_item(index, operation_count):
            return {
                "file": f"unused-{index}",
                "expectedSha256": "0" * 64,
                "operations": [
                    {"op": "append"} for _ in range(operation_count)
                ],
            }

        implementation._validate_transaction_arguments(
            {
                "files": [
                    edit_item(0, implementation.MAX_OPERATIONS_PER_FILE)
                ],
                "maxBytes": implementation.MAX_FILE_BYTES,
            }
        )
        with self.assertRaisesRegex(ToolInputError, "at most 256 items"):
            implementation._validate_transaction_arguments(
                {
                    "files": [
                        edit_item(
                            0, implementation.MAX_OPERATIONS_PER_FILE + 1
                        )
                    ]
                }
            )
        aggregate = [
            edit_item(index, implementation.MAX_OPERATIONS_PER_FILE)
            for index in range(
                implementation.MAX_TOTAL_OPERATIONS
                // implementation.MAX_OPERATIONS_PER_FILE
            )
        ]
        implementation._validate_transaction_arguments({"files": aggregate})
        with self.assertRaisesRegex(
            ToolInputError, "at most 1024 operations"
        ):
            implementation._validate_transaction_arguments(
                {"files": aggregate + [edit_item(99, 1)]}
            )
        with self.assertRaisesRegex(ToolInputError, "at most 128 items"):
            implementation._validate_transaction_arguments(
                {
                    "files": [
                        edit_item(index, 1)
                        for index in range(implementation.MAX_FILES + 1)
                    ]
                }
            )
        with self.assertRaisesRegex(ToolInputError, "transactionId only"):
            implementation._validate_transaction_arguments(
                {"transactionId": "tx_example", "dryRun": False}
            )

    def test_transaction_validates_fuzzy_worker_options(self):
        for value in (0, 9, True, 2.5, "2"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ToolInputError,
                    "fuzzyWorkers must be auto or an integer from 1 to 8",
                ):
                    server.execute_tool(
                        "safe_edit_transaction",
                        {
                            "files": [{"file": "unused"}],
                            "fuzzyWorkers": value,
                        },
                    )

    def test_transaction_validates_fuzzy_flags(self):
        for name in ("autoMatch", "fuzzy"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    ToolInputError,
                    f"{name} must be a boolean",
                ):
                    server.execute_tool(
                        "safe_edit_transaction",
                        {
                            "files": [{"file": "unused"}],
                            name: 1,
                        },
                    )

    def test_tool_content_validation_returns_error_results(self):
        valid_create = {
            "file": str(self.root / "new.txt"),
            "action": "create",
            "text": "content\n",
            "encoding": "utf-8",
            "lineEnding": "lf",
        }
        invalid_calls = (
            (
                "safe_edit_preflight",
                {"file": "example.txt", "unknown": True},
            ),
            (
                "safe_edit_stat",
                {
                    "files": [
                        {
                            "file": "example.txt",
                            "followSymlink": "false",
                        }
                    ]
                },
            ),
            (
                "safe_edit_stat",
                {"files": [{"file": "example.txt", "unknown": 1}]},
            ),
            (
                "safe_edit_transaction",
                {
                    "files": [
                        {**valid_create, "followSymlink": "false"}
                    ]
                },
            ),
            (
                "safe_edit_transaction",
                {"files": [{**valid_create, "unknown": 1}]},
            ),
            (
                "safe_edit_transaction",
                {
                    "files": [
                        {
                            "file": "edit.txt",
                            "operations": [{"op": "append", "text": "x"}],
                            "expectedSha256": "0" * 63,
                        }
                    ]
                },
            ),
            (
                "safe_edit_transaction",
                {
                    "files": [
                        {
                            "file": "edit.txt",
                            "operations": ["not-an-object"],
                            "expectedSha256": "0" * 64,
                        }
                    ]
                },
            ),
            (
                "safe_edit_transaction",
                {
                    "files": [
                        {
                            "file": "new.txt",
                            "action": "create",
                            "text": "content",
                        }
                    ]
                },
            ),
            (
                "safe_edit_transaction",
                {"files": [valid_create], "unknown": True},
            ),
        )

        for request_id, (name, arguments) in enumerate(
            invalid_calls, start=100
        ):
            with self.subTest(name=name, arguments=arguments):
                response = server.handle_message(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    }
                )
                self.assertNotIn("error", response)
                result = response["result"]
                self.assertTrue(result["isError"])
                payload = result["structuredContent"]
                self.assertEqual(payload["failureReason"], "validation_error")
                self.assertFalse(payload["written"])
                self.assertEqual(
                    json.loads(result["content"][0]["text"]), payload
                )

    def test_false_transaction_flags_are_not_truthiness_coerced(self):
        item = {
            "file": str(self.root / "new.txt"),
            "action": "create",
            "text": "content\n",
            "encoding": "utf-8",
            "lineEnding": "lf",
            "followSymlink": False,
            "forceWrite": False,
            "allowNul": False,
            "trimTrailingWhitespace": False,
            "diff": False,
        }
        implementation._validate_transaction_arguments({"files": [item]})
        args = implementation._fresh_args("transaction")
        implementation._configure_match_options(args, {})
        child = server.core.request_item_args(args, item, True)

        self.assertFalse(child.follow_symlink)
        self.assertFalse(child.force_write)
        self.assertFalse(child.allow_nul)
        self.assertFalse(child.trim_trailing_whitespace)
        self.assertFalse(child.diff)

    def test_transaction_rejects_non_finite_and_excessive_lock_numbers(self):
        invalid_values = (
            -1,
            float("nan"),
            float("inf"),
            float("-inf"),
            1e999,
            10 ** 1000,
        )
        for name in ("lockTimeout", "lockStaleSeconds"):
            for value in invalid_values:
                with self.subTest(name=name, value=value), self.assertRaisesRegex(
                    ToolInputError, "finite non-negative number"
                ):
                    server.execute_tool(
                        "safe_edit_stat",
                        {"files": ["unused"], name: value},
                    )

        with self.assertRaisesRegex(ToolInputError, "must be at most"):
            server.execute_tool(
                "safe_edit_stat",
                {
                    "files": ["unused"],
                    "lockTimeout": (
                        implementation.MAX_LOCK_TIMEOUT_SECONDS + 1
                    ),
                },
            )

        target = self.root / "zero-lock-options.txt"
        target.write_bytes(b"content\n")
        result = server.execute_tool(
            "safe_edit_stat",
            {
                "files": [str(target)],
                "lockTimeout": 0,
                "lockStaleSeconds": 0,
            },
        )
        self.assertTrue(result["ok"])

    def test_stdio_rejects_invalid_wire_json_and_continues(self):
        ping = b'{"jsonrpc":"2.0","id":7,"method":"ping"}\n'
        long_integer = (
            b'{"jsonrpc":"2.0","id":'
            + b"9" * (implementation.MAX_JSON_INTEGER_DIGITS + 1)
            + b',"method":"ping"}\n'
        )
        deep_nesting = (
            b"[" * 2000 + b"0" + b"]" * 2000 + b"\n"
        )
        invalid_packets = (
            b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n',
            b'{"jsonrpc":"2.0","id":Infinity,"method":"ping"}\n',
            b'{"jsonrpc":"2.0","id":-Infinity,"method":"ping"}\n',
            b'{"jsonrpc":"2.0","id":1e999,"method":"ping"}\n',
            long_integer,
            deep_nesting,
            b"\xff\n",
        )

        for packet in invalid_packets:
            with self.subTest(packet=packet[:80]):
                source = io.BytesIO(packet + ping)
                target = io.BytesIO()
                implementation.serve(source, target)
                responses = [
                    json.loads(line)
                    for line in target.getvalue().splitlines()
                ]
                self.assertEqual(len(responses), 2)
                self.assertEqual(responses[0]["error"]["code"], -32700)
                self.assertIsNone(responses[0]["id"])
                self.assertEqual(responses[1], {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "result": {},
                })

    def test_wire_json_nesting_boundary_is_explicit(self):
        at_limit = (
            b"[" * implementation.MAX_JSON_NESTING
            + b"0"
            + b"]" * implementation.MAX_JSON_NESTING
        )
        too_deep = b"[" + at_limit + b"]"

        self.assertIsInstance(
            implementation._decode_wire_message(at_limit), list
        )
        with self.assertRaisesRegex(ValueError, "nesting is too deep"):
            implementation._decode_wire_message(too_deep)

    def test_transaction_runs_fuzzy_workers(self):
        target = self.root / "fuzzy-workers.txt"
        target.write_bytes(
            b"prefix\r\n"
            b"def calculate(price, qty):\r\n"
            b"value = price * qty\r\n"
            b"return value\r\n"
            b"suffix\r\n"
        )
        stat_summary = server.execute_tool(
            "safe_edit_stat", {"files": [str(target)]}
        )
        expected = stat_summary["files"][0]["sha256"]

        result = server.execute_tool(
            "safe_edit_transaction",
            {
                "autoMatch": True,
                "fuzzy": True,
                "fuzzyWorkers": 2,
                "files": [
                    {
                        "file": str(target),
                        "action": "edit",
                        "expectedSha256": expected,
                        "operations": [
                            {
                                "op": "edit",
                                "old": (
                                    "def calculate(cost, qty):\n"
                                    "value = price * qty\n"
                                    "return value"
                                ),
                                "new": "done",
                                "expected_count": 1,
                            }
                        ],
                    }
                ],
            },
        )

        self.assertTrue(result["written"])
        self.assertEqual(
            result["files"][0]["matchOptions"]["fuzzyWorkers"], 2
        )
        self.assertEqual(
            target.read_bytes(), b"prefix\r\ndone\r\nsuffix\r\n"
        )

    def test_large_create_dry_run_stays_in_memory(self):
        target = self.root / "large.html"
        content = (
            "<style>body{font-family:\"A B\"}</style>\n"
            + ("x" * (512 * 1024))
        )
        result = server.execute_tool(
            "safe_edit_transaction",
            {
                "dryRun": True,
                "files": [
                    {
                        "file": str(target),
                        "action": "create",
                        "text": content,
                        "encoding": "utf-8",
                        "lineEnding": "lf",
                    }
                ],
            },
        )

        self.assertTrue(result["dryRun"])
        self.assertFalse(target.exists())
        self.assertEqual(result["transport"], "mcp-structured")
        self.assertGreater(result["files"][0]["sizeBytes"], 512 * 1024)
        self.assertEqual(result["files"][0]["diffMode"], "compact")
        self.assertTrue(result["files"][0]["diffTruncated"])
        self.assertLess(len(result["files"][0]["diff"]), 13_000)

    def test_hot_path_reuses_parser_and_skips_json_decode(self):
        target = self.root / "hot-path.txt"
        target.write_bytes(b"hot\n")
        real_loads = server.core.json.loads

        with patch.object(
            server.core,
            "build_parser",
            side_effect=AssertionError("parser rebuilt on hot path"),
        ), patch.object(
            server.core.json, "loads", wraps=real_loads
        ) as loads_mock:
            first = server.execute_tool(
                "safe_edit_stat", {"files": [str(target)]}
            )
            second = server.execute_tool(
                "safe_edit_stat", {"files": [str(target)]}
            )

        self.assertEqual(first["files"][0]["sha256"], second["files"][0]["sha256"])
        self.assertEqual(loads_mock.call_count, 0)

    def test_confirmed_dry_run_commits_prepared_plan_without_repreparing(self):
        target = self.root / "prepared-confirm.txt"
        target.write_bytes(b"before\n")
        expected = hashlib.sha256(b"before\n").hexdigest()
        arguments = {
            "dryRun": True,
            "files": [
                {
                    "file": str(target),
                    "expectedSha256": expected,
                    "operations": [
                        {
                            "op": "edit",
                            "old": "before",
                            "new": "after",
                            "expected_count": 1,
                        }
                    ],
                }
            ],
        }

        with patch.object(
            server.core,
            "prepare_transaction",
            wraps=server.core.prepare_transaction,
        ) as prepare_mock, patch.object(
            server.core,
            "commit_prepared_transaction",
            wraps=server.core.commit_prepared_transaction,
        ) as commit_mock:
            preview = server.execute_tool(
                "safe_edit_transaction", arguments
            )
            self.assertEqual(prepare_mock.call_count, 1)
            pending = implementation._PENDING_TRANSACTIONS[
                preview["transactionId"]
            ]
            prepared = pending["prepared"]
            self.assertIsInstance(prepared, server.core.PreparedTransaction)
            self.assertIsNone(pending["argumentsJson"])
            self.assertGreaterEqual(
                prepared.retained_bytes, prepared.output_bytes
            )

            with patch.object(
                server.core,
                "prepare_transaction",
                side_effect=AssertionError("confirmation must not reprepare"),
            ):
                applied = server.execute_tool(
                    "safe_edit_transaction",
                    {"transactionId": preview["transactionId"]},
                )

        self.assertEqual(commit_mock.call_count, 1)
        self.assertTrue(applied["confirmed"])
        self.assertTrue(applied["written"])
        self.assertEqual(target.read_bytes(), b"after\n")

    def test_pending_dry_run_does_not_deepcopy_payload(self):
        target = self.root / "no-deepcopy.txt"
        target.write_bytes(b"before\n")
        expected = hashlib.sha256(b"before\n").hexdigest()

        with patch(
            "copy.deepcopy",
            side_effect=AssertionError("pending payload must not be deep-copied"),
        ):
            preview = server.execute_tool(
                "safe_edit_transaction",
                {
                    "dryRun": True,
                    "files": [
                        {
                            "file": str(target),
                            "expectedSha256": expected,
                            "operations": [
                                {
                                    "op": "edit",
                                    "old": "before",
                                    "new": "after",
                                    "expected_count": 1,
                                }
                            ],
                        }
                    ],
                },
            )

        self.assertIn(
            preview["transactionId"], implementation._PENDING_TRANSACTIONS
        )

    def test_pending_snapshot_is_detached_from_caller_mutation(self):
        arguments = {
            "dryRun": True,
            "files": [
                {
                    "file": "example.txt",
                    "operations": [{"op": "append", "text": "original"}],
                }
            ],
        }

        transaction_id = implementation._remember_pending_transaction(
            arguments
        )
        arguments["files"][0]["operations"][0]["text"] = "mutated"
        arguments["files"].append({"file": "injected.txt"})
        pending = implementation._consume_pending_transaction(transaction_id)
        snapshot = json.loads(pending["argumentsJson"])

        self.assertFalse(snapshot["dryRun"])
        self.assertEqual(len(snapshot["files"]), 1)
        self.assertEqual(
            snapshot["files"][0]["operations"][0]["text"],
            "original",
        )

    def test_pending_json_rejects_nan_and_preserves_lone_surrogates(self):
        surrogate = chr(0xD800)
        encoded = implementation._encode_pending_arguments(
            {"dryRun": True, "files": [], "label": surrogate}
        )
        self.assertEqual(json.loads(encoded)["label"], surrogate)

        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaisesRegex(
                ToolInputError, "cannot be cached"
            ):
                implementation._encode_pending_arguments(
                    {"dryRun": True, "files": [], "value": value}
                )

    def test_pending_prepared_plan_uses_real_retained_byte_admission(self):
        target = self.root / "prepared-size.txt"
        target.write_bytes(b"before\n" + (b"x" * (64 * 1024)))
        expected = hashlib.sha256(target.read_bytes()).hexdigest()
        arguments = {
            "dryRun": True,
            "files": [
                {
                    "file": str(target),
                    "expectedSha256": expected,
                    "operations": [
                        {
                            "op": "edit",
                            "old": "before",
                            "new": "after",
                            "expected_count": 1,
                        }
                    ],
                }
            ],
        }
        preview = server.execute_tool("safe_edit_transaction", arguments)
        prepared = implementation._PENDING_TRANSACTIONS[
            preview["transactionId"]
        ]["prepared"]

        self.assertIsInstance(prepared, server.core.PreparedTransaction)
        self.assertGreaterEqual(prepared.retained_bytes, prepared.output_bytes)

        oversized = prepared._replace(
            output_bytes=(
                implementation.MAX_PENDING_PREPARED_OUTPUT_BYTES + 1
            ),
            retained_bytes=max(
                prepared.retained_bytes,
                implementation.MAX_PENDING_PREPARED_OUTPUT_BYTES + 1,
            ),
        )
        inconsistent = prepared._replace(
            retained_bytes=prepared.output_bytes - 1
        )
        for case, candidate in (
            ("output-limit", oversized),
            ("inconsistent-size", inconsistent),
        ):
            with self.subTest(case=case):
                transaction_id = implementation._remember_pending_transaction(
                    arguments, candidate
                )
                pending = implementation._PENDING_TRANSACTIONS[transaction_id]
                self.assertIsNone(pending["prepared"])
                self.assertIsInstance(pending["argumentsJson"], bytes)

        admission_limit = (
            prepared.retained_bytes
            + implementation.PENDING_TRANSACTION_OVERHEAD_BYTES
            - 1
        )
        with patch.object(
            implementation,
            "MAX_PENDING_TRANSACTION_BYTES",
            admission_limit,
        ):
            transaction_id = implementation._remember_pending_transaction(
                arguments, prepared
            )
        pending = implementation._PENDING_TRANSACTIONS[transaction_id]
        self.assertIsNone(pending["prepared"])
        self.assertIsInstance(pending["argumentsJson"], bytes)

    def test_pending_single_item_limit_preserves_existing_tokens(self):
        existing_id = implementation._remember_pending_transaction(
            {"dryRun": True, "files": [{"file": "existing.txt"}]}
        )

        with patch.object(
            implementation, "MAX_PENDING_TRANSACTION_BYTES", 512
        ), self.assertRaisesRegex(ToolExecutionError, "cache limit"):
            implementation._remember_pending_transaction(
                {
                    "dryRun": True,
                    "files": [{"file": "new.txt", "text": "x" * 4096}],
                }
            )

        self.assertIn(existing_id, implementation._PENDING_TRANSACTIONS)

    def test_pending_ttl_expires_at_exact_boundary(self):
        now = 1234.5
        with patch.object(
            implementation.time, "monotonic", return_value=now
        ):
            transaction_id = implementation._remember_pending_transaction(
                {"dryRun": True, "files": [{"file": "example.txt"}]}
            )
        expires_at = implementation._PENDING_TRANSACTIONS[transaction_id][
            "expiresAt"
        ]

        implementation._prune_pending_transactions(expires_at - 0.001)
        self.assertIn(transaction_id, implementation._PENDING_TRANSACTIONS)
        implementation._prune_pending_transactions(expires_at)
        self.assertNotIn(transaction_id, implementation._PENDING_TRANSACTIONS)

    def test_stale_confirmation_is_business_error_and_consumes_token(self):
        target = self.root / "stale-confirmation.txt"
        target.write_bytes(b"before\n")
        expected = hashlib.sha256(b"before\n").hexdigest()
        preview = server.execute_tool(
            "safe_edit_transaction",
            {
                "dryRun": True,
                "files": [
                    {
                        "file": str(target),
                        "expectedSha256": expected,
                        "operations": [
                            {
                                "op": "edit",
                                "old": "before",
                                "new": "after",
                                "expected_count": 1,
                            }
                        ],
                    }
                ],
            },
        )
        transaction_id = preview["transactionId"]
        target.write_bytes(b"changed externally\n")

        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 41,
                "method": "tools/call",
                "params": {
                    "name": "safe_edit_transaction",
                    "arguments": {"transactionId": transaction_id},
                },
            }
        )

        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])
        payload = response["result"]["structuredContent"]
        self.assertEqual(payload["error"]["type"], "hash_mismatch")
        self.assertNotIn(transaction_id, implementation._PENDING_TRANSACTIONS)
        retry = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 42,
                "method": "tools/call",
                "params": {
                    "name": "safe_edit_transaction",
                    "arguments": {"transactionId": transaction_id},
                },
            }
        )
        self.assertNotIn("error", retry)
        self.assertTrue(retry["result"]["isError"])
        retry_payload = retry["result"]["structuredContent"]
        self.assertEqual(retry_payload["failureReason"], "execution_error")
        self.assertIn("unknown or expired", retry_payload["error"]["message"])
        self.assertFalse(retry_payload["written"])

    def test_confirmation_internal_failure_reports_uncertain_write_state(self):
        target = self.root / "internal-confirmation.txt"
        target.write_bytes(b"before\n")
        expected = hashlib.sha256(b"before\n").hexdigest()
        preview = server.execute_tool(
            "safe_edit_transaction",
            {
                "dryRun": True,
                "files": [
                    {
                        "file": str(target),
                        "expectedSha256": expected,
                        "operations": [
                            {
                                "op": "edit",
                                "old": "before",
                                "new": "after",
                                "expected_count": 1,
                            }
                        ],
                    }
                ],
            },
        )
        transaction_id = preview["transactionId"]
        stderr = io.StringIO()

        with patch.object(
            implementation.core,
            "commit_prepared_transaction",
            side_effect=RuntimeError("sensitive commit detail"),
        ), patch.object(implementation.sys, "stderr", stderr):
            response = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 411,
                    "method": "tools/call",
                    "params": {
                        "name": "safe_edit_transaction",
                        "arguments": {"transactionId": transaction_id},
                    },
                }
            )

        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])
        payload = response["result"]["structuredContent"]
        self.assertEqual(payload["failureStage"], "transaction_commit")
        self.assertIsNone(payload["writeAttempted"])
        self.assertIsNone(payload["written"])
        self.assertIsNone(payload["partialWrite"])
        self.assertTrue(payload["outcomeUncertain"])
        self.assertTrue(payload["statRequired"])
        self.assertTrue(payload["transactionIdConsumed"])
        self.assertEqual(payload["failureClass"], "RE_READ_REQUIRED")
        self.assertEqual(
            payload["recommendedAction"]["type"],
            "re_stat_and_retry_with_fresh_dry_run",
        )
        self.assertEqual(
            payload["serverInstanceId"], preview["serverInstanceId"]
        )
        self.assertNotIn(transaction_id, implementation._PENDING_TRANSACTIONS)
        self.assertEqual(target.read_bytes(), b"before\n")
        self.assertNotIn("sensitive commit detail", json.dumps(payload))
        log_event = json.loads(stderr.getvalue())
        self.assertEqual(log_event["failureStage"], "transaction_commit")
        self.assertTrue(log_event["transactionIdConsumed"])
        self.assertTrue(log_event["frames"])
        self.assertNotIn("sensitive commit detail", stderr.getvalue())

    def test_direct_transaction_internal_failure_requires_restat(self):
        target = self.root / "internal-direct.txt"
        target.write_bytes(b"before\n")
        expected = hashlib.sha256(b"before\n").hexdigest()
        stderr = io.StringIO()
        with patch.object(
            implementation.core,
            "run_transaction_payload",
            side_effect=RuntimeError("sensitive direct detail"),
        ), patch.object(implementation.sys, "stderr", stderr):
            response = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 413,
                    "method": "tools/call",
                    "params": {
                        "name": "safe_edit_transaction",
                        "arguments": {
                            "files": [
                                {
                                    "file": str(target),
                                    "expectedSha256": expected,
                                    "operations": [
                                        {
                                            "op": "edit",
                                            "old": "before",
                                            "new": "after",
                                            "expected_count": 1,
                                        }
                                    ],
                                }
                            ]
                        },
                    },
                }
            )

        payload = response["result"]["structuredContent"]
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(payload["failureStage"], "transaction_commit")
        self.assertIsNone(payload["writeAttempted"])
        self.assertTrue(payload["outcomeUncertain"])
        self.assertTrue(payload["statRequired"])
        self.assertFalse(payload["transactionIdConsumed"])
        self.assertEqual(target.read_bytes(), b"before\n")
        self.assertNotIn("sensitive direct detail", json.dumps(payload))
        self.assertNotIn("sensitive direct detail", stderr.getvalue())

    def test_foreign_server_transaction_id_has_specific_recovery_feedback(self):
        foreign_instance = "0" * 16
        if foreign_instance == implementation.SERVER_INSTANCE_ID:
            foreign_instance = "f" * 16
        transaction_id = f"tx_{foreign_instance}_example"

        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 412,
                "method": "tools/call",
                "params": {
                    "name": "safe_edit_transaction",
                    "arguments": {"transactionId": transaction_id},
                },
            }
        )

        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])
        payload = response["result"]["structuredContent"]
        self.assertEqual(
            payload["failureReason"],
            "transaction_server_instance_mismatch",
        )
        self.assertEqual(
            payload["transactionServerInstanceId"], foreign_instance
        )
        self.assertEqual(
            payload["serverInstanceId"], implementation.SERVER_INSTANCE_ID
        )
        self.assertFalse(payload["transactionIdConsumed"])
        self.assertFalse(payload["statRequired"])
        self.assertEqual(
            payload["recommendedAction"]["type"], "run_new_dry_run"
        )

    def test_symlink_target_change_rejects_confirmation(self):
        first = self.root / "symlink-first.txt"
        second = self.root / "symlink-second.txt"
        link = self.root / "symlink-target.txt"
        first.write_bytes(b"before\n")
        second.write_bytes(b"before\n")
        transaction_target = link
        follow_symlink = True
        confirmation_context = nullcontext()
        try:
            link.symlink_to(first)
        except (NotImplementedError, OSError):
            transaction_target = first
            follow_symlink = False
            confirmation_context = patch.object(
                server.core,
                "resolve_target_path",
                return_value=second,
            )

        expected = hashlib.sha256(b"before\n").hexdigest()
        preview = server.execute_tool(
            "safe_edit_transaction",
            {
                "dryRun": True,
                "files": [
                    {
                        "file": str(transaction_target),
                        "followSymlink": follow_symlink,
                        "expectedSha256": expected,
                        "operations": [
                            {
                                "op": "edit",
                                "old": "before",
                                "new": "after",
                                "expected_count": 1,
                            }
                        ],
                    }
                ],
            },
        )
        transaction_id = preview["transactionId"]
        if link.is_symlink():
            link.unlink()
            link.symlink_to(second)

        with confirmation_context:
            response = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 43,
                    "method": "tools/call",
                    "params": {
                        "name": "safe_edit_transaction",
                        "arguments": {"transactionId": transaction_id},
                    },
                }
            )

        self.assertTrue(response["result"]["isError"])
        payload = response["result"]["structuredContent"]
        self.assertIn("canonical path changed", payload["error"]["message"])
        self.assertNotIn(transaction_id, implementation._PENDING_TRANSACTIONS)
        self.assertEqual(first.read_bytes(), b"before\n")
        self.assertEqual(second.read_bytes(), b"before\n")

    def test_pending_transactions_enforce_aggregate_byte_budget(self):
        target = self.root / "pending-budget.txt"
        target.write_bytes(b"before\n")
        expected = hashlib.sha256(b"before\n").hexdigest()
        arguments = {
            "dryRun": True,
            "files": [
                {
                    "file": str(target),
                    "expectedSha256": expected,
                    "operations": [
                        {
                            "op": "edit",
                            "old": "before",
                            "new": "after",
                            "expected_count": 1,
                        }
                    ],
                }
            ],
        }

        sample = server.execute_tool("safe_edit_transaction", arguments)
        sample_size = implementation._PENDING_TRANSACTIONS[
            sample["transactionId"]
        ]["sizeBytes"]
        implementation._PENDING_TRANSACTIONS.clear()
        total_limit = max(1, sample_size * 2 - 1)

        with patch.object(
            implementation, "MAX_PENDING_TOTAL_BYTES", total_limit
        ), patch.object(implementation, "MAX_PENDING_TRANSACTIONS", 32):
            first_id = server.execute_tool(
                "safe_edit_transaction", arguments
            )["transactionId"]
            with self.assertRaisesRegex(
                ToolExecutionError, "cache is full"
            ):
                server.execute_tool("safe_edit_transaction", arguments)

        self.assertLessEqual(
            sum(
                item["sizeBytes"]
                for item in implementation._PENDING_TRANSACTIONS.values()
            ),
            total_limit,
        )
        self.assertIn(first_id, implementation._PENDING_TRANSACTIONS)
        confirmed = server.execute_tool(
            "safe_edit_transaction", {"transactionId": first_id}
        )
        self.assertTrue(confirmed["confirmed"])
        self.assertEqual(target.read_bytes(), b"after\n")

    def test_pending_count_limit_preserves_existing_token(self):
        first_id = implementation._remember_pending_transaction(
            {"dryRun": True, "files": [{"file": "existing.txt"}]}
        )
        with patch.object(
            implementation, "MAX_PENDING_TRANSACTIONS", 1
        ), self.assertRaisesRegex(ToolExecutionError, "cache is full"):
            implementation._remember_pending_transaction(
                {"dryRun": True, "files": [{"file": "new.txt"}]}
            )

        self.assertIn(first_id, implementation._PENDING_TRANSACTIONS)

    def test_stdio_writer_avoids_concatenating_large_packet_and_newline(self):
        class RecordingStream:
            def __init__(self):
                self.writes = []
                self.flushes = 0

            def write(self, value):
                self.writes.append(value)

            def flush(self):
                self.flushes += 1

        stream = RecordingStream()
        implementation._write_message(
            stream,
            {"jsonrpc": "2.0", "id": 1, "result": {"text": "x" * 4096}},
        )

        self.assertEqual(len(stream.writes), 2)
        self.assertEqual(stream.writes[1], b"\n")
        self.assertEqual(stream.flushes, 1)
        self.assertEqual(json.loads(stream.writes[0])["id"], 1)

    def test_stdio_reader_checks_whitespace_without_strip_copy(self):
        class NoStripBytes(bytes):
            def strip(self, *args, **kwargs):
                raise AssertionError("serve must not copy packets with strip()")

        class Source:
            def __init__(self):
                self.lines = [
                    NoStripBytes(
                        b'{"jsonrpc":"2.0","id":7,"method":"ping"}\n'
                    ),
                    b"",
                ]

            def readline(self, _limit):
                return self.lines.pop(0)

        class Target:
            def __init__(self):
                self.writes = []

            def write(self, value):
                self.writes.append(value)

            def flush(self):
                pass

        target = Target()
        implementation.serve(Source(), target)
        response = json.loads(b"".join(target.writes))
        self.assertEqual(response["id"], 7)
        self.assertEqual(response["result"], {})

    def test_filesystem_capability_cache_survives_fresh_mcp_args(self):
        target = self.root / "cached-capability.txt"
        target.write_bytes(b"content\n")

        with patch.object(
            server.core,
            "check_fs_capability",
            wraps=server.core.check_fs_capability,
        ) as capability_mock:
            server.execute_tool("safe_edit_stat", {"files": [str(target)]})
            server.execute_tool("safe_edit_stat", {"files": [str(target)]})

        self.assertEqual(capability_mock.call_count, 1)

    def test_repository_version_entry_does_not_import_editing_core(self):
        code = (
            "import runpy,sys\n"
            f"sys.argv = [{str(SERVER_PATH)!r}, '--version']\n"
            "try:\n"
            f"    runpy.run_path({str(SERVER_PATH)!r}, run_name='__main__')\n"
            "except SystemExit as exc:\n"
            "    print('exit=' + str(exc.code))\n"
            "print('core-loaded=' + str('safe_edit' in sys.modules))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("1.2.1", completed.stdout)
        self.assertIn("exit=0", completed.stdout)
        self.assertIn("core-loaded=False", completed.stdout)

    def test_stdio_initialize_and_tool_discovery(self):
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": _initialize_params(),
            },
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ]
        completed = subprocess.run(
            [sys.executable, str(SERVER_PATH)],
            input="\n".join(json.dumps(item) for item in messages) + "\n",
            text=True,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        responses = [
            json.loads(line)
            for line in completed.stdout.splitlines()
            if line.strip()
        ]

        self.assertEqual([item["id"] for item in responses], [1, 2, 3])
        self.assertEqual(
            responses[0]["result"]["serverInfo"]["name"], "safe-edit"
        )
        self.assertEqual(len(responses[1]["result"]["tools"]), 3)

    def test_stdio_dry_run_and_confirmation_share_server_instance(self):
        target = self.root / "stdio-confirmation.txt"
        target.write_bytes(b"before\n")
        expected = hashlib.sha256(b"before\n").hexdigest()
        process = subprocess.Popen(
            [sys.executable, str(SERVER_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.assertIsNotNone(process.stdin)
        self.assertIsNotNone(process.stdout)
        self.assertIsNotNone(process.stderr)
        stderr_output = ""

        def exchange(message):
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            response_line = process.stdout.readline()
            if not response_line:
                self.fail(process.stderr.read())
            return json.loads(response_line)

        try:
            initialized = exchange(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _initialize_params(),
                }
            )
            self.assertEqual(
                initialized["result"]["serverInfo"]["name"], "safe-edit"
            )
            preview = exchange(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "safe_edit_transaction",
                        "arguments": {
                            "dryRun": True,
                            "files": [
                                {
                                    "file": str(target),
                                    "expectedSha256": expected,
                                    "operations": [
                                        {
                                            "op": "edit",
                                            "old": "before",
                                            "new": "after",
                                            "expected_count": 1,
                                        }
                                    ],
                                }
                            ],
                        },
                    },
                }
            )
            preview_payload = preview["result"]["structuredContent"]
            self.assertFalse(preview_payload["written"])
            transaction_id = preview_payload["transactionId"]
            server_instance_id = preview_payload["serverInstanceId"]
            self.assertTrue(
                transaction_id.startswith(f"tx_{server_instance_id}_")
            )

            confirmed = exchange(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "safe_edit_transaction",
                        "arguments": {"transactionId": transaction_id},
                    },
                }
            )
            confirmed_payload = confirmed["result"]["structuredContent"]
            self.assertFalse(confirmed["result"].get("isError", False))
            self.assertTrue(confirmed_payload["written"])
            self.assertTrue(confirmed_payload["confirmed"])
            self.assertEqual(
                confirmed_payload["serverInstanceId"], server_instance_id
            )
            self.assertEqual(target.read_bytes(), b"after\n")
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            stderr_output = process.stderr.read()
            process.stdout.close()
            process.stderr.close()

        self.assertEqual(process.returncode, 0, stderr_output)

    def test_initialize_negotiates_supported_protocol_version(self):
        supported = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": _initialize_params("2024-11-05"),
            }
        )
        unsupported = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": _initialize_params("2099-01-01"),
            }
        )
        retired = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "initialize",
                "params": _initialize_params("2025-03-26"),
            }
        )

        self.assertEqual(
            supported["result"]["protocolVersion"], "2024-11-05"
        )
        self.assertEqual(
            unsupported["result"]["protocolVersion"], "2025-11-25"
        )
        self.assertEqual(
            retired["result"]["protocolVersion"], "2025-11-25"
        )
        self.assertNotIn(
            "2025-03-26", implementation.SUPPORTED_PROTOCOL_VERSIONS
        )

    def test_initialize_requires_protocol_capabilities_and_client_info(self):
        invalid_params = (
            {},
            {
                "protocolVersion": "",
                "capabilities": {},
                "clientInfo": {"name": "client", "version": "1.0"},
            },
            {
                "protocolVersion": "2025-11-25",
                "capabilities": [],
                "clientInfo": {"name": "client", "version": "1.0"},
            },
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": [],
            },
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "", "version": "1.0"},
            },
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "client", "version": 1},
            },
        )
        for params in invalid_params:
            with self.subTest(params=params):
                response = server.handle_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "initialize",
                        "params": params,
                    }
                )
                self.assertEqual(response["error"]["code"], -32602)

    def test_non_object_params_return_json_rpc_error(self):
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": [],
            }
        )
        self.assertEqual(response["error"]["code"], -32602)

    def test_json_rpc_rejects_invalid_envelopes_and_ids(self):
        invalid_messages = (
            {"id": 1, "method": "ping"},
            {"jsonrpc": "1.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 1, "method": 7},
            {"method": "ping"},
            {"jsonrpc": "1.0", "method": "ping"},
            {"jsonrpc": "2.0", "method": 7},
            {"jsonrpc": "2.0", "id": True, "method": "ping"},
            {"jsonrpc": "2.0", "id": None, "method": "ping"},
            {"jsonrpc": "2.0", "id": 1.5, "method": "ping"},
        )
        for message in invalid_messages:
            with self.subTest(message=message):
                response = server.handle_message(message)
                self.assertEqual(response["error"]["code"], -32600)
                self.assertIsNone(response["id"])

    def test_notifications_never_execute_request_methods_or_respond(self):
        notifications = (
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "safe_edit_preflight",
                    "arguments": {},
                },
            },
            {"jsonrpc": "2.0", "method": "ping"},
            {"jsonrpc": "2.0", "method": "tools/call", "params": []},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "method": "unknown/notification"},
        )
        with patch.object(implementation, "execute_tool") as execute_mock:
            for message in notifications:
                with self.subTest(message=message):
                    self.assertIsNone(server.handle_message(message))
        execute_mock.assert_not_called()

    def test_unknown_tool_and_malformed_calls_are_invalid_params(self):
        calls = (
            {
                "name": "does_not_exist",
                "arguments": {},
            },
            {"name": 7, "arguments": {}},
            {"name": "safe_edit_stat", "arguments": []},
            {"name": "safe_edit_stat", "arguments": None},
            {"name": "safe_edit_stat", "arguments": "files"},
        )
        for params in calls:
            with self.subTest(params=params):
                response = server.handle_message(
                    {
                        "jsonrpc": "2.0",
                        "id": "request-id",
                        "method": "tools/call",
                        "params": params,
                    }
                )
                self.assertEqual(response["error"]["code"], -32602)
                self.assertEqual(response["id"], "request-id")

    def test_safe_edit_error_preserves_core_write_state_in_tool_result(self):
        error = implementation.core.SafeEditError("commit failed")
        core_payload = {
            "ok": False,
            "command": "transaction",
            "error": {
                "type": "rollback_error",
                "message": "commit failed",
                "reason": "rollback_error",
            },
            "failureReason": "rollback_error",
            "written": True,
            "rolledBack": False,
            "partialWrite": True,
            "rollbackConflict": True,
        }
        with patch.object(
            implementation, "execute_tool", side_effect=error
        ), patch.object(
            implementation.core,
            "build_error_payload",
            return_value=core_payload,
        ) as build_payload:
            response = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {
                        "name": "safe_edit_transaction",
                        "arguments": {},
                    },
                }
            )

        self.assertNotIn("error", response)
        result = response["result"]
        self.assertTrue(result["isError"])
        payload = result["structuredContent"]
        self.assertEqual(
            json.loads(result["content"][0]["text"]), payload
        )
        self.assertTrue(payload["written"])
        self.assertFalse(payload["rolledBack"])
        self.assertTrue(payload["partialWrite"])
        self.assertTrue(payload["rollbackConflict"])
        self.assertEqual(payload["transport"], "mcp-structured")
        build_payload.assert_called_once_with(
            error, command="safe_edit_transaction"
        )

    def test_validation_tool_failure_remains_non_written_error(self):
        result = implementation._tool_failure(
            ValueError("invalid input"), "safe_edit_stat"
        )

        self.assertTrue(result["isError"])
        payload = result["structuredContent"]
        self.assertEqual(
            json.loads(result["content"][0]["text"]), payload
        )
        self.assertFalse(payload["written"])
        self.assertEqual(payload["failureReason"], "validation_error")
        self.assertEqual(payload["transport"], "mcp-structured")

    def test_large_tool_failure_uses_bounded_compatibility_text(self):
        marker = "failure-marker-"
        error = ToolExecutionError(
            marker + "x" * implementation.MAX_COMPAT_TEXT_BYTES
        )
        result = implementation._tool_failure(error, "safe_edit_transaction")
        compatibility = json.loads(result["content"][0]["text"])

        self.assertTrue(result["isError"])
        self.assertTrue(compatibility["truncated"])
        self.assertEqual(
            compatibility["compatibilityTextLimitBytes"],
            implementation.MAX_COMPAT_TEXT_BYTES,
        )
        self.assertNotIn(marker, result["content"][0]["text"])
        self.assertIn(marker, result["structuredContent"]["error"]["message"])
        self.assertLess(len(result["content"][0]["text"]), 1024)

    def test_unexpected_tool_exception_is_diagnostic_tool_failure(self):
        stderr = io.StringIO()
        with patch.object(
            implementation,
            "execute_tool",
            side_effect=RuntimeError("sensitive detail"),
        ), patch.object(implementation.sys, "stderr", stderr):
            response = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {
                        "name": "safe_edit_preflight",
                        "arguments": {},
                    },
                }
            )

        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])
        payload = response["result"]["structuredContent"]
        self.assertEqual(payload["failureReason"], "internal_execution_error")
        self.assertEqual(payload["failureStage"], "preflight_execution")
        self.assertEqual(payload["exception"]["type"], "RuntimeError")
        self.assertFalse(payload["writeAttempted"])
        self.assertFalse(payload["outcomeUncertain"])
        self.assertFalse(payload["statRequired"])
        self.assertEqual(
            payload["serverInstanceId"], implementation.SERVER_INSTANCE_ID
        )
        self.assertNotIn("sensitive detail", json.dumps(payload))
        log_event = json.loads(stderr.getvalue())
        self.assertEqual(log_event["incidentId"], payload["incidentId"])
        self.assertEqual(log_event["exception"]["type"], "RuntimeError")
        self.assertNotIn("sensitive detail", stderr.getvalue())

    def test_result_serialization_failure_keeps_write_state(self):
        stderr = io.StringIO()
        summary = {
            "ok": True,
            "command": "preflight",
            "written": False,
        }
        with patch.object(
            implementation, "execute_tool", return_value=summary
        ), patch.object(
            implementation,
            "_tool_result",
            side_effect=RuntimeError("sensitive result detail"),
        ), patch.object(implementation.sys, "stderr", stderr):
            response = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 91,
                    "method": "tools/call",
                    "params": {
                        "name": "safe_edit_preflight",
                        "arguments": {},
                    },
                }
            )

        payload = response["result"]["structuredContent"]
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(payload["failureStage"], "result_serialization")
        self.assertFalse(payload["writeAttempted"])
        self.assertFalse(payload["written"])
        self.assertNotIn("sensitive result detail", json.dumps(payload))
        self.assertNotIn("sensitive result detail", stderr.getvalue())

    def test_internal_os_error_reports_safe_metadata_without_filename(self):
        stderr = io.StringIO()
        error = PermissionError(13, "permission denied", "sensitive-name.txt")
        with patch.object(
            implementation,
            "execute_tool",
            side_effect=error,
        ), patch.object(implementation.sys, "stderr", stderr):
            response = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 92,
                    "method": "tools/call",
                    "params": {
                        "name": "safe_edit_stat",
                        "arguments": {"files": ["target.txt"]},
                    },
                }
            )

        payload = response["result"]["structuredContent"]
        self.assertEqual(payload["exception"]["type"], "PermissionError")
        self.assertEqual(payload["exception"]["errno"], 13)
        self.assertEqual(payload["exception"]["strerror"], "permission denied")
        self.assertNotIn("sensitive-name.txt", json.dumps(payload))
        log_event = json.loads(stderr.getvalue())
        self.assertEqual(log_event["exception"], payload["exception"])
        self.assertNotIn("sensitive-name.txt", stderr.getvalue())

    def test_valid_request_execution_state_error_is_tool_result(self):
        with patch.object(
            implementation,
            "execute_tool",
            side_effect=ToolExecutionError("pending cache is full"),
        ):
            response = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {
                        "name": "safe_edit_transaction",
                        "arguments": {
                            "files": [
                                {
                                    "file": "new.txt",
                                    "text": "content",
                                    "encoding": "utf-8",
                                    "lineEnding": "lf",
                                }
                            ]
                        },
                    },
                }
            )

        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])
        payload = response["result"]["structuredContent"]
        self.assertEqual(payload["failureReason"], "execution_error")
        self.assertFalse(payload["written"])

    def test_success_content_text_contains_structured_json(self):
        summary = {
            "ok": True,
            "command": "preflight",
            "dryRun": True,
            "written": False,
            "message": "兼容",
        }
        with patch.object(
            implementation, "execute_tool", return_value=summary
        ):
            response = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {
                        "name": "safe_edit_preflight",
                        "arguments": {},
                    },
                }
            )

        result = response["result"]
        self.assertEqual(
            json.loads(result["content"][0]["text"]),
            result["structuredContent"],
        )

    def test_json_string_size_fast_path_matches_json_encoding(self):
        values = (
            "",
            "plain ASCII metadata",
            'quote: "',
            "backslash: \\",
            "controls: \b\f\n\r\t",
            "boundary: \x00\x1f\x20\x7f",
            "兼容",
            "emoji: \U0001f680",
            "surrogate: \ud800",
        )
        for value in values:
            with self.subTest(value=repr(value)):
                expected = len(
                    json.dumps(
                        value,
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ).encode("ascii")
                )
                self.assertEqual(
                    implementation._json_string_size_with_limit(
                        value, expected
                    ),
                    expected,
                )
                self.assertIsNone(
                    implementation._json_string_size_with_limit(
                        value, expected - 1
                    )
                )

    def test_json_string_size_plain_ascii_fast_path_does_not_iterate(self):
        class NonIterableAscii(str):
            def __iter__(self):
                raise AssertionError("plain ASCII fast path iterated")

        value = NonIterableAscii("serverInstanceId-0123456789abcdef")
        self.assertEqual(
            implementation._json_string_size_with_limit(value, 100),
            len(value) + 2,
        )

    def test_large_structured_result_uses_bounded_compatibility_text(self):
        marker = "large-diff-marker-"
        summary = {
            "ok": True,
            "command": "transaction",
            "fileCount": 1,
            "written": False,
            "files": [
                {
                    "file": "large.txt",
                    "diff": marker
                    + ("x" * implementation.MAX_COMPAT_TEXT_BYTES),
                }
            ],
        }

        with patch.object(
            implementation,
            "_json_bytes",
            wraps=implementation._json_bytes,
        ) as json_bytes:
            result = implementation._tool_result(summary)
        compatibility = json.loads(result["content"][0]["text"])

        self.assertFalse(
            any(call.args and call.args[0] is summary for call in json_bytes.call_args_list)
        )
        self.assertIs(result["structuredContent"], summary)
        self.assertTrue(compatibility["truncated"])
        self.assertEqual(compatibility["ok"], True)
        self.assertEqual(compatibility["command"], "transaction")
        self.assertEqual(compatibility["fileCount"], 1)
        self.assertEqual(compatibility["written"], False)
        self.assertEqual(
            compatibility["compatibilityTextLimitBytes"],
            implementation.MAX_COMPAT_TEXT_BYTES,
        )
        self.assertNotIn(marker, result["content"][0]["text"])
        self.assertLess(len(result["content"][0]["text"]), 1024)

    def test_repository_launcher_reports_package_version(self):
        completed = subprocess.run(
            [sys.executable, str(SERVER_PATH), "--version"],
            text=True,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        self.assertEqual(completed.stdout.strip(), "1.2.1")

    def test_benchmark_validates_every_timed_result(self):
        benchmark_path = REPO_ROOT / "tests" / "perf" / "benchmark.py"
        spec = importlib.util.spec_from_file_location(
            "safe_edit_benchmark_test", benchmark_path
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        benchmark = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(benchmark)
        outputs = iter([1, 2, 3])
        validated = []

        result = benchmark.measure(
            "validation-test",
            lambda: next(outputs),
            input_bytes=0,
            iterations=3,
            warmups=0,
            validate=validated.append,
            trace_memory=False,
        )

        self.assertEqual(validated, [1, 2, 3])
        self.assertEqual(result["iterations"], 3)

    def test_benchmark_confirm_does_not_reprepare_across_iterations(self):
        benchmark = REPO_ROOT / "tests" / "perf" / "benchmark.py"
        completed = subprocess.run(
            [
                sys.executable,
                str(benchmark),
                "--sizes-mib",
                "0.004",
                "--iterations",
                "2",
                "--warmups",
                "1",
                "--context-matches",
                "10",
                "--batch-operations",
                "4",
                "--json",
            ],
            cwd=self.root,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=60,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        confirm = next(
            item
            for item in payload["results"]
            if item["name"] == "core.transaction-confirm-revalidate"
        )
        self.assertEqual(confirm["iterations"], 2)
        self.assertFalse(confirm["replanned"])


if __name__ == "__main__":
    unittest.main()
