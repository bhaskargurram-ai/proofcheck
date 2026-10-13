"""Multi-project repositories: [[project]] config, routing, two-project runs, init detection."""

import importlib.util
import json
import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

import _helpers
from _helpers import BASE_TESTS, BUGGY, WEAK_TESTS, GitRepo
from _rust_fixtures import SHARED_TARGET, base_files, cargo_env, cargo_path, MATHX_LIB_FIXED, ARITH_TESTS_WITH_ADD, PARSE_RS_FIXED

from click.testing import CliRunner

from proofcheck import cli
from proofcheck.card import render_card
from proofcheck.check import PRContext, run_check
from proofcheck.config import ConfigError, config_from_dict, parse_config_text
from proofcheck.init_cmd import detect_projects, init_repo
from proofcheck.projects import ProjectSet, build_projects, project_for, split_test_id
from proofcheck.result import ResultValidationError, validate_result


class ProjectConfigTest(unittest.TestCase):
    def test_tables_and_defaults(self):
        cfg = parse_config_text(textwrap.dedent("""
            runner = "unittest"
            timeout = 300

            [[project]]
            name = "python-sdk"
            path = "./adapters/python/"
            runner = "pytest"
            test_paths = ["tests/*"]
            test_env = { SDK = "1" }

            [[project]]
            name = "rust"
            path = "rust"
            runner = "cargo"
            setup_command = "cargo fetch"
        """), "proofcheck.toml")
        self.assertEqual([(p.name, p.path, p.runner) for p in cfg.projects],
                         [("python-sdk", "adapters/python", "pytest"), ("rust", "rust", "cargo")])
        self.assertEqual(cfg.projects[0].test_patterns, ["tests/*"])
        self.assertEqual(cfg.projects[1].setup_command, ["cargo", "fetch"])
        projects = build_projects(cfg)
        self.assertEqual([p.name for p in projects], ["root", "python-sdk", "rust"])
        self.assertEqual(projects[0].runner.name, "unittest")
        self.assertEqual([p.timeout for p in projects], [300, 300, 300])  # top-level timeout is explicit
        self.assertEqual(projects[1].env({"A": "b"}), {"A": "b", "SDK": "1"})
        self.assertIn("projects", cfg.to_dict())

    def test_cargo_timeout_default_and_root_project(self):
        cfg = config_from_dict({"project": [{"name": "rust", "path": ".", "runner": "cargo"},
                                            {"name": "py", "path": "py"}]})
        projects = build_projects(cfg, repo="/repo")
        self.assertEqual([p.name for p in projects], ["rust", "py"])  # no implicit root: one table is at "."
        self.assertEqual(projects[0].timeout, 1800)
        self.assertEqual(projects[1].timeout, 600)
        self.assertEqual(build_projects(config_from_dict({"runner": "cargo"}))[0].timeout, 1800)
        self.assertEqual(build_projects(config_from_dict({"runner": "cargo", "timeout": 60}))[0].timeout, 60)
        cfg = config_from_dict({"runner": "cargo", "cargo_target_dir": "build/tgt"})
        self.assertEqual(build_projects(cfg, repo="/repo")[0].runner.target_dir, "/repo/build/tgt")
        # Single-project configurations are unchanged.
        self.assertEqual(config_from_dict({}).projects, [])

    def test_invalid_tables(self):
        bad = [
            ({"project": {"name": "a", "path": "a"}}, "array of tables"),
            ({"project": [{"path": "a"}]}, "name"),
            ({"project": [{"name": "a b", "path": "a"}]}, "name"),
            ({"project": [{"name": "a"}]}, "path is required"),
            ({"project": [{"name": "a", "path": "../x"}]}, ".."),
            ({"project": [{"name": "a", "path": "/abs"}]}, "relative"),
            ({"project": [{"name": "a", "path": "a\\b"}]}, "relative"),
            ({"project": [{"name": "a", "path": "a", "runner": "nose"}]}, "runner"),
            ({"project": [{"name": "a", "path": "a", "cmd": "x"}]}, "unknown"),
            ({"project": [{"name": "a", "path": "a"}, {"name": "a", "path": "b"}]}, "same name"),
            ({"project": [{"name": "a", "path": "x/"}, {"name": "b", "path": "./x"}]}, "same path"),
            ({"project": [{"name": "root", "path": "x"}]}, "reserved"),
            ({"runner": "pytest", "project": [{"name": "r", "path": "."}]}, "move them"),
            ({"project": [{"name": "a", "path": "a", "test_paths": ["t"], "test_patterns": ["t"]}]}, "not both"),
            ({"project": [{"name": "a", "path": "a", "timeout": 0}]}, ">="),
        ]
        for data, msg in bad:
            with self.subTest(data=data), self.assertRaisesRegex(ConfigError, msg):
                config_from_dict(data)


