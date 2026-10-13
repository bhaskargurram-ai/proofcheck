"""The cargo runner: libtest output parsing, classification, and end-to-end runs with real cargo.

The end-to-end tests build std-only fixture workspaces (no crates.io access)
and are skipped when cargo is not installed.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import _helpers  # noqa: F401
from _rust_fixtures import SHARED_TARGET, cargo_env, cargo_path, make_rust_repo

from proofcheck import git as gitmod
from proofcheck.cargo import (
    MARKER_NAME,
    CargoRunner,
    crate_info,
    ensure_fresh,
    first_compile_error,
    is_integration_test_module,
    is_integration_test_related,
    is_rust_source,
    module_path,
    parse_libtest,
)
from proofcheck.card import render_card
from proofcheck.check import PRContext, run_check
from proofcheck.config import config_from_dict
from proofcheck.junit import FAILED, PASSED, SKIPPED
from proofcheck.result import validate_result
from proofcheck.runners import get_runner

PRETTY = """
running 5 tests
test tests::bad ... FAILED
test tests::ign ... ignored
test tests::why ... ignored, needs docker
test tests::ok1 ... ok
test tests::sp - should panic ... ok

failures:

---- tests::bad stdout ----
hello out

thread 'tests::bad' (7353) panicked at src/lib.rs:6:47:
assertion `left == right` failed: custom msg
  left: -1
 right: 3
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace


failures:
    tests::bad

