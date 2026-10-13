"""Diff evidence map (hunk ablation) on real temporary git repositories."""

import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _helpers
from _helpers import BASE_TESTS, BUGGY, FIXED, GOOD_TESTS, GitRepo, scrubbed_env

from proofcheck import ablation as A
from proofcheck import git as gitmod
from proofcheck.card import render_card
from proofcheck.check import PRContext, run_check
from proofcheck.config import Config, config_from_dict
from proofcheck.result import validate_result
from proofcheck.runners import get_runner

UNITTEST_CMD = [sys.executable, "{unittest_junit}", "--junitxml", "{junitxml}", "{tests}"]

UTIL = "def greet():\n    return 'hi'\n"
UTIL_CHANGED = "def greet():\n    return 'hello'\n"

HELPER_FIX = "def plus(a, b):\n    return a + b\n"
CALC_USES_HELPER = FIXED.replace("def add(a, b):\n    return a + b", "from helpers import plus\n\n\ndef add(a, b):\n    return plus(a, b)")


def cfg(**ablation):
    c = Config(runner="unittest", test_command=list(UNITTEST_CMD))
    for k, v in ablation.items():
        setattr(c.ablation, k, v)
    return c


def leftover_worktrees(repo: Path) -> list[str]:
    return [w for w in gitmod.list_worktrees(repo) if Path(w).resolve() != repo.resolve()]


class AblationRepoCase(unittest.TestCase):
    def setUp(self):
        self.repo = GitRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.commit({"calc.py": BUGGY, "util.py": UTIL, "README.md": "# calc\n", "tests/test_calc.py": BASE_TESTS}, "base")

    def pr(self, files, name="pr"):
        self.repo.branch(name, "main")
        self.repo.commit(files, name)
        self.repo.checkout("main")

    def check(self, head="pr", body="Fixes #1", issues=None, **ablation):
        ctx = PRContext(title="Fix add", body=body, issues=issues or {})
        raw = run_check(self.repo.path, "main", head, cfg(**ablation), ctx, environ=scrubbed_env())
        return validate_result(json.loads(json.dumps(raw)))


class RequiredAndUnexercisedTest(AblationRepoCase):
    def test_required_and_not_exercised_hunks(self):
        self.pr({"calc.py": FIXED, "util.py": UTIL_CHANGED, "README.md": "# calc\n\nFixed.\n", "tests/test_calc.py": GOOD_TESTS})
        result = self.check()
        self.assertEqual(result["tests"]["status"], "evidence")
        a = result["ablation"]
        self.assertEqual(a["status"], "ran")
        self.assertEqual(a["evidence"], ["tests.test_calc.AddTest::test_add"])
        by_file = {h["file"]: h for h in a["hunks"]}
        self.assertEqual(sorted(by_file), ["calc.py", "util.py"])  # README.md excluded, test file excluded
        self.assertEqual(by_file["calc.py"]["status"], "required")
        self.assertEqual(by_file["calc.py"]["broken_by"], ["tests.test_calc.AddTest::test_add"])
        self.assertEqual(by_file["calc.py"]["location"], "calc.py:2")
        self.assertEqual(by_file["util.py"]["status"], "not_exercised")
        self.assertEqual((a["tested"], a["required"], a["not_exercised"], a["skipped"]), (2, 1, 1, 0))
        flags = [f for f in result["scope"]["flags"] if f["kind"] == "unexercised"]
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["paths"], ["util.py"])
        self.assertEqual(result["verdict"], "evidence")  # the map never changes the verdict

        card = render_card(result)
        self.assertIn("### Diff evidence map", card)
        self.assertIn("1/2 changed hunks are required by the evidence", card)
        self.assertIn("| `calc.py:2` | ✅ required | `tests.test_calc.AddTest::test_add` |", card)
        self.assertIn("| `util.py:2` | ⚠️ not exercised | — |", card)
        self.assertIn("Changed, but no evidence depends on it", card)
        # the worktree used for ablation is gone and the repository is untouched
        self.assertEqual(leftover_worktrees(self.repo.path), [])
        self.assertEqual(_helpers.git(self.repo.path, "status", "--porcelain"), "")

    def test_disabled_and_not_applicable(self):
        self.pr({"calc.py": FIXED, "tests/test_calc.py": GOOD_TESTS})
        self.assertIsNone(self.check(enabled=False)["ablation"])
        # no fail->pass evidence (test-only change that fails on head): nothing to ablate against
        self.repo.branch("weak", "main")
        self.repo.commit({"util.py": UTIL_CHANGED}, "no tests")
        self.repo.checkout("main")
        a = self.check(head="weak")["ablation"]
        self.assertEqual(a["status"], "not_applicable")
        self.assertEqual(a["hunks"], [])
        self.assertNotIn("Diff evidence map", render_card(self.check(head="weak")))


