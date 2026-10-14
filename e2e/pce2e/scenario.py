"""Scenario sets: loading and validation.

Layout of a set (see e2e/README.md)::

    <set>/
      set.toml              optional: description, allowed paths
      _base/files/...       overlay committed first, on the base branch
      <scenario>/
        scenario.toml       PR title/body, expectations
        files/...           overlay committed on the head branch, or
        patch.diff          a git patch applied on the head branch
        issue.md            optional: body of an issue opened first

Directories whose name starts with ``_`` or ``.`` are not scenarios.
"""

from __future__ import annotations

import fnmatch
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
DEFAULT_ALLOWED_PATHS = ("e2e_toy/*",)
MAX_FILE_BYTES = 1_000_000

#: Expectations that only the online run can check (the comment workflow adds
#: labels and trusted warnings; the agent runs in its own workflow).
ONLINE_ONLY = ("label", "warnings", "agent_status")

_STR_OR_LIST = "str_or_list"
_INT = "int"
_BOOL = "bool"
_STR_LIST = "str_list"
_CLAIMS = "claims"

EXPECT_KEYS: dict[str, str] = {
    "verdict": _STR_OR_LIST,
    "label": _STR_OR_LIST,
    "tests_status": _STR_OR_LIST,
    "min_fail_to_pass": _INT,
    "max_fail_to_pass": _INT,
    "min_pass_to_pass": _INT,
    "max_pass_to_pass": _INT,
    "min_fail_on_head": _INT,
    "max_fail_on_head": _INT,
    "ablation_status": _STR_OR_LIST,
    "min_ablation_required": _INT,
    "max_ablation_required": _INT,
    "min_ablation_not_exercised": _INT,
    "max_ablation_not_exercised": _INT,
    "agent_status": _STR_OR_LIST,
    "flags": _STR_LIST,
    "absent_flags": _STR_LIST,
    "repro": _STR_LIST,
    "warnings": _BOOL,
    "claim_count": _INT,
    "claims": _CLAIMS,
    "card_contains": _STR_LIST,
    "card_not_contains": _STR_LIST,
}
CLAIM_KEYS = {"type": _STR_OR_LIST, "status": _STR_OR_LIST, "verified_by": _STR_OR_LIST, "text_contains": _STR_OR_LIST}
SCENARIO_KEYS = {"title", "body", "description", "draft", "labels", "delete", "issue", "wait", "expected", "expected_local"}
SET_KEYS = {"description", "allowed_paths"}


class ScenarioError(ValueError):
    """A scenario or scenario set is malformed."""


@dataclass
class Scenario:
    name: str
    path: Path
    title: str
    body: str
    description: str = ""
    draft: bool = False
    labels: list[str] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    overlay: Path | None = None
    patch: Path | None = None
    issue_title: str | None = None
    issue_body: str | None = None
    wait_agent: bool = False
    agent_grace: int = 1200
    expected: dict[str, Any] = field(default_factory=dict)
    expected_local: dict[str, Any] = field(default_factory=dict)

    @property
    def has_issue(self) -> bool:
        return self.issue_body is not None

    def expectations(self, mode: str, require_agent: bool = False) -> dict[str, Any]:
        """Expectations for ``mode`` ("online" or "local").

        ``require_agent`` (online): for scenarios that wait for the agent,
        "none" is no longer an acceptable agent status.
        """
        if mode == "online":
            out = dict(self.expected)
            if require_agent and self.wait_agent and "agent_status" in out:
                allowed = out["agent_status"] if isinstance(out["agent_status"], list) else [out["agent_status"]]
                strict = [a for a in allowed if a.lower() not in ("none", "absent")]
                if strict:
                    out["agent_status"] = strict
            return out
        out = {k: v for k, v in self.expected.items() if k not in ONLINE_ONLY}
        out.update(self.expected_local)
        return out


@dataclass
class ScenarioSet:
    name: str
    path: Path
    scenarios: list[Scenario]
    description: str = ""
    base_overlay: Path | None = None
    allowed_paths: tuple[str, ...] = DEFAULT_ALLOWED_PATHS

    def path_allowed(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, pat) for pat in self.allowed_paths)

    def select(self, only: list[str] | None) -> list[Scenario]:
        if not only:
            return list(self.scenarios)
        names = {s.name for s in self.scenarios}
        unknown = [n for n in only if n not in names]
        if unknown:
            raise ScenarioError(f"unknown scenario(s): {', '.join(unknown)} (available: {', '.join(sorted(names))})")
        return [s for s in self.scenarios if s.name in only]


def substitute(text: str, issue: int | None) -> str:
    """Replace ``{issue}`` with the issue number (and nothing else: bodies may contain braces)."""
    return text.replace("{issue}", str(issue) if issue is not None else "?")


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ScenarioError(f"{path}: {exc}") from exc


