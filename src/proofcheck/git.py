"""Thin, typed wrappers around the ``git`` command line.

All git invocations use argument lists (never a shell). Worktrees are created
in fresh temporary directories and are always removed, even when the code
running inside them raises.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class GitError(RuntimeError):
    """Raised when a git command fails."""


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    # Never prompt for credentials and never page output.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    env.setdefault("LC_ALL", "C")
    return env


def git(repo: str | os.PathLike[str], *args: str, check: bool = True, binary: bool = False) -> str | bytes:
    """Run ``git -C repo <args>`` and return stdout.

    Args:
        repo: Repository (or worktree) directory.
        args: Git arguments.
        check: Raise :class:`GitError` on a non-zero exit code.
        binary: Return raw bytes instead of decoded text.
    """
    argv = ["git", "-C", str(repo), "-c", "core.quotepath=off", *args]
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_git_env(), shell=False)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.decode(errors='replace').strip()}")
    if binary:
        return proc.stdout
    return proc.stdout.decode("utf-8", errors="replace")


def resolve_ref(repo: str | os.PathLike[str], ref: str) -> str:
    """Resolve ``ref`` to a full commit SHA."""
    if ref.startswith("-"):
        raise GitError(f"refusing suspicious ref {ref!r}")
    out = git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    sha = str(out).strip()
    if not sha:
        raise GitError(f"unknown git ref: {ref!r}")
    return sha


def merge_base(repo: str | os.PathLike[str], base: str, head: str) -> str:
    """Return the merge base of two commits (falls back to ``base``)."""
    out = str(git(repo, "merge-base", base, head, check=False)).strip()
    return out or resolve_ref(repo, base)


@dataclass
class ChangedFile:
    """A file touched by the diff between two commits."""

    path: str
    status: str  # A, M, D, R, C, T
    old_path: str | None = None
    additions: int = 0
    deletions: int = 0
    binary: bool = False

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "status": self.status,
            "old_path": self.old_path,
            "additions": self.additions,
            "deletions": self.deletions,
            "binary": self.binary,
        }


def _parse_name_status(raw: str) -> list[ChangedFile]:
    parts = raw.split("\0")
    files: list[ChangedFile] = []
    i = 0
    while i < len(parts):
        code = parts[i]
        if not code:
            i += 1
            continue
        letter = code[0]
        if letter in ("R", "C"):
            old, new = parts[i + 1], parts[i + 2]
            files.append(ChangedFile(path=new, status=letter, old_path=old))
            i += 3
        else:
            files.append(ChangedFile(path=parts[i + 1], status=letter))
            i += 2
    return files


def _parse_numstat(raw: str) -> dict[str, tuple[int, int, bool]]:
    """Parse ``git diff --numstat -z`` output into ``{path: (add, del, binary)}``."""
    stats: dict[str, tuple[int, int, bool]] = {}
    parts = raw.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        if not entry:
            i += 1
            continue
        fields = entry.split("\t")
        added, deleted = fields[0], fields[1]
        binary = added == "-" and deleted == "-"
        a = 0 if binary else int(added)
        d = 0 if binary else int(deleted)
        if len(fields) >= 3 and fields[2]:
            stats[fields[2]] = (a, d, binary)
            i += 1
        else:  # rename: path pair follows as two NUL-separated entries
            new = parts[i + 2] if i + 2 < len(parts) else ""
            stats[new] = (a, d, binary)
            i += 3
    return stats


def changed_files(repo: str | os.PathLike[str], base: str, head: str) -> list[ChangedFile]:
    """List files changed between ``base`` and ``head`` (two-dot diff of the given commits)."""
    names = str(git(repo, "diff", "--name-status", "-z", "-M", base, head))
    files = _parse_name_status(names)
    stats = _parse_numstat(str(git(repo, "diff", "--numstat", "-z", "-M", base, head)))
    for f in files:
        if f.path in stats:
            f.additions, f.deletions, f.binary = stats[f.path]
    return files


def diff_text(repo: str | os.PathLike[str], base: str, head: str, paths: list[str] | None = None, context: int = 0) -> str:
    """Return the unified diff between two commits, optionally limited to ``paths``."""
    args = ["diff", f"-U{context}", "--no-color", "--no-ext-diff", base, head]
    if paths:
        args += ["--", *paths]
    return str(git(repo, *args))


def show_file(repo: str | os.PathLike[str], ref: str, path: str) -> bytes | None:
    """Return the bytes of ``path`` at ``ref`` or ``None`` if it does not exist."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_env(),
        shell=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def list_worktrees(repo: str | os.PathLike[str]) -> list[str]:
    """Return the paths of all registered worktrees (including the main one)."""
    out = str(git(repo, "worktree", "list", "--porcelain"))
    return [line[len("worktree "):] for line in out.splitlines() if line.startswith("worktree ")]


@contextmanager
def worktree(repo: str | os.PathLike[str], ref: str, prefix: str = "proofcheck-") -> Iterator[Path]:
    """Create a detached worktree of ``ref`` in a temporary directory.

    The worktree is removed (and pruned) when the context exits, including
    when the body raises.
    """
    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    path = tmp / "wt"
    created = False
    try:
        git(repo, "worktree", "add", "--detach", "--force", str(path), ref)
        created = True
        yield path
    finally:
        if created:
            git(repo, "worktree", "remove", "--force", "--force", str(path), check=False)
        shutil.rmtree(tmp, ignore_errors=True)
        git(repo, "worktree", "prune", check=False)
