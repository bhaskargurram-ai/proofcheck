"""Scope check: what does the PR touch, and is anything surprising?

Flags (none of them is a verdict; they tell a reviewer where to look):

* ``workflow``     -- changes under ``.github/workflows/`` or ``action.yml`` (CI
                      and supply-chain sensitive)
* ``dependency``   -- dependency manifests and lockfiles
* ``binary``       -- binary files (cannot be reviewed as text)
* ``large_diff``   -- total changed lines above ``large_diff_threshold``
* ``unmentioned``  -- *heuristic*: a non-test file whose path shares no word
                      with the PR title/body, in a PR whose text does mention
                      other changed files
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable, Sequence

from .git import ChangedFile

WORKFLOW_PATTERNS = (".github/workflows/*", ".github/actions/*", "action.yml", "action.yaml", ".gitlab-ci.yml", ".circleci/*")
DEPENDENCY_PATTERNS = (
    "requirements*.txt", "requirements/*.txt", "requirements/*.in", "requirements*.in", "constraints*.txt",
    "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "Pipfile.lock", "*.lock", "poetry.lock", "uv.lock",
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile", "Gemfile.lock", "environment.yml", "conda-lock.yml",
    ".pre-commit-config.yaml", "Dockerfile", "*.Dockerfile",
)
_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "when", "fix", "fixes", "fixed", "add", "adds", "added",
    "src", "lib", "test", "tests", "main", "init", "index", "util", "utils", "py", "js", "ts", "md", "txt", "toml",
    "yml", "yaml", "json", "cfg", "core", "app", "module", "file", "files", "code", "new", "old", "use", "not",
}
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+")


def _match(path: str, patterns: Sequence[str]) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(name, p) for p in patterns)


def tokens(text: str) -> set[str]:
    """Lowercased word tokens, splitting snake_case and camelCase; stopwords removed."""
    out: set[str] = set()
    for raw in _TOKEN_RE.findall(text or ""):
        parts = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", raw) or [raw]
        for p in parts + [raw]:
            p = p.lower()
            if len(p) >= 3 and p not in _STOPWORDS:
                out.add(p)
                # cheap stemming so "parser"/"parsing"/"parse" meet
                for suffix in ("ing", "ers", "er", "es", "s", "ed"):
                    if p.endswith(suffix) and len(p) - len(suffix) >= 3:
                        out.add(p[: -len(suffix)])
                        break
    return out


@dataclass
class ScopeFlag:
    kind: str
    message: str
    paths: list[str] = field(default_factory=list)
    heuristic: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "paths": list(self.paths), "heuristic": self.heuristic}


@dataclass
class ScopeReport:
    files: list[ChangedFile]
    total_additions: int
    total_deletions: int
    flags: list[ScopeFlag] = field(default_factory=list)

    @property
    def total_changed(self) -> int:
        return self.total_additions + self.total_deletions

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": [f.to_dict() for f in self.files],
            "file_count": len(self.files),
            "total_additions": self.total_additions,
            "total_deletions": self.total_deletions,
            "flags": [f.to_dict() for f in self.flags],
        }


def analyze_scope(
    files: Sequence[ChangedFile],
    pr_text: str,
    is_test_file: Callable[[str], bool],
    large_diff_threshold: int = 1000,
) -> ScopeReport:
    """Inspect the changed files and produce scope flags."""
    adds = sum(f.additions for f in files)
    dels = sum(f.deletions for f in files)
    report = ScopeReport(list(files), adds, dels)

    workflows = [f.path for f in files if _match(f.path, WORKFLOW_PATTERNS)]
    if workflows:
        report.flags.append(ScopeFlag("workflow", "Changes CI/workflow configuration; review carefully (runs with repository credentials after merge).", workflows))
    deps = [f.path for f in files if _match(f.path, DEPENDENCY_PATTERNS)]
    if deps:
        report.flags.append(ScopeFlag("dependency", "Changes dependency manifests or lockfiles.", deps))
    binaries = [f.path for f in files if f.binary]
    if binaries:
        report.flags.append(ScopeFlag("binary", "Adds or modifies binary files that cannot be reviewed as text.", binaries))
    if report.total_changed > large_diff_threshold:
        report.flags.append(
            ScopeFlag("large_diff", f"Large diff: {report.total_changed} changed lines (threshold {large_diff_threshold}).")
        )

    # Heuristic: files whose path shares no word with the PR description.
    words = tokens(pr_text)
    candidates = [f for f in files if not is_test_file(f.path) and f.path not in workflows and f.path not in deps and not f.binary
                  and not PurePosixPath(f.path).name.lower().startswith(("readme", "changelog", "news", "history"))]
    if words and len(candidates) >= 2:
        mentioned, unmentioned = [], []
        for f in candidates:
            (mentioned if tokens(f.path) & words else unmentioned).append(f.path)
        if mentioned and unmentioned:
            report.flags.append(
                ScopeFlag(
                    "unmentioned",
                    "Heuristic: these files share no words with the PR title/description; check they belong in this PR.",
                    unmentioned,
                    heuristic=True,
                )
            )
    return report
