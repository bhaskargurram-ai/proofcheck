"""Issue reproduction: run code snippets from an issue on base and on head.

Issue bodies frequently contain a minimal reproducer in a fenced
```` ```python ```` block. Running it against both commits shows whether the
PR changes the behaviour the issue describes. A changed exit code or output
is *evidence that something changed*; it is not proof that the change is the
right one, and the card says so.

Snippets are untrusted. They run in a subprocess with a timeout, with
secret-looking environment variables removed, in a throwaway directory.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .proc import run_command, scrub_env

BEHAVIOR_CHANGED = "behavior_changed"
UNCHANGED = "unchanged"
ERROR = "error"

#: Maximum number of snippets taken from one issue and their maximum size.
MAX_SNIPPETS = 5
MAX_SNIPPET_CHARS = 20_000

_FENCE_RE = re.compile(
    r"^(?P<indent>[ \t]{0,3})(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^\n`]*)\n(?P<body>.*?)\n[ \t]{0,3}(?P=fence)[ \t]*$",
    re.M | re.S,
)
_PY_LANGS = {"python", "py", "python3", "py3"}

#: Lines that make a block look like an interactive session / traceback rather than a script.
_NON_SCRIPT_RE = re.compile(r"^\s*(>>>|\$ |Traceback \(most recent call last\))", re.M)

_ADDR_RE = re.compile(r"0x[0-9a-fA-F]{6,}")


def extract_python_blocks(text: str, limit: int = MAX_SNIPPETS) -> list[str]:
    """Return the bodies of fenced ``python``/``py`` code blocks in ``text``.

    Blocks that look like interactive sessions (``>>>``), shell transcripts or
    tracebacks are skipped, as are oversized blocks.
    """
    blocks: list[str] = []
    for m in _FENCE_RE.finditer(text or ""):
        info = m.group("info").strip().split()
        lang = info[0].lower() if info else ""
        if lang.startswith("{") and lang.endswith("}"):
            lang = lang.strip("{}.")
        if lang not in _PY_LANGS:
            continue
        body = m.group("body")
        if not body.strip() or len(body) > MAX_SNIPPET_CHARS or _NON_SCRIPT_RE.search(body):
            continue
        blocks.append(body)
        if len(blocks) >= limit:
            break
    return blocks


def normalize_output(text: str, workdirs: list[str]) -> str:
    """Remove noise that differs between runs (worktree paths, object addresses)."""
    for wd in sorted(workdirs, key=len, reverse=True):
        if wd:
            text = text.replace(wd, "<worktree>")
    text = _ADDR_RE.sub("0x…", text)
    return text.strip()


@dataclass
class SnippetRun:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str | None = None

    def last_line(self) -> str:
        for stream in (self.stderr, self.stdout):
            lines = [line for line in stream.strip().splitlines() if line.strip()]
            if lines:
                return lines[-1][:300]
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "error": self.error,
            "stdout_tail": self.stdout[-1500:],
            "stderr_tail": self.stderr[-1500:],
            "last_line": self.last_line(),
        }


@dataclass
class SnippetResult:
    index: int
    status: str
    base: SnippetRun
    head: SnippetRun
    code_preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "status": self.status,
            "code_preview": self.code_preview,
            "base": self.base.to_dict(),
            "head": self.head.to_dict(),
        }


@dataclass
class IssueRepro:
    issue: str  # "#12" or "file:<name>"
    status: str  # "reproduced_change" | "unchanged" | "no_snippets" | "error"
    snippets: list[SnippetResult] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"issue": self.issue, "status": self.status, "note": self.note, "snippets": [s.to_dict() for s in self.snippets]}


def _pythonpath_for(workdir: Path) -> str:
    parts = []
    if (workdir / "src").is_dir():
        parts.append(str(workdir / "src"))
    parts.append(str(workdir))
    return os.pathsep.join(parts)


def run_snippet(code: str, workdir: Path, python: str, timeout: float, environ: Mapping[str, str] | None = None) -> SnippetRun:
    """Run ``code`` with ``python`` against the code in ``workdir``.

    The snippet is written to a private temporary directory which is also the
    working directory, so stray files it creates never land in the worktree.
    """
    tmp = Path(tempfile.mkdtemp(prefix="proofcheck-repro-"))
    try:
        script = tmp / "snippet.py"
        script.write_text(code, encoding="utf-8")
        env = scrub_env(environ)
        pythonpath = _pythonpath_for(workdir)
        if env.get("PYTHONPATH"):
            pythonpath += os.pathsep + env["PYTHONPATH"]
        env.update({"PYTHONPATH": pythonpath, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"})
        res = run_command([python, "-B", str(script)], cwd=tmp, env=env, timeout=timeout)
        return SnippetRun(
            res.returncode,
            normalize_output(res.stdout, [str(workdir), str(tmp)]),
            normalize_output(res.stderr, [str(workdir), str(tmp)]),
            res.timed_out,
            res.launch_error,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def compare_runs(base: SnippetRun, head: SnippetRun) -> str:
    """Classify a pair of snippet runs."""
    if base.error or head.error:
        return ERROR
    if base.timed_out and head.timed_out:
        return ERROR
    if (base.exit_code, base.stdout, base.stderr, base.timed_out) == (head.exit_code, head.stdout, head.stderr, head.timed_out):
        return UNCHANGED
    return BEHAVIOR_CHANGED


def reproduce_issue(
    label: str, body: str, base_wt: Path, head_wt: Path, python: str, timeout: float, environ: Mapping[str, str] | None = None
) -> IssueRepro:
    """Run every Python snippet from an issue body on base and head."""
    blocks = extract_python_blocks(body)
    if not blocks:
        return IssueRepro(label, "no_snippets", note="No runnable ```python code block found in the issue.")
    results = []
    for i, code in enumerate(blocks, 1):
        b = run_snippet(code, base_wt, python, timeout, environ)
        h = run_snippet(code, head_wt, python, timeout, environ)
        preview = code.strip().splitlines()[0][:120] if code.strip() else ""
        results.append(SnippetResult(i, compare_runs(b, h), b, h, preview))
    statuses = {r.status for r in results}
    if BEHAVIOR_CHANGED in statuses:
        status = "reproduced_change"
    elif statuses == {UNCHANGED}:
        status = "unchanged"
    elif UNCHANGED in statuses:
        status = "unchanged"
    else:
        status = "error"
    return IssueRepro(label, status, results)