class FileKindsTest(AblationRepoCase):
    def test_new_file_is_one_hunk_and_reverting_deletes_it(self):
        self.pr({"calc.py": CALC_USES_HELPER, "helpers.py": HELPER_FIX, "tests/test_calc.py": GOOD_TESTS})
        a = self.check()["ablation"]
        by_file = {h["file"]: h for h in a["hunks"]}
        self.assertEqual(by_file["helpers.py"]["kind"], "add")
        self.assertEqual(by_file["helpers.py"]["location"], "helpers.py (new file)")
        self.assertEqual(by_file["helpers.py"]["status"], "required")
        self.assertTrue(all(h["status"] == "required" for h in a["hunks"]), a["hunks"])

    def test_rename_delete_and_binary(self):
        self.repo.commit({"old_name.py": "VALUE = 1\n", "gone.py": "X = 1\n", "logo.bin": b"\x00\x01\x02"}, "more files")
        self.pr({"calc.py": FIXED, "tests/test_calc.py": GOOD_TESTS, "old_name.py": None, "new_name.py": "VALUE = 1\n",
                 "gone.py": None, "logo.bin": b"\x00\x09\x02\x03"})
        a = self.check()["ablation"]
        kinds = {h["file"]: (h["kind"], h["status"]) for h in a["hunks"]}
        self.assertEqual(kinds["new_name.py"], ("rename", "not_exercised"))
        self.assertEqual(kinds["gone.py"], ("delete", "not_exercised"))
        self.assertEqual(kinds["calc.py"], ("modify", "required"))
        self.assertNotIn("logo.bin", kinds)  # binary files are skipped
        self.assertEqual(_helpers.git(self.repo.path, "status", "--porcelain"), "")

    def test_list_hunks_for_renamed_and_modified_file(self):
        self.repo.commit({"pkg/a.py": "A = 1\nB = 2\nC = 3\nD = 4\nE = 5\n"}, "pkg")
        self.pr({"pkg/a.py": None, "pkg/b.py": "A = 1\nB = 20\nC = 3\nD = 4\nE = 50\n"})
        changed = gitmod.changed_files(self.repo.path, "main", "pr")
        runner = get_runner("unittest", command=UNITTEST_CMD)
        hunks = A.list_hunks(self.repo.path, "main", "pr", changed, runner.is_test_file)
        self.assertEqual([(h.file, h.kind, h.new_start, h.old_path) for h in hunks],
                         [("pkg/b.py", "modify", 2, "pkg/a.py"), ("pkg/b.py", "modify", 5, "pkg/a.py")])
        # each hunk reverses on its own against the head file
        with gitmod.worktree(self.repo.path, "pr") as wt:
            for h in hunks:
                with tempfile.TemporaryDirectory() as tmp:
                    self.assertIsNone(A._reverse_apply(wt, h, Path(tmp)))
            self.assertEqual((wt / "pkg/b.py").read_text(), "A = 1\nB = 2\nC = 3\nD = 4\nE = 5\n")


class ReproducerEvidenceTest(AblationRepoCase):
    def test_reproducer_snippet_is_evidence(self):
        self.pr({"calc.py": FIXED, "util.py": UTIL_CHANGED})  # no tests at all
        issue = "```python\nfrom calc import add\nassert add(2, 3) == 5\nprint('ok')\n```\n"
        result = self.check(issues={"#1": issue})
        a = result["ablation"]
        self.assertEqual(a["evidence"], ["#1 snippet 1"])
        by_file = {h["file"]: h for h in a["hunks"]}
        self.assertEqual(by_file["calc.py"]["status"], "required")
        self.assertEqual(by_file["calc.py"]["broken_by"], ["#1 snippet 1"])
        self.assertEqual(by_file["util.py"]["status"], "not_exercised")