class RoutingTest(unittest.TestCase):
    def setUp(self):
        cfg = config_from_dict({"runner": "unittest", "project": [
            {"name": "sdk", "path": "adapters/python", "runner": "pytest"},
            {"name": "inner", "path": "adapters/python/plugins/x"},
            {"name": "rust", "path": "rust", "runner": "cargo"}]})
        self.projects = build_projects(cfg)
        self.view = ProjectSet(self.projects)

    def test_deepest_project_wins(self):
        name = lambda p: project_for(self.projects, p).name  # noqa: E731
        self.assertEqual(name("README.md"), "root")
        self.assertEqual(name("adapters/python/tests/test_a.py"), "sdk")
        self.assertEqual(name("adapters/python/plugins/x/tests/test_b.py"), "inner")
        self.assertEqual(name("adapters/pythonic/tests/test_c.py"), "root")  # prefix of a name is not containment
        self.assertEqual(name("rust/crates/a/tests/t.rs"), "rust")

    def test_classification_uses_project_relative_paths(self):
        self.assertTrue(self.view.is_test_file("adapters/python/tests/conftest.py"))
        self.assertTrue(self.view.is_test_module("adapters/python/tests/test_client.py"))
        self.assertTrue(self.view.is_test_file("rust/crates/a/tests/t.rs"))
        self.assertFalse(self.view.is_test_file("rust/crates/a/src/lib.rs"))
        self.assertTrue(self.view.is_test_file("tests/data.json"))  # root project (unittest): its tests/ dir
        self.assertFalse(self.view.is_test_file("calc.py"))
        self.assertEqual(self.view.name, "cargo,pytest,unittest")

    def test_test_ids(self):
        p, local = split_test_id("sdk:tests.test_client::test_x", self.projects)
        self.assertEqual((p.name, local), ("sdk", "tests.test_client::test_x"))
        self.assertEqual(split_test_id("nope:x::y", self.projects), (None, "nope:x::y"))
        self.assertTrue(self.view.test_belongs_to("sdk:tests.test_client::test_x", "adapters/python/tests/test_client.py"))
        self.assertFalse(self.view.test_belongs_to("root:tests.test_client::test_x", "adapters/python/tests/test_client.py"))


def write_body(d: Path, text: str) -> str:
    p = d / "body.md"
    p.write_text(text)
    return str(p)


SDK_CLIENT = 'def greet(name):\n    return "hello " + name\n'
SDK_CLIENT_FIXED = 'def greet(name):\n    return "Hello, " + name\n'
SDK_PYPROJECT = '[project]\nname = "sdkpkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
SDK_CONFTEST = 'import pytest\n\n\n@pytest.fixture\ndef name():\n    return "Ada"\n'
SDK_TEST_PYTEST = 'from sdkpkg.client import greet\n\n\ndef test_greet(name):\n    assert greet(name) == "Hello, Ada"\n'
SDK_TEST_UNITTEST = '''import unittest

from sdkpkg.client import greet


class GreetTest(unittest.TestCase):
    def test_greet(self):
        self.assertEqual(greet("Ada"), "Hello, Ada")
'''


