"""End-to-end agent tests: scripted LLM (both wire formats) -> propose -> verify on a real git repo."""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _helpers  # noqa: F401  (sets up sys.path)
from _agent_fakes import (
    FAILS_HEAD,
    ISSUE,
    PASS_BOTH,
    PR_BODY,
    STRONG,
    WEAK,
    ScriptedTransport,
    make_textutil_repo,
    submit,
    unittest_runner,
)
from _helpers import scrubbed_env
from click.testing import CliRunner

from proofcheck import cli
from proofcheck import git as gitmod
from proofcheck.agent import FAIL_TO_PASS, FAIL_TO_PASS_WEAK, FAILS_ON_HEAD, PASS_TO_PASS, VARIANTS
from proofcheck.agent.config import AgentConfig
from proofcheck.agent.llm_tools import ToolLLM
from proofcheck.agent.loop import run_agent
from proofcheck.agent.propose import propose, write_proposal
from proofcheck.agent.schema import load_manifest
from proofcheck.agent.verify import merge_into_result, verify_candidates_dir
from proofcheck.card import render_card
from proofcheck.result import validate_result

UNITTEST_CMD = f"{__import__('sys').executable} {{unittest_junit}} --junitxml {{junitxml}} {{tests}}"


def all_variant_script():
    """One scripted conversation per variant, in VARIANTS order."""
    return [
        # reproduce-issue: look around, submit a broken file, then the (typo'd) strong test
        [("get_issue", {}), ("grep", {"pattern": "def word_count", "ref": "base"})],
        [("read_file", {"path": "textutil.py", "ref": "base"}), ("list_tests", {})],
        [submit("def broken(:\n    pass\n")],
        [submit(STRONG)],
        # exercise-diff: new API only -> weak
        [("get_diff", {})],
        [submit(WEAK)],
        # mimic-existing-tests: passes on both
        [("read_file", {"path": "tests/test_textutil.py"})],
        [submit(PASS_BOTH)],
        # localize-first: wrong expectation -> fails on head
        "Plan: word_count in textutil.py; input 'hello  world'.",
        [submit(FAILS_HEAD)],
    ]