class _CountingRunner:
    """Wraps a runner: advances a fake clock per run and can raise on a given call."""

    def __init__(self, inner, clock, step=0.0, raise_on=None):
        self.inner, self.clock, self.step, self.raise_on, self.calls = inner, clock, step, raise_on, 0

    def is_test_file(self, path):
        return self.inner.is_test_file(path)

    def run(self, wt, modules, env, timeout):
        self.calls += 1
        if self.raise_on == self.calls:
            raise RuntimeError("runner exploded")
        self.clock.now += self.step
        return self.inner.run(wt, modules, env, timeout)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class BudgetCapCleanupTest(AblationRepoCase):
    def setUp(self):
        super().setUp()
        self.repo.commit({"more.py": "def a():\n    return 1\n\n\ndef b():\n    return 2\n"}, "more")
        self.pr({"calc.py": FIXED, "util.py": UTIL_CHANGED, "more.py": "def a():\n    return 10\n\n\ndef b():\n    return 20\n",
                 "tests/test_calc.py": GOOD_TESTS})
        base_sha = _helpers.git(self.repo.path, "rev-parse", "main")
        self.head_sha = _helpers.git(self.repo.path, "rev-parse", "pr")
        self.changed = gitmod.changed_files(self.repo.path, base_sha, self.head_sha)
        self.base_sha = base_sha
        result = self.check(enabled=False)
        # Rebuild the TestCheck-like evidence from the validated result.
        tests = result["tests"]
        self.tests = type("T", (), {"tests": [type("R", (), t) for t in tests["tests"]]})()
        self.inner = get_runner("unittest", command=UNITTEST_CMD)

    def ablate(self, runner, clock, **kw):
        return A.run_ablation(self.repo.path, self.base_sha, self.head_sha, self.changed, runner, self.tests, [], {},
                              scrubbed_env(), environ=scrubbed_env(), clock=clock, **kw)

    def test_hunk_cap_and_deterministic_order(self):
        clock = FakeClock()
        res = self.ablate(_CountingRunner(self.inner, clock), clock, max_hunks=2)
        self.assertEqual(res.total_hunks, 4)
        self.assertEqual([h.hunk.location() for h in res.hunks], ["calc.py:2", "more.py:2"])
        self.assertEqual(res.untested_over_cap, 2)
        d = res.to_dict()
        self.assertEqual((d["tested"], d["untested_over_cap"]), (2, 2))
        self.assertIn("2 more hunk(s) not tested", " ".join(d["notes"]))

    def test_time_budget_skips_remaining_hunks(self):
        clock = FakeClock()
        runner = _CountingRunner(self.inner, clock, step=200)
        res = self.ablate(runner, clock, time_budget_s=300)
        # baseline (t: 0 -> 200), first hunk (200 -> 400), then the budget is gone
        self.assertEqual(res.status, "ran")
        self.assertEqual([h.status for h in res.hunks], ["required", "skipped", "skipped", "skipped"])
        self.assertEqual(res.hunks[1].reason, "time budget exhausted")
        self.assertTrue(res.budget_exhausted)
        self.assertEqual(runner.calls, 2)
        self.assertEqual(leftover_worktrees(self.repo.path), [])

    def test_cleanup_when_a_run_raises(self):
        before = {p for p in os.listdir(tempfile.gettempdir()) if p.startswith("proofcheck-ablation-")}
        clock = FakeClock()
        res = self.ablate(_CountingRunner(self.inner, clock, raise_on=2), clock)
        self.assertEqual(res.status, "error")
        self.assertIn("RuntimeError: runner exploded", " ".join(res.notes))
        self.assertEqual(leftover_worktrees(self.repo.path), [])
        after = {p for p in os.listdir(tempfile.gettempdir()) if p.startswith("proofcheck-ablation-")}
        self.assertEqual(after - before, set())
        self.assertEqual(_helpers.git(self.repo.path, "status", "--porcelain"), "")

    def test_baseline_failure_is_reported(self):
        clock = FakeClock()

        class Broken(_CountingRunner):
            def run(self, wt, modules, env, timeout):
                (wt / "calc.py").write_text(BUGGY)  # simulate evidence that does not hold in a fresh worktree
                return super().run(wt, modules, env, timeout)

        res = self.ablate(Broken(self.inner, clock), clock)
        self.assertEqual(res.status, "baseline_failed")
        self.assertEqual(res.hunks, [])
        self.assertEqual(leftover_worktrees(self.repo.path), [])

    def test_unrevertable_hunk_is_skipped(self):
        clock = FakeClock()
        hunks = A.list_hunks(self.repo.path, self.base_sha, self.head_sha, self.changed, self.inner.is_test_file)
        broken = hunks[0]
        broken.patch = broken.patch.replace("+    return a + b", "+    return a * b")  # no longer matches head
        with mock.patch.object(A, "list_hunks", return_value=[broken]):
            res = self.ablate(_CountingRunner(self.inner, clock), clock)
        self.assertEqual(res.hunks[0].status, "skipped")
        self.assertIn("could not reverse-apply", res.hunks[0].reason)


