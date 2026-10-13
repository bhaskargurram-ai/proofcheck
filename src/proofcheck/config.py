"""Configuration loading (``proofcheck.toml`` or ``[tool.proofcheck]`` in pyproject).

By default the configuration is read from the **base** commit of the pull
request, not from the PR head. A pull request therefore cannot weaken its own
check by editing ``proofcheck.toml`` (for example by replacing the test
command with ``true``).
"""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass, field, fields, is_dataclass
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


DEFAULT_ABLATION_EXCLUDE = ("*.md", "*.rst", "*.txt", "docs/*", "LICENSE*", "CHANGELOG*", ".github/*")


@dataclass
class AblationConfig:
    """Settings for the diff evidence map (hunk ablation)."""

    enabled: bool = True
    max_hunks: int = 12
    time_budget_s: float = 300
    #: Changed files matching these patterns are not ablated (documentation and similar).
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_ABLATION_EXCLUDE))


#: Keys of a ``[[project]]`` table.
PROJECT_KEYS = frozenset({
    "name", "path", "runner", "test_command", "setup_command", "test_paths", "test_patterns", "test_module_patterns",
    "timeout", "test_env", "cargo_target_dir",
})
#: Top-level keys that describe the implicit root project (conflict with a [[project]] whose path is ".").
ROOT_PROJECT_KEYS = ("runner", "test_command", "setup_command", "test_patterns", "test_module_patterns")
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
ROOT_PROJECT_NAME = "root"


@dataclass
class ProjectConfig:
    """One ``[[project]]`` table: a test project in a subdirectory (or ``path = "."``)."""

    name: str
    path: str  # POSIX, relative to the repository root; "." for the root
    runner: str = "pytest"
    test_command: list[str] | None = None
    setup_command: list[str] | None = None
    test_patterns: list[str] | None = None  # from `test_paths` (or `test_patterns`), relative to the project
    test_module_patterns: list[str] | None = None
    timeout: float | None = None  # None: the top-level timeout (cargo: 1800 unless `timeout` is set at the top level)
    test_env: dict[str, str] = field(default_factory=dict)
    cargo_target_dir: str | None = None


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
    ablation: AblationConfig = field(default_factory=AblationConfig)
    source: str = "defaults"
    #: ``[[project]]`` tables (empty: the top-level keys describe the only project, at the root).
    projects: list[ProjectConfig] = field(default_factory=list)
    #: Shared CARGO_TARGET_DIR for cargo projects (None: $CARGO_TARGET_DIR, else <tmp>/proofcheck-cargo-target).
    cargo_target_dir: str | None = None
    #: True when ``timeout`` was set in the configuration (cargo projects otherwise default to 1800 s).
    timeout_explicit: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, list) and v and is_dataclass(v[0]):
                v = [x.__dict__.copy() for x in v]
            out[f.name] = v.__dict__.copy() if is_dataclass(v) else v
        return out


_SIMPLE_KEYS = {
    "runner", "test_command", "test_env", "test_patterns", "test_module_patterns", "setup_command",
    "timeout", "benchmark_command", "benchmark_runs", "benchmark_timeout", "large_diff_threshold",
    "max_issues", "repro", "llm",
}
_SIMPLE_KEYS |= {"ablation", "agent"}  # [agent] is validated by proofcheck.agent.config
_SIMPLE_KEYS |= {"project", "cargo_target_dir"}  # [[project]] tables (multi-project repositories)


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
        cfg.timeout_explicit = True
    if "cargo_target_dir" in data:
        cfg.cargo_target_dir = _as_nonempty_str(data["cargo_target_dir"], "cargo_target_dir")
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
    _parse_ablation(data.get("ablation", {}), cfg.ablation)
    if "project" in data:
        cfg.projects = _parse_projects(data["project"])
        if any(p.path == "." for p in cfg.projects):
            clash = [k for k in ROOT_PROJECT_KEYS if k in data]
            if clash:
                raise ConfigError(f"a [[project]] has path \".\", so the top-level key(s) {', '.join(clash)} "
                                  "would describe the same project: move them into that [[project]] table")
    return cfg


