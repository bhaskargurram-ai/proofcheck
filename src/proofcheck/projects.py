"""Multi-project repositories: several test projects, each in its own directory.

A repository can hold more than one test project, for example a Rust
workspace at the root and a Python SDK in ``adapters/python``. Each
``[[project]]`` table in the configuration describes one (name, directory,
runner, commands, timeout, environment); the top-level keys keep describing
the implicit root project, so single-project configurations and their
results are unchanged.

Rules:

* every changed file belongs to the *deepest* project whose directory
  contains it (the root project contains everything);
* test files are classified by that project's runner, using paths relative
  to the project directory;
* tests run with ``cwd = <worktree>/<project path>`` in the base and head
  worktrees (and in the ablation worktree);
* with more than one project, test ids are prefixed with the project name
  (``python-sdk:tests/test_client.py::test_x``) so that cards and the diff
  evidence map stay unambiguous.

API used by the fail-to-pass check, the diff evidence map and the agent:

* :func:`build_projects` -- ``Config`` -> ``[Project]``;
* :func:`project_for` -- the project a repository path belongs to;
* :meth:`Project.local` / :meth:`Project.repo_path` / :meth:`Project.workdir`;
* :func:`split_test_id` / :meth:`ProjectSet.project_of_test` -- the project of a (prefixed) test id;
* :class:`ProjectSet` -- a :class:`~proofcheck.runners.TestRunner`-compatible
  view over all projects that takes repository-relative paths and returns
  (prefixed) ids;
* :func:`run_projects_fail_to_pass` -- the fail-to-pass check over all projects.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import git as gitmod
from .config import ROOT_PROJECT_NAME, Config
from .failtopass import (
    STATUS_ERROR,
    STATUS_EVIDENCE,
    STATUS_FAIL_ON_HEAD,
    STATUS_NO_EVIDENCE,
    STATUS_NO_TESTS,
    TestCheck,
    run_fail_to_pass,
)
from .junit import TestOutcome
from .proc import run_command
from .runners import RunResult, TestRunner, get_runner

ID_SEPARATOR = ":"


@dataclass
class Project:
    """A test project: a directory of the repository with its own runner."""

    name: str
    path: str  # "." for the repository root, else a POSIX path without a trailing slash
    runner: TestRunner
    timeout: float
    setup_command: list[str] | None = None
    #: Variables added on top of the (scrubbed) environment for this project's commands.
    test_env: dict[str, str] = field(default_factory=dict)
    #: True when the project comes from a [[project]] table, False for the implicit root project.
    explicit: bool = False

    @property
    def is_root(self) -> bool:
        return self.path == "."

    def contains(self, repo_path: str) -> bool:
        return self.is_root or repo_path == self.path or repo_path.startswith(self.path + "/")

    def local(self, repo_path: str) -> str:
        """Repository-relative path -> project-relative path."""
        if self.is_root:
            return repo_path
        if not repo_path.startswith(self.path + "/"):
            raise ValueError(f"{repo_path!r} is not inside project {self.name!r} ({self.path})")
        return repo_path[len(self.path) + 1:]

    def repo_path(self, local_path: str) -> str:
        """Project-relative path -> repository-relative path."""
        return local_path if self.is_root else f"{self.path}/{local_path}"

    def workdir(self, worktree: Path) -> Path:
        """The directory tests run in, inside ``worktree``."""
        return Path(worktree) if self.is_root else Path(worktree) / self.path

    def env(self, env: Mapping[str, str]) -> dict[str, str]:
        out = dict(env)
        out.update(self.test_env)
        return out

    def summary(self) -> str:
        return f"{self.name} ({self.runner.name}, {'repository root' if self.is_root else self.path})"


def _resolve_dir(value: str | None, repo: str | Path | None) -> str | None:
    if not value:
        return None
    p = Path(os.path.expanduser(value))
    if not p.is_absolute() and repo is not None:
        p = Path(repo).resolve() / p
    return str(p)


def _make_runner(runner: str, command: Any, patterns: Any, modules: Any, target_dir: str | None) -> TestRunner:
    if runner == "cargo":
        return get_runner(runner, command, patterns, modules, target_dir=target_dir)
    return get_runner(runner, command, patterns, modules)


def build_projects(cfg: Config, repo: str | Path | None = None) -> list[Project]:
    """The projects described by ``cfg``: the ``[[project]]`` tables plus, unless one of
    them is at ``"."``, the implicit root project from the top-level keys (named ``root``).

    The root project comes first; explicit projects follow in configuration order.
    """
    target_dir = _resolve_dir(cfg.cargo_target_dir, repo)
    projects: list[Project] = []
    if not any(p.path == "." for p in cfg.projects):
        runner = _make_runner(cfg.runner, cfg.test_command, cfg.test_patterns, cfg.test_module_patterns, target_dir)
        timeout = cfg.timeout if cfg.timeout_explicit or runner.default_timeout is None else runner.default_timeout
        projects.append(Project(ROOT_PROJECT_NAME, ".", runner, float(timeout), cfg.setup_command, {}, explicit=False))
    for pc in cfg.projects:
        runner = _make_runner(pc.runner, pc.test_command, pc.test_patterns, pc.test_module_patterns,
                              _resolve_dir(pc.cargo_target_dir, repo) or target_dir)
        if pc.timeout is not None:
            timeout = pc.timeout
        elif cfg.timeout_explicit or runner.default_timeout is None:
            timeout = cfg.timeout
        else:
            timeout = runner.default_timeout
        projects.append(Project(pc.name, pc.path, runner, float(timeout), pc.setup_command, dict(pc.test_env), explicit=True))
    projects.sort(key=lambda p: (not p.is_root,))
    return projects


def project_for(projects: Sequence[Project], repo_path: str) -> Project | None:
    """The deepest project whose directory contains ``repo_path``."""
    best: Project | None = None
    for p in projects:
        if p.contains(repo_path) and (best is None or (not p.is_root and (best.is_root or len(p.path) > len(best.path)))):
            best = p
    return best


def partition(projects: Sequence[Project], changed: Sequence[gitmod.ChangedFile]) -> dict[str, list[gitmod.ChangedFile]]:
    """Changed files (repository-relative) grouped by the name of the project they belong to."""
    out: dict[str, list[gitmod.ChangedFile]] = {p.name: [] for p in projects}
    for f in changed:
        p = project_for(projects, f.path)
        if p is not None:
            out[p.name].append(f)
    return out


def prefix_test_id(project: str, test_id: str) -> str:
    return f"{project}{ID_SEPARATOR}{test_id}"


def split_test_id(test_id: str, projects: Sequence[Project]) -> tuple[Project | None, str]:
    """``("python-sdk:tests/x.py::t")`` -> ``(<python-sdk>, "tests/x.py::t")``; unprefixed ids belong to
    the only project (or to nobody when there are several)."""
    if len(projects) == 1:
        return projects[0], test_id
    name, sep, rest = test_id.partition(ID_SEPARATOR)
    if sep:
        for p in projects:
            if p.name == name:
                return p, rest
    return None, test_id


class ProjectSet:
    """All projects behind the :class:`~proofcheck.runners.TestRunner` interface.

    Paths are repository-relative; ids are prefixed with the project name
    when there is more than one project. Used by the scope analysis and the
    diff evidence map, which work on repository paths.
    """

    __test__ = False

    def __init__(self, projects: Sequence[Project]) -> None:
        if not projects:
            raise ValueError("at least one project is required")
        self.projects = list(projects)
        self.prefix = len(self.projects) > 1
        self.name = ",".join(sorted({p.runner.name for p in self.projects}))

    # -- mapping -------------------------------------------------------------------
    def project_for(self, repo_path: str) -> Project | None:
        return project_for(self.projects, repo_path)

    def project_of_test(self, test_id: str) -> Project | None:
        return split_test_id(test_id, self.projects)[0]

    def full_id(self, project: Project, local_id: str) -> str:
        return prefix_test_id(project.name, local_id) if self.prefix else local_id

    # -- TestRunner interface ----------------------------------------------------
    def is_test_file(self, path: str) -> bool:
        p = self.project_for(path)
        if p is None or path == p.path:
            return False
        return p.runner.is_test_file(p.local(path))

    def is_test_module(self, path: str) -> bool:
        p = self.project_for(path)
        return p is not None and path != p.path and p.runner.is_test_module(p.local(path))

    def test_regions(self, path: str, source: str) -> list[tuple[int, int]] | None:
        p = self.project_for(path)
        if p is None or path == p.path:
            return []
        return p.runner.test_regions(p.local(path), source)

    def test_belongs_to(self, test_id: str, path: str) -> bool:
        p = self.project_for(path)
        if p is None:
            return False
        owner, local_id = split_test_id(test_id, self.projects)
        return owner is p and p.runner.test_belongs_to(local_id, p.local(path))

    def run(self, worktree: Path, test_files: Sequence[str], env: Mapping[str, str], timeout: float) -> RunResult:
        return self._run(worktree, test_files, None, env, timeout)

    def run_selected(self, worktree: Path, test_files: Sequence[str], test_ids: Sequence[str], env: Mapping[str, str],
                     timeout: float) -> RunResult:
        return self._run(worktree, test_files, list(test_ids), env, timeout)

    def _run(self, worktree: Path, test_files: Sequence[str], ids: list[str] | None, env: Mapping[str, str],
             timeout: float) -> RunResult:
        groups: dict[str, list[str]] = {}
        for f in test_files:
            p = self.project_for(f)
            if p is not None:
                groups.setdefault(p.name, []).append(p.local(f))
        merged = RunResult()
        errors: list[str] = []
        for p in self.projects:
            files = groups.get(p.name)
            if not files:
                continue
            wd = p.workdir(worktree)
            if ids is None:
                r = p.runner.run(wd, files, p.env(env), timeout)
            else:
                local_ids = [lid for owner, lid in (split_test_id(t, self.projects) for t in ids) if owner is p]
                r = p.runner.run_selected(wd, files, local_ids, p.env(env), timeout)
            merged.command = merged.command + (["&&"] if merged.command else []) + r.command
            if r.returncode or merged.returncode is None:
                merged.returncode = r.returncode
            merged.timed_out = merged.timed_out or r.timed_out
            merged.duration += r.duration
            merged.output_tail = (merged.output_tail + "\n" + r.output_tail).strip()[-4000:]
            if r.error:
                errors.append(f"{p.name}: {r.error}" if self.prefix else r.error)
            for tid, o in r.outcomes.items():
                full = self.full_id(p, tid)
                merged.outcomes[full] = TestOutcome(full, o.outcome, o.message, o.file, o.detail, o.kind)
        if errors:
            merged.error = "; ".join(errors)
        return merged


# --------------------------------------------------------------------------- running

def setup_projects(projects: Sequence[Project], worktree: Path, env: Mapping[str, str]) -> list[str]:
    """Run each project's ``setup_command`` in ``worktree``. Returns error notes."""
    notes = []
    for p in projects:
        if not p.setup_command:
            continue
        wd = p.workdir(worktree)
        if not wd.is_dir():
            continue
        r = run_command(p.setup_command, cwd=wd, env=p.env(env), timeout=p.timeout)
        if not r.ok:
            why = "timed out" if r.timed_out else (r.launch_error or f"exit code {r.returncode}")
            where = worktree.name if p.is_root else f"{worktree.name}/{p.path}"
            notes.append(f"setup_command failed in {where}: {why}")
    return notes


