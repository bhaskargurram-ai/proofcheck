"""The fail-to-pass test check.

Idea (borrowed from how SWE-bench validates fixes): a test that was added or
changed by a pull request should **fail on the base commit** (the bug is
still there) and **pass on the head commit** (the PR fixes it). A test that
passes on both does not exercise the change, no matter how plausible it looks.

Procedure:

1. find test-related files changed between base and head;
2. copy those files, as they are at head, into a worktree of the base commit;
3. run the changed test modules there, and in a worktree of the head commit;
4. classify every touched test by its pair of outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import git as gitmod
from .junit import ERROR, FAILED, PASSED, SKIPPED, TestOutcome
from .runners import RunResult, TestRunner, added_lines_from_diff

# Per-test classifications
FAIL_TO_PASS = "fail_to_pass"
PASS_TO_PASS = "pass_to_pass"
FAIL_ON_HEAD = "fail_on_head"
ERROR_CLS = "error"
SKIPPED_CLS = "skipped"

# Overall status of the check
STATUS_EVIDENCE = "evidence"  # >=1 fail_to_pass, nothing failing on head
STATUS_NO_EVIDENCE = "no_evidence"  # tests ran, none failed on base
STATUS_FAIL_ON_HEAD = "fail_on_head"  # changed tests fail on head
STATUS_NO_TESTS = "no_tests"  # PR changes no runnable tests
STATUS_ERROR = "error"  # the runner could not produce results
STATUS_SKIPPED = "skipped"  # check disabled


@dataclass
class TestResult:
    """Classification of one test id."""

    __test__ = False  # not a pytest test class

    test_id: str
    file: str
    classification: str
    base: str  # outcome on base (or "missing")
    head: str  # outcome on head (or "missing")
    touched: bool = True
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class TestCheck:
    """Result of the fail-to-pass check."""

    __test__ = False  # not a pytest test class

    status: str
    runner: str = ""
    test_files: list[str] = field(default_factory=list)
    test_modules: list[str] = field(default_factory=list)
    tests: list[TestResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    base_run: dict[str, Any] = field(default_factory=dict)
    head_run: dict[str, Any] = field(default_factory=dict)
    old_tests_on_head: dict[str, Any] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        counts = {k: 0 for k in (FAIL_TO_PASS, PASS_TO_PASS, FAIL_ON_HEAD, ERROR_CLS, SKIPPED_CLS)}
        for t in self.tests:
            if t.touched:
                counts[t.classification] = counts.get(t.classification, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "runner": self.runner,
            "test_files": list(self.test_files),
            "test_modules": list(self.test_modules),
            "counts": self.counts(),
            "tests": [t.to_dict() for t in self.tests],
            "notes": list(self.notes),
            "base_run": dict(self.base_run),
            "head_run": dict(self.head_run),
            "old_tests_on_head": dict(self.old_tests_on_head),
        }


def _run_summary(r: RunResult) -> dict[str, Any]:
    return {
        "command": r.command,
        "returncode": r.returncode,
        "timed_out": r.timed_out,
        "error": r.error,
        "duration": round(r.duration, 3),
        "output_tail": r.output_tail[-2000:],
    }


def select_test_files(runner: TestRunner, changed: Sequence[gitmod.ChangedFile]) -> tuple[list[str], list[str]]:
    """Return ``(test_related_files, runnable_test_modules)`` among changed files."""
    files = [f.path for f in changed if f.status != "D" and not f.binary and runner.is_test_file(f.path)]
    modules = [p for p in files if runner.is_test_module(p)]
    return files, modules


def copy_files_from_ref(repo: str | Path, ref: str, paths: Sequence[str], dest: Path) -> list[str]:
    """Write ``paths`` as they exist at ``ref`` into directory ``dest``.

    Paths are validated so that a crafted file name cannot escape ``dest``.
    Returns the paths that were copied.
    """
    copied = []
    root = dest.resolve()
    for rel in paths:
        target = (dest / rel).resolve()
        if root != target and root not in target.parents:
            continue
        data = gitmod.show_file(repo, ref, rel)
        if data is None:
            continue
        if target.is_symlink():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        copied.append(rel)
    return copied


def _base_name(test_name: str) -> str:
    """Strip pytest parametrization: ``test_x[1-2]`` -> ``test_x``."""
    return test_name.split("[", 1)[0]


def _matches_touched(test_id: str, module_prefix_ok: bool, touched: set[tuple[str | None, str]]) -> bool:
    classname, _, name = test_id.rpartition("::")
    if not module_prefix_ok:
        return False
    if not classname:  # unittest-style "pkg.mod.Class.test" without ::
        classname, _, name = test_id.rpartition(".")
    name = _base_name(name)
    cls_last = classname.rsplit(".", 1)[-1] if classname else ""
    for cls, fn in touched:
        if fn != name:
            continue
        if cls is None or cls == cls_last:
            return True
    return False


def classify(base: TestOutcome | None, head: TestOutcome | None, base_collection_error: bool) -> str:
    """Classify a single test from its base and head outcomes."""
    head_o = head.outcome if head else None
    base_o = base.outcome if base else None
    if head_o is None:
        return ERROR_CLS
    if head_o in (FAILED, ERROR):
        return FAIL_ON_HEAD
    if head_o == SKIPPED:
        return SKIPPED_CLS
    # head passed
    if base_o in (FAILED, ERROR):
        return FAIL_TO_PASS
    if base_o is None:
        return FAIL_TO_PASS if base_collection_error else ERROR_CLS
    if base_o == SKIPPED:
        return SKIPPED_CLS
    return PASS_TO_PASS


def is_collection_error_id(test_id: str) -> bool:
    """True for pseudo-tests that represent a whole module failing to import/collect.

    Real tests always have ``classname::name`` ids; pytest (and
    :mod:`proofcheck.unittest_junit`) report collection errors with an empty
    classname, i.e. an id without ``::``.
    """
    return "::" not in test_id


def _collection_errors(outcomes: Mapping[str, TestOutcome], runner: TestRunner, modules: Sequence[str]) -> set[str]:
    """Modules whose import/collection failed in a run."""
    bad = set()
    for tid, o in outcomes.items():
        if o.outcome == ERROR and is_collection_error_id(tid):
            for m in modules:
                if runner.test_belongs_to(tid, m):
                    bad.add(m)
    return bad


def _module_of(test_id: str, runner: TestRunner, modules: Sequence[str]) -> str:
    for m in modules:
        if runner.test_belongs_to(test_id, m):
            return m
    return ""


def classify_runs(
    runner: TestRunner,
    modules: Sequence[str],
    base_run: RunResult,
    head_run: RunResult,
    touched_by_module: Mapping[str, set[tuple[str | None, str]] | None],
) -> list[TestResult]:
    """Pair up base and head outcomes and classify each test id."""
    base_bad = _collection_errors(base_run.outcomes, runner, modules)
    results: list[TestResult] = []
    ids = list(head_run.outcomes) + [t for t in base_run.outcomes if t not in head_run.outcomes]
    for tid in ids:
        module = _module_of(tid, runner, modules)
        head = head_run.outcomes.get(tid)
        base = base_run.outcomes.get(tid)
        if is_collection_error_id(tid):
            if head is None:
                # Collection error only on base: the individual head tests
                # are classified (as fail_to_pass) instead.
                continue
            if head.outcome == ERROR:
                results.append(TestResult(tid, module, FAIL_ON_HEAD, base.outcome if base else "missing", head.outcome,
                                          True, ("module failed to import/collect on head: " + head.message)[:500]))
                continue
        cls = classify(base, head, module in base_bad)
        touched_set = touched_by_module.get(module)
        touched = True if touched_set is None else _matches_touched(tid, True, touched_set)
        msg = ""
        if cls == FAIL_ON_HEAD and head is not None:
            msg = head.message
        elif cls == FAIL_TO_PASS:
            msg = base.message if base is not None else "test module failed to import/collect on base"
        results.append(
            TestResult(tid, module, cls, base.outcome if base else "missing", head.outcome if head else "missing", touched, msg[:500])
        )
    order = {FAIL_ON_HEAD: 0, FAIL_TO_PASS: 1, ERROR_CLS: 2, PASS_TO_PASS: 3, SKIPPED_CLS: 4}
    results.sort(key=lambda r: (not r.touched, order.get(r.classification, 9), r.test_id))
    return results


def overall_status(check: TestCheck) -> str:
    """Derive the overall status from the per-test classifications."""
    c = check.counts()
    if c[FAIL_ON_HEAD]:
        return STATUS_FAIL_ON_HEAD
    if c[FAIL_TO_PASS]:
        return STATUS_EVIDENCE
    if c[PASS_TO_PASS]:
        return STATUS_NO_EVIDENCE
    return STATUS_ERROR


def touched_tests_for(
    runner: TestRunner, repo: str | Path, base: str, head: str, changed: Sequence[gitmod.ChangedFile], modules: Sequence[str]
) -> dict[str, set[tuple[str | None, str]] | None]:
    """Compute which tests in each changed test module were touched by the diff."""
    status = {f.path: f.status for f in changed}
    out: dict[str, set[tuple[str | None, str]] | None] = {}
    for m in modules:
        raw = gitmod.show_file(repo, head, m)
        if raw is None:
            out[m] = None
            continue
        if status.get(m) in ("A", "C"):
            lines = None
        else:
            src = m
            for f in changed:
                if f.path == m and f.old_path:
                    src = f.old_path
            diff = gitmod.diff_text(repo, base, head, [src, m] if src != m else [m])
            lines = added_lines_from_diff(diff)
        out[m] = runner.touched_tests(m, raw.decode("utf-8", errors="replace"), lines)
    return out


def run_fail_to_pass(
    runner: TestRunner,
    repo: str | Path,
    base: str,
    head: str,
    changed: Sequence[gitmod.ChangedFile],
    base_wt: Path,
    head_wt: Path,
    env: Mapping[str, str],
    timeout: float,
    check_old_tests: bool = False,
) -> TestCheck:
    """Run the fail-to-pass check using already-created base and head worktrees.

    ``base_wt`` is modified (head versions of the test files are written into it).
    When ``check_old_tests`` is set, the *base* versions of modified test
    modules are additionally run against the head code ("do the old
    expectations still hold?") and restored afterwards.
    """
    files, modules = select_test_files(runner, changed)
    check = TestCheck(status=STATUS_NO_TESTS, runner=runner.name, test_files=files, test_modules=modules)
    if not files:
        check.notes.append("The PR does not add or modify any test files.")
        return check
    if not modules:
        check.notes.append("Test-related files changed, but none of them is a runnable test module.")
        return check

    copy_files_from_ref(repo, head, files, base_wt)
    touched = touched_tests_for(runner, repo, base, head, changed, modules)

    head_run = runner.run(head_wt, modules, env, timeout)
    base_run = runner.run(base_wt, modules, env, timeout)
    check.head_run = _run_summary(head_run)
    check.base_run = _run_summary(base_run)
    if not head_run.ok:
        check.status = STATUS_ERROR
        check.notes.append(f"Head run failed: {head_run.error}")
        return check
    if not base_run.ok:
        check.notes.append(f"Base run failed: {base_run.error}; base outcomes are unknown.")
    check.tests = classify_runs(runner, modules, base_run, head_run, touched)
    if not base_run.ok:
        for t in check.tests:
            if t.classification in (FAIL_TO_PASS, PASS_TO_PASS):
                t.classification = ERROR_CLS
                t.message = "base run did not produce results"
    if not any(t.touched for t in check.tests):
        check.notes.append("No individual test function was touched by the diff (only helpers/fixtures changed).")
    untouched = sum(1 for t in check.tests if not t.touched)
    if untouched:
        check.notes.append(f"{untouched} pre-existing test(s) in the changed files were not touched by the diff and are not counted as evidence.")
    check.status = overall_status(check) if check.tests else STATUS_ERROR
    if not check.tests:
        check.notes.append("The test runner reported no tests for the changed files.")

    if check_old_tests:
        modified = [f.path for f in changed if f.status == "M" and f.path in modules]
        if modified:
            check.old_tests_on_head = _run_old_tests_on_head(runner, repo, base, head, modified, head_wt, env, timeout)
    return check


def _run_old_tests_on_head(
    runner: TestRunner, repo: str | Path, base: str, head: str, modified: Sequence[str], head_wt: Path, env: Mapping[str, str], timeout: float
) -> dict[str, Any]:
    """Run base versions of modified test modules against head code, then restore them."""
    copy_files_from_ref(repo, base, modified, head_wt)
    try:
        r = runner.run(head_wt, modified, env, timeout)
    finally:
        copy_files_from_ref(repo, head, modified, head_wt)
    if not r.ok:
        return {"ran": False, "error": r.error, "files": list(modified)}
    failing = sorted(t for t, o in r.outcomes.items() if o.outcome in (FAILED, ERROR))
    passed = sum(1 for o in r.outcomes.values() if o.outcome == PASSED)
    return {"ran": True, "files": list(modified), "passed": passed, "failing": failing[:50]}
