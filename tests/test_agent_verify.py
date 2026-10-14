"""Candidate classification, failure-kind detection, claim upgrades and the result schema."""

import copy
import unittest

import _helpers  # noqa: F401  (sets up sys.path)

from proofcheck.agent import FAIL_TO_PASS, FAIL_TO_PASS_WEAK, FAILS_ON_HEAD, PASS_TO_PASS
from proofcheck.agent.schema import ManifestError, validate_candidate_path
from proofcheck.agent.verify import (
    CandidateOutcome,
    classify_candidate,
    exception_name,
    failure_kind,
    merge_into_result,
    overall_status,
    pick_winner,
    raise_location,
)
from proofcheck.junit import ERROR, FAILED, PASSED, TestOutcome, parse_junit_string
from proofcheck.result import ResultValidationError, validate_result
from proofcheck.runners import RunResult, UnittestRunner

PATH = "tests/proofcheck_agent/test_reproduce_issue.py"
MOD = "tests.proofcheck_agent.test_reproduce_issue"


def o(outcome, message="", detail=""):
    return TestOutcome("t", outcome, message, "", detail)


class FailureKindTest(unittest.TestCase):
    def test_pytest_messages(self):
        self.assertEqual(exception_name(o(FAILED, "assert 3 == 2\n +  where 3 = f()", "tests/x.py:3: AssertionError")), "AssertionError")
        self.assertEqual(exception_name(o(FAILED, "ValueError: bad input", "")), "ValueError")
        self.assertEqual(exception_name(o(FAILED, "pkg.errors.ParseError: x")), "ParseError")
        self.assertEqual(exception_name(o(FAILED, "Failed: DID NOT RAISE <class 'ValueError'>", "tests/x.py:7: Failed")), "Failed")
        coll = o(ERROR, "collection failure", "ImportError while importing test module '/w/tests/x.py'.\nE   ImportError: cannot import name 'normalize'")
        self.assertEqual(exception_name(coll), "ImportError")
        self.assertEqual(failure_kind(coll), "missing_symbol")

    def test_strong_vs_weak(self):
        self.assertEqual(failure_kind(o(FAILED, "AssertionError: 3 != 2")), "behavioural")
        self.assertEqual(failure_kind(o(ERROR, "KeyError: 'x'")), "behavioural")
        self.assertEqual(failure_kind(o(ERROR, "ModuleNotFoundError: No module named 'pkg.new'")), "missing_symbol")
        # AttributeError raised in the test itself: the name is new on base -> weak
        tb_test = 'Traceback (most recent call last):\n  File "/w/tests/proofcheck_agent/test_x.py", line 8, in test_a\n    calc.new()\nAttributeError: module \'calc\' has no attribute \'new\''
        self.assertEqual(failure_kind(o(ERROR, "AttributeError: module 'calc' has no attribute 'new'", tb_test), "tests/proofcheck_agent/test_x.py"), "missing_symbol")
        # ... raised inside the project's code: that is the bug -> strong
        tb_lib = tb_test.replace('  File "/w/tests/proofcheck_agent/test_x.py", line 8, in test_a\n    calc.new()\n',
                                 '  File "/w/tests/proofcheck_agent/test_x.py", line 8, in test_a\n  File "/w/calc.py", line 3, in run\n')
        self.assertEqual(raise_location(o(ERROR, "", tb_lib)), "/w/calc.py")
        self.assertEqual(failure_kind(o(ERROR, "AttributeError: 'Cart' object has no attribute 'items'", tb_lib), "tests/proofcheck_agent/test_x.py"), "behavioural")
        self.assertEqual(failure_kind(o(FAILED, "AttributeError: 'NoneType' object has no attribute 'x'", "tests/proofcheck_agent/test_x.py:4: AttributeError"),
                                      "tests/proofcheck_agent/test_x.py"), "behavioural")
        self.assertEqual(failure_kind(o(FAILED, "TypeError: f() got an unexpected keyword argument 'strict'", "tests/proofcheck_agent/test_x.py:5: TypeError"),
                                      "tests/proofcheck_agent/test_x.py"), "missing_symbol")
        self.assertEqual(failure_kind(o(FAILED, "TypeError: unsupported operand type(s)", "calc.py:5: TypeError"), "x/test_x.py"), "behavioural")

    def test_junit_detail_is_kept(self):
        xml = ('<testsuite><testcase classname="" name="tests.t"><error message="collection failure">'
               'E   ImportError: cannot import name x</error></testcase></testsuite>')
        out = parse_junit_string(xml)["tests.t"]
        self.assertEqual(out.message, "collection failure")
        self.assertIn("ImportError", out.detail)


def run(**outcomes):
    return RunResult(outcomes={tid: TestOutcome(tid, *v) if isinstance(v, tuple) else TestOutcome(tid, v) for tid, v in outcomes.items()})


