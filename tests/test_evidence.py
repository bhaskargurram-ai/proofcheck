import unittest

import _helpers  # noqa: F401

from proofcheck import claims as C
from proofcheck.benchmark import BenchmarkResult, judge_speedup
from proofcheck.evidence import CONTRADICTED, UNVERIFIED, VERIFIED, assess_claims, verdict
from proofcheck.failtopass import FAIL_ON_HEAD, FAIL_TO_PASS, PASS_TO_PASS, STATUS_NO_TESTS, TestCheck, TestResult
from proofcheck.repro import IssueRepro, SnippetResult, SnippetRun


def tests_with(*classes, old=None):
    t = TestCheck(status="evidence", runner="pytest", test_files=["tests/test_x.py"], test_modules=["tests/test_x.py"])
    t.tests = [TestResult(f"tests.test_x::t{i}", "tests/test_x.py", c, "x", "y") for i, c in enumerate(classes)]
    t.old_tests_on_head = old or {}
    return t


def claim(text):
    (c,) = C.extract_claims("", text)
    return c


def status(text, tests=None, repros=(), bench=None):
    (a,) = assess_claims([claim(text)], tests, list(repros), bench)
    return a.status


class EvidenceTest(unittest.TestCase):
    def test_fixes_issue(self):
        self.assertEqual(status("Fixes #1", tests_with(FAIL_TO_PASS)), VERIFIED)
        self.assertEqual(status("Fixes #1", tests_with(PASS_TO_PASS)), UNVERIFIED)
        self.assertEqual(status("Fixes #1", tests_with(FAIL_TO_PASS, FAIL_ON_HEAD)), CONTRADICTED)
        self.assertEqual(status("Fixes #1", TestCheck(status=STATUS_NO_TESTS)), UNVERIFIED)

    def test_fixes_issue_with_reproducer(self):
        fixed = IssueRepro("#1", "reproduced_change", [SnippetResult(1, "behavior_changed", SnippetRun(1, "", "Err"), SnippetRun(0, "ok", ""))])
        still = IssueRepro("#1", "unchanged", [SnippetResult(1, "unchanged", SnippetRun(1, "", "Err"), SnippetRun(1, "", "Err"))])
        other_issue = IssueRepro("#2", "reproduced_change", fixed.snippets)
        self.assertEqual(status("Fixes #1", TestCheck(status=STATUS_NO_TESTS), [fixed]), VERIFIED)
        self.assertEqual(status("Fixes #1", tests_with(PASS_TO_PASS), [still]), CONTRADICTED)
        self.assertEqual(status("Fixes #1", TestCheck(status=STATUS_NO_TESTS), [other_issue]), UNVERIFIED)

    def test_adds_tests(self):
        self.assertEqual(status("Adds tests", tests_with(PASS_TO_PASS)), VERIFIED)
        self.assertEqual(status("Adds tests", TestCheck(status=STATUS_NO_TESTS)), CONTRADICTED)

    def test_no_behavior_change(self):
        self.assertEqual(status("No functional change.", tests_with(FAIL_TO_PASS)), CONTRADICTED)
        self.assertEqual(status("No functional change.", tests_with(PASS_TO_PASS, old={"ran": True, "failing": ["a::b"]})), CONTRADICTED)
        self.assertEqual(status("No functional change.", tests_with(PASS_TO_PASS, old={"ran": True, "failing": [], "passed": 4})), UNVERIFIED)

    def test_backwards_compatible(self):
        self.assertEqual(status("Backwards compatible.", tests_with(old={"ran": True, "failing": ["a::b"]})), CONTRADICTED)
        self.assertEqual(status("Backwards compatible.", tests_with()), UNVERIFIED)

    def test_performance(self):
        self.assertEqual(status("3x faster"), UNVERIFIED)
        fast = BenchmarkResult(["b"], 3, [3.0, 3.1, 2.9], [1.0, 1.1, 0.9])
        slow = BenchmarkResult(["b"], 3, [1.0, 1.0, 1.0], [1.5, 1.5, 1.5])
        meh = BenchmarkResult(["b"], 3, [1.2, 1.2, 1.2], [1.0, 1.0, 1.0])
        self.assertEqual(status("3x faster", bench=fast), VERIFIED)
        self.assertEqual(status("3x faster", bench=slow), CONTRADICTED)
        self.assertEqual(status("3x faster", bench=meh), UNVERIFIED)
        self.assertEqual(status("reduces memory by 30%", bench=fast), UNVERIFIED)

    def test_judge_speedup_threshold(self):
        self.assertEqual(judge_speedup(3.0, 2.0)[0], "verified")  # exactly half the claimed improvement
        self.assertEqual(judge_speedup(3.0, 1.9)[0], "unverified")
        self.assertEqual(judge_speedup(1.4, 1.03)[0], "unverified")  # within noise
        self.assertEqual(judge_speedup(2.0, None)[0], "unverified")

    def test_verdict(self):
        c = [claim("Fixes #1")]
        self.assertEqual(verdict(assess_claims(c, tests_with(FAIL_TO_PASS), [], None), tests_with(FAIL_TO_PASS)), "evidence")
        self.assertEqual(verdict(assess_claims(c, tests_with(PASS_TO_PASS), [], None), tests_with(PASS_TO_PASS)), "no_evidence")
        self.assertEqual(verdict([], tests_with(FAIL_ON_HEAD)), "contradicted")
        both = [claim("Fixes #1"), claim("3x faster")]
        self.assertEqual(verdict(assess_claims(both, tests_with(FAIL_TO_PASS), [], None), tests_with(FAIL_TO_PASS)), "partial")


if __name__ == "__main__":
    unittest.main()