def _as_nonempty_str(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string")
    return value


def normalize_project_path(value: Any, key: str = "project.path") -> str:
    """Validate a project directory: relative POSIX path inside the repository, no ``..``."""
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    p = value.strip()
    if "\\" in p or p.startswith("/") or re.match(r"^[A-Za-z]:", p) or "\x00" in p:
        raise ConfigError(f"{key} must be a relative POSIX path inside the repository (got {value!r})")
    while p.startswith("./"):
        p = p[2:]
    p = p.rstrip("/")
    if p in ("", "."):
        return "."
    parts = p.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ConfigError(f"{key} must not contain '..', '.' or empty components (got {value!r})")
    return p


def _parse_projects(tables: Any) -> list[ProjectConfig]:
    from .runners import RUNNER_NAMES

    if not isinstance(tables, list) or not all(isinstance(t, dict) for t in tables):
        raise ConfigError("project must be an array of tables ([[project]])")
    if len(tables) > 20:
        raise ConfigError("at most 20 [[project]] tables are supported")
    out: list[ProjectConfig] = []
    for i, t in enumerate(tables):
        where = f"project[{i}]"
        unknown = set(t) - PROJECT_KEYS
        if unknown:
            raise ConfigError(f"unknown {where} key(s): {', '.join(sorted(unknown))}")
        name = t.get("name")
        if not isinstance(name, str) or not _PROJECT_NAME_RE.match(name):
            raise ConfigError(f"{where}.name must match {_PROJECT_NAME_RE.pattern} (letters, digits, '_', '.', '-')")
        if "path" not in t:
            raise ConfigError(f"{where}.path is required")
        pc = ProjectConfig(name=name, path=normalize_project_path(t["path"], f"{where}.path"))
        runner = t.get("runner", "pytest")
        if runner not in RUNNER_NAMES:
            raise ConfigError(f"{where}.runner must be one of {', '.join(RUNNER_NAMES)}")
        pc.runner = runner
        pc.test_command = _as_command(t.get("test_command"), f"{where}.test_command")
        pc.setup_command = _as_command(t.get("setup_command"), f"{where}.setup_command")
        if "test_paths" in t and "test_patterns" in t:
            raise ConfigError(f"{where}: use test_paths or test_patterns, not both")
        for key in ("test_paths", "test_patterns"):
            if key in t:
                pc.test_patterns = _as_str_list(t[key], f"{where}.{key}")
        if "test_module_patterns" in t:
            pc.test_module_patterns = _as_str_list(t["test_module_patterns"], f"{where}.test_module_patterns")
        if "timeout" in t:
            pc.timeout = _as_number(t["timeout"], f"{where}.timeout", 1)
        if "test_env" in t:
            env = t["test_env"]
            if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
                raise ConfigError(f"{where}.test_env must be a table of string values")
            pc.test_env = dict(env)
        if "cargo_target_dir" in t:
            pc.cargo_target_dir = _as_nonempty_str(t["cargo_target_dir"], f"{where}.cargo_target_dir")
        out.append(pc)
    for attr in ("name", "path"):
        seen: set[str] = set()
        for pc in out:
            v = getattr(pc, attr)
            if v in seen:
                raise ConfigError(f"two [[project]] tables have the same {attr} {v!r}")
            seen.add(v)
    if any(pc.name == ROOT_PROJECT_NAME and pc.path != "." for pc in out) and not any(pc.path == "." for pc in out):
        raise ConfigError(f"project name {ROOT_PROJECT_NAME!r} is reserved for the repository root (path \".\")")
    return out


def _parse_ablation(section: Any, out: AblationConfig) -> None:
    """Parse the ``[ablation]`` table into ``out``."""
    if not isinstance(section, dict):
        raise ConfigError("[ablation] must be a table")
    unknown = set(section) - {"enabled", "max_hunks", "time_budget_s", "exclude"}
    if unknown:
        raise ConfigError(f"unknown ablation key(s): {', '.join(sorted(unknown))}")
    if "enabled" in section:
        if not isinstance(section["enabled"], bool):
            raise ConfigError("ablation.enabled must be true or false")
        out.enabled = section["enabled"]
    if "max_hunks" in section:
        out.max_hunks = int(_as_number(section["max_hunks"], "ablation.max_hunks", 0))
    if "time_budget_s" in section:
        out.time_budget_s = _as_number(section["time_budget_s"], "ablation.time_budget_s", 0)
    if "exclude" in section:
        out.exclude = _as_str_list(section["exclude"], "ablation.exclude")


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