class ClassifyCandidateTest(unittest.TestCase):
    runner = UnittestRunner()
    T = f"{MOD}.ReproTest::test_a"

    def test_classes(self):
        cases = [
            (run(**{self.T: (FAILED, "AssertionError: 3 != 2")}), run(**{self.T: PASSED}), FAIL_TO_PASS),
            (run(**{MOD: (ERROR, "ImportError: cannot import name 'normalize'")}), run(**{self.T: PASSED}), FAIL_TO_PASS_WEAK),
            (run(**{self.T: PASSED}), run(**{self.T: PASSED}), PASS_TO_PASS),
            (run(**{self.T: PASSED}), run(**{self.T: (FAILED, "AssertionError: 2 != 3")}), FAILS_ON_HEAD),
            (run(**{self.T: (FAILED, "AssertionError")}), run(**{MOD: (ERROR, "SyntaxError")}), FAILS_ON_HEAD),
        ]
        for base, head, expected in cases:
            cls, msg, tests = classify_candidate(self.runner, PATH, base, head)
            self.assertEqual(cls, expected, msg)

    def test_errors(self):
        cls, msg, _ = classify_candidate(self.runner, PATH, run(), RunResult(error="test command timed out"))
        self.assertEqual(cls, "error")
        self.assertIn("timed out", msg)
        cls, _, _ = classify_candidate(self.runner, PATH, run(), run())
        self.assertEqual(cls, "error")
        cls, _, _ = classify_candidate(self.runner, PATH, RunResult(error="no report"), run(**{self.T: PASSED}))
        self.assertEqual(cls, "error")

    def test_one_strong_test_is_enough_but_head_failures_win(self):
        t2 = f"{MOD}.ReproTest::test_b"
        base = run(**{self.T: (FAILED, "AssertionError: x"), t2: PASSED})
        cls, _, tests = classify_candidate(self.runner, PATH, base, run(**{self.T: PASSED, t2: PASSED}))
        self.assertEqual(cls, FAIL_TO_PASS)
        self.assertEqual(len(tests), 2)
        cls, _, _ = classify_candidate(self.runner, PATH, base, run(**{self.T: PASSED, t2: (FAILED, "boom")}))
        self.assertEqual(cls, FAILS_ON_HEAD)


def outcome(variant, cls, code="x"):
    return CandidateOutcome(variant, f"tests/proofcheck_agent/test_{variant.replace('-', '_')}.py", cls, code=code)


class StatusAndWinnerTest(unittest.TestCase):
    def test_overall_status(self):
        self.assertEqual(overall_status([outcome("exercise-diff", PASS_TO_PASS), outcome("reproduce-issue", FAIL_TO_PASS)]), "evidence")
        self.assertEqual(overall_status([outcome("exercise-diff", FAIL_TO_PASS_WEAK)]), "weak_evidence")
        self.assertEqual(overall_status([outcome("exercise-diff", FAILS_ON_HEAD), outcome("reproduce-issue", PASS_TO_PASS)]), "no_evidence")
        self.assertEqual(overall_status([outcome("exercise-diff", FAILS_ON_HEAD)]), "inconclusive")
        self.assertEqual(overall_status([outcome("exercise-diff", "error")]), "error")

    def test_winner_prefers_strong_then_short(self):
        w = pick_winner([outcome("exercise-diff", FAIL_TO_PASS, "x" * 50), outcome("reproduce-issue", FAIL_TO_PASS, "x" * 10),
                         outcome("localize-first", FAIL_TO_PASS_WEAK, "x")])
        self.assertEqual(w.variant, "reproduce-issue")
        self.assertIsNone(pick_winner([outcome("exercise-diff", PASS_TO_PASS)]))


BASE_RESULT = {
    "schema": "proofcheck/result", "version": 1, "base": "a" * 40, "head": "b" * 40, "verdict": "no_evidence",
    "claims": [
        {"type": "fixes_issue", "text": "fixes #7", "source": "Fixes #7.", "details": {"number": 7}, "status": "unverified", "evidence": "no test"},
        {"type": "fixes_issue", "text": "fixes #8", "source": "Fixes #8.", "details": {"number": 8}, "status": "unverified", "evidence": "no test"},
        {"type": "adds_tests", "text": "adds tests", "source": "Adds tests.", "status": "contradicted", "evidence": "none"},
    ],
}


def section(status, winner=True):
    s = {"status": status, "reason": "", "provider": "openai", "model": "m", "target_claim": "fixes #7",
         "variants": ["reproduce-issue"], "rounds": 1, "usage": {}, "cost_usd": None, "candidates": [], "notes": [],
         "winner": {"variant": "reproduce-issue", "file": "tests/proofcheck_agent/test_reproduce_issue.py",
                    "classification": FAIL_TO_PASS, "code": "def test(): pass", "rationale": ""} if winner else None,
         "upgraded_claims": []}
    return s


