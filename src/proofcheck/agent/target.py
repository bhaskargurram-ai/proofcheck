"""Where an agent candidate goes: the project, the language and (for Rust) the crate.

The agent writes one test file per prompt variant. Which project it belongs
to, and therefore which runner executes it, is decided by *localisation*:
the project that holds most of the pull request's changed source lines
(test files excluded) is the project of the target claim.

* **Python** (pytest/unittest): ``<project>/<agent.test_dir>/test_<variant>.py``.
* **Rust** (cargo): an *integration test*
  ``<crate>/tests/proofcheck_agent_<variant>.rs`` in the crate that holds most
  of the changed non-test ``.rs`` lines. Integration tests are separate crates
  that link the library, so they can only use its **public** API
  (``use <lib_name>::...``); private items and inline ``#[cfg(test)]``
  modules are out of reach in this version.

Everything here reads git objects only (``git ls-tree`` / ``git show``), so
``propose`` can call it without checking anything out. ``verify`` calls
:func:`check_manifest_target` to re-validate the location a manifest names
against its own configuration before writing any file.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable, Sequence

from .. import git as gitmod
from ..projects import Project, project_for
from .tools import list_head_files, read_blob_text

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib

PYTHON = "python"
RUST = "rust"
LANGUAGES = (PYTHON, RUST)
PYTHON_RUNNERS = ("pytest", "unittest")
RUST_PREFIX = "proofcheck_agent_"
MAX_CRATES = 200

_PACKAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_DIR_PART_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_RUST_FILE_RE = re.compile(r"^proofcheck_agent_[a-z0-9_]{1,60}\.rs$")


class TargetError(ValueError):
    """The manifest names a location that this configuration does not allow."""


def language_of(runner_name: str) -> str | None:
    if runner_name in PYTHON_RUNNERS:
        return PYTHON
    if runner_name == "cargo":
        return RUST
    return None


def python_filename(variant: str) -> str:
    return "test_" + variant.replace("-", "_") + ".py"


def rust_filename(variant: str) -> str:
    return RUST_PREFIX + variant.replace("-", "_") + ".rs"


@dataclass
class CrateRef:
    """A cargo package, as seen in a ``Cargo.toml`` at the head commit."""

    dir: str  # repository-relative crate directory ("" for the repository root)
    package: str
    lib: str | None  # library crate name (``use <lib>::...``); None when the package has no library
    autotests: bool = True

    @property
    def tests_dir(self) -> str:
        return f"{self.dir}/tests" if self.dir else "tests"

    def describe(self) -> str:
        where = self.dir or "(repository root)"
        if self.lib is None:
            return f"{where}: package `{self.package}`, no library target (integration tests cannot import it)"
        return f"{where}: package `{self.package}`, library crate `{self.lib}` (`use {self.lib}::...`)"


def parse_crate(manifest_text: str | None, crate_dir: str, has_lib_rs: bool) -> CrateRef | None:
    """A :class:`CrateRef` from ``Cargo.toml`` text (None when it has no ``[package]``)."""
    if not manifest_text:
        return None
    try:
        data = tomllib.loads(manifest_text)
    except tomllib.TOMLDecodeError:
        return None
    pkg = data.get("package")
    if not isinstance(pkg, dict) or not isinstance(pkg.get("name"), str) or not pkg["name"]:
        return None
    name = pkg["name"]
    if not _PACKAGE_RE.fullmatch(name):
        return None  # cargo rejects such names too; never put odd text into prompts or paths
    lib_table = data.get("lib")
    lib: str | None = None
    if isinstance(lib_table, dict) or has_lib_rs:
        custom = lib_table.get("name") if isinstance(lib_table, dict) else None
        lib = custom if isinstance(custom, str) and custom else name.replace("-", "_")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", lib):
            lib = None
    return CrateRef(crate_dir, name, lib, pkg.get("autotests", True) is not False)


@dataclass
class AgentTarget:
    """The project (and crate) candidates are written for."""

    project: Project
    language: str | None
    test_dir: str  # repository-relative directory holding the candidates
    crate: CrateRef | None = None
    crates: list[CrateRef] = field(default_factory=list)  # all crates of the project (Rust)
    reason: str = ""  # how the project/crate was chosen (for the manifest)
    error: str = ""  # set when no candidate can be written here

    def candidate_path(self, variant: str) -> str:
        name = rust_filename(variant) if self.language == RUST else python_filename(variant)
        return f"{self.test_dir}/{name}"

    def local(self, repo_path: str) -> str:
        return self.project.local(repo_path)

    def manifest_fields(self) -> dict[str, Any]:
        return {
            "language": self.language or "",
            "project": self.project.name,
            "project_path": self.project.path,
            "runner": self.project.runner.name,
            "crate": None if self.crate is None else {"dir": self.crate.dir, "package": self.crate.package,
                                                      "lib": self.crate.lib or ""},
            "target_reason": self.reason[:300],
        }

    def crates_text(self) -> str:
        """Human-readable crate list for the ``list_crates`` tool."""
        if self.language != RUST:
            return ""
        lines = [f"Crates of the cargo project `{self.project.name}` at head:"]
        lines += [f"- {c.describe()}" for c in self.crates[:MAX_CRATES]] or ["- (none found)"]
        if self.crate is not None:
            lines += ["", f"Your test is an integration test of `{self.crate.package}` (`{self.crate.dir or '.'}`), "
                          f"stored as {self.test_dir}/{RUST_PREFIX}<variant>.rs and run with "
                          f"`cargo test -p {self.crate.package} --test <file stem>`.",
                      f"It is a separate crate: reach the code only through the public API, `use {self.crate.lib}::...`."]
        return "\n".join(lines)


def _source_ext_ok(language: str | None, path: str) -> bool:
    if language == PYTHON:
        return path.endswith(".py")
    if language == RUST:
        return path.endswith(".rs")
    return False


def _changed_source(projects: Sequence[Project], changed: Sequence[gitmod.ChangedFile]) -> list[tuple[Project, gitmod.ChangedFile, int]]:
    """Changed non-test files with their project and changed line count."""
    out = []
    for f in changed:
        if f.binary:
            continue
        p = project_for(projects, f.path)
        if p is None or f.path == p.path:
            continue
        try:
            local = p.local(f.path)
        except ValueError:
            continue
        if p.runner.is_test_file(local):
            continue
        out.append((p, f, max(1, f.additions + f.deletions)))
    return out


def crates_at(paths: Sequence[str], read: Callable[[str], str | None], project: Project) -> list[CrateRef]:
    """Every crate (``Cargo.toml`` with ``[package]``) inside ``project`` at the commit ``paths`` lists."""
    names = set(paths)
    crates = []
    for p in paths:
        if PurePosixPath(p).name != "Cargo.toml" or not project.contains(p):
            continue
        d = str(PurePosixPath(p).parent)
        d = "" if d == "." else d
        parts = PurePosixPath(d).parts if d else ()
        if any(part in ("target", "vendor") or not _DIR_PART_RE.fullmatch(part) or part in (".", "..") for part in parts):
            continue  # build output, vendored code, or a directory name we will not put into prompts and paths
        c = parse_crate(read(p), d, (f"{d}/src/lib.rs" if d else "src/lib.rs") in names)
        if c is not None:
            crates.append(c)
        if len(crates) >= MAX_CRATES:
            break
    crates.sort(key=lambda c: c.dir)
    return crates


def crate_of(path: str, crates: Sequence[CrateRef]) -> CrateRef | None:
    """The crate whose directory most deeply contains ``path``."""
    best = None
    for c in crates:
        if (not c.dir or path.startswith(c.dir + "/")) and (best is None or len(c.dir) > len(best.dir)):
            best = c
    return best


def select_target(
    repo: str,
    head_sha: str,
    projects: Sequence[Project],
    changed: Sequence[gitmod.ChangedFile],
    test_dir: str,
    paths: Sequence[str] | None = None,
) -> AgentTarget:
    """Choose the project (and crate) whose source the pull request mainly changes.

    The project with the most changed lines in source files of its own
    language (``.py`` for pytest/unittest, ``.rs`` for cargo; test files
    excluded) wins; without any, the most changed non-test lines of any kind;
    without those, the first (root) project. Ties go to the earlier project.
    """
    if not projects:
        raise ValueError("at least one project is required")
    rows = _changed_source(projects, changed)
    own: dict[str, int] = {}
    other: dict[str, int] = {}
    for p, f, n in rows:
        bucket = own if _source_ext_ok(language_of(p.runner.name), f.path) else other
        bucket[p.name] = bucket.get(p.name, 0) + n
    order = {p.name: i for i, p in enumerate(projects)}
    if own:
        name = min(own, key=lambda k: (-own[k], order[k]))
        reason = f"most changed source lines ({own[name]}) are in project {name}"
    elif other:
        name = min(other, key=lambda k: (-other[k], order[k]))
        reason = f"most changed non-test lines ({other[name]}) are in project {name}"
    else:
        name = projects[0].name
        reason = "the pull request changes no source file; using the first project"
    project = next(p for p in projects if p.name == name)
    language = language_of(project.runner.name)
    if language == PYTHON:
        return AgentTarget(project, language, project.repo_path(test_dir), reason=reason)
    if language is None:
        return AgentTarget(project, None, "", reason=reason,
                           error=(f"the agent only supports Python test runners (pytest, unittest) and cargo, "
                                  f"not {project.runner.name}"))
    # Rust: the crate with the most changed non-test .rs lines.
    all_paths = list(paths) if paths is not None else list_head_files(repo, head_sha)
    crates = crates_at(all_paths, lambda p: read_blob_text(repo, head_sha, p), project)
    per_crate: dict[str, int] = {}
    for p, f, n in rows:
        if p is project and f.path.endswith(".rs"):
            c = crate_of(f.path, crates)
            if c is not None:
                per_crate[c.dir] = per_crate.get(c.dir, 0) + n
    target = AgentTarget(project, RUST, "", crates=crates, reason=reason)
    if not per_crate:
        target.error = f"the pull request changes no Rust source file of a crate in project {project.name}"
        return target
    crate_dir = min(per_crate, key=lambda d: (-per_crate[d], d))
    crate = next(c for c in crates if c.dir == crate_dir)
    target.crate = crate
    target.test_dir = crate.tests_dir
    target.reason = f"{reason}; crate {crate.package} has the most changed Rust lines ({per_crate[crate_dir]})"
    if crate.lib is None:
        target.error = (f"crate {crate.package} has no library target: integration tests can only use a library's "
                        "public API (inline unit tests are not supported by the agent yet)")
    elif not crate.autotests:
        target.error = f"crate {crate.package} sets autotests = false, so a new tests/*.rs file would not be built"
    return target


def default_project(projects: Sequence[Project]) -> Project:
    return next((p for p in projects if p.is_root), projects[0])


def check_manifest_target(manifest: dict[str, Any], projects: Sequence[Project], repo: str, head_sha: str) -> AgentTarget:
    """Re-validate the location named by a candidates manifest against *this* configuration.

    Returns the :class:`AgentTarget` to run the candidates with. Raises
    :class:`TargetError` when the manifest names an unknown project, a runner
    of another language, or a directory outside the project (or, for Rust,
    a directory that is not ``<crate>/tests`` of a crate with a library).
    """
    language = manifest.get("language") or PYTHON
    name = manifest.get("project") or ""
    if name:
        project = next((p for p in projects if p.name == name), None)
        if project is None:
            raise TargetError(f"the candidates were proposed for project {name!r}, which this configuration does not define")
    else:
        project = default_project(projects)
    if language_of(project.runner.name) != language:
        raise TargetError(f"the candidates are {language} tests, but project {project.name!r} runs {project.runner.name}")
    test_dir = manifest.get("test_dir") or ""
    if not project.contains(test_dir) or test_dir == project.path:
        raise TargetError(f"candidate directory {test_dir[:100]!r} is outside project {project.name!r}")
    if language == PYTHON:
        return AgentTarget(project, PYTHON, test_dir)
    parts = PurePosixPath(test_dir).parts
    if not parts or parts[-1] != "tests":
        raise TargetError(f"Rust candidates must be in a crate's tests/ directory, not {test_dir[:100]!r}")
    crate_dir = "/".join(parts[:-1])
    if crate_dir and not project.contains(crate_dir):
        raise TargetError(f"crate directory {crate_dir!r} is outside project {project.name!r}")
    manifest_path = f"{crate_dir}/Cargo.toml" if crate_dir else "Cargo.toml"
    lib_rs = f"{crate_dir}/src/lib.rs" if crate_dir else "src/lib.rs"
    crate = parse_crate(read_blob_text(repo, head_sha, manifest_path), crate_dir,
                        read_blob_text(repo, head_sha, lib_rs) is not None)
    if crate is None or crate.lib is None:
        raise TargetError(f"{crate_dir or '.'} is not a crate with a library target at head")
    return AgentTarget(project, RUST, test_dir, crate=crate)


def candidate_file_ok(path: str, test_dir: str, language: str) -> bool:
    """Name check for a candidate file inside ``test_dir``."""
    p = PurePosixPath(path)
    if str(p.parent) != test_dir:
        return False
    if language == RUST:
        return bool(_RUST_FILE_RE.match(p.name))
    return p.name.startswith("test_") and p.suffix == ".py" and all(c.isalnum() or c in "_." for c in p.name)
