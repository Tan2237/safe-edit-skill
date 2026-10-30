import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "safe-edit"
sys.path.insert(0, str(SKILL_DIR))

import safe_edit_mcp  # noqa: E402
from safe_edit_mcp import server  # noqa: E402


class ReleaseMetadataTests(unittest.TestCase):
    def test_pyproject_uses_dynamic_version(self):
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('dynamic = ["version"]', text)
        self.assertIn("[tool.setuptools.dynamic]", text)
        self.assertIn(
            'version = {attr = "safe_edit_mcp.__version__"}', text
        )
        self.assertNotIn('version = "', text)

    def test_versions_are_consistent(self):
        plugin = json.loads(
            (REPO_ROOT / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(plugin["version"], safe_edit_mcp.__version__)
        self.assertEqual(server.SERVER_VERSION, safe_edit_mcp.__version__)

    def test_version_is_semver(self):
        self.assertRegex(safe_edit_mcp.__version__, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
