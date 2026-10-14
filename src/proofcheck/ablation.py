"""Diff evidence map: which changed hunks does the evidence actually depend on?

For every hunk of the pull request's non-test changes, proofcheck takes a
worktree of the head commit, reverse-applies **just that hunk** and re-runs
only the evidence it already found:

* the tests classified ``fail_to_pass`` (they fail on base and pass on head);
* the issue-reproducer snippets whose behaviour changed between base and head.

If any of them now fails (or, for a reproducer, behaves differently from the
head run), the hunk is **required** by the evidence. If everything still
passes, the hunk is **not exercised** by any evidence: it changed, but nothing
that proofcheck measured depends on it. Hunks that cannot be reverted on their
own, or whose run times out, are **skipped**.

This is deterministic and needs no LLM. It is bounded by a hunk cap and a
total time budget, hunks are processed in a stable order (path, then line),
and the temporary worktree is always removed, even when a run raises.

Notes on the mechanics:

* hunks come from ``git diff -U0`` and are reversed with
  ``git apply -R --unidiff-zero``;
* a new file is a single hunk whose reversal deletes the file; a deleted file
  is a single hunk whose reversal recreates it; a pure rename (no content
  change) is a single hunk whose reversal renames the file back; content
  hunks of a renamed file are applied to the new path;
* binary files, test files and files matching ``ablation.exclude`` are not
  ablated, nor are hunks that lie entirely inside test code of a source file
  (Rust ``#[cfg(test)]`` modules, reported by the runner's ``test_regions``);
* one worktree is reused for all hunks: after each run the touched paths are
  restored from the head commit and verified clean.
"""

from __future__ import annotations

import fnmatch
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from . import git as gitmod
from .junit import PASSED
from .repro import BEHAVIOR_CHANGED, SnippetRun, compare_runs, extract_python_blocks, run_snippet

# Per-hunk statuses
REQUIRED = "required"
NOT_EXERCISED = "not_exercised"
SKIPPED = "skipped"

# Overall statuses
STATUS_RAN = "ran"
STATUS_NOT_APPLICABLE = "not_applicable"  # no fail->pass test or behaviour-changing reproducer
STATUS_NO_HUNKS = "no_hunks"  # nothing to ablate (only tests/docs/binaries changed)
STATUS_BASELINE_FAILED = "baseline_failed"  # the evidence did not hold in the clean ablation worktree
STATUS_ERROR = "error"

HUNK_KINDS = ("modify", "add", "delete", "rename")

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

#: Upper bound on evidence items re-run per hunk (keeps one ablation run small).
MAX_EVIDENCE_TESTS = 50
MAX_EVIDENCE_SNIPPETS = 5


@dataclass
class Hunk:
    """One independently revertible change."""

    file: str  # path at head (for deletions: the deleted path)
    kind: str  # modify | add | delete | rename
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    patch: str  # a complete patch containing only this hunk
    old_path: str | None = None

    def added_lines(self) -> list[str]:
        """The head-side lines of this hunk (what reverting removes)."""
        body = self.patch.split("\n@@", 1)[-1].split("\n")[1:]
        return [line[1:] for line in body if line.startswith("+")]

    def paths(self) -> list[str]:
        out = [self.file]
        if self.old_path and self.old_path != self.file:
            out.append(self.old_path)
        return out

    def location(self) -> str:
        if self.kind == "add":
            return f"{self.file} (new file)"
        if self.kind == "delete":
            return f"{self.file} (deleted)"
        if self.kind == "rename":
            return f"{self.old_path} → {self.file} (rename)"
        if self.new_count == 0:
            return f"{self.file}:{self.new_start} (−{self.old_count})"
        end = self.new_start + self.new_count - 1
        return f"{self.file}:{self.new_start}" + (f"-{end}" if end != self.new_start else "")


@dataclass
class HunkResult:
    hunk: Hunk
    status: str
    broken_by: list[str] = field(default_factory=list)
    reason: str = ""
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        h = self.hunk
        return {
            "file": h.file,
            "old_path": h.old_path,
            "kind": h.kind,
            "location": h.location(),
            "start": h.new_start,
            "end": h.new_start + max(h.new_count, 1) - 1,
            "added": h.new_count,
            "removed": h.old_count,
            "status": self.status,
            "broken_by": list(self.broken_by[:10]),
            "reason": self.reason,
            "duration": round(self.duration, 3),
        }