test result: FAILED. 2 passed; 1 failed; 2 ignored; 0 measured; 0 filtered out; finished in 0.08s
"""


class ParseTest(unittest.TestCase):
    def test_pretty_output(self):
        run = parse_libtest(PRETTY)
        self.assertTrue(run.started and run.finished)
        self.assertEqual({k: v[0] for k, v in run.results.items()},
                         {"tests::bad": FAILED, "tests::ign": SKIPPED, "tests::why": SKIPPED, "tests::ok1": PASSED,
                          "tests::sp": PASSED})
        outcome, message, detail = run.results["tests::bad"]
        self.assertEqual(message, "assertion `left == right` failed: custom msg left: -1 right: 3 (at src/lib.rs:6:47)")
        self.assertIn("hello out", detail)

    def test_captured_output_cannot_forge_results(self):
        out = ("running 2 tests\ntest a ... FAILED\ntest b ... ok\n\nfailures:\n\n---- a stdout ----\n"
               "running 9 tests\ntest b ... FAILED\ntest forged ... ok\ntest result: ok.\n\nfailures:\n    a\n\n"
               "test result: FAILED. 1 passed; 1 failed; 0 ignored\n")
        run = parse_libtest(out)
        self.assertEqual({k: v[0] for k, v in run.results.items()}, {"a": FAILED, "b": PASSED})

    def test_interleaved_child_output_and_crash(self):
        out = "running 3 tests\ntest a ... child process says hi\nok\ntest b ... ok\ntest c ... "
        run = parse_libtest(out)
        self.assertEqual(run.results["a"][0], PASSED)
        self.assertEqual(run.results["b"][0], PASSED)
        self.assertEqual(run.crashed, "c")
        self.assertFalse(run.finished)

    def test_compile_error_line(self):
        stderr = ("   Compiling mathx v0.1.0\nwarning: unused import\nerror[E0432]: unresolved import `mathx::sub`\n"
                  " --> crates/mathx/tests/sub.rs:1:5\n  |\nerror: could not compile `mathx` (test \"sub\") due to 1 previous error\n")
        self.assertEqual(first_compile_error(stderr),
                         "error[E0432]: unresolved import `mathx::sub` (crates/mathx/tests/sub.rs:1:5)")
        self.assertEqual(first_compile_error("error: package ID specification `x` did not match any packages\n"),
                         "error: package ID specification `x` did not match any packages")


class ClassificationTest(unittest.TestCase):
    def test_paths(self):
        self.assertTrue(is_integration_test_module("crates/foo/tests/cli.rs"))
        self.assertTrue(is_integration_test_module("tests/cli.rs"))
        self.assertTrue(is_integration_test_module("crates/foo/tests/big/main.rs"))
        self.assertFalse(is_integration_test_module("crates/foo/tests/common/mod.rs"))
        self.assertTrue(is_integration_test_related("crates/foo/tests/common/mod.rs"))
        self.assertTrue(is_integration_test_related("crates/foo/tests/fixtures/policy.yaml"))
        self.assertFalse(is_integration_test_related("crates/foo/src/tests/mod.rs"))
        self.assertFalse(is_integration_test_related("crates/foo/src/lib.rs"))
        self.assertTrue(is_rust_source("crates/foo/src/parser/mod.rs"))
        self.assertFalse(is_rust_source("crates/foo/build.rs"))
        self.assertEqual(module_path("lib.rs"), "")
        self.assertEqual(module_path("parser/mod.rs"), "parser")
        self.assertEqual(module_path("a/b.rs"), "a::b")
        r = get_runner("cargo")
        self.assertTrue(r.is_test_file("crates/foo/tests/x.rs"))
        self.assertFalse(r.is_test_file("crates/foo/src/x.rs"))  # inline tests are transplanted, not copied
        self.assertEqual(r.default_timeout, 1800)

    def test_belongs_to(self):
        r = CargoRunner()
        self.assertTrue(r.test_belongs_to("crates/a/tests/x.rs::m::t", "crates/a/tests/x.rs"))
        self.assertTrue(r.test_belongs_to("crates/a/tests/x.rs", "crates/a/tests/x.rs"))  # build-error pseudo test
        self.assertFalse(r.test_belongs_to("crates/a/tests/xy.rs::t", "crates/a/tests/x.rs"))
        self.assertTrue(r.test_belongs_to("a::parse::tests::t", "crates/a/src/parse.rs"))
        self.assertFalse(r.test_belongs_to("a::other::tests::t", "crates/a/src/parse.rs"))
        self.assertTrue(r.test_belongs_to("a::tests::t", "crates/a/src/lib.rs"))

    def test_crate_info(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "c" / "src").mkdir(parents=True)
        (d / "c" / "Cargo.toml").write_text('[package]\nname = "c-cli"\nversion = "0.1.0"\nautotests = false\n\n'
                                             '[[bin]]\nname = "cee"\npath = "src/main.rs"\n\n[[test]]\nname = "e2e"\npath = "tests/end_to_end.rs"\n')
        (d / "c" / "src" / "main.rs").write_text("fn main() {}\n")
        info = crate_info(d, "c")
        self.assertEqual((info.package, info.has_lib, info.bins, info.autotests), ("c-cli", False, ["cee"], False))
        self.assertEqual(info.test_targets, {"tests/end_to_end.rs": "e2e"})
        self.assertIsNone(crate_info(d, "missing"))

    def test_ensure_fresh_touches_only_when_the_worktree_changes(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        for name in ("one", "two"):
            (d / name / ".git").mkdir(parents=True)
            (d / name / "src").mkdir()
            (d / name / "src" / "lib.rs").write_text("")
            os.utime(d / name / "src" / "lib.rs", (1, 1))
        target = d / "target"
        target.mkdir()
        self.assertTrue(ensure_fresh(d / "one", str(target)))
        self.assertGreater((d / "one" / "src" / "lib.rs").stat().st_mtime, 1)
        self.assertEqual((d / "two" / "src" / "lib.rs").stat().st_mtime, 1)
        self.assertFalse(ensure_fresh(d / "one" / "src", str(target)))  # same checkout: no touching
        self.assertTrue(ensure_fresh(d / "two", str(target)))
        self.assertEqual((target / MARKER_NAME).read_text().strip(), str((d / "two").resolve()))


@unittest.skipUnless(cargo_path(), "cargo is not installed")
class CargoEndToEndTest(unittest.TestCase):
    """Real cargo on a std-only 2-crate workspace (see tests/_rust_fixtures.py)."""

    @classmethod
    def setUpClass(cls):
        cls.repo = make_rust_repo()
        cls.env = cargo_env()

    @classmethod
    def tearDownClass(cls):
        cls.repo.cleanup()

    def check(self, branch, title="Fix it", body="Fixes #1", ablation=True, target=None):
        cfg = config_from_dict({"runner": "cargo", "ablation": {"enabled": ablation, "time_budget_s": 600}})
        env = dict(self.env, CARGO_TARGET_DIR=str(target or SHARED_TARGET))  # the inherited target dir is used
        raw = run_check(self.repo.path, "main", branch, cfg, PRContext(title=title, body=body), environ=env)
        result = validate_result(raw)
        render_card(result)  # renders without error
        return result

    def ids_of(self, result):
        return {t["test_id"]: t for t in result["tests"]["tests"]}

    def test_integration_fail_to_pass_with_ablation(self):
        res = self.check("fix")
        self.assertEqual(res["verdict"], "evidence")
        tests = res["tests"]
        self.assertEqual(tests["status"], "evidence")
        self.assertEqual(tests["runner"], "cargo")
        by_id = self.ids_of(res)
        adds = by_id["crates/mathx/tests/arith.rs::adds"]
        self.assertEqual((adds["classification"], adds["base"], adds["head"], adds["touched"]),
                         ("fail_to_pass", "failed", "passed", True))
        self.assertIn("left: -1", adds["message"])
        self.assertFalse(by_id["crates/mathx/tests/arith.rs::multiplies"]["touched"])
        self.assertIn("--test", tests["head_run"]["command"])
        self.assertIn("--test-threads=1", tests["head_run"]["command"])
        # Diff evidence map: the fix is required, the unrelated textx change is not exercised.
        abl = res["ablation"]
        self.assertEqual(abl["status"], "ran")
        status = {h["file"]: h["status"] for h in abl["hunks"]}
        self.assertEqual(status, {"crates/mathx/src/lib.rs": "required", "crates/textx/src/lib.rs": "not_exercised"})
        required = next(h for h in abl["hunks"] if h["status"] == "required")
        self.assertEqual(required["broken_by"], ["crates/mathx/tests/arith.rs::adds"])
        self.assertEqual(res["claims"][0]["status"], "verified")
        card = render_card(res)
        self.assertIn("crates/mathx/tests/arith.rs::adds", card)
        self.assertIn("Diff evidence map", card)

    def test_pass_to_pass(self):
        res = self.check("passpass", ablation=False)
        self.assertEqual(res["tests"]["status"], "no_evidence")
        t = self.ids_of(res)["crates/mathx/tests/arith.rs::multiplies_by_zero"]
        self.assertEqual(t["classification"], "pass_to_pass")
        self.assertEqual(res["claims"][0]["status"], "unverified")

    def test_compile_error_on_base(self):
        res = self.check("newapi")
        t = self.ids_of(res)["crates/mathx/tests/sub.rs::subtracts"]
        self.assertEqual(t["classification"], "fail_to_pass")
        self.assertEqual(t["base_failure_kind"], "build")
        self.assertEqual(t["base"], "build error")
        self.assertTrue(t["message"].startswith("fails on base because it does not compile there: error[E0432]"), t["message"])
        claim = res["claims"][0]
        self.assertEqual(claim["status"], "verified")
        self.assertIn("only because the test does not compile there", claim["evidence"])
        card = render_card(res)
        self.assertIn("do not compile there", card)
        self.assertIn("build error", card)

    def test_failures_on_head(self):
        res = self.check("broken", ablation=False)
        self.assertEqual(res["tests"]["status"], "fail_on_head")
        self.assertEqual(self.ids_of(res)["crates/mathx/tests/arith.rs::adds"]["classification"], "fail_on_head")
        res = self.check("headbuild", ablation=False)
        self.assertEqual(res["tests"]["status"], "fail_on_head")
        t = self.ids_of(res)["crates/mathx/tests/div.rs"]
        self.assertEqual(t["classification"], "fail_on_head")
        self.assertIn("does not compile on head", t["message"])
        self.assertEqual(res["verdict"], "contradicted")

    def test_inline_module_transplant(self):
        res = self.check("inline")
        by_id = self.ids_of(res)
        t = by_id["mathx::parse::tests::parses_padded"]
        self.assertEqual((t["classification"], t["base"], t["head"], t["touched"]), ("fail_to_pass", "failed", "passed", True))
        self.assertEqual(t["file"], "crates/mathx/src/parse.rs")
        self.assertFalse(by_id["mathx::parse::tests::parses_plain_number"]["touched"])
        self.assertEqual(res["tests"]["test_files"], ["crates/mathx/src/parse.rs"])
        self.assertIn("--lib", res["tests"]["base_run"]["command"])
        self.assertIn("parse::tests::", res["tests"]["base_run"]["command"])
        abl = res["ablation"]
        self.assertEqual([(h["file"], h["status"]) for h in abl["hunks"]], [("crates/mathx/src/parse.rs", "required")])
        self.assertIn("1 hunk(s) inside inline test modules are test code and were not ablated.", abl["notes"])
        # adds_tests: the inline file counts as a changed test file
        res = self.check("inline", title="Trim input", body="Adds tests for padded input.", ablation=False)
        self.assertEqual(res["claims"][0]["type"], "adds_tests")
        self.assertEqual(res["claims"][0]["status"], "verified")

    def test_inline_lexer_ambiguity_is_skipped_with_a_note(self):
        res = self.check("ambiguous", ablation=False)
        self.assertEqual(res["tests"]["status"], "no_tests")
        self.assertTrue(any("crates/mathx/src/parse.rs could not be separated from the code" in n
                            and "no fail→pass evidence" in n for n in res["tests"]["notes"]), res["tests"]["notes"])
        self.assertIn("could not be separated", render_card(res))

    def test_shared_target_dir_is_reused_and_safe(self):
        target = Path(tempfile.mkdtemp(prefix="proofcheck-shared-target-"))
        self.addCleanup(shutil.rmtree, target, True)
        # binfix: the test runs the textx binary via CARGO_BIN_EXE_textx, an absolute path inside the
        # shared target dir. If the base run reused the head build (same unit hashes, older mtimes),
        # the test would wrongly pass on base.
        res = self.check("binfix", target=target)
        t = self.ids_of(res)["crates/textx/tests/cli.rs::binary_shouts"]
        self.assertEqual(t["classification"], "fail_to_pass")
        self.assertEqual([h["status"] for h in res["ablation"]["hunks"]], ["required"])
        self.assertTrue((target / MARKER_NAME).is_file())
        cli_bins = [p for p in (target / "debug" / "deps").iterdir() if p.name.startswith("cli-") and os.access(p, os.X_OK)
                    and p.suffix == ""]
        self.assertGreaterEqual(len(cli_bins), 1)
        # A second run reuses the target dir: nothing is re-created from scratch.
        before = {p.name for p in (target / "debug" / "deps").iterdir()}
        self.check("binfix", ablation=False, target=target)
        after = {p.name for p in (target / "debug" / "deps").iterdir()}
        self.assertTrue(before <= after)

    def test_run_selected_runs_exact_names(self):
        runner = CargoRunner(target_dir=str(SHARED_TARGET))
        with gitmod.worktree(self.repo.path, "fix") as wt:
            r = runner.run_selected(wt, ["crates/mathx/tests/arith.rs"], ["crates/mathx/tests/arith.rs::adds"],
                                    self.env, 600)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(list(r.outcomes), ["crates/mathx/tests/arith.rs::adds"])
        self.assertIn("--exact", r.command)

    def test_missing_cargo_is_an_error_not_a_crash(self):
        runner = CargoRunner(command=["definitely-not-cargo-xyz", "test"], target_dir=str(SHARED_TARGET))
        with gitmod.worktree(self.repo.path, "fix") as wt:
            r = runner.run(wt, ["crates/mathx/tests/arith.rs"], self.env, 60)
        self.assertFalse(r.ok)
        self.assertIn("could not start", r.error)


if __name__ == "__main__":
    unittest.main()
