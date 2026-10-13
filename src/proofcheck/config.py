"""Configuration loading (``proofcheck.toml`` or ``[tool.proofcheck]`` in pyproject).

By default the configuration is read from the **base** commit of the pull
request, not from the PR head. A pull request therefore cannot weaken its own
check by editing ``proofcheck.toml`` (for example by replacing the test
command with ``true``).
"""

from __future__ import annotations

import shlex
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .proc import default_python

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib

CONFIG_FILENAME = "proofcheck.toml"


class ConfigError(ValueError):
    """Raised for invalid configuration values."""


def _as_command(value: Any, key: str) -> list[str] | None:
    """Normalize a command given as a string (shlex-split) or a list of strings."""
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ConfigError(f"{key} must be a string or a list of strings")


def _as_str_list(value: Any, key: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ConfigError(f"{key} must be a list of strings")


def _as_number(value: Any, key: str, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be a number")
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}")
    return value


@dataclass
class LLMConfig:
    """Settings for the optional LLM claim extractor (off by default)."""

    enabled: bool = False
    provider: str = ""  # "openai" (any OpenAI-compatible endpoint) or "anthropic"
    model: str = ""
    base_url: str = ""
    timeout: float = 60


@dataclass
class Config:
    """Effective proofcheck configuration."""

    runner: str = "pytest"
    test_command: list[str] | None = None
    test_env: dict[str, str] = field(default_factory=dict)
    test_patterns: list[str] | None = None
    test_module_patterns: list[str] | None = None
    setup_command: list[str] | None = None
    timeout: float = 600
    benchmark_command: list[str] | None = None
    benchmark_runs: int = 5
    benchmark_timeout: float = 600
    large_diff_threshold: int = 1000
    repro_enabled: bool = True
    repro_timeout: float = 60
    repro_python: str = field(default_factory=default_python)
    max_issues: int = 5
    llm: LLMConfig = field(default_factory=LLMConfig)
    source: str = "defaults"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            out[f.name] = v.__dict__.copy() if isinstance(v, LLMConfig) else v
        return out


_SIMPLE_KEYS = {
    "runner", "test_command", "test_env", "test_patterns", "test_module_patterns", "setup_command",
    "timeout", "benchmark_command", "benchmark_runs", "benchmark_timeout", "large_diff_threshold",
    "max_issues", "repro", "llm",
}
_SIMPLE_KEYS.add("agent")  # the [agent] table; validated by proofcheck.agent.config


def config_from_dict(data: dict[str, Any], source: str = "<dict>") -> Config:
    """Build a :class:`Config` from parsed TOML data, validating every key."""
    if "proofcheck" in data and isinstance(data["proofcheck"], dict) and len(data) == 1:
        data = data["proofcheck"]
    unknown = set(data) - _SIMPLE_KEYS
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(sorted(unknown))}")
    if "agent" in data:
        _validate_agent_table(data["agent"], source)
    cfg = Config(source=source)
    if "runner" in data:
        if not isinstance(data["runner"], str):
            raise ConfigError("runner must be a string")
        cfg.runner = data["runner"]
    cfg.test_command = _as_command(data.get("test_command"), "test_command")
    cfg.setup_command = _as_command(data.get("setup_command"), "setup_command")
    cfg.benchmark_command = _as_command(data.get("benchmark_command"), "benchmark_command")
    if "test_env" in data:
        env = data["test_env"]
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ConfigError("test_env must be a table of string values")
        cfg.test_env = dict(env)
    if "test_patterns" in data:
        cfg.test_patterns = _as_str_list(data["test_patterns"], "test_patterns")
    if "test_module_patterns" in data:
        cfg.test_module_patterns = _as_str_list(data["test_module_patterns"], "test_module_patterns")
    if "timeout" in data:
        cfg.timeout = _as_number(data["timeout"], "timeout", 1)
    if "benchmark_runs" in data:
        cfg.benchmark_runs = int(_as_number(data["benchmark_runs"], "benchmark_runs", 1))
    if "benchmark_timeout" in data:
        cfg.benchmark_timeout = _as_number(data["benchmark_timeout"], "benchmark_timeout", 1)
    if "large_diff_threshold" in data:
        cfg.large_diff_threshold = int(_as_number(data["large_diff_threshold"], "large_diff_threshold", 1))
    if "max_issues" in data:
        cfg.max_issues = int(_as_number(data["max_issues"], "max_issues", 0))
    repro = data.get("repro", {})
    if not isinstance(repro, dict):
        raise ConfigError("[repro] must be a table")
    if "enabled" in repro:
        cfg.repro_enabled = bool(repro["enabled"])
    if "timeout" in repro:
        cfg.repro_timeout = _as_number(repro["timeout"], "repro.timeout", 1)
    if "python" in repro:
        if not isinstance(repro["python"], str):
            raise ConfigError("repro.python must be a string")
        cfg.repro_python = repro["python"]
    llm = data.get("llm", {})
    if not isinstance(llm, dict):
        raise ConfigError("[llm] must be a table")
    for key in ("provider", "model", "base_url"):
        if key in llm:
            if not isinstance(llm[key], str):
                raise ConfigError(f"llm.{key} must be a string")
            setattr(cfg.llm, key, llm[key])
    if "enabled" in llm:
        cfg.llm.enabled = bool(llm["enabled"])
    if "timeout" in llm:
        cfg.llm.timeout = _as_number(llm["timeout"], "llm.timeout", 1)
    if "api_key" in llm:
        raise ConfigError("never put API keys in proofcheck.toml; use the PROOFCHECK_LLM_API_KEY environment variable")
    return cfg


def _validate_agent_table(table: Any, source: str) -> None:
    """Fail early on an invalid ``[agent]`` table (it is used by ``proofcheck agent``)."""
    from .agent.config import AgentConfigError, parse_agent_config

    try:
        parse_agent_config(table, source)
    except AgentConfigError as exc:
        raise ConfigError(str(exc)) from exc


def parse_config_text(text: str, source: str, pyproject: bool = False) -> Config | None:
    """Parse TOML text. For ``pyproject.toml`` only ``[tool.proofcheck]`` is used."""
    data = tomllib.loads(text)
    if pyproject:
        section = data.get("tool", {}).get("proofcheck")
        if section is None:
            return None
        return config_from_dict(section, source=f"{source} [tool.proofcheck]")
    return config_from_dict(data, source=source)


def load_config_file(path: str | Path) -> Config:
    """Load configuration from an explicit file path."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    cfg = parse_config_text(text, str(p), pyproject=p.name == "pyproject.toml")
    return cfg if cfg is not None else Config(source=f"{p} (no [tool.proofcheck])")


def load_config_from_ref(repo: str | Path, ref: str) -> Config:
    """Load configuration as it exists at ``ref`` (normally the PR base commit)."""
    from .git import show_file

    raw = show_file(repo, ref, CONFIG_FILENAME)
    if raw is not None:
        return parse_config_text(raw.decode("utf-8"), f"{CONFIG_FILENAME}@{ref[:12]}") or Config()
    raw = show_file(repo, ref, "pyproject.toml")
    if raw is not None:
        try:
            cfg = parse_config_text(raw.decode("utf-8"), f"pyproject.toml@{ref[:12]}", pyproject=True)
        except tomllib.TOMLDecodeError:
            cfg = None
        if cfg is not None:
            return cfg
    return Config()
