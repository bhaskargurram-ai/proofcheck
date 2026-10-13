import tempfile
import unittest
from pathlib import Path

import _helpers  # noqa: F401  (sets up sys.path)
from _helpers import GitRepo

from proofcheck.config import Config, ConfigError, config_from_dict, load_config_file, load_config_from_ref, parse_config_text

TOML = """
runner = "unittest"
test_command = "python -m pytest -x --tb=short"
test_patterns = ["tests/*", "*_test.py"]
timeout = 120
benchmark_command = ["python", "bench.py"]
benchmark_runs = 3
large_diff_threshold = 400

[test_env]
APP_MODE = "test"

[repro]
timeout = 15

[llm]
enabled = true
provider = "openai"
model = "some-model"
"""


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = Config()
        self.assertEqual(cfg.runner, "pytest")
        self.assertIsNone(cfg.test_command)
        self.assertFalse(cfg.llm.enabled)
        self.assertTrue(cfg.repro_python)

    def test_parse_full(self):
        cfg = parse_config_text(TOML, "proofcheck.toml")
        self.assertEqual(cfg.runner, "unittest")
        self.assertEqual(cfg.test_command, ["python", "-m", "pytest", "-x", "--tb=short"])
        self.assertEqual(cfg.benchmark_command, ["python", "bench.py"])
        self.assertEqual((cfg.timeout, cfg.benchmark_runs, cfg.large_diff_threshold, cfg.repro_timeout), (120, 3, 400, 15))
        self.assertEqual(cfg.test_env, {"APP_MODE": "test"})
        self.assertTrue(cfg.llm.enabled)
        self.assertEqual(cfg.llm.model, "some-model")

    def test_invalid_values(self):
        for bad in ({"timeout": "soon"}, {"timeout": 0}, {"test_command": 5}, {"bogus": 1}, {"llm": {"api_key": "sk-1"}},
                    {"test_env": {"A": 1}}, {"benchmark_runs": True}):
            with self.assertRaises(ConfigError, msg=bad):
                config_from_dict(bad)

    def test_pyproject_section(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "pyproject.toml"
            p.write_text('[project]\nname="x"\n\n[tool.proofcheck]\ntimeout = 30\n', encoding="utf-8")
            self.assertEqual(load_config_file(p).timeout, 30)
            p.write_text('[project]\nname="x"\n', encoding="utf-8")
            self.assertEqual(load_config_file(p).timeout, 600)

    def test_config_is_read_from_base_ref_not_head(self):
        r = GitRepo()
        self.addCleanup(r.cleanup)
        base = r.commit({"proofcheck.toml": "timeout = 50\n"}, "base")
        r.branch("pr")
        head = r.commit({"proofcheck.toml": 'timeout = 50\ntest_command = "true"\n'}, "pr weakens config")
        self.assertIsNone(load_config_from_ref(r.path, base).test_command)
        self.assertEqual(load_config_from_ref(r.path, head).test_command, ["true"])
        self.assertIn("proofcheck.toml@", load_config_from_ref(r.path, base).source)


if __name__ == "__main__":
    unittest.main()