def make_two_project_repo(sdk_runner: str) -> GitRepo:
    """Root: unittest project (calc). ``sdk/``: src-layout package with a conftest.py.

    Branch ``pr`` fixes the sdk bug with a fail->pass test, adds a root test that also passes on
    base, and changes calc.py in a way no test depends on.
    """
    r = GitRepo()
    config = f'runner = "unittest"\n\n[ablation]\ntime_budget_s = 120\n\n[[project]]\nname = "sdk"\npath = "sdk"\nrunner = "{sdk_runner}"\n'
    if sdk_runner == "pytest":
        config += 'test_command = "python -m pytest -p no:cacheprovider -q"\n'
    r.commit({
        "proofcheck.toml": config,
        "calc.py": BUGGY,
        "tests/test_calc.py": BASE_TESTS,
        "sdk/pyproject.toml": SDK_PYPROJECT,
        "sdk/src/sdkpkg/__init__.py": "",
        "sdk/src/sdkpkg/client.py": SDK_CLIENT,
        "sdk/tests/conftest.py": SDK_CONFTEST if sdk_runner == "pytest" else "",
    }, "base")
    r.branch("pr")
    r.commit({
        "calc.py": BUGGY.replace("return a * b", "return b * a"),
        "tests/test_calc.py": WEAK_TESTS,
        "sdk/src/sdkpkg/client.py": SDK_CLIENT_FIXED,
        "sdk/tests/test_client.py": SDK_TEST_PYTEST if sdk_runner == "pytest" else SDK_TEST_UNITTEST,
    }, "fix greeting")
    r.checkout("main")
    return r


