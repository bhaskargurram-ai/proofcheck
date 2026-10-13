import unittest

import _helpers  # noqa: F401

from proofcheck.git import ChangedFile
from proofcheck.runners import get_runner
from proofcheck.scope import analyze_scope, tokens


def kinds(report):
    return {f.kind: f for f in report.flags}


class ScopeTest(unittest.TestCase):
    def setUp(self):
        self.is_test = get_runner("pytest").is_test_file

    def test_sensitive_files_are_flagged(self):
        files = [
            ChangedFile(".github/workflows/ci.yml", "M", additions=3),
            ChangedFile("requirements-dev.txt", "M", additions=1),
            ChangedFile("frontend/package-lock.json", "M", additions=1),
            ChangedFile("pyproject.toml", "M", additions=1),
            ChangedFile("assets/logo.png", "A", binary=True),
            ChangedFile("src/parser.py", "M", additions=5),
        ]
        k = kinds(analyze_scope(files, "Fix parser", self.is_test))
        self.assertEqual(k["workflow"].paths, [".github/workflows/ci.yml"])
        self.assertEqual(set(k["dependency"].paths), {"requirements-dev.txt", "frontend/package-lock.json", "pyproject.toml"})
        self.assertEqual(k["binary"].paths, ["assets/logo.png"])
        self.assertNotIn("large_diff", k)

    def test_large_diff(self):
        files = [ChangedFile("a.py", "M", additions=600, deletions=500)]
        report = analyze_scope(files, "", self.is_test, large_diff_threshold=1000)
        self.assertIn("large_diff", kinds(report))
        self.assertEqual(report.total_changed, 1100)

    def test_unmentioned_heuristic(self):
        files = [
            ChangedFile("src/pkg/parser.py", "M", additions=4),
            ChangedFile("src/pkg/telemetry_upload.py", "A", additions=40),
            ChangedFile("tests/test_parser.py", "M", additions=4),
        ]
        k = kinds(analyze_scope(files, "Fix parsing of empty input", self.is_test))
        self.assertTrue(k["unmentioned"].heuristic)
        self.assertEqual(k["unmentioned"].paths, ["src/pkg/telemetry_upload.py"])

    def test_unmentioned_not_flagged_when_nothing_is_mentioned(self):
        files = [ChangedFile("a/x.py", "M"), ChangedFile("b/y.py", "M")]
        self.assertNotIn("unmentioned", kinds(analyze_scope(files, "Small cleanup", self.is_test)))

    def test_tokens(self):
        t = tokens("parseHTTPResponse snake_case_name parsing")
        self.assertTrue({"parse", "http", "response", "snake", "case", "name"} <= t)


if __name__ == "__main__":
    unittest.main()