@dataclass
class Ablation:
    """Result of the diff evidence map."""

    status: str
    evidence: list[str] = field(default_factory=list)
    hunks: list[HunkResult] = field(default_factory=list)
    total_hunks: int = 0
    untested_over_cap: int = 0
    budget_s: float = 0.0
    elapsed_s: float = 0.0
    budget_exhausted: bool = False
    notes: list[str] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for h in self.hunks if h.status == status)

    def unexercised_paths(self) -> list[str]:
        seen: list[str] = []
        for h in self.hunks:
            if h.status == NOT_EXERCISED and h.hunk.file not in seen:
                seen.append(h.hunk.file)
        return seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "evidence": list(self.evidence[:MAX_EVIDENCE_TESTS + MAX_EVIDENCE_SNIPPETS]),
            "hunks": [h.to_dict() for h in self.hunks],
            "total_hunks": self.total_hunks,
            "tested": len(self.hunks),
            "required": self.count(REQUIRED),
            "not_exercised": self.count(NOT_EXERCISED),
            "skipped": self.count(SKIPPED),
            "untested_over_cap": self.untested_over_cap,
            "budget_s": float(self.budget_s),
            "elapsed_s": round(self.elapsed_s, 3),
            "budget_exhausted": self.budget_exhausted,
            "notes": list(self.notes[:10]),
        }


# --------------------------------------------------------------------------- hunks

def _matches(path: str, patterns: Sequence[str]) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(name, p) for p in patterns)


def split_file_diff(diff: str) -> tuple[list[str], list[tuple[tuple[int, int, int, int], list[str]]]]:
    """Split one file's ``-U0`` diff into header lines and ``(numbers, hunk_lines)``."""
    header: list[str] = []
    hunks: list[tuple[tuple[int, int, int, int], list[str]]] = []
    current: list[str] | None = None
    lines = diff.split("\n")  # not splitlines(): keep "\r" and form feeds inside lines
    if lines and lines[-1] == "":
        lines.pop()
    for line in lines:
        m = _HUNK_HEADER_RE.match(line)
        if m:
            nums = (int(m.group(1)), int(m.group(2)) if m.group(2) is not None else 1,
                    int(m.group(3)), int(m.group(4)) if m.group(4) is not None else 1)
            current = [line]
            hunks.append((nums, current))
        elif current is None:
            header.append(line)
        else:
            current.append(line)
    return header, hunks


def _quote(path: str) -> str:
    """Path as written in a patch header (git's quoting for unusual names)."""
    if re.search(r'[\s"\\\x00-\x1f\x7f]', path):
        esc = path.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
        return f'"{esc}"'
    return path


def _plain_header(path: str) -> str:
    a, b = _quote("a/" + path), _quote("b/" + path)
    return f"diff --git {a} {b}\n--- {a}\n+++ {b}\n"


def _in_regions(hunk: Hunk, regions: Sequence[tuple[int, int]]) -> bool:
    """True if every head line of a modify hunk (or the gap of a pure deletion) lies inside one region."""
    if hunk.kind != "modify":
        return False
    first = hunk.new_start
    last = hunk.new_start + hunk.new_count - 1 if hunk.new_count else hunk.new_start + 1
    return any(lo <= first and last <= hi for lo, hi in regions)