def check_rel_path(path: str, where: str) -> str:
    """A safe, relative POSIX path inside the repository (no ``..``, not ``.git``)."""
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise ScenarioError(f"{where}: unsafe path {path!r}")
    parts = PurePosixPath(path).parts
    if any(p in ("", ".", "..") for p in path.split("/")) or parts[0] == ".git":
        raise ScenarioError(f"{where}: unsafe path {path!r}")
    return path


def overlay_files(root: Path) -> list[str]:
    """Relative POSIX paths of the regular files below ``root``; symlinks are rejected."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for d in list(dirnames):
            if (Path(dirpath) / d).is_symlink():
                raise ScenarioError(f"{root}: symlinks are not allowed in overlays ({d})")
        for f in filenames:
            p = Path(dirpath) / f
            if p.is_symlink() or not p.is_file():
                raise ScenarioError(f"{root}: only regular files are allowed in overlays ({p.relative_to(root)})")
            if p.stat().st_size > MAX_FILE_BYTES:
                raise ScenarioError(f"{p}: file too large for an e2e overlay")
            rel = p.relative_to(root).as_posix()
            if "__pycache__" in rel.split("/") or rel.endswith(".pyc"):
                continue
            out.append(check_rel_path(rel, str(root)))
    return sorted(out)


_DIFF_PATH_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)


def patch_paths(patch_text: str) -> list[str]:
    """Paths touched by a git patch (both sides of every ``diff --git`` header)."""
    out: set[str] = set()
    for a, b in _DIFF_PATH_RE.findall(patch_text):
        out.update((a, b))
    for m in re.finditer(r"^(?:rename|copy) (?:from|to) (.+)$", patch_text, re.M):
        out.add(m.group(1))
    return sorted(out)


def _check_value(kind: str, value: Any, where: str) -> Any:
    if kind == _STR_OR_LIST:
        if isinstance(value, str) or (isinstance(value, list) and value and all(isinstance(v, str) for v in value)):
            return value
        raise ScenarioError(f"{where}: expected a string or a non-empty list of strings")
    if kind == _INT:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        raise ScenarioError(f"{where}: expected a non-negative integer")
    if kind == _BOOL:
        if isinstance(value, bool):
            return value
        raise ScenarioError(f"{where}: expected true or false")
    if kind == _STR_LIST:
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return value
        raise ScenarioError(f"{where}: expected a list of strings")
    if kind == _CLAIMS:
        if not isinstance(value, list):
            raise ScenarioError(f"{where}: expected a list of claim tables")
        out = []
        for i, c in enumerate(value):
            if not isinstance(c, dict) or "type" not in c:
                raise ScenarioError(f"{where}[{i}]: each claim needs at least a `type`")
            for k, v in c.items():
                if k not in CLAIM_KEYS:
                    raise ScenarioError(f"{where}[{i}]: unknown key {k!r} (allowed: {', '.join(CLAIM_KEYS)})")
                _check_value(CLAIM_KEYS[k], v, f"{where}[{i}].{k}")
            out.append(dict(c))
        return out
    raise AssertionError(kind)  # pragma: no cover


def validate_expected(data: Any, where: str) -> dict[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ScenarioError(f"{where}: expected a table")
    out = {}
    for k, v in data.items():
        if k not in EXPECT_KEYS:
            raise ScenarioError(f"{where}: unknown expectation {k!r} (allowed: {', '.join(EXPECT_KEYS)})")
        out[k] = _check_value(EXPECT_KEYS[k], v, f"{where}.{k}")
    for lo, hi in (("min_fail_to_pass", "max_fail_to_pass"), ("min_pass_to_pass", "max_pass_to_pass"),
                   ("min_fail_on_head", "max_fail_on_head"), ("min_ablation_required", "max_ablation_required"),
                   ("min_ablation_not_exercised", "max_ablation_not_exercised")):
        if lo in out and hi in out and out[lo] > out[hi]:
            raise ScenarioError(f"{where}: {lo} > {hi}")
    return out


def load_scenario(path: Path, allowed: ScenarioSet | None = None) -> Scenario:
    """Load ``<path>/scenario.toml`` and the scenario's change."""
    name = path.name
    if not NAME_RE.match(name):
        raise ScenarioError(f"{path}: scenario directory names must match {NAME_RE.pattern}")
    toml = path / "scenario.toml"
    if not toml.is_file():
        raise ScenarioError(f"{path}: missing scenario.toml")
    d = _load_toml(toml)
    where = f"{name}/scenario.toml"
    unknown = set(d) - SCENARIO_KEYS
    if unknown:
        raise ScenarioError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    title = d.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ScenarioError(f"{where}: `title` (the PR title) is required")
    body = d.get("body", "")
    if not isinstance(body, str):
        raise ScenarioError(f"{where}: `body` must be a string")
    s = Scenario(name=name, path=path, title=title.strip(), body=body)
    s.description = str(d.get("description", ""))
    s.draft = _check_value(_BOOL, d.get("draft", False), f"{where}.draft")
    s.labels = _check_value(_STR_LIST, d.get("labels", []), f"{where}.labels")
    s.delete = [check_rel_path(p, f"{where}.delete") for p in _check_value(_STR_LIST, d.get("delete", []), f"{where}.delete")]

    overlay, patch = path / "files", path / "patch.diff"
    if overlay.is_dir() and patch.is_file():
        raise ScenarioError(f"{path}: use either files/ or patch.diff, not both")
    if overlay.is_dir():
        s.overlay = overlay
        changed = overlay_files(overlay)
    elif patch.is_file():
        s.patch = patch
        text = patch.read_text(encoding="utf-8")
        if "\nnew file mode 120000" in text or "\nnew mode 120000" in text:
            raise ScenarioError(f"{patch}: patches must not create symlinks")
        changed = [check_rel_path(p, str(patch)) for p in patch_paths(text)]
        if not changed:
            raise ScenarioError(f"{patch}: no `diff --git` headers found")
    else:
        changed = []
    changed += s.delete
    if not changed:
        raise ScenarioError(f"{path}: the scenario changes nothing (add files/, patch.diff or `delete`)")
    if allowed is not None:
        bad = [p for p in changed if not allowed.path_allowed(p)]
        if bad:
            raise ScenarioError(f"{path}: paths outside the set's allowed_paths {list(allowed.allowed_paths)}: {', '.join(bad)}")

    issue = d.get("issue")
    issue_md = path / "issue.md"
    if issue is not None or issue_md.is_file():
        if not issue_md.is_file():
            raise ScenarioError(f"{where}: [issue] given but issue.md is missing")
        if not isinstance(issue, dict) or not isinstance(issue.get("title"), str) or not issue["title"].strip():
            raise ScenarioError(f"{where}: issue.md needs an [issue] table with a `title`")
        if set(issue) - {"title"}:
            raise ScenarioError(f"{where}: [issue] only takes `title` (the body is issue.md)")
        s.issue_title = issue["title"].strip()
        s.issue_body = issue_md.read_text(encoding="utf-8")
    elif "{issue}" in s.title + s.body:
        raise ScenarioError(f"{where}: the PR text uses {{issue}} but the scenario has no issue")

    wait = d.get("wait") or {}
    if not isinstance(wait, dict) or set(wait) - {"agent", "agent_grace"}:
        raise ScenarioError(f"{where}: [wait] takes `agent` and `agent_grace`")
    s.wait_agent = _check_value(_BOOL, wait.get("agent", False), f"{where}.wait.agent")
    s.agent_grace = _check_value(_INT, wait.get("agent_grace", 1200), f"{where}.wait.agent_grace")

    if "expected" not in d:
        raise ScenarioError(f"{where}: an [expected] table is required")
    s.expected = validate_expected(d.get("expected"), f"{where}.expected")
    if not s.expected:
        raise ScenarioError(f"{where}: [expected] is empty")
    s.expected_local = validate_expected(d.get("expected_local"), f"{where}.expected_local")
    return s