class TwoProjectRunTest(unittest.TestCase):
    """`proofcheck run` on a repository with a root project and an sdk/ subproject."""

    def run_cli(self, sdk_runner):
        repo = make_two_project_repo(sdk_runner)
        self.addCleanup(repo.cleanup)
        out = repo.tmp
        res = CliRunner().invoke(cli.main, [
            "run", "--repo", str(repo.path), "--base", "main", "--head", "pr",
            "--pr-body-file", write_body(out, "# Fix the SDK greeting\n\nFixes #3. Adds tests."),
            "--json", str(out / "result.json"), "--out", str(out / "card.md"),
        ], catch_exceptions=False)
        self.assertEqual(res.exit_code, 0, res.output)
        result = validate_result(json.loads((out / "result.json").read_text()))
        return result, (out / "card.md").read_text()

    def check_result(self, result, card, sdk_id):
        tests = result["tests"]
        self.assertEqual(tests["status"], "evidence")
        self.assertEqual(tests["runner"], "unittest" if sdk_id.startswith("sdk:tests.test_client.") else "pytest,unittest")
        projects = {p["name"]: p for p in tests["projects"]}
        self.assertEqual(set(projects), {"root", "sdk"})
        self.assertEqual((projects["root"]["path"], projects["root"]["status"]), (".", "no_evidence"))
        self.assertEqual((projects["sdk"]["path"], projects["sdk"]["status"]), ("sdk", "evidence"))
        self.assertEqual(projects["sdk"]["counts"]["fail_to_pass"], 1)
        by_id = {t["test_id"]: t for t in tests["tests"]}
        self.assertEqual(by_id[sdk_id]["classification"], "fail_to_pass")
        self.assertEqual(by_id[sdk_id]["file"], "sdk/tests/test_client.py")
        self.assertEqual(by_id["root:tests.test_calc.MulTest::test_mul_zero"]["classification"], "pass_to_pass")
        self.assertFalse(by_id["root:tests.test_calc.MulTest::test_mul"]["touched"])
        self.assertIn("sdk/tests/test_client.py", tests["test_files"])
        self.assertTrue(projects["sdk"]["head_run"]["command"])
        self.assertEqual(result["verdict"], "evidence")
        fixes = next(c for c in result["claims"] if c["type"] == "fixes_issue")
        self.assertEqual(fixes["status"], "verified")
        # The diff evidence map runs the sdk test inside sdk/ and keeps the prefixed id.
        abl = result["ablation"]
        self.assertEqual(abl["status"], "ran", abl)
        status = {h["file"]: (h["status"], h["broken_by"]) for h in abl["hunks"]}
        self.assertEqual(status["sdk/src/sdkpkg/client.py"], ("required", [sdk_id]))
        self.assertEqual(status["calc.py"][0], "not_exercised")
        # The card shows the projects and unambiguous ids.
        self.assertIn("Projects:", card)
        self.assertIn("`sdk` (", card)
        self.assertIn(sdk_id, card)
        self.assertEqual(card, render_card(result))

    def test_root_unittest_and_subfolder_src_layout(self):
        result, card = self.run_cli("unittest")
        self.check_result(result, card, "sdk:tests.test_client.GreetTest::test_greet")

    @unittest.skipUnless(importlib.util.find_spec("pytest"), "pytest is not installed")
    def test_subfolder_pytest_project_with_conftest(self):
        result, card = self.run_cli("pytest")
        self.check_result(result, card, "sdk:tests.test_client::test_greet")

    def test_validator_rejects_bad_project_entries(self):
        base = {"schema": "proofcheck/result", "version": 1, "base": "a" * 40, "head": "b" * 40, "verdict": "evidence"}
        ok = dict(base, tests={"status": "evidence", "projects": [{"name": "sdk", "path": "sdk", "runner": "pytest",
                                                                    "status": "evidence", "counts": {"fail_to_pass": 1}}]})
        self.assertEqual(validate_result(ok)["tests"]["projects"][0]["counts"]["fail_to_pass"], 1)
        self.assertNotIn("projects", validate_result(dict(base, tests={"status": "no_tests"}))["tests"])
        for bad in ({"name": "<b>", "runner": "pytest", "status": "evidence"},
                    {"name": "x", "runner": "make", "status": "evidence"},
                    {"name": "x", "runner": "pytest", "status": "great"}):
            with self.subTest(bad=bad), self.assertRaises(ResultValidationError):
                validate_result(dict(base, tests={"status": "evidence", "projects": [bad]}))
        with self.assertRaises(ResultValidationError):
            validate_result(dict(base, tests={"status": "evidence", "tests": [
                {"test_id": "t", "classification": "fail_to_pass", "base_failure_kind": "magic"}]}))


