"""Agent support for Rust and multi-project repositories.

* static checks and path repair for Rust candidates (no cargo needed);
* target selection: project of the main source change, crate of the most
  changed ``.rs`` lines, library names, re-validation in verify;
* end to end with a scripted LLM on real std-only cargo workspaces
  (``CARGO_NET_OFFLINE=true``; skipped without cargo): a strong fail->pass
  candidate, one that only fails to compile on base (weak), a pass->pass one,
  one that fails on head, and submissions rejected by the denylist or
  repaired (``use crate::``, hyphenated package names);
* a two-project repository (cargo root + Python ``sdk/``) whose PR fixes the
  Python SDK: the agent writes and runs its test inside ``sdk/``.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _helpers  # noqa: F401  (sets up sys.path)
from _agent_fakes import ScriptedTransport, unittest_runner
from _helpers import GitRepo, scrubbed_env
from _rust_fixtures import SHARED_TARGET, TEXTX_LIB, cargo_env, cargo_path, crate_toml
from click.testing import CliRunner

from proofcheck import cli
from proofcheck import git as gitmod
from proofcheck.agent import FAIL_TO_PASS, FAIL_TO_PASS_WEAK, FAILS_ON_HEAD, INVALID, PASS_TO_PASS, VARIANTS
from proofcheck.agent.config import AgentConfig, parse_agent_config
from proofcheck.agent.llm_tools import ToolCall, ToolLLM
from proofcheck.agent.propose import propose, write_proposal
from proofcheck.agent.prompts import feedback_message
from proofcheck.agent.ruststatic import RustTarget, validate_rust_test
from proofcheck.agent.schema import ManifestError, load_manifest, validate_candidate_path
from proofcheck.agent.target import (
    TargetError,
    check_manifest_target,
    crates_at,
    parse_crate,
    select_target,
)
from proofcheck.agent.verify import merge_into_result, verify_candidates_dir
from proofcheck.card import render_card
from proofcheck.config import config_from_dict
from proofcheck.projects import build_projects
from proofcheck.result import validate_result

# --------------------------------------------------------------------------- fixtures

WORKSPACE = '[workspace]\nresolver = "2"\nmembers = ["crates/core-math", "crates/textx"]\n'

CORE_LIB = """//! Arithmetic helpers.

pub mod stats;

/// Adds two numbers.
pub fn add(a: i64, b: i64) -> i64 {
    a - b
}

/// Multiplies two numbers.
pub fn mul(a: i64, b: i64) -> i64 {
    a * b
}

fn private_helper() -> i64 {
    41
}

