"""Match claims to the evidence gathered by the checks.

Every claim ends up in one of three states:

* ``verified``     -- executed evidence supports it
* ``unverified``   -- no evidence either way (the default; *not* an accusation)
* ``contradicted`` -- executed evidence conflicts with it

Rules (deliberately simple and documented in the README):

``fixes_issue``
    verified by >= 1 fail-to-pass test, or by an issue reproducer from that
    issue that exits non-zero on base and zero on head; contradicted when
    changed tests fail on head, or when the issue's reproducer fails on base
    and still fails identically (same exit code and output) on head.
``adds_tests``
    verified when test functions were added/changed and ran; contradicted
    when the diff changes no test files.
``no_behavior_change``
    contradicted by fail-to-pass tests (a test that fails before and passes
    after demonstrates a behaviour change), by an issue reproducer that
    behaves differently, or by base versions of modified tests failing on
    head; otherwise unverified (absence of change cannot be proven).
``backwards_compatible``
    contradicted when the base versions of modified test files fail on head
    (existing expectations had to change); otherwise unverified.
``performance``
    verified/contradicted only by the optional benchmark; memory claims are
    always unverified (no memory benchmark yet).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import claims as C
from .benchmark import BenchmarkResult, judge_speedup
from .failtopass import FAIL_ON_HEAD, FAIL_TO_PASS, PASS_TO_PASS, STATUS_NO_TESTS, TestCheck
from .repro import BEHAVIOR_CHANGED, UNCHANGED, IssueRepro

VERIFIED = "verified"
UNVERIFIED = "unverified"
CONTRADICTED = "contradicted"


@dataclass
class Assessment:
    claim: C.Claim
    status: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        d = self.claim.to_dict()
        d.update(status=self.status, evidence=self.evidence)
        return d


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _agree(n: int, singular: str, plural: str) -> str:
    """Pick the verb form agreeing with ``n``."""
    return singular if n == 1 else plural


def _repro_for(issue_number: int | None, repros: list[IssueRepro]) -> list[IssueRepro]:
    if issue_number is None:
        return list(repros)
    return [r for r in repros if r.issue == f"#{issue_number}" or r.issue.startswith("issue-file")]


def _assess_fixes(claim: C.Claim, tests: TestCheck | None, repros: list[IssueRepro]) -> tuple[str, str]:
    counts = tests.counts() if tests else {}
    f2p, foh, p2p = counts.get(FAIL_TO_PASS, 0), counts.get(FAIL_ON_HEAD, 0), counts.get(PASS_TO_PASS, 0)
    notes: list[str] = []
    if foh:
        return CONTRADICTED, f"{_plural(foh, 'changed test')} {_agree(foh, 'fails', 'fail')} on the PR head"
    relevant = _repro_for(claim.details.get("number"), repros)
    fixed_snippets = []
    changed_snippets = 0
    for r in relevant:
        for s in r.snippets:
            if s.status == BEHAVIOR_CHANGED:
                changed_snippets += 1
                if s.base.exit_code not in (0, None) and s.head.exit_code == 0:
                    fixed_snippets.append(s)
    if f2p:
        notes.append(f"{_plural(f2p, 'test')} {_agree(f2p, 'fails', 'fail')} on base and {_agree(f2p, 'passes', 'pass')} on head")
    if fixed_snippets:
        notes.append(f"issue reproducer fails on base (exit {fixed_snippets[0].base.exit_code}) and succeeds on head")
    if notes:
        return VERIFIED, "; ".join(notes)
    if changed_snippets:
        return UNVERIFIED, "issue reproducer output changes on head, but no fail-to-pass test; check the new output is correct"
    still_failing = [s for r in relevant for s in r.snippets
                     if s.status == UNCHANGED and s.base.exit_code not in (0, None) and not s.base.timed_out]
    if still_failing:
        return CONTRADICTED, (f"the issue reproducer still fails on head exactly as on base "
                              f"(exit {still_failing[0].head.exit_code})")
    if any(r.status == "unchanged" for r in relevant):
        notes.append("issue reproducer behaves identically on base and head")
    if p2p:
        notes.append(f"the changed tests also pass on base ({_plural(p2p, 'test')}), so they do not exercise the fix")
    elif tests is None or tests.status == STATUS_NO_TESTS:
        notes.append("no test was added or changed")
    elif not notes:
        notes.append("no fail-to-pass test")
    return UNVERIFIED, "; ".join(notes)


def _assess_adds_tests(tests: TestCheck | None) -> tuple[str, str]:
    if tests is None or not tests.test_files:
        return CONTRADICTED, "the diff does not add or modify any test file"
    touched = [t for t in tests.tests if t.touched]
    if not touched:
        if not tests.test_modules:
            return UNVERIFIED, f"test-related files changed ({_plural(len(tests.test_files), 'file')}) but no runnable test module"
        return UNVERIFIED, "test files changed, but no individual test function was added or modified"
    return VERIFIED, f"{_plural(len(touched), 'test')} added/changed in {_plural(len(tests.test_modules), 'file')}"


def _old_tests_failing(tests: TestCheck | None) -> list[str]:
    if tests is None or not tests.old_tests_on_head.get("ran"):
        return []
    return list(tests.old_tests_on_head.get("failing", []))


def _assess_no_behavior(tests: TestCheck | None, repros: list[IssueRepro]) -> tuple[str, str]:
    reasons = []
    f2p = tests.counts().get(FAIL_TO_PASS, 0) if tests else 0
    if f2p:
        reasons.append(f"{_plural(f2p, 'test')} {_agree(f2p, 'fails', 'fail')} on base and {_agree(f2p, 'passes', 'pass')} on head, which demonstrates a behaviour change")
    old_failing = _old_tests_failing(tests)
    if old_failing:
        reasons.append(f"{_plural(len(old_failing), 'pre-existing test')} (base version) {_agree(len(old_failing), 'fails', 'fail')} on head")
    if any(s.status == BEHAVIOR_CHANGED for r in repros for s in r.snippets):
        reasons.append("an issue reproducer behaves differently on head")
    if reasons:
        return CONTRADICTED, "; ".join(reasons)
    support = []
    if tests and tests.old_tests_on_head.get("ran"):
        support.append(f"base versions of modified tests still pass on head ({tests.old_tests_on_head.get('passed', 0)} passed)")
    if tests and tests.counts().get(PASS_TO_PASS, 0):
        support.append("changed tests pass on both base and head")
    text = "; ".join(support) if support else "no evidence either way"
    return UNVERIFIED, f"not contradicted by the tests that ran ({text}); absence of behaviour change cannot be proven"


def _assess_backcompat(tests: TestCheck | None) -> tuple[str, str]:
    old_failing = _old_tests_failing(tests)
    if old_failing:
        return CONTRADICTED, f"{_plural(len(old_failing), 'pre-existing test')} (base version) {_agree(len(old_failing), 'fails', 'fail')} on head: existing expectations changed"
    if tests and tests.old_tests_on_head.get("ran"):
        return UNVERIFIED, f"base versions of modified tests still pass on head ({tests.old_tests_on_head.get('passed', 0)} passed); not a full compatibility check"
    return UNVERIFIED, "no compatibility evidence (no pre-existing tests were modified)"


def _assess_perf(claim: C.Claim, bench: BenchmarkResult | None) -> tuple[str, str]:
    factor = float(claim.details.get("factor") or 0)
    if claim.details.get("metric") == "memory":
        return UNVERIFIED, "unverified: no memory benchmark evidence"
    if bench is None:
        return UNVERIFIED, "unverified: no benchmark evidence (configure benchmark_command to measure it)"
    if bench.error:
        return UNVERIFIED, f"benchmark failed: {bench.error}"
    status, text = judge_speedup(factor, bench.speedup)
    return status, f"{text} (median of {bench.runs} runs: {bench.base_median:.3f}s → {bench.head_median:.3f}s)"


def assess_claims(
    claims: list[C.Claim],
    tests: TestCheck | None,
    repros: list[IssueRepro],
    bench: BenchmarkResult | None,
) -> list[Assessment]:
    """Assess every claim against the gathered evidence."""
    out = []
    for claim in claims:
        if claim.type == C.FIXES_ISSUE:
            status, ev = _assess_fixes(claim, tests, repros)
        elif claim.type == C.ADDS_TESTS:
            status, ev = _assess_adds_tests(tests)
        elif claim.type == C.NO_BEHAVIOR_CHANGE:
            status, ev = _assess_no_behavior(tests, repros)
        elif claim.type == C.BACKWARDS_COMPATIBLE:
            status, ev = _assess_backcompat(tests)
        elif claim.type == C.PERFORMANCE:
            status, ev = _assess_perf(claim, bench)
        else:  # pragma: no cover - guarded by CLAIM_TYPES
            status, ev = UNVERIFIED, "unknown claim type"
        out.append(Assessment(claim, status, ev))
    return out


def verdict(assessments: list[Assessment], tests: TestCheck | None) -> str:
    """Overall verdict: ``contradicted``, ``evidence``, ``partial`` or ``no_evidence``."""
    counts = tests.counts() if tests else {}
    if any(a.status == CONTRADICTED for a in assessments) or counts.get(FAIL_ON_HEAD, 0):
        return "contradicted"
    verified = any(a.status == VERIFIED for a in assessments) or counts.get(FAIL_TO_PASS, 0)
    unverified = any(a.status == UNVERIFIED for a in assessments)
    if verified and not unverified:
        return "evidence"
    if verified:
        return "partial"
    return "no_evidence"