@unittest.skipUnless(cargo_path(), "cargo is not installed")
class CargoSubprojectTest(unittest.TestCase):
    """A cargo workspace in rust/ next to a root unittest project; integration + inline tests."""

    def test_cargo_project_in_a_subdirectory(self):
        r = GitRepo()
        self.addCleanup(r.cleanup)
        files = {f"rust/{k}": v for k, v in base_files().items()}
        r.commit({**files, "calc.py": BUGGY, "tests/test_calc.py": BASE_TESTS}, "base")
        r.branch("pr")
        r.commit({
            "rust/crates/mathx/src/lib.rs": MATHX_LIB_FIXED,
            "rust/crates/mathx/tests/arith.rs": ARITH_TESTS_WITH_ADD,
            "rust/crates/mathx/src/parse.rs": PARSE_RS_FIXED,
            "calc.py": BUGGY.replace("return a * b", "return b * a"),
        }, "fix")
        cfg = config_from_dict({"runner": "unittest", "ablation": {"time_budget_s": 600}, "project": [
            {"name": "rust", "path": "rust", "runner": "cargo", "cargo_target_dir": str(SHARED_TARGET)}]})
        result = validate_result(run_check(r.path, "main", "pr", cfg, PRContext(title="Fix add", body="Fixes #1"),
                                           environ=cargo_env()))
        tests = result["tests"]
        by_id = {t["test_id"]: t for t in tests["tests"]}
        self.assertEqual(by_id["rust:crates/mathx/tests/arith.rs::adds"]["classification"], "fail_to_pass")
        self.assertEqual(by_id["rust:crates/mathx/tests/arith.rs::adds"]["file"], "rust/crates/mathx/tests/arith.rs")
        self.assertEqual(by_id["rust:mathx::parse::tests::parses_padded"]["classification"], "fail_to_pass")
        self.assertEqual(sorted(tests["test_files"]), ["rust/crates/mathx/src/parse.rs", "rust/crates/mathx/tests/arith.rs"])
        self.assertEqual({p["name"]: p["status"] for p in tests["projects"]}, {"root": "no_tests", "rust": "evidence"})
        abl = result["ablation"]
        status = {h["file"]: (h["status"], h["broken_by"]) for h in abl["hunks"]}
        self.assertEqual(status["rust/crates/mathx/src/lib.rs"], ("required", ["rust:crates/mathx/tests/arith.rs::adds"]))
        self.assertEqual(status["rust/crates/mathx/src/parse.rs"], ("required", ["rust:mathx::parse::tests::parses_padded"]))
        self.assertEqual(status["calc.py"][0], "not_exercised")
        self.assertIn("1 hunk(s) inside inline test modules are test code and were not ablated.", abl["notes"])