def list_hunks(
    repo: str | Path,
    base: str,
    head: str,
    changed: Sequence[gitmod.ChangedFile],
    is_test_file: Callable[[str], bool],
    exclude: Sequence[str] = (),
    test_regions: Callable[[str, str], Sequence[tuple[int, int]] | None] | None = None,
    skipped_test_hunks: list[str] | None = None,
) -> list[Hunk]:
    """All ablatable hunks of the non-test, non-binary changed files, in a stable order.

    ``test_regions(path, head_source)`` returns line ranges of test code inside
    a source file; hunks entirely inside them are left out (their locations
    are appended to ``skipped_test_hunks``).
    """
    out: list[Hunk] = []
    for f in sorted(changed, key=lambda c: c.path):
        if f.binary or is_test_file(f.path) or (f.old_path and is_test_file(f.old_path)):
            continue
        if _matches(f.path, exclude) or (f.old_path and _matches(f.old_path, exclude)):
            continue
        if f.status not in ("A", "M", "D", "R"):
            continue  # copies, type changes: not reverted
        paths = [f.old_path, f.path] if f.status == "R" and f.old_path else [f.path]
        raw = gitmod.git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", "-M",
                         "--src-prefix=a/", "--dst-prefix=b/", base, head, "--", *paths, binary=True)
        diff = bytes(raw).decode("utf-8", errors="surrogateescape")  # round-trips non-UTF-8 bytes into the patch
        if not diff.strip() or "\nGIT binary patch" in diff or "\nBinary files " in diff:
            continue
        header, hunks = split_file_diff(diff)
        head_text = "\n".join(header) + "\n"
        if f.status == "R":
            if not hunks:
                out.append(Hunk(f.path, "rename", 0, 0, 0, 0, head_text, old_path=f.old_path))
                continue
            prefix = _plain_header(f.path)
            for nums, lines in hunks:
                out.append(Hunk(f.path, "modify", *nums, patch=prefix + "\n".join(lines) + "\n", old_path=f.old_path))
            continue
        if f.status in ("A", "D"):
            if len(hunks) != 1:
                continue  # empty new/deleted file: nothing meaningful to revert
            nums, lines = hunks[0]
            out.append(Hunk(f.path, "add" if f.status == "A" else "delete", *nums, patch=head_text + "\n".join(lines) + "\n"))
            continue
        prefix = _plain_header(f.path)
        regions: Sequence[tuple[int, int]] | None = None
        if test_regions is not None:
            data = gitmod.show_file(repo, head, f.path)
            regions = test_regions(f.path, data.decode("utf-8", "replace")) if data is not None else None
        for nums, lines in hunks:
            h = Hunk(f.path, "modify", *nums, patch=prefix + "\n".join(lines) + "\n")
            if regions and _in_regions(h, regions):
                if skipped_test_hunks is not None:
                    skipped_test_hunks.append(h.location())
                continue
            out.append(h)
    out.sort(key=lambda h: (h.file, h.new_start, h.old_start))
    return out


# --------------------------------------------------------------------------- evidence

@dataclass
class SnippetEvidence:
    """A behaviour-changing issue reproducer and its head-run result."""

    label: str
    code: str
    head: SnippetRun

    @property
    def name(self) -> str:
        return self.label


def collect_evidence(tests: Any, repros: Sequence[Any], issues: Mapping[str, str]) -> tuple[list[str], list[str], list[SnippetEvidence]]:
    """Return ``(fail_to_pass test ids, their modules, behaviour-changing snippets)``.

    ``tests`` is a :class:`~proofcheck.failtopass.TestCheck` (or None) and
    ``repros`` a list of :class:`~proofcheck.repro.IssueRepro`.
    """
    ids: list[str] = []
    modules: list[str] = []
    if tests is not None:
        for t in tests.tests:
            if t.touched and t.classification == "fail_to_pass" and "::" in t.test_id:
                ids.append(t.test_id)
                if t.file and t.file not in modules:
                    modules.append(t.file)
    ids = sorted(set(ids))[:MAX_EVIDENCE_TESTS]
    snippets: list[SnippetEvidence] = []
    for r in repros:
        body = issues.get(r.issue)
        if body is None:
            continue
        blocks = extract_python_blocks(body)
        for s in r.snippets:
            if s.status != BEHAVIOR_CHANGED or s.head.timed_out or not 1 <= s.index <= len(blocks):
                continue
            snippets.append(SnippetEvidence(f"{r.issue} snippet {s.index}", blocks[s.index - 1], s.head))
    return ids, sorted(modules), snippets[:MAX_EVIDENCE_SNIPPETS]


# --------------------------------------------------------------------------- running

