"""End-to-end tests of the fail-to-pass check with real git worktrees."""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import _helpers  # noqa: F401  (sets up sys.path)
from _helpers import FAKE_PYTEST, RepoTestCase, scrubbed_env

from proofcheck import git as gitmod
from proofcheck.check import PRContext, run_check
from proofcheck.config import load_config_from_ref
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
from proofcheck.runners import PytestRunner, UnittestRunner, VitestRunner, get_runner
from proofcheck.projects import _npm_ci_command


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


class VitestEndToEndTest(unittest.TestCase):
    """Opt-in integration test for real worktree setup and Vitest execution."""

    def test_real_vitest_fix_is_fail_to_pass(self):
        if os.environ.get("PROOFCHECK_VITEST_E2E") != "1":
            self.skipTest("set PROOFCHECK_VITEST_E2E=1 to run the npm/Vitest integration test")
        if not shutil.which("node") or not shutil.which("npm"):
            self.skipTest("Node.js and npm are required for the Vitest integration test")

        npm_command, npm_error = _npm_ci_command()
        if npm_error:
            self.fail(npm_error)

        from _helpers import GitRepo

        repo = GitRepo()
        self.addCleanup(repo.cleanup)
        package = {
            "private": True,
            "scripts": {"test": "vitest run"},
            "devDependencies": {"vitest": "2.1.9"},
        }
        config = """runner = "vitest"
setup_command = "npm ci"
timeout = 180

[ablation]
enabled = false
"""
        test_source = """import { describe, expect, it } from "vitest";
import { add } from "../src/math.js";

describe("math", () => {
  it("adds two numbers", () => {
    expect(add(2, 3)).toBe(5);
  });
});
"""
        repo.write({
            "package.json": json.dumps(package, indent=2) + "\n",
            "proofcheck.toml": config,
            "src/math.js": "export function add(a, b) { return a - b; }\n",
            "tests/math.test.js": test_source,
        })
        lock_command = [*npm_command[:2], "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund"]
        lock_result = subprocess.run(
            lock_command,
            cwd=repo.path,
            env=scrubbed_env(),
            capture_output=True,
            text=True,
            timeout=180,
            shell=False,
        )
        if lock_result.returncode != 0:
            self.fail(f"could not create the committed package-lock.json:\n{lock_result.stdout}\n{lock_result.stderr}")
        self.assertTrue((repo.path / "package-lock.json").is_file())
        self.assertFalse((repo.path / "node_modules").exists())
        base_sha = repo.commit({}, "base")

        repo.branch("head", start=base_sha)
        head_sha = repo.commit({
            "src/math.js": "export function add(a, b) { return a + b; }\n",
            "tests/math.test.js": "// The test remains logically identical across the fix.\n" + test_source,
        }, "fix addition")

        base_ref = "refs/heads/main"
        head_ref = "refs/heads/head"
        self.assertEqual(_helpers.git(repo.path, "rev-parse", base_ref), base_sha)
        self.assertEqual(_helpers.git(repo.path, "rev-parse", head_ref), head_sha)
        self.assertNotEqual(_helpers.git(repo.path, "rev-parse", base_ref), _helpers.git(repo.path, "rev-parse", head_ref))
        changed_files = _helpers.git(repo.path, "diff", "--name-only", f"{base_ref}..{head_ref}").splitlines()
        self.assertIn("src/math.js", changed_files)
        self.assertIn("tests/math.test.js", changed_files)
        for ref in (base_ref, head_ref):
            self.assertEqual(_helpers.git(repo.path, "show", f"{ref}:package-lock.json"), _helpers.git(
                repo.path, "show", f"{base_ref}:package-lock.json",
            ))
            self.assertEqual(_helpers.git(repo.path, "show", f"{ref}:proofcheck.toml"), config.rstrip())

        repo.checkout(base_ref)
        cfg = load_config_from_ref(repo.path, base_ref)
        self.assertEqual(cfg.runner, "vitest")
        self.assertEqual(cfg.setup_command, ["npm", "ci"])
        self.assertEqual(cfg.timeout, 180)
        cfg.ablation.enabled = False
        result = run_check(
            repo.path,
            base_sha,
            head_ref,
            cfg,
            PRContext(title="Fix addition", body=""),
            environ=scrubbed_env(),
        )

        self.assertEqual(result["tests"]["status"], "evidence", result)
        self.assertEqual(result["tests"]["runner"], "vitest")
        matching = [test for test in result["tests"]["tests"] if test["touched"]]
        self.assertEqual(len(matching), 1, result["tests"])
        self.assertEqual(matching[0]["classification"], "fail_to_pass")
        self.assertEqual(matching[0]["test_id"], "tests/math.test.js::math > adds two numbers")
        self.assertEqual(matching[0]["file"], "tests/math.test.js")
        self.assertEqual(result["tests"]["base_run"]["returncode"], 1)
        self.assertEqual(result["tests"]["head_run"]["returncode"], 0)
        self.assertFalse((repo.path / "node_modules").exists())
        self.assertFalse(result["notes"], result["notes"])


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

    def test_vitest_runner(self):
        r = get_runner("vitest")
        self.assertIsInstance(r, VitestRunner)
        for path in ("src/sum.test.js", "src/sum.test.ts", "src/sum.spec.jsx", "src/sum.spec.tsx"):
            self.assertTrue(r.is_test_module(path))
        self.assertFalse(r.is_test_module("src/sum.js"))
        self.assertFalse(r.is_test_module("src/sum.ts"))

        cmd = r.build_command(["tests/math.test.js"], "/tmp/x/report.xml")
        self.assertEqual(cmd, [
            "node", "node_modules/vitest/vitest.mjs", "run", "--reporter=junit",
            "--outputFile=/tmp/x/report.xml", "tests/math.test.js",
        ])
        self.assertNotIn("jest-junit", " ".join(cmd))
        self.assertEqual(r.env_for(Path("."), "/tmp/x/report.xml"), {})

        self.assertTrue(r.test_belongs_to(
            "tests/math.test.js::math operations > adds two numbers",
            "tests/math.test.js",
        ))
        self.assertTrue(r.test_belongs_to(
            r".\tests\math.test.js::math operations > adds two numbers",
            "./tests/math.test.js",
        ))
        self.assertFalse(r.test_belongs_to(
            "other/tests/math.test.js::math operations > adds two numbers",
            "tests/math.test.js",
        ))
        self.assertFalse(r.test_belongs_to(
            "tests/math.test.js.bak::math operations > adds two numbers",
            "tests/math.test.js",
        ))

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
            parts = out.read_text(encoding="utf-8").split(os.pathsep)
            self.assertEqual(parts, [str(wt / "src"), str(wt), "/user/extra"])

    def test_placeholders(self):
        r = PytestRunner(command=["python", "-m", "pytest", "--junit-xml={junitxml}", "{tests}", "-x"])
        self.assertEqual(r.build_command(["t/test_a.py"], "/r.xml"),
                         ["python", "-m", "pytest", "--junit-xml=/r.xml", "t/test_a.py", "-x"])


if __name__ == "__main__":
    unittest.main()