class ProposeVerifyE2E(unittest.TestCase):
    def setUp(self):
        self.repo = make_textutil_repo()
        self.addCleanup(self.repo.cleanup)
        self.out = Path(tempfile.mkdtemp(prefix="proofcheck-agent-"))
        self.addCleanup(lambda: shutil.rmtree(self.out, ignore_errors=True))

    def _propose(self, provider):
        transport = ScriptedTransport(provider, all_variant_script())
        llm = ToolLLM(provider, "test-model", "secret-key-123456", transport=transport, sleep=lambda s: None)
        cfg = AgentConfig(variants=list(VARIANTS), price_input_per_mtok=3.0, price_output_per_mtok=15.0)
        proposal = propose(self.repo.path, "main", "fix", agent_cfg=cfg, runner=unittest_runner(),
                           llm_factory=lambda: llm, pr_title="Fix word_count", pr_body=PR_BODY,
                           issues={"#7": ISSUE}, secrets_=["secret-key-123456"])
        write_proposal(self.out, proposal)
        return proposal, transport

    def _check(self, provider):
        proposal, transport = self._propose(provider)
        m = proposal.manifest
        self.assertEqual(m["status"], "proposed")
        self.assertEqual(m["style"], "unittest")
        self.assertEqual(m["target_claim"], "fixes #7")
        self.assertEqual([c["status"] for c in m["candidates"]], ["submitted"] * 4)
        self.assertEqual(transport.steps, [])
        # import repair: textutils -> textutil
        first = m["candidates"][0]
        self.assertTrue(any("textutils" in r and "textutil" in r for r in first["repairs"]), first["repairs"])
        stored = (self.out / "tests/proofcheck_agent/test_reproduce_issue.py").read_text()
        self.assertIn("from textutil import word_count", stored)
        # the syntax error was rejected and reported back to the model
        self.assertIn("Rejected by proofcheck", transport.request_text(3))
        # tool results reached the model as untrusted data
        self.assertIn("UNTRUSTED_", transport.request_text(1))
        self.assertIn("len(text.split(\\\" \\\"))", transport.request_text(2))
        # usage and cost accounting
        self.assertEqual(m["usage"]["requests"], 10)
        self.assertEqual(m["usage"]["output_tokens"], 300)
        self.assertIsNotNone(m["cost_usd"])
        # transcript is redacted
        transcript = (self.out / "transcript.jsonl").read_text()
        self.assertNotIn("secret-key-123456", transcript)
        self.assertEqual(load_manifest(self.out)["candidates"][0]["file"], "tests/proofcheck_agent/test_reproduce_issue.py")

        manifest, section = verify_candidates_dir(self.repo.path, "main", "fix", self.out, unittest_runner(),
                                                  scrubbed_env(), 60)
        by_variant = {c["variant"]: c["classification"] for c in section["candidates"]}
        self.assertEqual(by_variant, {"reproduce-issue": FAIL_TO_PASS, "exercise-diff": FAIL_TO_PASS_WEAK,
                                      "mimic-existing-tests": PASS_TO_PASS, "localize-first": FAILS_ON_HEAD}, section)
        self.assertEqual(section["status"], "evidence")
        self.assertEqual(section["winner"]["variant"], "reproduce-issue")
        weak = next(c for c in section["candidates"] if c["variant"] == "exercise-diff")
        self.assertIn("ImportError", weak["message"])
        strong = next(c for c in section["candidates"] if c["variant"] == "reproduce-issue")
        self.assertEqual(strong["tests"][0]["base_kind"], "AssertionError")
        return manifest, section

    def test_anthropic_wire_format(self):
        _, section = self._check("anthropic")
        self.assertEqual(section["provider"], "anthropic")
        self.assertEqual(section["model"], "test-model")

    def test_openai_wire_format(self):
        manifest, section = self._check("openai")
        # merge into a result and render the card
        base = gitmod.resolve_ref(self.repo.path, "main")
        head = gitmod.resolve_ref(self.repo.path, "fix")
        result = validate_result({
            "schema": "proofcheck/result", "version": 1, "base": base, "head": head, "verdict": "no_evidence",
            "claims": [{"type": "fixes_issue", "text": "fixes #7", "source": "Fixes #7.", "details": {"number": 7},
                        "status": "unverified", "evidence": "no test was added or changed"}],
        })
        merged = validate_result(merge_into_result(result, section, manifest["target_issue"]))
        self.assertEqual(merged["claims"][0]["status"], "verified")
        self.assertEqual(merged["claims"][0]["verified_by"], "agent")
        self.assertEqual(merged["verdict"], "evidence")
        card = render_card(merged)
        self.assertIn("verified (agent test)", card)
        self.assertIn("Agent evidence (experimental)", card)
        self.assertIn("Commit this test", card)


