import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "safe-edit"
SKILL = SKILL_DIR / "SKILL.md"
README = REPO_ROOT / "README.md"


class DocumentationTests(unittest.TestCase):
    def test_skill_body_is_under_500_lines(self):
        text = SKILL.read_text(encoding="utf-8")
        body = text.split("---", 2)[2]
        self.assertLess(len(body.splitlines()), 500)

    def test_reference_files_exist_and_are_linked(self):
        text = SKILL.read_text(encoding="utf-8")
        links = set(re.findall(r"`(references/[^`]+\.md)`", text))
        self.assertEqual(
            links,
            {
                "references/protocol.md",
                "references/recovery.md",
                "references/windows.md",
                "references/git-conflicts.md",
            },
        )
        for link in links:
            self.assertTrue((SKILL_DIR / link).is_file(), link)

    def test_skill_keeps_hard_rules(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("Do NOT infer, generate, or invent", text)
        self.assertIn("## Allowed Commands", text)
        self.assertIn("SAFE_EDIT_SCRIPT", text)
        self.assertIn("sibling file `safe_edit.py`", text)
        self.assertIn("STOP", text)
        self.assertIn("serverInstanceId", text)
        self.assertIn("transactionIdConsumed", text)

    def test_skill_documents_batching_contract(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("## Batching and Confirmation Rules", text)
        self.assertIn("same dry-run or preview twice", text)
        self.assertIn("diffBudget", text)
        self.assertIn("Simple single-file edit", text)

    def test_readme_documents_new_workflow(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("doctor", text)
        self.assertIn("kimi-code", text)
        self.assertIn("~/.kimi-code/mcp.json", text)
        self.assertIn("32,000", text)
        self.assertIn("diffBudget", text)
        self.assertIn("同一请求不重复 preview", text)
        self.assertIn('{"transactionId":"..."}', text)

    def test_git_conflict_checklist_has_semantic_steps(self):
        text = (SKILL_DIR / "references" / "git-conflicts.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("stage 2", text)
        self.assertIn("complete function", text)
        self.assertIn("callers", text)
        self.assertIn("no markers", text)
        self.assertIn("rollbackConflict", text)

    def test_manual_compact_hunk_no_longer_forces_truncation(self):
        text = (SKILL_DIR / "safe_edit.py").read_text(encoding="utf-8")
        self.assertNotIn("        True\n        or prefix_truncated", text)


if __name__ == "__main__":
    unittest.main()