class _Budget:
    def __init__(self, seconds: float, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.start = clock()
        self.seconds = seconds

    def remaining(self) -> float:
        return self.seconds - (self.clock() - self.start)

    def elapsed(self) -> float:
        return self.clock() - self.start


def _restore(wt: Path, hunk: Hunk) -> None:
    """Put the paths touched by ``hunk`` back to their head state; raise if that fails."""
    for p in hunk.paths():
        exists_at_head = str(gitmod.git(wt, "ls-tree", "--name-only", "HEAD", "--", p)).strip() != ""
        target = wt / p
        if exists_at_head:
            gitmod.git(wt, "checkout", "--force", "HEAD", "--", p)
        elif target.is_file() or target.is_symlink():
            target.unlink()
    status = str(gitmod.git(wt, "status", "--porcelain", "--untracked-files=all", "--", *hunk.paths())).strip()
    if status:
        raise gitmod.GitError(f"could not restore {', '.join(hunk.paths())} after ablation: {status[:200]}")


def _at_exact_position(wt: Path, hunk: Hunk) -> bool:
    """True if the head file has this hunk's lines exactly where the hunk says.

    ``git apply`` searches for a matching location at an offset when the
    expected position does not match. With ``--unidiff-zero`` (no context
    lines) that could revert identical text somewhere else in the file, so the
    exact position is checked first.
    """
    if hunk.kind != "modify" or hunk.new_count == 0:
        return True
    target = wt / hunk.file
    if not target.is_file():
        return False
    text = target.read_bytes().decode("utf-8", errors="surrogateescape")
    lines = text.split("\n")
    start = hunk.new_start - 1
    return lines[start:start + hunk.new_count] == hunk.added_lines()


def _reverse_apply(wt: Path, hunk: Hunk, tmp: Path) -> str | None:
    """Reverse-apply one hunk in ``wt``. Returns an error message or None."""
    if not _at_exact_position(wt, hunk):
        return f"could not reverse-apply this hunk on its own (head file differs at line {hunk.new_start})"
    patch = tmp / "hunk.patch"
    patch.write_bytes(hunk.patch.encode("utf-8", errors="surrogateescape"))
    try:
        gitmod.git(wt, "apply", "-R", "--unidiff-zero", "--whitespace=nowarn", str(patch))
    except gitmod.GitError as exc:
        return f"could not reverse-apply this hunk on its own ({str(exc).splitlines()[0][:160]})"
    return None


def _evaluate(
    runner: Any,
    wt: Path,
    test_ids: Sequence[str],
    modules: Sequence[str],
    snippets: Sequence[SnippetEvidence],
    env: Mapping[str, str],
    environ: Mapping[str, str] | None,
    python: str,
    test_timeout: float,
    repro_timeout: float,
    budget: _Budget,
) -> tuple[list[str], str | None]:
    """Run the evidence in ``wt``. Returns ``(broken evidence names, skip reason)``."""
    broken: list[str] = []
    if test_ids:
        left = budget.remaining()
        if left <= 1:
            return [], "time budget exhausted"
        run_selected = getattr(runner, "run_selected", None)
        if run_selected is not None:
            run = run_selected(wt, list(modules), list(test_ids), env, min(test_timeout, left))
        else:
            run = runner.run(wt, list(modules), env, min(test_timeout, left))
        if run.timed_out:
            return [], "test run timed out"
        if not run.ok and not run.outcomes:
            # No report at all (e.g. a module that no longer imports and a runner
            # that stops) -- every test counts as broken.
            broken.extend(test_ids)
        else:
            for tid in test_ids:
                o = run.outcomes.get(tid)
                if o is None or o.outcome != PASSED:
                    broken.append(tid)
    for s in snippets:
        left = budget.remaining()
        if left <= 1:
            return broken, None if broken else "time budget exhausted"
        r = run_snippet(s.code, wt, python, min(repro_timeout, left), environ)
        if r.timed_out and not s.head.timed_out:
            if not broken:
                return [], "reproducer timed out"
            continue
        if compare_runs(s.head, r) == BEHAVIOR_CHANGED:
            broken.append(s.name)
    return broken, None


#: Extensions treated as source code when choosing which hunks to test first.
CODE_SUFFIXES = frozenset({
    ".py", ".pyx", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".rb",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".swift", ".scala", ".php",
})
_NON_SOURCE_DIRS = ("examples/", "example/", "docs/", "doc/", "scripts/", "benchmarks/", ".github/")


def _module_names(path: str) -> list[str]:
    """Names a test could use to refer to ``path`` (dotted module, file stem)."""
    p = PurePosixPath(path)
    parts = list(p.with_suffix("").parts)
    if parts and parts[0] in ("src", "lib", "python"):
        parts = parts[1:]
    if parts and parts[-1] in ("__init__", "index", "mod", "lib", "main"):
        parts = parts[:-1]
    names = []
    if len(parts) > 1:
        names.append(".".join(parts))
        names.append("/".join(parts))
    if parts and len(parts[-1]) >= 3:
        names.append(parts[-1])
    return names


def _hunk_rank(hunk: Hunk, evidence_text: str) -> int:
    """0: source the evidence refers to; 1: other source; 2: everything else (config, examples, data)."""
    path = hunk.file
    if PurePosixPath(path).suffix.lower() not in CODE_SUFFIXES or path.startswith(_NON_SOURCE_DIRS):
        return 2
    for name in _module_names(path):
        if re.search(rf"(?<![\w.]){re.escape(name)}(?![\w])", evidence_text):
            return 0
    return 1


def prioritize_hunks(hunks: Sequence[Hunk], evidence_texts: Sequence[str]) -> list[Hunk]:
    """Order hunks so the cap spends its budget on code the evidence can depend on.

    Source files that the fail→pass tests (or reproducers) mention come first,
    then other source files, then configuration, examples and data. The order
    is stable and deterministic within each group.
    """
    text = "\n".join(evidence_texts)
    return sorted(hunks, key=lambda h: (_hunk_rank(h, text), h.file, h.new_start, h.old_start))


def _evidence_texts(repo: str | Path, head: str, test_files: Sequence[str]) -> list[str]:
    out = []
    for path in test_files[:50]:
        try:
            data = gitmod.show_file(repo, head, path)
        except gitmod.GitError:
            data = None
        if data:
            out.append(data[:200_000].decode("utf-8", "replace"))
    return out


def run_ablation(
    repo: str | Path,
    base: str,
    head: str,
    changed: Sequence[gitmod.ChangedFile],
    runner: Any,
    tests: Any,
    repros: Sequence[Any],
    issues: Mapping[str, str],
    env: Mapping[str, str],
    *,
    environ: Mapping[str, str] | None = None,
    max_hunks: int = 12,
    time_budget_s: float = 300,
    test_timeout: float = 600,
    repro_timeout: float = 60,
    python: str = "python3",
    exclude: Sequence[str] = (),
    setup: Callable[[Path], str | None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Ablation:
    """Build the diff evidence map. Never raises for run failures; returns status ``error`` instead."""
    budget = _Budget(float(time_budget_s), clock)
    test_ids, modules, snippets = collect_evidence(tests, repros, issues)
    evidence = list(test_ids) + [s.name for s in snippets]
    result = Ablation(status=STATUS_NOT_APPLICABLE, evidence=evidence, budget_s=float(time_budget_s))
    if not evidence:
        result.notes.append("No fail→pass test or behaviour-changing issue reproducer to ablate against.")
        return result
    in_tests: list[str] = []
    try:
        hunks = list_hunks(repo, base, head, changed, runner.is_test_file, exclude,
                           test_regions=getattr(runner, "test_regions", None), skipped_test_hunks=in_tests)
    except gitmod.GitError as exc:
        result.status = STATUS_ERROR
        result.notes.append(f"could not list hunks: {exc}")
        return result
    result.total_hunks = len(hunks)
    if in_tests:
        result.notes.append(f"{len(in_tests)} hunk(s) inside inline test modules are test code and were not ablated.")
    if not hunks:
        result.status = STATUS_NO_HUNKS
        result.notes.append("No non-test text changes to ablate.")
        return result
    hunks = prioritize_hunks(hunks, _evidence_texts(repo, head, modules) + [s.code for s in snippets])
    cap = max(0, int(max_hunks))
    selected, result.untested_over_cap = hunks[:cap], max(0, len(hunks) - cap)
    if result.untested_over_cap:
        result.notes.append(f"{result.untested_over_cap} more hunk(s) not tested (ablation.max_hunks = {cap}).")
    if not selected:
        result.status = STATUS_RAN
        result.elapsed_s = budget.elapsed()
        return result

    tmp = Path(tempfile.mkdtemp(prefix="proofcheck-ablation-"))
    try:
        with gitmod.worktree(repo, head, prefix="proofcheck-ablation-") as wt:
            if setup is not None:
                err = setup(wt)
                if err:
                    result.notes.append(err)
            # Baseline: the evidence must hold in an unmodified head worktree,
            # otherwise flaky or environment-dependent evidence would make every
            # hunk look "required".
            broken, why = _evaluate(runner, wt, test_ids, modules, snippets, env, environ, python,
                                    test_timeout, repro_timeout, budget)
            if why or broken:
                result.status = STATUS_BASELINE_FAILED
                result.notes.append("The evidence did not reproduce in a clean head worktree "
                                    f"({why or 'failing: ' + ', '.join(broken[:3])}); ablation results would not be meaningful.")
                return result
            result.status = STATUS_RAN
            for hunk in selected:
                if budget.remaining() <= 1:
                    result.budget_exhausted = True
                    result.hunks.append(HunkResult(hunk, SKIPPED, reason="time budget exhausted"))
                    continue
                started = clock()
                err = _reverse_apply(wt, hunk, tmp)
                if err:
                    _restore(wt, hunk)
                    result.hunks.append(HunkResult(hunk, SKIPPED, reason=err, duration=clock() - started))
                    continue
                try:
                    broken, why = _evaluate(runner, wt, test_ids, modules, snippets, env, environ, python,
                                            test_timeout, repro_timeout, budget)
                finally:
                    _restore(wt, hunk)
                took = clock() - started
                if why:
                    if why == "time budget exhausted":
                        result.budget_exhausted = True
                    result.hunks.append(HunkResult(hunk, SKIPPED, reason=why, duration=took))
                elif broken:
                    result.hunks.append(HunkResult(hunk, REQUIRED, broken_by=broken, duration=took))
                else:
                    result.hunks.append(HunkResult(hunk, NOT_EXERCISED, duration=took))
            if result.budget_exhausted:
                result.notes.append(f"Time budget of {time_budget_s:g}s exhausted; remaining hunks were skipped.")
    except Exception as exc:  # noqa: BLE001 - the map is optional; never lose the main result over it
        result.status = STATUS_ERROR
        result.notes.append(f"ablation aborted: {type(exc).__name__}: {str(exc)[:300]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        result.elapsed_s = budget.elapsed()
    return result


def summary_text(ablation: Mapping[str, Any]) -> str:
    """One-line summary of a (validated) ``ablation`` result section, or ''."""
    if not ablation or ablation.get("status") != STATUS_RAN:
        return ""
    tested = int(ablation.get("tested", 0))
    req = int(ablation.get("required", 0))
    if not tested:
        return ""
    noun, verb = ("hunk", "is") if tested == 1 else ("hunks", "are")
    untested = int(ablation.get("untested_over_cap", 0) or 0)
    if untested:
        total = int(ablation.get("total_hunks", tested + untested) or tested + untested)
        return f"{req}/{tested} tested {noun} {verb} required by the evidence ({untested} of {total} changed hunks not tested)"
    return f"{req}/{tested} changed {noun} {verb} required by the evidence"


def ablation_scope_flag(ablation: Ablation) -> Any:
    """A scope note listing files whose changed hunks no evidence depends on (or None)."""
    from .scope import ScopeFlag

    paths = ablation.unexercised_paths()
    if ablation.status != STATUS_RAN or not paths:
        return None
    n = ablation.count(NOT_EXERCISED)
    return ScopeFlag(
        "unexercised",
        f"Changed, but no evidence depends on it: reverting {'this hunk' if n == 1 else f'each of these {n} hunks'} "
        "leaves every fail→pass test and changed reproducer passing. Check it belongs in this PR.",
        paths,
    )