#[cfg(test)]
mod tests {
    #[test]
    fn helper() {
        assert_eq!(super::private_helper(), 41);
    }
}
"""

CORE_LIB_FIXED = CORE_LIB.replace("    a - b\n", "    a + b\n").replace(
    "/// Multiplies two numbers.",
    "/// Sums a slice.\npub fn add_all(xs: &[i64]) -> i64 {\n    xs.iter().fold(0, |acc, x| add(acc, *x))\n}\n\n"
    "/// Multiplies two numbers.")

STATS_RS = "/// Mean of a slice.\npub fn mean(xs: &[i64]) -> Option<i64> {\n    if xs.is_empty() { None } else { Some(xs.iter().sum::<i64>() / xs.len() as i64) }\n}\n"

ARITH = "use core_math::mul;\n\n#[test]\nfn multiplies() {\n    assert_eq!(mul(2, 3), 6);\n}\n"

ISSUE = "add() subtracts.\n\n```rust\nassert_eq!(core_math::add(2, 3), 5); // gives -1\n```\n"
PR_BODY = "Fix add\n\nFixes #12. Also adds add_all()."


def rust_files() -> dict:
    return {
        "Cargo.toml": WORKSPACE,
        ".gitignore": "target/\n",
        "crates/core-math/Cargo.toml": crate_toml("core-math"),
        "crates/core-math/src/lib.rs": CORE_LIB,
        "crates/core-math/src/stats.rs": STATS_RS,
        "crates/core-math/tests/arith.rs": ARITH,
        "crates/textx/Cargo.toml": crate_toml("textx"),
        "crates/textx/src/lib.rs": TEXTX_LIB,
    }


def make_agent_rust_repo() -> GitRepo:
    """main: ``core_math::add`` subtracts. Branch ``fix``: fixes it, adds ``add_all`` and touches textx (no test)."""
    r = GitRepo()
    r.commit(rust_files(), "base")
    r.branch("fix")
    r.commit({"crates/core-math/src/lib.rs": CORE_LIB_FIXED,
              "crates/textx/src/lib.rs": TEXTX_LIB.replace('"{}!"', '"{}!" ')}, "fix add")
    r.checkout("main")
    return r


def rs(body: str, uses: str = "use core_math::add;") -> str:
    return f"{uses}\n\n{body}"


STRONG_VIA_CRATE = rs("#[test]\nfn adds_two_numbers() {\n    assert_eq!(add(2, 3), 5);\n}\n", uses="use crate::add;")
DENIED = rs("#[test]\nfn adds() {\n    let out = std::process::Command::new(\"cargo\").output().unwrap();\n"
            "    assert!(out.status.success());\n    assert_eq!(add(2, 3), 5);\n}\n")
WEAK_HYPHEN = rs("#[test]\nfn sums() {\n    assert_eq!(core-math::add_all(&[1, 2, 3]), 6);\n}\n", uses="use core-math::add_all;")
PASS_BOTH = rs("#[test]\nfn multiplies() {\n    assert_eq!(core_math::mul(4, 5), 20);\n}\n")
FAILS_HEAD = rs("#[test]\nfn wrong_expectation() {\n    assert_eq!(add(2, 3), 6);\n}\n")


def submit(code: str, name: str = "issue_12.rs") -> tuple:
    return ("submit_test", {"filename": name, "code": code, "rationale": "add() subtracts instead of adding.",
                            "targets_claim": "fixes #12"})


def cargo_projects(cfg_dict=None):
    return build_projects(config_from_dict(dict({"runner": "cargo", "cargo_target_dir": str(SHARED_TARGET)},
                                                **(cfg_dict or {}))))


# --------------------------------------------------------------------------- static checks

class RustStaticTest(unittest.TestCase):
    T = RustTarget("core_math", "core-math")

    def check(self, code):
        return validate_rust_test(code, self.T)

    def test_accepts_a_public_api_test(self):
        r = self.check(rs("#[test]\nfn adds() {\n    assert_eq!(add(2, 3), 5);\n}\n"))
        self.assertTrue(r.ok, r.errors)
        self.assertEqual((r.test_names, r.repairs), (["adds"], []))
        self.assertTrue(self.check(rs("#[tokio::test]\nasync fn adds() {\n    assert_eq!(add(2, 3), 5);\n}\n")).ok)

    def test_repairs(self):
        r = self.check(STRONG_VIA_CRATE.replace("assert_eq!(add(", "assert_eq!(crate::add("))
        self.assertTrue(r.ok, r.errors)
        self.assertIn("use core_math::add;", r.code)
        self.assertIn("core_math::add(2, 3)", r.code)
        self.assertNotIn("crate::", r.code)
        self.assertTrue(any("crate::" in x for x in r.repairs))
        r = self.check(WEAK_HYPHEN + "\n#[test]\nfn arithmetic() {\n    let x = 3 - 2;\n    assert_eq!(x, 1);\n}\n")
        self.assertTrue(r.ok, r.errors)
        self.assertIn("use core_math::add_all;", r.code)
        self.assertIn("core_math::add_all(&[1, 2, 3])", r.code)
        self.assertIn("3 - 2", r.code)  # ordinary subtraction is left alone
        r = self.check("use super::*;\n\n#[test]\nfn adds() {\n    assert_eq!(add(2, 3), 5);\n}\n")
        self.assertTrue(r.ok, r.errors)
        self.assertTrue(r.code.startswith("use core_math::*;"))
        # a lib name that differs from the package name
        r = validate_rust_test("use my_pkg::f;\n#[test]\nfn t() { assert!(f()); }\n", RustTarget("mylib", "my-pkg"))
        self.assertTrue(r.ok, r.errors)
        self.assertIn("use mylib::f;", r.code)
        # crate:: naming an item the file defines itself is not rewritten
        r = self.check("use core_math::add;\nmod util { pub fn two() -> i64 { 2 } }\n"
                       "#[test]\nfn t() { assert_eq!(add(crate::util::two(), 3), 5); }\n")
        self.assertTrue(r.ok, r.errors)
        self.assertEqual(r.repairs, [])

    def test_denylist(self):
        cases = {
            DENIED: "Command::new",
            rs("use std::process::exit;\n#[test]\nfn t() { assert_eq!(add(1, 1), 2); }\n"): "std::process",
            rs("use std::{fs, net::TcpStream};\n#[test]\nfn t() { assert_eq!(add(1, 1), 2); }\n"): "std::net",
            rs("#[test]\nfn t() { let _s = std::net::TcpStream::connect(\"x:1\"); assert_eq!(add(1, 1), 2); }\n"): "TcpStream",
            rs("#[test]\nfn t() { unsafe { assert_eq!(add(1, 1), 2); } }\n"): "unsafe",
            rs("#[test]\nfn t() { let _k = std::env::var(\"TOKEN\"); assert_eq!(add(1, 1), 2); }\n"): "env::var",
            rs("use std::env::{var};\n#[test]\nfn t() { let _k = var(\"TOKEN\"); assert_eq!(add(1, 1), 2); }\n"): "environment",
            rs("#[test]\nfn t() { let _k = env!(\"HOME\"); assert_eq!(add(1, 1), 2); }\n"): "env!()",
            rs("#[test]\nfn t() { let _k = include_str!(\"/etc/passwd\"); assert_eq!(add(1, 1), 2); }\n"): "include_str",
            rs("extern \"C\" { fn abs(x: i32) -> i32; }\n#[test]\nfn t() { assert_eq!(add(1, 1), 2); }\n"): "FFI",
        }
        for code, needle in cases.items():
            with self.subTest(needle=needle):
                r = self.check(code)
                self.assertFalse(r.ok)
                self.assertTrue(any(needle in e for e in r.errors), r.errors)

    def test_allowed_lookalikes(self):
        ok = [
            # forbidden words inside strings and comments
            rs("// std::process::Command::new, unsafe\n#[test]\nfn t() { let s = \"unsafe std::net\"; "
               "assert_eq!(add(1, 1), 2); assert!(!s.is_empty()); }\n"),
            rs("#[test]\nfn t() { let d = env!(\"CARGO_MANIFEST_DIR\"); assert!(!d.is_empty()); assert_eq!(add(1, 1), 2); }\n"),
            # the crate's own Command type
            "use core_math::Command;\n#[test]\nfn t() { let _c = Command::new(); }\n",
        ]
        for code in ok:
            with self.subTest(code=code[:40]):
                self.assertTrue(self.check(code).ok, self.check(code).errors)

    def test_structural_errors(self):
        self.assertIn("unbalanced", " ".join(self.check(rs("#[test]\nfn t() {\n    assert_eq!(add(1, 1), 2);\n")).errors))
        self.assertIn("no tests found", " ".join(self.check(rs("fn t() {\n    assert_eq!(add(1, 1), 2);\n}\n")).errors))
        r = self.check("#[test]\nfn t() {\n    assert_eq!(1 + 1, 2);\n}\n")
        self.assertIn("does not use the crate under test", " ".join(r.errors))
        self.assertIn("cannot tokenize", " ".join(self.check(rs("#[test]\nfn t() { let s = \"open; }\n")).errors))
        self.assertIn("empty", " ".join(self.check("").errors))


# --------------------------------------------------------------------------- target selection

def changed(path, n=5, status="M"):
    return gitmod.ChangedFile(path, status, None, n, 0, False)


class TargetSelectionTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_agent_rust_repo()
        self.addCleanup(self.repo.cleanup)
        self.head = gitmod.resolve_ref(self.repo.path, "fix")

    def test_crate_with_most_changed_rust_lines(self):
        projects = cargo_projects()
        t = select_target(str(self.repo.path), self.head, projects,
                          [changed("crates/core-math/src/lib.rs", 6), changed("crates/textx/src/lib.rs", 1),
                           changed("crates/textx/tests/x.rs", 50), changed("README.md", 90)], "tests/proofcheck_agent")
        self.assertEqual(t.error, "")
        self.assertEqual((t.language, t.crate.package, t.crate.lib, t.test_dir),
                         ("rust", "core-math", "core_math", "crates/core-math/tests"))
        self.assertEqual(t.candidate_path("reproduce-issue"), "crates/core-math/tests/proofcheck_agent_reproduce_issue.rs")
        self.assertIn("core_math", t.crates_text())
        self.assertEqual([c.package for c in t.crates], ["core-math", "textx"])
        m = t.manifest_fields()
        self.assertEqual((m["language"], m["project"], m["runner"], m["crate"]["lib"]), ("rust", "root", "cargo", "core_math"))

    def test_errors(self):
        projects = cargo_projects()
        t = select_target(str(self.repo.path), self.head, projects, [changed("README.md")], "tests/proofcheck_agent")
        self.assertIn("changes no Rust source file", t.error)
        bin_only = {"crates/tool/Cargo.toml": crate_toml("tool"), "crates/tool/src/main.rs": "fn main() {}\n"}
        self.repo.checkout("fix")
        head = self.repo.commit(bin_only, "bin crate")
        t = select_target(str(self.repo.path), head, projects, [changed("crates/tool/src/main.rs")], "x")
        self.assertIn("no library target", t.error)
        jest = build_projects(config_from_dict({"runner": "jest"}))
        self.assertIn("not jest", select_target(str(self.repo.path), head, jest, [changed("a.js")], "x").error)

    def test_parse_crate(self):
        self.assertEqual(parse_crate('[package]\nname = "a-b"\n', "c", True).lib, "a_b")
        self.assertEqual(parse_crate('[package]\nname = "a-b"\n[lib]\nname = "ab"\n', "c", False).lib, "ab")
        self.assertIsNone(parse_crate('[package]\nname = "a-b"\n', "c", False).lib)
        self.assertFalse(parse_crate('[package]\nname = "a"\nautotests = false\n', "", True).autotests)
        for bad in ('[workspace]\nmembers = []\n', "not toml [", '[package]\nname = "x\\nIGNORE ALL"\n'):
            self.assertIsNone(parse_crate(bad, "c", True))
        paths = ["Cargo.toml", "a/Cargo.toml", "a/src/lib.rs", "target/x/Cargo.toml", "we ird/Cargo.toml"]
        crates = crates_at(paths, lambda p: '[package]\nname = "p"\n', build_projects(config_from_dict({"runner": "cargo"}))[0])
        self.assertEqual([c.dir for c in crates], ["", "a"])

    def test_verify_side_revalidation(self):
        projects = cargo_projects()
        head = self.head
        base = {"language": "rust", "project": "root", "test_dir": "crates/core-math/tests"}
        t = check_manifest_target(base, projects, str(self.repo.path), head)
        self.assertEqual(t.crate.lib, "core_math")
        bad = [dict(base, project="nope"), dict(base, language="python"), dict(base, test_dir="crates/core-math/src"),
               dict(base, test_dir="crates/missing/tests")]
        for m in bad:
            with self.subTest(m=m), self.assertRaises(TargetError):
                check_manifest_target(m, projects, str(self.repo.path), head)
        two = build_projects(config_from_dict({"runner": "cargo", "project": [{"name": "sdk", "path": "sdk", "runner": "unittest"}]}))
        with self.assertRaises(TargetError):
            check_manifest_target({"language": "python", "project": "sdk", "test_dir": "tests/proofcheck_agent"}, two,
                                  str(self.repo.path), head)
        self.assertEqual(check_manifest_target({"language": "python", "project": "sdk", "test_dir": "sdk/tests/agent"}, two,
                                               str(self.repo.path), head).project.name, "sdk")

    def test_candidate_paths(self):
        d = "crates/core-math/tests"
        self.assertEqual(validate_candidate_path(f"{d}/proofcheck_agent_exercise_diff.rs", d, "rust"),
                         f"{d}/proofcheck_agent_exercise_diff.rs")
        for bad in (f"{d}/main.rs", f"{d}/proofcheck_agent_x.py", f"{d}/test_x.py", "crates/other/tests/proofcheck_agent_x.rs",
                    f"{d}/proofcheck_agent_X Y.rs", f"{d}/../proofcheck_agent_x.rs"):
            with self.subTest(bad=bad), self.assertRaises(ManifestError):
                validate_candidate_path(bad, d, "rust")

    def test_timeouts_and_prompts(self):
        self.assertEqual(AgentConfig().timeout_for("pytest"), 120)
        self.assertEqual(AgentConfig().timeout_for("cargo"), 1800)
        self.assertEqual(AgentConfig().timeout_for("cargo", 600), 600)
        self.assertEqual(parse_agent_config({"test_timeout": 30}).timeout_for("cargo", 600), 30)
        self.assertIn("does not COMPILE", feedback_message("fail_to_pass_weak", "x", "n", "rust"))
        self.assertIn("ImportError", feedback_message("fail_to_pass_weak", "x", "n"))


# --------------------------------------------------------------------------- end to end (cargo)

def rust_script():
    """One scripted conversation per variant, in VARIANTS order."""
    return [
        # reproduce-issue: crates, then a denylisted file, then `use crate::add` (repaired) -> strong
        [("list_crates", {}), ("list_tests", {})],
        [submit(DENIED)],
        [submit(STRONG_VIA_CRATE)],
        # exercise-diff: new API through the hyphenated package name -> repaired; compile error on base -> weak
        [("get_diff", {})],
        [submit(WEAK_HYPHEN)],
        # mimic-existing-tests: passes on both
        [("read_file", {"path": "crates/core-math/tests/arith.rs"})],
        [submit(PASS_BOTH)],
        # localize-first: wrong expectation -> fails on head
        "Plan: core_math::add in crates/core-math/src/lib.rs.",
        [submit(FAILS_HEAD)],
    ]


@unittest.skipUnless(cargo_path(), "cargo is not installed")
class RustProposeVerifyE2E(unittest.TestCase):
    def setUp(self):
        self.repo = make_agent_rust_repo()
        self.addCleanup(self.repo.cleanup)
        self.out = Path(tempfile.mkdtemp(prefix="proofcheck-agent-rs-"))
        self.addCleanup(lambda: shutil.rmtree(self.out, ignore_errors=True))

    def test_propose_and_verify(self):
        transport = ScriptedTransport("anthropic", rust_script())
        llm = ToolLLM("anthropic", "test-model", "secret-key-123456", transport=transport, sleep=lambda s: None)
        projects = cargo_projects()
        proposal = propose(self.repo.path, "main", "fix", agent_cfg=AgentConfig(variants=list(VARIANTS)),
                           runner=projects[0].runner, llm_factory=lambda: llm, pr_title="Fix add", pr_body=PR_BODY,
                           issues={"#12": ISSUE}, projects=projects)
        write_proposal(self.out, proposal)
        m = proposal.manifest
        self.assertEqual(m["status"], "proposed", m["reason"])
        self.assertEqual((m["language"], m["style"], m["project"], m["test_dir"]), ("rust", "rust", "root", "crates/core-math/tests"))
        self.assertEqual(m["crate"], {"dir": "crates/core-math", "package": "core-math", "lib": "core_math"})
        self.assertEqual([c["status"] for c in m["candidates"]], ["submitted"] * 4)
        self.assertEqual(transport.steps, [])
        # the prompts carry the Rust guidance and the crate facts
        first = transport.request_text(0)
        self.assertIn("use core_math::", first)
        self.assertIn("PUBLIC API", first)
        self.assertIn("cargo test -p core-math --test proofcheck_agent_reproduce_issue", first)
        self.assertIn("list_crates", first)
        self.assertIn("library crate `core_math`", transport.request_text(1))  # list_crates result
        # the denylisted file was rejected, the crate:: path repaired
        self.assertIn("Rejected by proofcheck", transport.request_text(2))
        self.assertIn("Command::new", transport.request_text(2))
        repro = m["candidates"][0]
        self.assertTrue(any("crate::" in r for r in repro["repairs"]), repro["repairs"])
        stored = (self.out / "crates/core-math/tests/proofcheck_agent_reproduce_issue.rs").read_text()
        self.assertTrue(stored.startswith("use core_math::add;"))
        self.assertIn("use core_math::add_all;",
                      (self.out / "crates/core-math/tests/proofcheck_agent_exercise_diff.rs").read_text())
        loaded = load_manifest(self.out)
        self.assertEqual(loaded["language"], "rust")
        self.assertEqual(loaded["candidates"][1]["file"], "crates/core-math/tests/proofcheck_agent_exercise_diff.rs")

        manifest, section = verify_candidates_dir(self.repo.path, "main", "fix", self.out, None, cargo_env(), None,
                                                  projects=projects)
        by_variant = {c["variant"]: c for c in section["candidates"]}
        self.assertEqual({v: c["classification"] for v, c in by_variant.items()},
                         {"reproduce-issue": FAIL_TO_PASS, "exercise-diff": FAIL_TO_PASS_WEAK,
                          "mimic-existing-tests": PASS_TO_PASS, "localize-first": FAILS_ON_HEAD}, section)
        strong = by_variant["reproduce-issue"]
        self.assertEqual(strong["tests"][0]["test_id"],
                         "crates/core-math/tests/proofcheck_agent_reproduce_issue.rs::adds_two_numbers")
        self.assertEqual(strong["tests"][0]["base_kind"], "panic")
        self.assertIn("does not compile there", by_variant["exercise-diff"]["message"])
        self.assertIn("add_all", by_variant["exercise-diff"]["message"])
        self.assertEqual((section["status"], section["language"], section["project"]), ("evidence", "rust", "root"))
        self.assertEqual(section["winner"]["variant"], "reproduce-issue")
        # the candidate files were removed from the worktrees and the repository is untouched
        self.assertFalse((self.repo.path / "crates/core-math/tests/proofcheck_agent_reproduce_issue.rs").exists())

        base = gitmod.resolve_ref(self.repo.path, "main")
        head = gitmod.resolve_ref(self.repo.path, "fix")
        result = validate_result({
            "schema": "proofcheck/result", "version": 1, "base": base, "head": head, "verdict": "no_evidence",
            "claims": [{"type": "fixes_issue", "text": "fixes #12", "source": "Fixes #12.", "details": {"number": 12},
                        "status": "unverified", "evidence": "no test was added or changed"}],
        })
        merged = validate_result(merge_into_result(result, section, manifest["target_issue"]))
        self.assertEqual(merged["claims"][0]["status"], "verified")
        self.assertEqual(merged["agent"]["language"], "rust")
        card = render_card(merged)
        self.assertIn("```rust\nuse core_math::add;", card)
        self.assertIn("verified (agent test)", card)

    def test_weak_only_and_static_reverification(self):
        """A run whose only candidate fails to compile on base is weak; a tampered candidate is rejected in verify."""
        transport = ScriptedTransport("openai", [[submit(WEAK_HYPHEN)]])
        llm = ToolLLM("openai", "m", "k", transport=transport, sleep=lambda s: None)
        projects = cargo_projects()
        proposal = propose(self.repo.path, "main", "fix", agent_cfg=AgentConfig(variants=["exercise-diff"]),
                           runner=projects[0].runner, llm_factory=lambda: llm, pr_body=PR_BODY, projects=projects)
        write_proposal(self.out, proposal)
        _, section = verify_candidates_dir(self.repo.path, "main", "fix", self.out, None, cargo_env(), None, projects=projects)
        self.assertEqual(section["status"], "weak_evidence", section)
        self.assertIn("does not compile", render_card(validate_result(merge_into_result(
            _result(self.repo), section, 12))))
        # tamper with the stored candidate: verify re-runs the static checks before compiling anything
        f = self.out / "crates/core-math/tests/proofcheck_agent_exercise_diff.rs"
        f.write_text(DENIED)
        _, section = verify_candidates_dir(self.repo.path, "main", "fix", self.out, None, cargo_env(), None, projects=projects)
        self.assertEqual(section["candidates"][0]["classification"], INVALID)
        self.assertIn("Command::new", section["candidates"][0]["message"])

    def test_cli_runner_cargo(self):
        out = self.out
        (out / "pr.md").write_text("Title: Fix add\n\n" + PR_BODY)
        (out / "issue.md").write_text(ISSUE)
        env = {k: v for k, v in cargo_env().items() if not k.startswith(("GITHUB_", "PROOFCHECK_"))}
        env["CARGO_TARGET_DIR"] = str(SHARED_TARGET)
        transport = ScriptedTransport("openai", [[submit(STRONG_VIA_CRATE)]])
        fake = ToolLLM("openai", "m", "k", transport=transport, sleep=lambda s: None)
        common = ["--repo", str(self.repo.path), "--base", "main", "--head", "fix", "--runner", "cargo"]
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("proofcheck.agent.cli.client_from_env", return_value=fake):
            res = CliRunner().invoke(cli.main, ["agent", "propose", *common, "--enable", "--variants", "1",
                                                "--pr-body-file", str(out / "pr.md"), "--issue-body-file", str(out / "issue.md"),
                                                "--out-dir", str(out / "cands")], catch_exceptions=False)
            self.assertEqual(res.exit_code, 0, res.output)
            res = CliRunner().invoke(cli.main, ["agent", "verify", *common, "--candidates", str(out / "cands"),
                                                "--json", str(out / "merged.json"), "--out", str(out / "card.md")],
                                     catch_exceptions=False)
        self.assertEqual(res.exit_code, 0, res.output)
        merged = json.loads((out / "merged.json").read_text())
        self.assertEqual(merged["agent"]["status"], "evidence", merged["agent"])
        self.assertEqual(merged["agent"]["winner"]["file"], "crates/core-math/tests/proofcheck_agent_reproduce_issue.rs")


def _result(repo):
    return validate_result({
        "schema": "proofcheck/result", "version": 1, "base": gitmod.resolve_ref(repo.path, "main"),
        "head": gitmod.resolve_ref(repo.path, "fix"), "verdict": "no_evidence",
        "claims": [{"type": "fixes_issue", "text": "fixes #12", "source": "Fixes #12.", "details": {"number": 12},
                    "status": "unverified", "evidence": "no test was added or changed"}],
    })


# --------------------------------------------------------------------------- two projects: cargo root + Python sdk/

SDK_CLIENT = 'def greet(name):\n    return "hello " + name\n'
SDK_CLIENT_FIXED = 'def greet(name):\n    return "Hello, " + name\n'
SDK_REPRO = '''import unittest

from sdkpkg.clint import greet


class GreetTest(unittest.TestCase):
    def test_greeting_is_capitalised(self):
        self.assertEqual(greet("Ada"), "Hello, Ada")
'''


def make_two_project_repo() -> GitRepo:
    """Root: cargo workspace. ``sdk/``: a unittest project (src layout). Branch ``pr`` fixes the SDK only,
    plus a one-line Rust comment change (the Python change is larger)."""
    r = GitRepo()
    config = '[agent]\nenabled = true\n\n[[project]]\nname = "sdk"\npath = "sdk"\nrunner = "unittest"\n'
    r.commit({**rust_files(), "proofcheck.toml": 'runner = "cargo"\n\n' + config,
              "sdk/pyproject.toml": '[project]\nname = "sdkpkg"\nversion = "0.1"\n',
              "sdk/src/sdkpkg/__init__.py": "", "sdk/src/sdkpkg/client.py": SDK_CLIENT,
              "sdk/tests/test_existing.py": "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n"},
             "base")
    r.branch("pr")
    r.commit({"sdk/src/sdkpkg/client.py": "# Greeting helpers.\n\n\n" + SDK_CLIENT_FIXED,
              "crates/core-math/src/stats.rs": "// stats\n" + STATS_RS}, "fix greeting")
    r.checkout("main")
    return r


class TwoProjectAgentTest(unittest.TestCase):
    """The target claim's change is in the Python sdk/ project: candidates go there and run there (no cargo needed)."""

    def setUp(self):
        self.repo = make_two_project_repo()
        self.addCleanup(self.repo.cleanup)
        self.out = Path(tempfile.mkdtemp(prefix="proofcheck-agent-2p-"))
        self.addCleanup(lambda: shutil.rmtree(self.out, ignore_errors=True))

    def test_selection_follows_the_main_change(self):
        projects = build_projects(config_from_dict({"runner": "cargo", "project": [
            {"name": "sdk", "path": "sdk", "runner": "unittest"}]}))
        head = gitmod.resolve_ref(self.repo.path, "pr")
        t = select_target(str(self.repo.path), head, projects,
                          [changed("sdk/src/sdkpkg/client.py", 4), changed("crates/core-math/src/stats.rs", 1)],
                          "tests/proofcheck_agent")
        self.assertEqual((t.project.name, t.language, t.test_dir), ("sdk", "python", "sdk/tests/proofcheck_agent"))
        t = select_target(str(self.repo.path), head, projects,
                          [changed("sdk/src/sdkpkg/client.py", 1), changed("crates/core-math/src/stats.rs", 3),
                           changed("sdk/tests/test_existing.py", 40)], "tests/proofcheck_agent")
        self.assertEqual((t.project.name, t.language, t.test_dir), ("root", "rust", "crates/core-math/tests"))

    def test_cli_propose_verify_in_the_python_subproject(self):
        out = self.out
        (out / "pr.md").write_text("Title: Fix the greeting\n\nFixes #3.")
        (out / "issue.md").write_text("greet() should say 'Hello, Ada'.")
        transport = ScriptedTransport("anthropic", [[("list_tests", {})], [submit(SDK_REPRO, "test_greet.py")]])
        fake = ToolLLM("anthropic", "m", "k", transport=transport, sleep=lambda s: None)
        env = {k: v for k, v in scrubbed_env().items() if not k.startswith(("GITHUB_", "PROOFCHECK_"))}
        common = ["--repo", str(self.repo.path), "--base", "main", "--head", "pr"]
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("proofcheck.agent.cli.client_from_env", return_value=fake):
            res = CliRunner().invoke(cli.main, ["agent", "propose", *common, "--variants", "1", "--pr-body-file",
                                                str(out / "pr.md"), "--issue-body-file", str(out / "issue.md"),
                                                "--out-dir", str(out / "cands")], catch_exceptions=False)
            self.assertEqual(res.exit_code, 0, res.output)
            manifest = json.loads((out / "cands/manifest.json").read_text())
            self.assertEqual((manifest["project"], manifest["project_path"], manifest["language"], manifest["style"]),
                             ("sdk", "sdk", "python", "unittest"))
            cand = manifest["candidates"][0]
            self.assertEqual(cand["file"], "sdk/tests/proofcheck_agent/test_reproduce_issue.py")
            self.assertTrue(any("sdkpkg.clint" in r for r in cand["repairs"]), cand["repairs"])  # module index of sdk/src
            listing = transport.request_text(1)
            self.assertIn("sdk/tests/test_existing.py", listing)
            self.assertNotIn("crates/core-math/tests/arith.rs", listing)  # only the target project's tests
            self.assertIn("Tests of this project run in: sdk", transport.request_text(0))
            res = CliRunner().invoke(cli.main, ["agent", "verify", *common, "--candidates", str(out / "cands"),
                                                "--json", str(out / "merged.json"), "--out", str(out / "card.md")],
                                     catch_exceptions=False)
        self.assertEqual(res.exit_code, 0, res.output)
        agent = json.loads((out / "merged.json").read_text())["agent"]
        self.assertEqual((agent["status"], agent["project"], agent["language"]), ("evidence", "sdk", "python"), agent)
        self.assertEqual(agent["candidates"][0]["classification"], FAIL_TO_PASS)
        self.assertEqual(agent["candidates"][0]["tests"][0]["base_kind"], "AssertionError")
        self.assertIn("```python", (out / "card.md").read_text())

    def test_verify_refuses_a_project_the_config_does_not_define(self):
        transport = ScriptedTransport("anthropic", [[submit(SDK_REPRO)]])
        fake = ToolLLM("anthropic", "m", "k", transport=transport, sleep=lambda s: None)
        projects = build_projects(config_from_dict({"runner": "cargo", "project": [
            {"name": "sdk", "path": "sdk", "runner": "unittest"}]}))
        proposal = propose(self.repo.path, "main", "pr", agent_cfg=AgentConfig(variants=["reproduce-issue"]),
                           runner=projects[0].runner, llm_factory=lambda: fake, pr_body="Fixes #3", projects=projects)
        write_proposal(self.out, proposal)
        _, section = verify_candidates_dir(self.repo.path, "main", "pr", self.out, unittest_runner(), scrubbed_env(), 60)
        self.assertEqual(section["status"], "error")
        self.assertIn("project 'sdk'", section["reason"])


class ListCratesToolTest(unittest.TestCase):
    def test_only_offered_for_cargo_targets(self):
        from proofcheck.agent.tools import RepoTools

        t = RepoTools(".", "a" * 40, "b" * 40, "n")
        self.assertNotIn("list_crates", [s.name for s in t.specs()])
        self.assertTrue(t.call(ToolCall("1", "list_crates", {})).is_error)
        t = RepoTools(".", "a" * 40, "b" * 40, "n", crates_text="Crates: x")
        self.assertIn("list_crates", [s.name for s in t.specs()])
        self.assertEqual(t.call(ToolCall("1", "list_crates", {})).content, "Crates: x")
        self.assertTrue(t.call(ToolCall("1", "list_crates", {"x": 1})).is_error)


if __name__ == "__main__":
    unittest.main()