class PytestRunnerVerifyTest(unittest.TestCase):
    """verify with the pytest runner: the stand-in always, real pytest when it is importable."""

    def setUp(self):
        self.repo = make_textutil_repo()
        self.addCleanup(self.repo.cleanup)
        self.out = Path(tempfile.mkdtemp(prefix="proofcheck-agent-pt-"))
        self.addCleanup(lambda: shutil.rmtree(self.out, ignore_errors=True))
        steps = [[submit(STRONG)], [submit(WEAK)], [submit(PASS_BOTH)], [submit(FAILS_HEAD)]]
        llm = ToolLLM("openai", "m", "k", transport=ScriptedTransport("openai", steps), sleep=lambda s: None)
        proposal = propose(self.repo.path, "main", "fix", agent_cfg=AgentConfig(variants=list(VARIANTS)),
                           runner=unittest_runner(), llm_factory=lambda: llm, pr_body=PR_BODY)
        write_proposal(self.out, proposal)

    def classify(self, runner, env):
        _, section = verify_candidates_dir(self.repo.path, "main", "fix", self.out, runner, env, 120)
        return {c["variant"]: c["classification"] for c in section["candidates"]}

    def test_fake_pytest(self):
        from _helpers import FAKE_PYTEST

        from proofcheck.runners import PytestRunner

        runner = PytestRunner(command=[__import__("sys").executable, str(FAKE_PYTEST), "-p", "no:cacheprovider", "-q"])
        self.assertEqual(self.classify(runner, scrubbed_env()), EXPECTED)

    @unittest.skipUnless(importlib.util.find_spec("pytest"), "pytest is not installed")
    def test_real_pytest(self):
        import pytest

        from proofcheck.runners import PytestRunner

        env = scrubbed_env()
        env["PYTHONPATH"] = str(Path(pytest.__file__).resolve().parent.parent)
        self.assertEqual(self.classify(PytestRunner(), env), EXPECTED)


EXPECTED = {"reproduce-issue": FAIL_TO_PASS, "exercise-diff": FAIL_TO_PASS_WEAK,
            "mimic-existing-tests": PASS_TO_PASS, "localize-first": FAILS_ON_HEAD}


class RefinementLoopTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_textutil_repo()
        self.addCleanup(self.repo.cleanup)

    def test_execution_feedback_leads_to_strong_test(self):
        steps = [[submit(PASS_BOTH)], [("read_file", {"path": "textutil.py", "ref": "base"})], [submit(STRONG)]]
        transport = ScriptedTransport("anthropic", steps)
        llm = ToolLLM("anthropic", "m", "k", transport=transport, sleep=lambda s: None)
        cfg = AgentConfig(variants=["reproduce-issue"], max_rounds=2)
        run = run_agent(self.repo.path, "main", "fix", agent_cfg=cfg, runner=unittest_runner(), llm_factory=lambda: llm,
                        env=scrubbed_env(), pr_body=PR_BODY, issues={"#7": ISSUE})
        self.assertEqual(run.rounds, 2)
        self.assertEqual([o.classification for o in run.outcomes], [PASS_TO_PASS, FAIL_TO_PASS])
        self.assertEqual(run.section["status"], "evidence")
        feedback = transport.request_text(1)
        self.assertIn("Result: pass_to_pass", feedback)
        self.assertIn("does not reproduce the bug", feedback)

    def test_stops_when_budget_exhausted(self):
        transport = ScriptedTransport("openai", [[submit(PASS_BOTH)]])
        llm = ToolLLM("openai", "m", "k", transport=transport, sleep=lambda s: None)
        cfg = AgentConfig(variants=["exercise-diff"], max_rounds=2, max_total_tokens=100)
        run = run_agent(self.repo.path, "main", "fix", agent_cfg=cfg, runner=unittest_runner(), llm_factory=lambda: llm,
                        env=scrubbed_env(), pr_body=PR_BODY)
        self.assertEqual(run.rounds, 1)
        self.assertEqual(run.section["status"], "no_evidence")
        self.assertTrue(any("budget" in n for n in run.notes))


class AgentCliTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_textutil_repo()
        self.addCleanup(self.repo.cleanup)
        self.out = Path(tempfile.mkdtemp(prefix="proofcheck-agent-cli-"))
        self.addCleanup(lambda: shutil.rmtree(self.out, ignore_errors=True))

    def invoke(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "PROOFCHECK_"))}
        env.pop("PYTHONPATH", None)
        with mock.patch.dict(os.environ, env, clear=True):
            return CliRunner().invoke(cli.main, list(args), catch_exceptions=False)

    def test_run_propose_verify_pipeline(self):
        (self.out / "pr.md").write_text("Title: Fix word_count\n\n" + PR_BODY)
        (self.out / "issue.md").write_text(ISSUE)
        res = self.invoke("run", "--repo", str(self.repo.path), "--base", "main", "--head", "fix", "--runner", "unittest",
                          "--test-command", UNITTEST_CMD, "--pr-body-file", str(self.out / "pr.md"),
                          "--json", str(self.out / "result.json"), "--out", str(self.out / "card.md"))
        self.assertEqual(res.exit_code, 0, res.output)
        result = json.loads((self.out / "result.json").read_text())
        self.assertEqual(result["claims"][0]["status"], "unverified")

        # needed?
        res = self.invoke("agent", "needed", "--json", str(self.out / "result.json"))
        self.assertEqual(res.output.splitlines()[0], "true")

        # disabled by default
        res = self.invoke("agent", "propose", "--repo", str(self.repo.path), "--base", "main", "--head", "fix",
                          "--runner", "unittest", "--result", str(self.out / "result.json"), "--out-dir", str(self.out / "c0"))
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertEqual(json.loads((self.out / "c0/manifest.json").read_text())["status"], "skipped")

        # missing model -> clear error
        res = self.invoke("agent", "propose", "--repo", str(self.repo.path), "--base", "main", "--head", "fix", "--enable",
                          "--runner", "unittest", "--result", str(self.out / "result.json"), "--out-dir", str(self.out / "c1"))
        self.assertNotEqual(res.exit_code, 0)
        self.assertIn("PROOFCHECK_LLM_PROVIDER", res.output)

        transport = ScriptedTransport("openai", [[submit(STRONG)]])
        fake = ToolLLM("openai", "m", "k", transport=transport, sleep=lambda s: None)
        with mock.patch("proofcheck.agent.cli.client_from_env", return_value=fake):
            res = self.invoke("agent", "propose", "--repo", str(self.repo.path), "--base", "main", "--head", "fix",
                              "--enable", "--variants", "1", "--runner", "unittest", "--issue-body-file", str(self.out / "issue.md"),
                              "--result", str(self.out / "result.json"), "--out-dir", str(self.out / "cands"))
        self.assertEqual(res.exit_code, 0, res.output)
        res = self.invoke("agent", "verify", "--repo", str(self.repo.path), "--base", "main", "--head", "fix",
                          "--runner", "unittest", "--test-command", UNITTEST_CMD, "--candidates", str(self.out / "cands"),
                          "--result", str(self.out / "result.json"), "--json", str(self.out / "merged.json"),
                          "--out", str(self.out / "merged.md"))
        self.assertEqual(res.exit_code, 0, res.output)
        merged = json.loads((self.out / "merged.json").read_text())
        self.assertEqual(merged["agent"]["status"], "evidence")
        self.assertEqual(merged["claims"][0]["status"], "verified")
        self.assertIn("verified (agent test)", (self.out / "merged.md").read_text())
        # the privileged comment path accepts the merged result
        res = self.invoke("comment", "--json", str(self.out / "merged.json"), "--pr", "3", "--dry-run")
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("Agent evidence", res.output)

    def test_verify_rejects_candidates_for_other_commits(self):
        transport = ScriptedTransport("anthropic", [[submit(STRONG)]])
        fake = ToolLLM("anthropic", "m", "k", transport=transport, sleep=lambda s: None)
        proposal = propose(self.repo.path, "main", "fix", agent_cfg=AgentConfig(variants=["reproduce-issue"]),
                           runner=unittest_runner(), llm_factory=lambda: fake, pr_body=PR_BODY)
        write_proposal(self.out / "c", proposal)
        self.repo.checkout("fix")
        self.repo.commit({"other.py": "x = 1\n"}, "more")
        manifest, section = verify_candidates_dir(self.repo.path, "main", "fix", self.out / "c", unittest_runner(),
                                                  scrubbed_env(), 60)
        self.assertEqual(section["status"], "error")
        self.assertIn("different commits", section["reason"])


if __name__ == "__main__":
    unittest.main()