def provio_like_tree(root: Path) -> None:
    """A minimal skeleton of a Rust workspace + Python SDK + TypeScript adapter repository."""
    files = {
        "Cargo.toml": '[workspace]\nresolver = "2"\nmembers = ["crates/core", "crates/cli"]\n',
        "rust-toolchain.toml": '[toolchain]\nchannel = "stable"\n',
        "crates/core/Cargo.toml": '[package]\nname = "x-core"\nversion = "0.1.0"\n',
        "crates/core/src/lib.rs": "#[cfg(test)]\nmod tests {}\n",
        "crates/cli/Cargo.toml": '[package]\nname = "x-cli"\nversion = "0.1.0"\n',
        "crates/cli/pyproject.toml": '[build-system]\nrequires = ["maturin"]\nbuild-backend = "maturin"\n',
        "crates/cli/tests/cli.rs": "#[test]\nfn t() {}\n",
        "crates/bench/Cargo.toml": '[package]\nname = "x-bench"\nversion = "0.1.0"\n',
        "fuzz/Cargo.toml": '[package]\nname = "x-fuzz"\nversion = "0.0.0"\n',
        "adapters/python/pyproject.toml": '[project]\nname = "x-sdk"\n\n[project.optional-dependencies]\ndev = ["pytest>=8"]\n'
                                          '\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "adapters/python/src/x_sdk/__init__.py": "",
        "adapters/python/tests/conftest.py": "",
        "adapters/python/tests/test_client.py": "def test_x():\n    pass\n",
        "adapters/typescript/package.json": json.dumps({"name": "x", "scripts": {"test": "node --test build-test/"}}),
        "packaging/legacy/sdk/pyproject.toml": '[project]\nname = "legacy"\n',
        "packaging/npm/cli/package.json": json.dumps({"name": "y", "scripts": {"test": "jest"}}),
        "examples/demo/pyproject.toml": '[tool.pytest.ini_options]\n',
        "docs/site/package.json": json.dumps({"scripts": {"test": "jest"}}),
        "site/package.json": json.dumps({"name": "site", "scripts": {"build": "vite build"}}),
        "tools/lint/setup.cfg": "[metadata]\nname = lint\n",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


class InitProjectsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="proofcheck-init-projects-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_detects_workspace_root_and_python_sdk(self):
        provio_like_tree(self.root)
        projects, skipped = detect_projects(self.root)
        self.assertEqual([(p.name, p.path, p.runner) for p in projects],
                         [("rust", ".", "cargo"), ("python", "adapters/python", "pytest")])
        skipped_paths = dict(skipped)
        self.assertIn("adapters/typescript/package.json", skipped_paths)
        self.assertIn("does not use jest", skipped_paths["adapters/typescript/package.json"])
        self.assertEqual(skipped_paths["crates/cli/pyproject.toml"], "Python packaging of a Rust crate")
        self.assertEqual(skipped_paths["tools/lint/setup.cfg"], "no tests found")
        for p in skipped_paths:  # skipped directories are never looked at
            self.assertFalse(p.startswith(("packaging/", "examples/", "docs/")), p)

    def test_init_writes_project_tables_that_load(self):
        provio_like_tree(self.root)
        res = CliRunner().invoke(cli.main, ["init", "--repo", str(self.root)], catch_exceptions=False)
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("Detected 2 test projects", res.output)
        self.assertIn("rust: . (cargo; Cargo.toml found)", res.output)
        self.assertIn("python: adapters/python (pytest;", res.output)
        self.assertIn("skipped adapters/typescript/package.json", res.output)
        from proofcheck.config import load_config_file

        cfg = load_config_file(self.root / "proofcheck.toml")
        self.assertEqual([(p.name, p.path, p.runner) for p in cfg.projects],
                         [("rust", ".", "cargo"), ("python", "adapters/python", "pytest")])
        self.assertEqual(cfg.ablation.time_budget_s, 1200)
        self.assertEqual([p.name for p in build_projects(cfg)], ["rust", "python"])
        wf = (self.root / ".github/workflows/proofcheck.yml").read_text()
        self.assertIn("timeout-minutes: 90", wf)
        self.assertIn("""install-command: 'pip install -e "./adapters/python[dev]" pytest || pip install pytest'""", wf)
        try:
            import yaml
        except ImportError:  # pragma: no cover
            return
        self.assertEqual(yaml.safe_load(wf)["jobs"]["evidence"]["with"]["timeout-minutes"], 90)

    def test_single_project_output_is_unchanged(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_a.py").write_text("")
        (self.root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
        res = init_repo(self.root)
        self.assertEqual((res.runner, res.projects), ("pytest", []))
        text = (self.root / "proofcheck.toml").read_text()
        self.assertNotIn("[[project]]", text)
        self.assertIn('runner = "pytest"', text)

    def test_python_root_with_cargo_subfolder(self):
        (self.root / "pyproject.toml").write_text('[tool.pytest.ini_options]\n')
        (self.root / "native" / "crates" / "a").mkdir(parents=True)
        (self.root / "native" / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/a"]\n')
        (self.root / "native" / "crates" / "a" / "Cargo.toml").write_text('[package]\nname = "a"\n')
        res = init_repo(self.root)
        self.assertEqual([(p.name, p.path, p.runner) for p in res.projects],
                         [("python", ".", "pytest"), ("native", "native", "cargo")])
        cfg = parse_config_text((self.root / "proofcheck.toml").read_text(), "proofcheck.toml")
        self.assertEqual(len(cfg.projects), 2)
        self.assertEqual(cfg.runner, "pytest")  # the top-level runner line is commented out

    def test_rust_only_repository(self):
        (self.root / "Cargo.toml").write_text('[package]\nname = "solo"\nversion = "0.1.0"\n')
        res = init_repo(self.root)
        self.assertEqual((res.runner, res.projects), ("cargo", []))
        text = (self.root / "proofcheck.toml").read_text()
        self.assertIn('runner = "cargo"', text)
        wf = (self.root / ".github/workflows/proofcheck.yml").read_text()
        self.assertIn("install-command: ''", wf)
        self.assertIn("timeout-minutes: 90", wf)


if __name__ == "__main__":
    unittest.main()
