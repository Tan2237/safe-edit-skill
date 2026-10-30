import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "safe-edit"
sys.path.insert(0, str(SKILL_DIR))

import safe_edit as se  # noqa: E402


def summary(mode="compact", lines=120):
    return {
        "diffMode": mode,
        "diff": "\n".join(f"+ line {index}" for index in range(lines)),
        "diffTruncated": False,
    }


class TransactionDiffBudgetTests(unittest.TestCase):
    def test_compact_budget_caps_aggregate(self):
        files = [summary() for _ in range(6)]
        budget = se._apply_transaction_diff_budget(files)
        self.assertTrue(budget["truncated"])
        self.assertLessEqual(budget["compactLinesUsed"], 240)
        self.assertLessEqual(budget["compactCharsUsed"], 32_000)
        self.assertEqual(sum(1 for item in files if item.get("diff")), 6)
        self.assertTrue(all(item["diffTruncatedBy"] == "transaction" for item in files))

    def test_budget_can_omit_exhausted_files(self):
        files = [summary(lines=5) for _ in range(3)]
        with mock.patch.object(se, "_TRANSACTION_DIFF_MAX_LINES", 2), \
             mock.patch.object(se, "_TRANSACTION_DIFF_MAX_CHARS", 20), \
             mock.patch.object(se, "_TRANSACTION_DIFF_MIN_FILE_LINES", 1), \
             mock.patch.object(se, "_TRANSACTION_DIFF_MIN_FILE_CHARS", 1):
            budget = se._apply_transaction_diff_budget(files)
        self.assertTrue(budget["omittedFiles"])
        self.assertTrue(any("diff" not in item for item in files))
        self.assertTrue(any(item.get("diffOmitted") for item in files))

    def test_full_diffs_are_exempt(self):
        files = [summary(mode="full") for _ in range(2)]
        budget = se._apply_transaction_diff_budget(files)
        self.assertEqual(budget["fullDiffFiles"], [1, 2])
        self.assertFalse(budget["truncated"])
        expected_size = len(summary()["diff"])
        self.assertEqual([len(item["diff"]) for item in files], [expected_size] * 2)

    def test_no_diffs_reports_empty_budget(self):
        budget = se._apply_transaction_diff_budget([{}, {"diffTruncated": False}])
        self.assertEqual(budget["filesWithDiff"], 0)
        self.assertEqual(budget["compactLinesUsed"], 0)
        self.assertFalse(budget["truncated"])

    def test_budget_is_deterministic(self):
        first = [summary(lines=50) for _ in range(4)]
        second = [summary(lines=50) for _ in range(4)]
        self.assertEqual(
            se._apply_transaction_diff_budget(first),
            se._apply_transaction_diff_budget(second),
        )
        self.assertEqual(first, second)

    def test_manual_compact_hunk_is_not_always_truncated(self):
        before = "\n".join(f"line {index}" for index in range(600)) + "\n"
        after = before.replace("line 300", "line 300 changed")
        text, truncated = se.generate_compact_diff(
            Path("x.txt"), before, after, 3
        )
        self.assertFalse(truncated)
        self.assertIn("+line 300 changed", text)

    def test_limiter_still_reports_real_truncation(self):
        lines = [f"+ line {index}" for index in range(200)]
        text, truncated = se._limit_diff_lines(lines, 100, 50)
        self.assertTrue(truncated)
        self.assertIn("[compact diff truncated]", text)

    def test_transaction_limiter_keeps_partial_nonfirst_line(self):
        text = "short\n" + "y" * 200 + "\ntail"
        bounded, cut = se._bound_transaction_diff(text, 80, 200)
        self.assertTrue(cut)
        self.assertTrue(bounded.startswith("short\n"))
        self.assertIn("y" * 154, bounded)
        self.assertNotIn("tail", bounded)
        self.assertTrue(
            bounded.endswith("... [transaction diff budget exhausted]")
        )
        self.assertEqual(len(bounded), 200)


if __name__ == "__main__":
    unittest.main()