def load_set(path: str | Path) -> ScenarioSet:
    """Load a scenario set directory."""
    root = Path(path).resolve()
    if not root.is_dir():
        raise ScenarioError(f"{path}: not a directory")
    meta: dict[str, Any] = {}
    if (root / "set.toml").is_file():
        meta = _load_toml(root / "set.toml")
        unknown = set(meta) - SET_KEYS
        if unknown:
            raise ScenarioError(f"set.toml: unknown key(s) {', '.join(sorted(unknown))}")
    allowed = tuple(_check_value(_STR_LIST, meta.get("allowed_paths", list(DEFAULT_ALLOWED_PATHS)), "set.toml.allowed_paths"))
    if not allowed:
        raise ScenarioError("set.toml: allowed_paths must not be empty")
    sset = ScenarioSet(name=root.name, path=root, scenarios=[], description=str(meta.get("description", "")),
                       allowed_paths=allowed)
    base = root / "_base" / "files"
    if base.is_dir():
        bad = [p for p in overlay_files(base) if not sset.path_allowed(p)]
        if bad:
            raise ScenarioError(f"{base}: paths outside allowed_paths: {', '.join(bad)}")
        sset.base_overlay = base
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.name.startswith(("_", ".")) and child.name != "__pycache__":
            sset.scenarios.append(load_scenario(child, sset))
    if not sset.scenarios:
        raise ScenarioError(f"{path}: no scenarios found")
    return sset
