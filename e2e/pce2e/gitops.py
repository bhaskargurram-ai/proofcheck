"""git helpers for the harness: argument lists only (never a shell).

The token never appears in a command line or a remote URL. For pushes it is
handed to git as an ``http.<server>/.extraheader`` through the
``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_n``/``GIT_CONFIG_VALUE_n`` environment
(the mechanism actions/checkout uses), and all output is redacted before it
is logged or raised.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .scenario import Scenario, ScenarioError, ScenarioSet, overlay_files

GIT_IDENTITY = ("-c", "user.name=proofcheck-e2e", "-c", "user.email=proofcheck-e2e@users.noreply.github.com",
                "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "-c", "init.defaultBranch=main")

#: Variables that redirect git to another repository or inject configuration.
_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                 "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
                 "GIT_ASKPASS", "SSH_ASKPASS")


class GitError(RuntimeError):
    pass


class Redactor:
    """Replaces secrets (and their base64 forms) in text."""

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        self.secrets: list[str] = []
        for s in secrets:
            self.add(s)

    def add(self, secret: str | None) -> None:
        if not secret or len(secret) < 4:
            return
        forms = {secret, base64.b64encode(secret.encode()).decode(),
                 base64.b64encode(f"x-access-token:{secret}".encode()).decode()}
        for f in forms:
            if f not in self.secrets:
                self.secrets.append(f)
        self.secrets.sort(key=len, reverse=True)

    def __call__(self, text: object) -> str:
        s = str(text)
        for secret in self.secrets:
            s = s.replace(secret, "***")
        return s


def clean_git_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment without variables that redirect or configure git."""
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if k not in _GIT_ENV_DROP and not k.startswith("GIT_CONFIG_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def auth_env(token: str, server_url: str = "https://github.com") -> dict[str, str]:
    """Environment entries that authenticate git over HTTPS without exposing the token in argv or URLs."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{server_url.rstrip('/')}/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


class Git:
    """Runs git in one repository directory."""

    def __init__(self, cwd: Path, env: Mapping[str, str] | None = None, redact: Callable[[object], str] | None = None,
                 timeout: float = 600) -> None:
        self.cwd = Path(cwd)
        self.env = clean_git_env()
        self.env.update(env or {})
        self.redact = redact or Redactor()
        self.timeout = timeout

    def __call__(self, *args: str, check: bool = True) -> str:
        cmd = ["git", *GIT_IDENTITY, "-C", str(self.cwd), *args]
        try:
            res = subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitError(self.redact(f"git {args[0] if args else ''}: {exc}")) from None
        if check and res.returncode != 0:
            msg = (res.stderr or res.stdout).strip()[-1500:]
            raise GitError(self.redact(f"git {' '.join(args[:3])} failed ({res.returncode}): {msg}"))
        return res.stdout.strip()

    def init(self) -> None:
        self.cwd.mkdir(parents=True, exist_ok=True)
        self("init", "-q")


def apply_overlay(repo: Path, overlay: Path) -> list[str]:
    """Copy the regular files below ``overlay`` into ``repo`` (paths were validated when loading)."""
    written = []
    for rel in overlay_files(overlay):
        dest = repo / rel
        # Never write through a symlink that already exists in the target repository.
        cur = repo
        for part in Path(rel).parts[:-1]:
            cur = cur / part
            if cur.is_symlink():
                raise ScenarioError(f"refusing to write {rel}: {cur.relative_to(repo)} is a symlink in the repository")
        if dest.is_symlink():
            raise ScenarioError(f"refusing to overwrite symlink {rel}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(overlay / rel, dest)
        written.append(rel)
    return written


def apply_change(git: Git, scenario: Scenario) -> None:
    """Apply a scenario's change (overlay or patch, then deletions) to the work tree and index."""
    if scenario.overlay is not None:
        apply_overlay(git.cwd, scenario.overlay)
    if scenario.patch is not None:
        git("apply", "--index", "--whitespace=nowarn", str(scenario.patch.resolve()))
    for rel in scenario.delete:
        p = git.cwd / rel
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif p.exists():
            raise ScenarioError(f"{scenario.name}: `delete` entry {rel} is not a file")
        else:
            raise ScenarioError(f"{scenario.name}: `delete` entry {rel} does not exist")


def commit_all(git: Git, message: str, allow_empty: bool = False) -> str:
    git("add", "-A")
    args = ["commit", "-q", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    git(*args)
    return git("rev-parse", "HEAD")


def build_branches(git: Git, sset: ScenarioSet, scenario: Scenario, start: str, base_branch: str,
                   head_branch: str) -> tuple[str, str]:
    """Create ``base_branch`` (start + the set's base overlay) and ``head_branch`` (+ the scenario change).

    Returns ``(base_sha, head_sha)``. The base commit is kept even when the
    overlay changes nothing, so every scenario has the same two-commit shape.
    """
    git("checkout", "-q", "-B", base_branch, start)
    git("clean", "-q", "-fdx")
    if sset.base_overlay is not None:
        apply_overlay(git.cwd, sset.base_overlay)
    base_sha = commit_all(git, f"proofcheck e2e: base for {scenario.name}", allow_empty=True)
    git("checkout", "-q", "-B", head_branch)
    apply_change(git, scenario)
    git("add", "-A")
    if not git("diff", "--cached", "--name-only"):
        raise ScenarioError(f"{scenario.name}: the change is empty on top of the base")
    head_sha = commit_all(git, f"proofcheck e2e: {scenario.title}")
    return base_sha, head_sha


def diffstat(git: Git, base: str, head: str) -> list[str]:
    out = git("diff", "--numstat", base, head)
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows.append(f"+{parts[0]}/-{parts[1]} {parts[2]}")
    return rows