class ClaimUpgradeTest(unittest.TestCase):
    def test_strong_upgrades_only_the_target_claim(self):
        r = merge_into_result(copy.deepcopy(BASE_RESULT), section("evidence"), 7)
        self.assertEqual([c["status"] for c in r["claims"]], ["verified", "unverified", "contradicted"])
        self.assertEqual(r["claims"][0]["verified_by"], "agent")
        self.assertIn("agent-written reproduction test fails on base and passes on head", r["claims"][0]["evidence"])
        self.assertEqual(r["agent"]["upgraded_claims"], [0])
        self.assertEqual(r["verdict"], "contradicted")  # the deterministic contradiction stands
        validate_result(r)

    def test_verdict_recomputed(self):
        res = copy.deepcopy(BASE_RESULT)
        res["claims"] = res["claims"][:1]
        r = merge_into_result(res, section("evidence"), 7)
        self.assertEqual(r["verdict"], "evidence")
        res["claims"].append(dict(BASE_RESULT["claims"][1]))
        self.assertEqual(merge_into_result(res, section("evidence"), 7)["verdict"], "partial")

    def test_contradicted_claim_is_never_upgraded(self):
        res = copy.deepcopy(BASE_RESULT)
        res["claims"][0]["status"] = "contradicted"
        r = merge_into_result(res, section("evidence"), 7)
        self.assertEqual(r["claims"][0]["status"], "contradicted")
        self.assertEqual(r["agent"]["upgraded_claims"], [])

    def test_weak_inconclusive_and_none_never_downgrade(self):
        for status in ("weak_evidence", "inconclusive", "no_evidence", "error", "skipped"):
            r = merge_into_result(copy.deepcopy(BASE_RESULT), section(status, winner=False), 7)
            self.assertEqual([c["status"] for c in r["claims"]], ["unverified", "unverified", "contradicted"], status)
            self.assertEqual(r["verdict"], "no_evidence")
            self.assertFalse(any(c.get("verified_by") for c in r["claims"]))
        r = merge_into_result(copy.deepcopy(BASE_RESULT), section("weak_evidence", winner=False), 7)
        self.assertIn("weak", r["claims"][0]["evidence"])

    def test_without_issue_number_only_one_claim(self):
        r = merge_into_result(copy.deepcopy(BASE_RESULT), section("evidence"), None)
        self.assertEqual([c["status"] for c in r["claims"]][:2], ["verified", "unverified"])


class ResultSchemaTest(unittest.TestCase):
    def test_agent_section_is_validated(self):
        r = merge_into_result(copy.deepcopy(BASE_RESULT), section("evidence"), 7)
        r["agent"]["candidates"] = [{"variant": "reproduce-issue", "classification": FAIL_TO_PASS, "file": "f.py",
                                     "tests": [{"test_id": "t", "base": "failed", "head": "passed", "evil": 1}], "unknown": "x"}]
        out = validate_result(r)
        self.assertNotIn("unknown", out["agent"]["candidates"][0])
        self.assertNotIn("evil", out["agent"]["candidates"][0]["tests"][0])
        for path, bad in ((("agent", "status"), "proven"), (("agent", "variants"), ["rm -rf"]),
                          (("agent", "winner"), {"variant": "x"}), (("agent", "cost_usd"), "free")):
            doc = copy.deepcopy(r)
            doc[path[0]][path[1]] = bad
            with self.assertRaises(ResultValidationError, msg=path):
                validate_result(doc)
        doc = copy.deepcopy(r)
        doc["claims"][0]["verified_by"] = "human"
        with self.assertRaises(ResultValidationError):
            validate_result(doc)
        doc["claims"][0].pop("verified_by")
        self.assertNotIn("verified_by", validate_result(doc)["claims"][0])

    def test_old_results_still_validate(self):
        out = validate_result(copy.deepcopy(BASE_RESULT))
        self.assertIsNone(out["agent"])

    def test_candidate_paths(self):
        self.assertEqual(validate_candidate_path("tests/proofcheck_agent/test_a.py", "tests/proofcheck_agent"),
                         "tests/proofcheck_agent/test_a.py")
        for bad in ("tests/proofcheck_agent/../../x.py", "/tmp/test_a.py", "tests/other/test_a.py",
                    "tests/proofcheck_agent/conftest.py", "tests/proofcheck_agent/test_a.sh", "tests/proofcheck_agent/test a.py"):
            with self.assertRaises(ManifestError, msg=bad):
                validate_candidate_path(bad, "tests/proofcheck_agent")


if __name__ == "__main__":
    unittest.main()