class ConfigAndValidationTest(unittest.TestCase):
    def test_config_table(self):
        c = config_from_dict({"ablation": {"enabled": False, "max_hunks": 3, "time_budget_s": 60, "exclude": ["*.cfg"]}})
        self.assertEqual((c.ablation.enabled, c.ablation.max_hunks, c.ablation.time_budget_s, c.ablation.exclude),
                         (False, 3, 60, ["*.cfg"]))
        self.assertTrue(Config().ablation.enabled)
        self.assertEqual(Config().to_dict()["ablation"]["max_hunks"], 12)
        from proofcheck.config import ConfigError

        for bad in ({"ablation": {"max_hunks": -1}}, {"ablation": {"nope": 1}}, {"ablation": 3}, {"ablation": {"enabled": "yes"}}):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                config_from_dict(bad)

    def test_validator_rejects_forged_ablation(self):
        base = {"schema": "proofcheck/result", "version": 1, "base": "a" * 40, "head": "b" * 40, "verdict": "evidence"}
        ok = validate_result(dict(base, ablation={"status": "ran", "hunks": [
            {"file": "x.py", "status": "required", "location": "x.py:1", "broken_by": ["t::a"], "extra": "dropped"}]}))
        self.assertNotIn("extra", ok["ablation"]["hunks"][0])
        self.assertIsNone(validate_result(base)["ablation"])
        from proofcheck.result import ResultValidationError

        for bad in ({"status": "pwned"}, {"status": "ran", "hunks": [{"file": "x", "status": "verified"}]},
                    {"status": "ran", "required": -1}, {"status": "ran", "hunks": "x"}):
            with self.assertRaises(ResultValidationError, msg=repr(bad)):
                validate_result(dict(base, ablation=bad))

    def test_card_escapes_hostile_hunk_data(self):
        base = {"schema": "proofcheck/result", "version": 1, "base": "a" * 40, "head": "b" * 40, "verdict": "evidence"}
        res = validate_result(dict(base, ablation={"status": "ran", "tested": 1, "required": 1, "hunks": [
            {"file": "a|b.py", "location": "a|b.py:1 <img src=x> @admin", "status": "required",
             "broken_by": ["t::x`|<b>"]},
            {"file": "c.py", "location": "c.py", "status": "skipped", "reason": "[link](http://evil) #12"}]}))
        card = render_card(res)
        # Untrusted locations and test ids only ever appear inside code spans (inert in GitHub Markdown),
        # with table pipes escaped; free text is escaped and defused.
        self.assertIn("| `a\\|b.py:1 <img src=x> @admin` | ✅ required | `t::x'\\|<b>` |", card)
        self.assertIn("\\[link\\](http\u200d://evil)", card)  # no link, no autolink
        self.assertIn("#\u200d12", card)
        self.assertIn("1/1 changed hunk is required by the evidence", card)


class CliFlagTest(AblationRepoCase):
    def test_no_ablation_flag(self):
        from click.testing import CliRunner

        from proofcheck import cli

        self.pr({"calc.py": FIXED, "tests/test_calc.py": GOOD_TESTS})
        out = Path(tempfile.mkdtemp(prefix="proofcheck-cli-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(out, ignore_errors=True))
        args = ["run", "--repo", str(self.repo.path), "--base", "main", "--head", "pr", "--runner", "unittest",
                "--test-command", shlex.join(UNITTEST_CMD), "--title", "Fixes #1", "--json", str(out / "r.json"),
                "--out", str(out / "card.md")]
        env = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
        with mock.patch.dict(os.environ, env, clear=True):
            res = CliRunner().invoke(cli.main, args + ["--no-ablation"], catch_exceptions=False)
            self.assertEqual(res.exit_code, 0, res.output)
            self.assertIsNone(json.loads((out / "r.json").read_text())["ablation"])
            res = CliRunner().invoke(cli.main, args, catch_exceptions=False)
            self.assertEqual(res.exit_code, 0, res.output)
            self.assertEqual(json.loads((out / "r.json").read_text())["ablation"]["required"], 1)


if __name__ == "__main__":
    unittest.main()