_NO_TEST_FILES = "The PR does not add or modify any test files."
_STATUS_ORDER = (STATUS_FAIL_ON_HEAD, STATUS_EVIDENCE, STATUS_ERROR, STATUS_NO_EVIDENCE, STATUS_NO_TESTS)


def combine_status(statuses: Sequence[str]) -> str:
    for s in _STATUS_ORDER:
        if s in statuses:
            return s
    return STATUS_NO_TESTS


def run_projects_fail_to_pass(
    projects: Sequence[Project],
    repo: str | Path,
    base: str,
    head: str,
    changed: Sequence[gitmod.ChangedFile],
    base_wt: Path,
    head_wt: Path,
    env: Mapping[str, str],
    check_old_tests: bool = False,
    multi: bool | None = None,
) -> TestCheck:
    """Run the fail-to-pass check for every project and combine the results.

    ``multi`` (default: more than one project, or any explicit ``[[project]]``)
    adds the per-project ``projects`` summaries. Without it, and with a single
    project, the result is exactly that project's :class:`TestCheck`.
    """
    multi = (len(projects) > 1 or any(p.explicit for p in projects)) if multi is None else multi
    groups = partition(projects, changed)
    prefix = len(projects) > 1
    checks: list[tuple[Project, TestCheck]] = []
    for p in projects:
        check = run_fail_to_pass(p.runner, repo, base, head, groups[p.name], base_wt, head_wt, p.env(env), p.timeout,
                                 check_old_tests=check_old_tests, subdir=p.path)
        checks.append((p, check))
    if not multi and len(checks) == 1:
        return checks[0][1]

    out = TestCheck(status=STATUS_NO_TESTS, runner=",".join(sorted({p.runner.name for p in projects}))[:40])
    old_files: list[str] = []
    old_failing: list[str] = []
    old_passed = 0
    old_ran = False
    old_errors: list[str] = []
    for p, c in checks:
        tag = f"[{p.name}] " if prefix else ""
        out.test_files += c.test_files
        out.test_modules += c.test_modules
        for t in c.tests:
            if prefix:
                t.test_id = prefix_test_id(p.name, t.test_id)
            out.tests.append(t)
        out.notes += [tag + n for n in c.notes if c.test_files or n != _NO_TEST_FILES]
        for key, label in (("head_run", "head"), ("base_run", "base")):
            run = getattr(c, key)
            if run.get("error"):
                out.notes.append(f"{tag}{label} run: {run['error']}")
        old = c.old_tests_on_head
        if old:
            old_files += old.get("files", [])
            if old.get("ran"):
                old_ran = True
                old_passed += old.get("passed", 0)
                old_failing += [prefix_test_id(p.name, t) if prefix else t for t in old.get("failing", [])]
            elif old.get("error"):
                old_errors.append(tag + str(old["error"]))
        out.projects.append({
            "name": p.name,
            "path": p.path,
            "runner": p.runner.name,
            "status": c.status,
            "counts": c.counts(),
            "base_run": dict(c.base_run),
            "head_run": dict(c.head_run),
        })
    if len(checks) == 1:
        c = checks[0][1]
        out.base_run, out.head_run = dict(c.base_run), dict(c.head_run)
        out.notes = [n for n in out.notes if not n.startswith(("head run: ", "base run: "))]
    if old_files:
        out.old_tests_on_head = {"ran": old_ran, "files": old_files, "passed": old_passed, "failing": old_failing[:50]}
        if not old_ran and old_errors:
            out.old_tests_on_head["error"] = "; ".join(old_errors)[:1000]
    with_tests = [c.status for _, c in checks if c.test_files]
    out.status = combine_status(with_tests) if with_tests else STATUS_NO_TESTS
    if not with_tests:
        out.notes.append(_NO_TEST_FILES)
    order = {"fail_on_head": 0, "fail_to_pass": 1, "error": 2, "pass_to_pass": 3, "skipped": 4}
    out.tests.sort(key=lambda r: (not r.touched, order.get(r.classification, 9), r.test_id))
    return out
