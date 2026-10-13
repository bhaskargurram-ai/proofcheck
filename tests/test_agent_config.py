"""The [agent] configuration table."""

import tempfile
import unittest
from pathlib import Path

import _helpers  # noqa: F401  (sets up sys.path)
from _helpers import GitRepo

from proofcheck.agent import VARIANTS
from proofcheck.agent.config import (
    AgentConfig,
    AgentConfigError,
    agent_config_from_text,
    load_agent_config_file,
    load_agent_config_from_ref,
    parse_agent_config,
    validate_test_dir,
)
from proofcheck.config import ConfigError, parse_config_text


class AgentConfigTest(unittest.TestCase):
    def test_defaults(self):
        c = AgentConfig()
        self.assertFalse(c.enabled)
        self.assertEqual(c.variants, ["reproduce-issue", "exercise-diff", "mimic-existing-tests"])
        self.assertEqual((c.max_rounds, c.max_tool_calls, c.when, c.test_dir), (2, 25, "missing-evidence", "tests/proofcheck_agent"))
        self.assertIsNone(c.price_input_per_mtok)
        self.assertIsNone(c.temperature)

    def test_parse_full_table(self):
        c = parse_agent_config({"enabled": True, "variants": 4, "max_rounds": 1, "max_tool_calls": 10, "when": "always",
                                "test_dir": "tests/agent/", "price_input_per_mtok": 3, "price_output_per_mtok": 15,
                                "max_cost_usd": 0.5, "temperature": 0.3, "test_timeout": 30})
        self.assertTrue(c.enabled)
        self.assertEqual(c.variants, list(VARIANTS))
        self.assertEqual(c.test_dir, "tests/agent")
        self.assertEqual((c.price_input_per_mtok, c.price_output_per_mtok, c.max_cost_usd), (3.0, 15.0, 0.5))
        self.assertEqual(parse_agent_config({"variants": ["localize-first", "localize-first"]}).variants, ["localize-first"])

    def test_invalid_values(self):
        bad = [{"enabled": "yes"}, {"variants": 0}, {"variants": 9}, {"variants": ["nope"]}, {"variants": []},
               {"max_rounds": -1}, {"max_tool_calls": 1000}, {"when": "sometimes"}, {"test_dir": "../x"},
               {"test_dir": "/abs"}, {"temperature": 5}, {"unknown_key": 1}, {"api_key": "sk-123"}, {"max_rounds": True}]
        for table in bad:
            with self.assertRaises(AgentConfigError, msg=table):
                parse_agent_config(table)
        with self.assertRaises(AgentConfigError):
            parse_agent_config("not a table")

    def test_test_dir(self):
        self.assertEqual(validate_test_dir("tests/agent_x-1/"), "tests/agent_x-1")
        for bad in ("./x", "a/../b", "a b", "a:b", "", "a\\b"):
            with self.assertRaises(AgentConfigError, msg=bad):
                validate_test_dir(bad)


class MainConfigIntegrationTest(unittest.TestCase):
    def test_main_config_accepts_and_validates_agent_table(self):
        cfg = parse_config_text('runner = "unittest"\n[agent]\nenabled = true\nvariants = 2\n', "proofcheck.toml")
        self.assertEqual(cfg.runner, "unittest")
        with self.assertRaisesRegex(ConfigError, "agent.when"):
            parse_config_text('[agent]\nwhen = "never"\n', "proofcheck.toml")

    def test_pyproject_and_file_loading(self):
        c = agent_config_from_text('[tool.proofcheck.agent]\nmax_rounds = 0\n', "pyproject.toml", pyproject=True)
        self.assertEqual(c.max_rounds, 0)
        self.assertIsNone(agent_config_from_text('[tool.black]\nx = 1\n', "pyproject.toml", pyproject=True))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "proofcheck.toml"
            p.write_text('[agent]\nvariants = ["exercise-diff"]\n')
            self.assertEqual(load_agent_config_file(p).variants, ["exercise-diff"])

    def test_loaded_from_the_base_commit(self):
        r = GitRepo()
        self.addCleanup(r.cleanup)
        r.commit({"proofcheck.toml": "[agent]\nenabled = false\nmax_tool_calls = 5\n"}, "base")
        base = r.commit({"x.py": "x = 1\n"}, "base2")
        r.branch("pr")
        r.commit({"proofcheck.toml": "[agent]\nenabled = true\nmax_tool_calls = 100\n"}, "pr raises its own budget")
        c = load_agent_config_from_ref(r.path, base)
        self.assertEqual((c.enabled, c.max_tool_calls), (False, 5))


if __name__ == "__main__":
    unittest.main()
