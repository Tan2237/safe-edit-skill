import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "safe-edit"
sys.path.insert(0, str(SKILL_DIR))

from safe_edit_mcp import doctor  # noqa: E402


def run_doctor(arguments):
    stream = io.StringIO()
    with redirect_stdout(stream):
        code = doctor.main(arguments)
    return code, json.loads(stream.getvalue())


class DoctorTests(unittest.TestCase):
    def test_core_only_reports_current_hash(self):
        code, report = run_doctor(["--core-sha256", "--json"])
        self.assertEqual(code, 0)
        expected = doctor._sha256(SKILL_DIR / "safe_edit.py")
        self.assertEqual(report["core"]["sha256"], expected)
        self.assertEqual(report["version"], "1.3.0")

    def test_clean_environment_without_handshake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, report = run_doctor(
                [
                    "--json",
                    "--no-handshake",
                    "--home",
                    str(root),
                    "--project-dir",
                    str(root),
                ]
            )
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["drift"], [])

    def test_expected_core_mismatch_is_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, report = run_doctor(
                [
                    "--json",
                    "--no-handshake",
                    "--home",
                    str(root),
                    "--project-dir",
                    str(root),
                    "--expect-core-sha256",
                    "0" * 64,
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn(
            "core_sha256_mismatch",
            [item["code"] for item in report["drift"]],
        )

    def test_stale_skill_copy_is_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / ".agents" / "skills" / "safe-edit"
            (skill / "safe_edit_mcp").mkdir(parents=True)
            (skill / "safe_edit.py").write_text("stale\n", encoding="utf-8")
            (skill / "safe_edit_mcp" / "__init__.py").write_text(
                '__version__ = "1.0.0"\n', encoding="utf-8"
            )
            code, report = run_doctor(
                [
                    "--json",
                    "--no-handshake",
                    "--home",
                    str(root),
                    "--project-dir",
                    str(root),
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn(
            "skill_core_mismatch",
            [item["code"] for item in report["drift"]],
        )

    def test_stale_skill_version_or_docs_is_drift(self):
        cases = (
            ("version", "skill_version_mismatch"),
            ("docs", "skill_docs_mismatch"),
        )
        for kind, expected_drift in cases:
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    skill = root / ".agents" / "skills" / "safe-edit"
                    shutil.copytree(
                        SKILL_DIR,
                        skill,
                        ignore=shutil.ignore_patterns(
                            "__pycache__", "*.egg-info"
                        ),
                    )
                    if kind == "version":
                        (skill / "safe_edit_mcp" / "__init__.py").write_text(
                            '__version__ = "0.0.0"\n', encoding="utf-8"
                        )
                    else:
                        with (skill / "SKILL.md").open(
                            "a", encoding="utf-8"
                        ) as stream:
                            stream.write("stale documentation\n")
                    code, report = run_doctor(
                        [
                            "--json",
                            "--no-handshake",
                            "--home",
                            str(root),
                            "--project-dir",
                            str(root),
                        ]
                    )

                self.assertEqual(code, 1)
                self.assertIn(
                    expected_drift,
                    [item["code"] for item in report["drift"]],
                )
                copied = next(
                    item
                    for item in report["skillCopies"]
                    if item["path"] == str(skill)
                )
                self.assertFalse(copied["matchesCurrent"])

    def test_kimi_user_config_path_is_scanned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".kimi-code" / "mcp.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "safe-edit": {
                                "command": sys.executable,
                                "args": ["--version"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            code, report = run_doctor(
                [
                    "--json",
                    "--no-handshake",
                    "--home",
                    str(root),
                    "--project-dir",
                    str(root),
                    "--client",
                    "kimi-code",
                    "--scope",
                    "user",
                ]
            )
        self.assertEqual(code, 0)
        configs = [item for item in report["configs"] if item["exists"]]
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["section"], "mcpServers")
        self.assertEqual(configs[0]["clients"], ["kimi-code"])
        self.assertTrue(configs[0]["entryPresent"])

    def test_missing_configured_script_or_module_is_drift(self):
        cases = (
            (["missing_launcher.py"], "config_launcher_missing"),
            (
                ["-m", "definitely_missing.safe_edit_module"],
                "config_module_missing",
            ),
        )
        for args, expected_drift in cases:
            with self.subTest(expected_drift=expected_drift):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    path = root / ".kimi-code" / "mcp.json"
                    path.parent.mkdir(parents=True)
                    path.write_text(
                        json.dumps(
                            {
                                "mcpServers": {
                                    "safe-edit": {
                                        "command": sys.executable,
                                        "args": args,
                                    }
                                }
                            }
                        ),
                        encoding="utf-8",
                    )
                    code, report = run_doctor(
                        [
                            "--json",
                            "--no-handshake",
                            "--home",
                            str(root),
                            "--project-dir",
                            str(root),
                            "--client",
                            "kimi-code",
                            "--scope",
                            "user",
                        ]
                    )

                self.assertEqual(code, 1)
                self.assertIn(
                    expected_drift,
                    [item["code"] for item in report["drift"]],
                )
                config = next(
                    item for item in report["configs"] if item["exists"]
                )
                self.assertEqual(config["status"], "invalid")
                self.assertFalse(config["launcherExists"])

    def test_invalid_json_config_is_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".kimi-code" / "mcp.json"
            path.parent.mkdir(parents=True)
            path.write_text("{not json", encoding="utf-8")
            code, report = run_doctor(
                [
                    "--json",
                    "--no-handshake",
                    "--home",
                    str(root),
                    "--project-dir",
                    str(root),
                    "--client",
                    "kimi-code",
                    "--scope",
                    "user",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn(
            "config_invalid", [item["code"] for item in report["drift"]]
        )

    def test_live_handshake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, report = run_doctor(
                [
                    "--json",
                    "--home",
                    str(root),
                    "--project-dir",
                    str(root),
                    "--timeout",
                    "15",
                ]
            )
        self.assertEqual(code, 0)
        self.assertTrue(report["handshake"]["ok"])
        self.assertEqual(report["handshake"]["serverName"], "safe-edit")
        self.assertEqual(
            sorted(report["handshake"]["tools"]),
            sorted(doctor.EXPECTED_TOOLS),
        )

    def test_server_module_dispatches_doctor(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SKILL_DIR)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "safe_edit_mcp.server",
                "doctor",
                "--core-sha256",
                "--json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=15,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["version"], "1.3.0")


if __name__ == "__main__":
    unittest.main()
