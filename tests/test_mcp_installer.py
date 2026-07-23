import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "skills" / "safe-edit"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from safe_edit_mcp import installer


class SafeEditMcpInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(
            prefix="safe-edit-mcp-installer-test-"
        )
        self.root = Path(self.tempdir.name)
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.home.mkdir()
        self.project.mkdir()
        self.invocation = installer.Invocation("safe-edit-mcp")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_project_install_writes_all_client_shapes(self):
        cursor_config = self.project / ".cursor" / "mcp.json"
        cursor_config.parent.mkdir()
        cursor_config.write_text(
            json.dumps({"mcpServers": {"other": {"command": "other"}}}),
            encoding="utf-8",
        )

        summary = installer.install_configs(
            clients=["all"],
            scope="project",
            home=self.home,
            project_dir=self.project,
            invocation=self.invocation,
        )

        self.assertTrue(summary["ok"])
        claude = json.loads(
            (self.project / ".mcp.json").read_text(encoding="utf-8")
        )
        cursor = json.loads(cursor_config.read_text(encoding="utf-8"))
        opencode = json.loads(
            (self.project / "opencode.json").read_text(encoding="utf-8")
        )
        vscode = json.loads(
            (self.project / ".vscode" / "mcp.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(
            claude["mcpServers"]["safe-edit"]["command"],
            "safe-edit-mcp",
        )
        self.assertIn("other", cursor["mcpServers"])
        self.assertEqual(
            opencode["mcp"]["safe-edit"]["command"],
            ["safe-edit-mcp"],
        )
        self.assertEqual(
            vscode["servers"]["safe-edit"]["type"], "stdio"
        )

    def test_user_install_uses_documented_locations_and_code_cli(self):
        with patch.object(installer.subprocess, "run") as run_mock:
            summary = installer.install_configs(
                clients=["all"],
                scope="user",
                home=self.home,
                project_dir=self.project,
                invocation=self.invocation,
                code_cli="code-test",
            )

        self.assertTrue((self.home / ".claude.json").is_file())
        self.assertTrue((self.home / ".cursor" / "mcp.json").is_file())
        self.assertTrue(
            (
                self.home
                / ".config"
                / "opencode"
                / "opencode.json"
            ).is_file()
        )
        run_mock.assert_called_once()
        argv = run_mock.call_args.args[0]
        self.assertEqual(argv[:2], ["code-test", "--add-mcp"])
        payload = json.loads(argv[2])
        self.assertEqual(payload["name"], "safe-edit")
        self.assertEqual(len(summary["results"]), 4)

    def test_dry_run_does_not_write(self):
        summary = installer.install_configs(
            clients=["cursor", "opencode"],
            scope="project",
            home=self.home,
            project_dir=self.project,
            invocation=self.invocation,
            dry_run=True,
        )

        self.assertFalse((self.project / ".cursor").exists())
        self.assertFalse((self.project / "opencode.json").exists())
        self.assertTrue(all(item["changed"] for item in summary["results"]))

    def test_invalid_json_prevents_all_file_writes(self):
        cursor_config = self.project / ".cursor" / "mcp.json"
        cursor_config.parent.mkdir()
        cursor_config.write_text("{ // jsonc\n}", encoding="utf-8")

        with self.assertRaises(installer.InstallError):
            installer.install_configs(
                clients=["claude-code", "cursor"],
                scope="project",
                home=self.home,
                project_dir=self.project,
                invocation=self.invocation,
            )

        self.assertFalse((self.project / ".mcp.json").exists())
        self.assertEqual(
            cursor_config.read_text(encoding="utf-8"), "{ // jsonc\n}"
        )

    def test_existing_config_gets_recoverable_backup(self):
        target = self.project / ".mcp.json"
        original = b'{"mcpServers":{"other":{"command":"x"}}}\n'
        target.write_bytes(original)

        summary = installer.install_configs(
            clients=["claude-code"],
            scope="project",
            home=self.home,
            project_dir=self.project,
            invocation=self.invocation,
        )

        backup = Path(summary["results"][0]["backup"])
        self.assertEqual(backup.read_bytes(), original)
        updated = json.loads(target.read_text(encoding="utf-8"))
        self.assertIn("other", updated["mcpServers"])
        self.assertIn("safe-edit", updated["mcpServers"])

    def test_write_failure_rolls_back_completed_file_actions(self):
        real_write = installer._write_atomic

        def fail_on_cursor(action):
            if action.client == "cursor":
                raise OSError("simulated write failure")
            return real_write(action)

        with patch.object(
            installer, "_write_atomic", side_effect=fail_on_cursor
        ), self.assertRaises(installer.InstallError):
            installer.install_configs(
                clients=["claude-code", "cursor"],
                scope="project",
                home=self.home,
                project_dir=self.project,
                invocation=self.invocation,
            )

        self.assertFalse((self.project / ".mcp.json").exists())


if __name__ == "__main__":
    unittest.main()
