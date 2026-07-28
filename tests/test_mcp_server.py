import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
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


class SafeEditMcpTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(
            prefix="safe-edit-mcp-test-"
        )
        self.root = Path(self.tempdir.name)
        server.execute_tool.__globals__[
            "_PENDING_TRANSACTIONS"
        ].clear()

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
            ToolInputError, "unknown or expired transactionId"
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

    def test_stdio_initialize_and_tool_discovery(self):
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
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

    def test_initialize_negotiates_supported_protocol_version(self):
        supported = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        )
        unsupported = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {"protocolVersion": "2099-01-01"},
            }
        )

        self.assertEqual(
            supported["result"]["protocolVersion"], "2024-11-05"
        )
        self.assertEqual(
            unsupported["result"]["protocolVersion"], "2025-11-25"
        )

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

    def test_repository_launcher_reports_package_version(self):
        completed = subprocess.run(
            [sys.executable, str(SERVER_PATH), "--version"],
            text=True,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        self.assertEqual(completed.stdout.strip(), "1.2.0")


if __name__ == "__main__":
    unittest.main()
