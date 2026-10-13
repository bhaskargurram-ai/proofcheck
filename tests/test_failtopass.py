"""End-to-end tests of the fail-to-pass check with real git worktrees."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import _helpers  # noqa: F401  (sets up sys.path)
from _helpers import FAKE_PYTEST, RepoTestCase, scrubbed_env

from proofcheck import git as gitmod
from proofcheck.failtopass import (
    FAIL_ON_HEAD,
    FAIL_TO_PASS,
    PASS_TO_PASS,
    STATUS_ERROR,
    STATUS_EVIDENCE,
    STATUS_FAIL_ON_HEAD,
    STATUS_NO_EVIDENCE,
    STATUS_NO_TESTS,
    copy_files_from_ref,
    run_fail_to_pass,
)
from proofcheck.runners import PytestRunner, UnittestRunner, get_runner


def run_ftp(repo_path: Path, head: str, runner, check_old_tests: bool = False):
    base = gitmod.resolve_ref(repo_path, "main")
    head_sha = gitmod.resolve_ref(repo_path, head)
    changed = gitmod.changed_files(repo_path, base, head_sha)
    with gitmod.worktree(repo_path, base) as bwt, gitmod.worktree(repo_path, head_sha) as hwt:
        return run_fail_to_pass(runner, repo_path, base, head_sha, changed, bwt, hwt, scrubbed_env(), 60, check_old_tests)


def unittest_runner():
    return UnittestRunner(command=[sys.executable, "{unittest_junit}", "--junitxml", "{junitxml}", "{tests}"])


def fake_pytest_runner():
    # Exercises the pytest runner's argument building with a stand-in for pytest.
    return PytestRunner(command=[sys.executable, str(FAKE_PYTEST), "-p", "no:cacheprovider", "-q"])


class FailToPassUnittestRunnerTest(RepoTestCase):
    def test_real_fix_is_fail_to_pass(self):
        check = run_ftp(self.repo.path, "good", unittest_runner())
        self.assertEqual(check.status, STATUS_EVIDENCE, check.notes)
        touched = [t for t in check.tests if t.touched]
        self.assertEqual([(t.test_id, t.classification) for t in touched],
                         [("tests.test_calc.AddTest::test_add", FAIL_TO_PASS)])
        # the pre-existing, untouched test is reported but not counted
        untouched = [t for t in check.tests if not t.touched]
        self.assertEqual([t.test_id for t in untouched], ["tests.test_calc.MulTest::test_mul"])
        self.assertEqual(check.counts()[FAIL_TO_PASS], 1)
        self.assertEqual(check.counts()[PASS_TO_PASS], 0)

    def test_weak_test_is_pass_to_pass(self):
        check = run_ftp(self.repo.path, "weak", unittest_runner())
        self.assertEqual(check.status, STATUS_NO_EVIDENCE)
        touched = [t for t in check.tests if t.touched]
        self.assertEqual([(t.test_id, t.classification) for t in touched],
                         [("tests.test_calc.MulTest::test_mul_zero", PASS_TO_PASS)])

    def test_failing_on_head(self):
        check = run_ftp(self.repo.path, "broken", unittest_runner())
        self.assertEqual(check.status, STATUS_FAIL_ON_HEAD)
        self.assertEqual([t.classification for t in check.tests if t.touched], [FAIL_ON_HEAD])

    def test_no_tests_changed(self):
        check = run_ftp(self.repo.path, "notests", unittest_runner())
        self.assertEqual(check.status, STATUS_NO_TESTS)
        self.assertEqual(check.tests, [])

    def test_old_tests_on_head(self):
        check = run_ftp(self.repo.path, "good", unittest_runner(), check_old_tests=True)
        self.assertTrue(check.old_tests_on_head["ran"])
        self.assertEqual(check.old_tests_on_head["failing"], [])
        self.assertEqual(check.old_tests_on_head["passed"], 1)

    def test_new_test_module_with_import_error_on_base(self):
        r = self.repo
        r.branch("newmod", "main")
        r.commit({"strutil.py": "def shout(s):\n    return s.upper() + '!'\n",
                  "tests/test_strutil.py": "import unittest\nfrom strutil import shout\n\n\nclass T(unittest.TestCase):\n"
                                           "    def test_shout(self):\n        self.assertEqual(shout('hi'), 'HI!')\n"}, "new module")
        check = run_ftp(r.path, "newmod", unittest_runner())
        self.assertEqual(check.status, STATUS_EVIDENCE)
        (t,) = [t for t in check.tests if t.touched]
        self.assertEqual((t.test_id, t.classification, t.base), ("tests.test_strutil.T::test_shout", FAIL_TO_PASS, "missing"))


class FailToPassPytestRunnerTest(RepoTestCase):
    def test_pytest_runner_with_stand_in(self):
        check = run_ftp(self.repo.path, "good", fake_pytest_runner())
        self.assertEqual(check.status, STATUS_EVIDENCE, (check.notes, check.head_run))
        self.assertIn("--rootdir=.", check.head_run["command"])
        self.assertTrue(any(a.startswith("--junitxml=") for a in check.head_run["command"]))

    def test_command_that_writes_no_report_is_an_error(self):
        runner = PytestRunner(command=[sys.executable, "-c", "import sys; sys.exit(3)"])
        check = run_ftp(self.repo.path, "good", runner)
        self.assertEqual(check.status, STATUS_ERROR)
        self.assertIn("without writing a JUnit report", check.head_run["error"])

    def test_timeout(self):
        runner = PytestRunner(command=[sys.executable, "-c", "import time; time.sleep(30)"])
        base = gitmod.resolve_ref(self.repo.path, "main")
        head = gitmod.resolve_ref(self.repo.path, "good")
        with gitmod.worktree(self.repo.path, head) as hwt:
            res = runner.run(hwt, ["tests/test_calc.py"], scrubbed_env(), timeout=1)
        self.assertTrue(res.timed_out)
        self.assertIsNotNone(res.error)
        self.assertNotEqual(base, head)


class WorktreeTest(RepoTestCase):
    def test_worktree_removed_on_exception(self):
        before = gitmod.list_worktrees(self.repo.path)
        holder = {}
        with self.assertRaises(RuntimeError):
            with gitmod.worktree(self.repo.path, "good") as wt:
                holder["wt"] = wt
                self.assertTrue((wt / "calc.py").exists())
                raise RuntimeError("boom")
        self.assertFalse(holder["wt"].exists())
        self.assertEqual(gitmod.list_worktrees(self.repo.path), before)

    def test_worktrees_cleaned_when_runner_crashes(self):
        runner = unittest_runner()
        with mock.patch.object(UnittestRunner, "run", side_effect=OSError("runner exploded")):
            with self.assertRaises(OSError):
                run_ftp(self.repo.path, "good", runner)
        self.assertEqual(len(gitmod.list_worktrees(self.repo.path)), 1)

    def test_copy_files_rejects_path_escape(self):
        with gitmod.worktree(self.repo.path, "main") as wt:
            copied = copy_files_from_ref(self.repo.path, "good", ["../evil.py", "tests/test_calc.py"], wt)
            self.assertEqual(copied, ["tests/test_calc.py"])
            self.assertFalse((wt.parent / "evil.py").exists())

    def test_changed_files_and_binary_detection(self):
        r = self.repo
        r.branch("bin", "main")
        r.commit({"logo.png": b"\x89PNG\x00\x01\x02", "calc.py": None}, "binary + delete")
        files = {f.path: f for f in gitmod.changed_files(r.path, "main", "bin")}
        self.assertTrue(files["logo.png"].binary)
        self.assertEqual(files["calc.py"].status, "D")
        with self.assertRaises(gitmod.GitError):
            gitmod.resolve_ref(r.path, "--upload-pack=evil")


class RunnerConfigTest(unittest.TestCase):
    def test_test_file_patterns(self):
        r = get_runner("pytest")
        self.assertTrue(r.is_test_module("tests/test_x.py"))
        self.assertTrue(r.is_test_module("pkg/x_test.py"))
        self.assertTrue(r.is_test_file("tests/conftest.py"))
        self.assertTrue(r.is_test_file("tests/data/sample.json"))
        self.assertFalse(r.is_test_module("tests/data/sample.json"))
        self.assertFalse(r.is_test_file("src/pkg/core.py"))

    def test_jest_runner(self):
        r = get_runner("jest")
        self.assertTrue(r.is_test_module("src/sum.test.ts"))
        self.assertTrue(r.is_test_file("src/__tests__/helpers.js"))
        self.assertFalse(r.is_test_file("src/sum.ts"))
        cmd = r.build_command(["src/sum.test.ts"], "/tmp/x/report.xml")
        self.assertEqual(cmd[-1], "src/sum.test.ts")
        env = r.env_for(Path("."), "/tmp/x/report.xml")
        self.assertEqual(env["JEST_JUNIT_OUTPUT_NAME"], "report.xml")
        self.assertTrue(r.test_belongs_to("/w/src/sum.test.ts::sum adds", "src/sum.test.ts"))

    def test_unknown_runner(self):
        with self.assertRaises(ValueError):
            get_runner("nose")

    def test_pythonpath_is_prepended_not_replaced(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            wt = Path(d)
            (wt / "src").mkdir()
            script = wt / "write_path.py"
            script.write_text("import os, sys\nopen(sys.argv[2], 'w').write(os.environ['PYTHONPATH'])\n"
                              "open(sys.argv[1], 'w').write('<testsuite/>')\n", encoding="utf-8")
            out = wt / "pp.txt"
            r = PytestRunner(command=[sys.executable, str(script), "{junitxml}", str(out)])
            res = r.run(wt, [], {"PYTHONPATH": "/user/extra", "PATH": "/usr/bin:/bin"}, 30)
            self.assertIsNone(res.error)
            parts = out.read_text().split(os.pathsep)
            self.assertEqual(parts, [str(wt / "src"), str(wt), "/user/extra"])

    def test_placeholders(self):
        r = PytestRunner(command=["python", "-m", "pytest", "--junit-xml={junitxml}", "{tests}", "-x"])
        self.assertEqual(r.build_command(["t/test_a.py"], "/r.xml"),
                         ["python", "-m", "pytest", "--junit-xml=/r.xml", "t/test_a.py", "-x"])


if __name__ == "__main__":
    unittest.main()
