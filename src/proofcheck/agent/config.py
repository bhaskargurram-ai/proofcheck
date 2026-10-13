"""The ``[agent]`` configuration table.

It lives in the same file as the rest of the configuration
(``proofcheck.toml`` or ``[tool.proofcheck]`` in ``pyproject.toml``) and, like
it, is read from the pull request's **base** commit by default, so a PR
cannot raise its own budgets or switch the agent on for itself.

API keys never belong in the file: the key is read from
``PROOFCHECK_LLM_API_KEY`` only.
"""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import DEFAULT_VARIANT_COUNT, VARIANTS

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib

WHEN_CHOICES = ("missing-evidence", "always")
DEFAULT_TEST_DIR = "tests/proofcheck_agent"


class AgentConfigError(ValueError):
    """Raised for an invalid ``[agent]`` table."""


@dataclass
class AgentConfig:
    """Effective agent settings (all budgets are per run unless noted)."""

    enabled: bool = False
    variants: list[str] = field(default_factory=lambda: list(VARIANTS[:DEFAULT_VARIANT_COUNT]))
    max_rounds: int = 2
    max_tool_calls: int = 25  # per variant and round
    when: str = "missing-evidence"
    test_dir: str = DEFAULT_TEST_DIR
    max_output_tokens: int = 4096  # per model response
    max_total_tokens: int = 1_500_000  # input + output over the whole run
    max_cost_usd: float = 0.0  # 0 = no cost cap (only enforced when prices are set)
    price_input_per_mtok: float | None = None  # USD per million input tokens
    price_output_per_mtok: float | None = None  # USD per million output tokens
    temperature: float | None = None  # omitted from requests unless set
    request_timeout: float = 120
    max_retries: int = 4
    test_timeout: float = 120  # per candidate and commit
    source: str = "defaults"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _int(value: Any, key: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentConfigError(f"agent.{key} must be an integer")
    if not lo <= value <= hi:
        raise AgentConfigError(f"agent.{key} must be between {lo} and {hi}")
    return value


def _num(value: Any, key: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AgentConfigError(f"agent.{key} must be a number")
    if not lo <= value <= hi:
        raise AgentConfigError(f"agent.{key} must be between {lo} and {hi}")
    return float(value)


def validate_test_dir(value: Any) -> str:
    """A relative POSIX directory inside the repository (no ``..``, not absolute)."""
    if not isinstance(value, str) or not value.strip():
        raise AgentConfigError("agent.test_dir must be a non-empty string")
    s = value.strip().rstrip("/")
    parts = s.split("/")
    if s.startswith("/") or "\\" in s or ":" in s or any(part in ("", ".", "..") for part in parts):
        raise AgentConfigError("agent.test_dir must be a relative path inside the repository without '.' or '..'")
    if parts[0] == ".git" or any(not all(c.isalnum() or c in "_-." for c in part) for part in parts):
        raise AgentConfigError("agent.test_dir may only contain letters, digits, '_', '-' and '.'")
    if len(s) > 120:
        raise AgentConfigError("agent.test_dir is too long")
    return s


def _variants(value: Any) -> list[str]:
    if isinstance(value, int) and not isinstance(value, bool):
        n = _int(value, "variants", 1, len(VARIANTS))
        return list(VARIANTS[:n])
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        unknown = [v for v in value if v not in VARIANTS]
        if unknown:
            raise AgentConfigError(f"unknown agent variant(s): {', '.join(unknown)}; choose from {', '.join(VARIANTS)}")
        out: list[str] = []
        for v in value:
            if v not in out:
                out.append(v)
        return out
    raise AgentConfigError(f"agent.variants must be a number (1-{len(VARIANTS)}) or a list of variant names")


_KEYS = {
    "enabled", "variants", "max_rounds", "max_tool_calls", "when", "test_dir", "max_output_tokens", "max_total_tokens",
    "max_cost_usd", "price_input_per_mtok", "price_output_per_mtok", "temperature", "request_timeout", "max_retries",
    "test_timeout",
}


def parse_agent_config(data: Any, source: str = "<dict>") -> AgentConfig:
    """Validate an ``[agent]`` table and return an :class:`AgentConfig`."""
    if data is None:
        return AgentConfig(source=source)
    if not isinstance(data, dict):
        raise AgentConfigError("[agent] must be a table")
    if "api_key" in data:
        raise AgentConfigError("never put API keys in the config; use the PROOFCHECK_LLM_API_KEY environment variable")
    unknown = set(data) - _KEYS
    if unknown:
        raise AgentConfigError(f"unknown agent config key(s): {', '.join(sorted(unknown))}")
    cfg = AgentConfig(source=source)
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise AgentConfigError("agent.enabled must be true or false")
        cfg.enabled = data["enabled"]
    if "variants" in data:
        cfg.variants = _variants(data["variants"])
    if "max_rounds" in data:
        cfg.max_rounds = _int(data["max_rounds"], "max_rounds", 0, 5)
    if "max_tool_calls" in data:
        cfg.max_tool_calls = _int(data["max_tool_calls"], "max_tool_calls", 1, 100)
    if "when" in data:
        if data["when"] not in WHEN_CHOICES:
            raise AgentConfigError(f"agent.when must be one of {', '.join(WHEN_CHOICES)}")
        cfg.when = data["when"]
    if "test_dir" in data:
        cfg.test_dir = validate_test_dir(data["test_dir"])
    if "max_output_tokens" in data:
        cfg.max_output_tokens = _int(data["max_output_tokens"], "max_output_tokens", 256, 64_000)
    if "max_total_tokens" in data:
        cfg.max_total_tokens = _int(data["max_total_tokens"], "max_total_tokens", 1_000, 50_000_000)
    if "max_cost_usd" in data:
        cfg.max_cost_usd = _num(data["max_cost_usd"], "max_cost_usd", 0, 10_000)
    for key in ("price_input_per_mtok", "price_output_per_mtok"):
        if key in data:
            setattr(cfg, key, _num(data[key], key, 0, 10_000))
    if "temperature" in data:
        cfg.temperature = _num(data["temperature"], "temperature", 0, 2)
    if "request_timeout" in data:
        cfg.request_timeout = _num(data["request_timeout"], "request_timeout", 5, 1800)
    if "max_retries" in data:
        cfg.max_retries = _int(data["max_retries"], "max_retries", 0, 10)
    if "test_timeout" in data:
        cfg.test_timeout = _num(data["test_timeout"], "test_timeout", 1, 3600)
    return cfg


def _section(data: dict[str, Any], pyproject: bool) -> tuple[dict[str, Any] | None, bool]:
    """Return ``(agent_table, found_proofcheck_config)``."""
    if pyproject:
        section = data.get("tool", {}).get("proofcheck")
        if not isinstance(section, dict):
            return None, False
        return section.get("agent"), True
    if "proofcheck" in data and isinstance(data["proofcheck"], dict) and len(data) == 1:
        data = data["proofcheck"]
    return data.get("agent"), True


def agent_config_from_text(text: str, source: str, pyproject: bool = False) -> AgentConfig | None:
    """Parse the ``[agent]`` table from TOML text (``None`` if a pyproject has no proofcheck section)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise AgentConfigError(f"{source}: invalid TOML: {exc}") from exc
    table, found = _section(data, pyproject)
    if not found:
        return None
    return parse_agent_config(table, source=source)


def load_agent_config_file(path: str | Path) -> AgentConfig:
    p = Path(path)
    cfg = agent_config_from_text(p.read_text(encoding="utf-8"), str(p), pyproject=p.name == "pyproject.toml")
    return cfg or AgentConfig(source=f"{p} (no [tool.proofcheck])")


def load_agent_config_from_ref(repo: str | Path, ref: str) -> AgentConfig:
    """Load ``[agent]`` as it exists at ``ref`` (normally the PR base commit)."""
    from ..config import CONFIG_FILENAME
    from ..git import show_file

    raw = show_file(repo, ref, CONFIG_FILENAME)
    if raw is not None:
        return agent_config_from_text(raw.decode("utf-8"), f"{CONFIG_FILENAME}@{ref[:12]}") or AgentConfig()
    raw = show_file(repo, ref, "pyproject.toml")
    if raw is not None:
        text = raw.decode("utf-8", errors="replace")
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return AgentConfig()  # same leniency as the main config loader
        cfg = agent_config_from_text(text, f"pyproject.toml@{ref[:12]}", pyproject=True)
        if cfg is not None:
            return cfg
    return AgentConfig()
