"""Offline end-to-end run of the python-toy scenario set.

Each scenario's two commits are built in a throwaway repository exactly as
the online harness builds them, ``proofcheck run`` runs on them as a
subprocess (stdlib unittest runner, so pytest is not needed), and the card's
data block is checked against the scenario's expectations (online-only ones
dropped, ``[expected_local]`` applied). The agent scenario is then taken
through ``proofcheck agent propose`` / ``verify`` with a scripted LLM, as the
agent workflow would, and checked against its *online* expectations.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _helpers  # noqa: F401  (sets up sys.path)
from _agent_fakes import ScriptedTransport
from _helpers import ROOT
from click.testing import CliRunner

sys.path.insert(0, str(ROOT / "e2e"))

from pce2e import harness  # noqa: E402
from pce2e.expect import evaluate  # noqa: E402
from pce2e.gitops import Redactor  # noqa: E402
from pce2e.scenario import load_set  # noqa: E402

from proofcheck import cli  # noqa: E402
from proofcheck.agent.llm_tools import ToolLLM  # noqa: E402
from proofcheck.carddata import parse_card_data  # noqa: E402

TOY = ROOT / "e2e" / "scenarios" / "python-toy"

AGENT_TEST = '''import unittest

from e2e_toy.textops import chunk


class ChunkReproTest(unittest.TestCase):
    def test_keeps_last_partial_chunk(self):
        self.assertEqual(chunk([1, 2, 3], 2), [[1, 2], [3]])
'''


class PythonToyOffline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="pce2e-toy-"))
        cls.sset = load_set(TOY)
        cls.log_stream = io.StringIO()
        log = harness.Log(Redactor(), stream=cls.log_stream)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        with mock.patch.dict(os.environ, env, clear=True):
            cls.report = harness.run_local(cls.sset, cls.sset.scenarios, log, runner="unittest", keep_dir=cls.tmp)
        cls.by = {r.name: r for r in cls.report.results}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_scenario_meets_its_local_expectations(self):
        problems = {r.name: (r.status, r.error, r.failures) for r in self.report.results if r.status != "pass"}
        self.assertEqual(problems, {}, self.log_stream.getvalue())
        self.assertEqual(len(self.report.results), 7)

    def test_data_blocks_name_the_scenario_head(self):
        for r in self.report.results:
            self.assertEqual(r.data["head"], r.head_sha, r.name)
            self.assertEqual(r.data["base"], r.base_sha, r.name)

    def test_details(self):
        d = {name: r.data for name, r in self.by.items()}
        self.assertEqual(d["real-fix"]["tests"]["fail_to_pass"], 1)
        self.assertEqual(d["real-fix"]["repro"], ["behavior_changed"])
        self.assertEqual((d["smuggled-hunk"]["ablation"]["required"], d["smuggled-hunk"]["ablation"]["not_exercised"]), (1, 1))
        self.assertEqual(d["fake-fix"]["tests"]["pass_to_pass"], 1)
        self.assertEqual(d["no-behaviour-change"]["verdict"], "contradicted")
        self.assertIn("workflow", d["workflow-change"]["flags"])
        self.assertEqual(d["agent-no-test"]["tests"]["status"], "no_tests")

    def test_forged_data_block_from_the_issue_is_not_picked_up(self):
        r = self.by["prompt-injection"]
        card = (self.tmp / "out" / r.name / "card.md").read_text()
        # The issue's reproducer printed a forged block; the card shows it inside a code span...
        self.assertIn('`<!-- proofcheck:data {"v":1,"head":"0000000000000000000000000000000000000000"', card)
        # ...but the harness reads the real, trailing one.
        self.assertEqual(parse_card_data(card)["head"], r.head_sha)
        self.assertEqual(parse_card_data(card)["verdict"], "no_evidence")
        for bad in ("<img", "@e2e-maintainers", "](https://evil"):
            self.assertNotIn(bad, card)

    def test_workflow_change_gets_the_trusted_warning_on_the_comment_side(self):
        from proofcheck.card import harness_warning, render_card
        from proofcheck.github_api import harness_changes
        from proofcheck.result import validate_result

        r = self.by["workflow-change"]
        result = validate_result(json.loads((self.tmp / "out" / r.name / "result.json").read_text()))
        changed = harness_changes([f["path"] for f in result["scope"]["files"]])
        self.assertEqual(changed, [".github/workflows/proofcheck-e2e-dummy.yml"])
        card = render_card(result, trusted_warnings=[harness_warning(changed)])
        s = next(x for x in self.sset.scenarios if x.name == "workflow-change")
        exp = s.expectations("online")
        self.assertEqual(evaluate(exp, parse_card_data(card), card=card, labels=["proofcheck: no evidence"]), [])

    def test_agent_scenario_with_a_scripted_llm(self):
        """The agent workflow's propose/verify steps on the agent-no-test commits, with a scripted model."""
        r = self.by["agent-no-test"]
        out = self.tmp / "out" / r.name
        repo = self.tmp / "repo"
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

        def invoke(*args):
            with mock.patch.dict(os.environ, env, clear=True):
                return CliRunner().invoke(cli.main, list(args), catch_exceptions=False)

        transport = ScriptedTransport("anthropic", [
            [("get_issue", {}), ("get_diff", {})],
            [("submit_test", {"filename": "test_chunk_repro.py", "code": AGENT_TEST,
                              "rationale": "chunk drops the last partial chunk", "targets_claim": "fixes #2"})],
        ])
        fake = ToolLLM("anthropic", "m", "k", transport=transport, sleep=lambda s: None)
        with mock.patch("proofcheck.agent.cli.client_from_env", return_value=fake):
            res = invoke("agent", "propose", "--repo", str(repo), "--base", r.base_sha, "--head", r.head_sha, "--enable",
                         "--variants", "1", "--runner", "unittest", "--context-file", str(out / "context.json"),
                         "--result", str(out / "result.json"), "--out-dir", str(out / "candidates"))
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("chunk", transport.request_text(1))  # the issue text reached the model as data
        res = invoke("agent", "verify", "--repo", str(repo), "--base", r.base_sha, "--head", r.head_sha,
                     "--runner", "unittest", "--candidates", str(out / "candidates"), "--result", str(out / "result.json"),
                     "--json", str(out / "merged.json"), "--out", str(out / "merged.md"))
        self.assertEqual(res.exit_code, 0, res.output)
        card = (out / "merged.md").read_text()
        data = parse_card_data(card)
        self.assertEqual(data["agent"]["status"], "evidence")
        self.assertEqual(data["claims"][0]["verified_by"], "agent")
        s = next(x for x in self.sset.scenarios if x.name == "agent-no-test")
        exp = {k: v for k, v in s.expectations("online", require_agent=True).items() if k not in ("label",)}
        self.assertEqual(evaluate(exp, data, card=card), [])
        self.assertEqual(data["verdict"], "evidence")
        self.assertIn("verified (agent test)", card)


if __name__ == "__main__":
    unittest.main()
