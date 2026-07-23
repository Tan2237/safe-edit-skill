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


class SafeEditMcpTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(
            prefix="safe-edit-mcp-test-"
        )
        self.root = Path(self.tempdir.name)

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
        self.assertEqual(completed.stdout.strip(), "1.1.0")


if __name__ == "__main__":
    unittest.main()
