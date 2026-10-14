"""Read-only repository tools for the agent, implemented with git plumbing.

The model can look at the repository at two commits ("base" and "head")
and nothing else. Every tool is answered from the git object database with
``git show``, ``git ls-tree``, ``git grep``, ``git cat-file`` and
``git diff``; no file from the repository is ever executed, imported or even
checked out. The model has no write, exec or network tool. Its only output
channel is ``submit_test``, whose arguments are validated against a schema
(see :func:`validate_submission`).

Safety rules for arguments chosen by the model:

* ``ref`` is the enum ``base``/``head`` (mapped to SHAs), never a free ref;
* paths must be relative POSIX paths without ``..``, empty segments,
  backslashes, control characters or a leading ``-`` / ``:`` (git pathspec
  magic); everything is passed after ``--`` or as ``SHA:path``;
* blobs larger than :data:`MAX_BLOB_BYTES` and binary blobs are not shown;
* every result is truncated to a character budget, and file contents are
  wrapped in delimiters that mark them as untrusted data.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable

from ..git import _git_env
from .llm_tools import ToolCall, ToolResult, ToolSpec

MAX_BLOB_BYTES = 1_000_000
MAX_RESULT_CHARS = 8_000
MAX_LINES_PER_READ = 400
MAX_LIST_ENTRIES = 300
MAX_GREP_LINES = 80
MAX_PATTERN_CHARS = 200
GIT_TIMEOUT = 30

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ToolInputError(ValueError):
    """The model passed an invalid argument; the message is shown to it."""


def safe_path(value: Any, *, allow_empty: bool = False, what: str = "path") -> str:
    """Validate a repository-relative path chosen by the model."""
    if value is None and allow_empty:
        return ""
    if not isinstance(value, str):
        raise ToolInputError(f"{what} must be a string")
    s = value.strip()
    while s.startswith("./"):
        s = s[2:]
    s = s.rstrip("/")
    if s in ("", ".") and allow_empty:
        return ""
    if not s:
        raise ToolInputError(f"{what} must not be empty")
    if len(s) > 400:
        raise ToolInputError(f"{what} is too long")
    if _CTRL_RE.search(s) or "\\" in s:
        raise ToolInputError(f"{what} contains forbidden characters")
    if s.startswith(("/", "-", ":", "~")) or re.match(r"^[A-Za-z]:", s):
        raise ToolInputError(f"{what} must be relative to the repository root")
    parts = s.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ToolInputError(f"{what} must not contain '..', '.' or empty segments")
    if parts[0] == ".git":
        raise ToolInputError(f"{what} must not point into .git")
    return s


def safe_glob(value: Any) -> str:
    """Validate a path glob for grep (same rules as paths, plus * ? [ ])."""
    s = safe_path(value, what="path_glob")
    if not re.fullmatch(r"[A-Za-z0-9_\-./*?\[\]{}+@=,]+", s):
        raise ToolInputError("path_glob may only contain letters, digits and _-./*?[]{}+@=,")
    return s


def untrusted(source: str, text: str, nonce: str) -> str:
    """Wrap ``text`` in delimiters that mark it as untrusted data."""
    text = re.sub(r"<<<(?:END_)?UNTRUSTED_[0-9a-fA-F]+[^>]*>>>", "[delimiter removed]", text)
    src = re.sub(r"[^A-Za-z0-9_.:/#@ -]", "_", source)[:200]
    return f"<<<UNTRUSTED_{nonce} source={src}>>>\n{text}\n<<<END_UNTRUSTED_{nonce}>>>"


def truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... truncated {len(text) - limit} characters; narrow the request ...]"


def _string(args: dict[str, Any], key: str, default: str | None = None) -> str:
    v = args.get(key, default)
    if v is None:
        raise ToolInputError(f"missing argument {key!r}")
    if not isinstance(v, str):
        raise ToolInputError(f"{key} must be a string")
    return v


def _optional_int(args: dict[str, Any], key: str) -> int | None:
    v = args.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise ToolInputError(f"{key} must be an integer")
    return v


# --------------------------------------------------------------------------- schemas

_REF = {"type": "string", "enum": ["base", "head"], "description": "'base' = before the PR (bug present), 'head' = after the PR. Default: head."}

READ_ONLY_TOOLS = [
    ToolSpec("list_files", "List the entries of a directory of the repository at a commit (like ls). "
             "Use recursive=true to list a whole subtree (capped).",
             {"type": "object", "properties": {
                 "ref": _REF,
                 "path": {"type": "string", "description": "Directory relative to the repository root; '' for the root."},
                 "recursive": {"type": "boolean"}},
              "additionalProperties": False}),
    ToolSpec("read_file", f"Read a text file at a commit, with line numbers. At most {MAX_LINES_PER_READ} lines per call; "
             "use start_line/end_line to page.",
             {"type": "object", "properties": {
                 "ref": _REF,
                 "path": {"type": "string", "description": "File path relative to the repository root."},
                 "start_line": {"type": "integer", "minimum": 1},
                 "end_line": {"type": "integer", "minimum": 1}},
              "required": ["path"], "additionalProperties": False}),
    ToolSpec("grep", "Search file contents at a commit (git grep, extended regular expressions). "
             "Returns path:line:text matches (capped).",
             {"type": "object", "properties": {
                 "ref": _REF,
                 "pattern": {"type": "string", "description": "Extended regular expression (max 200 characters)."},
                 "path_glob": {"type": "string", "description": "Optional glob limiting the search, e.g. 'src/**/*.py'."},
                 "ignore_case": {"type": "boolean"}},
              "required": ["pattern"], "additionalProperties": False}),
    ToolSpec("get_diff", "Show the pull request's diff (base -> head). Without a path: changed files with line counts, "
             "then the diff (truncated). With a path: the diff of that file.",
             {"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": False}),
    ToolSpec("get_issue", "Show the pull request title/description and the text of the issue(s) it claims to fix.",
             {"type": "object", "properties": {}, "additionalProperties": False}),
    ToolSpec("list_tests", "List the existing test modules at head, plus how tests are run in this project.",
             {"type": "object", "properties": {}, "additionalProperties": False}),
]

#: Offered only for cargo projects (see :attr:`RepoTools.crates_text`).
LIST_CRATES_TOOL = ToolSpec(
    "list_crates", "List the Rust crates (Cargo packages) of the project at head: directory, package name and the "
    "library crate name to `use` in an integration test, plus where your test will be stored and how it runs.",
    {"type": "object", "properties": {}, "additionalProperties": False})

SUBMIT_TOOL = ToolSpec(
    "submit_test",
    "Submit the complete reproduction test file. This is your only way to deliver output. "
    "proofcheck validates it statically; if it is rejected you get the reasons and may submit again.",
    {"type": "object", "properties": {
        "filename": {"type": "string", "description": "A descriptive file name such as test_issue_42.py. "
                                                      "proofcheck decides the final location."},
        "code": {"type": "string", "description": "The complete source of the test module."},
        "rationale": {"type": "string", "description": "2-5 sentences: the focal code, the triggering input, why the test "
                                                       "fails on base for the issue's reason and passes on head."},
        "targets_claim": {"type": "string", "description": "The claim this test is evidence for, e.g. 'fixes #42'."}},
     "required": ["filename", "code", "rationale", "targets_claim"], "additionalProperties": False},
)

_SUBMIT_LIMITS = {"filename": 200, "code": 40_000, "rationale": 2_000, "targets_claim": 200}


def validate_submission(args: Any) -> tuple[dict[str, str] | None, list[str]]:
    """Strictly validate ``submit_test`` arguments: exactly the four string fields, length-capped."""
    if not isinstance(args, dict):
        return None, ["arguments must be a JSON object"]
    errors = []
    extra = set(args) - set(_SUBMIT_LIMITS)
    if extra:
        errors.append(f"unexpected argument(s): {', '.join(sorted(map(str, extra)))[:200]}")
    out: dict[str, str] = {}
    for key, limit in _SUBMIT_LIMITS.items():
        v = args.get(key)
        if not isinstance(v, str):
            errors.append(f"{key} is required and must be a string")
            continue
        if len(v) > limit:
            errors.append(f"{key} is too long ({len(v)} characters, limit {limit})")
            continue
        out[key] = v
    if errors:
        return None, errors
    return out, []


# --------------------------------------------------------------------------- tools


@dataclass
class RepoTools:
    """Answers read-only tool calls from the git object database."""

    repo: str
    base_sha: str
    head_sha: str
    nonce: str
    issue_text: str = ""  # already wrapped as untrusted data
    is_test_module: Callable[[str], bool] = lambda p: False
    test_info: str = ""
    #: Crate list for cargo projects; when set, the ``list_crates`` tool is offered.
    crates_text: str = ""
    max_result_chars: int = MAX_RESULT_CHARS
    calls: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)

    def specs(self) -> list[ToolSpec]:
        return list(READ_ONLY_TOOLS) + ([LIST_CRATES_TOOL] if self.crates_text else [])

    # -- plumbing ------------------------------------------------------------------------
    def _git(self, *args: str, timeout: float = GIT_TIMEOUT) -> tuple[int, bytes]:
        argv = ["git", "-C", str(self.repo), "-c", "core.quotepath=off", "--no-pager", *args]
        try:
            proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_git_env(),
                                  shell=False, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ToolInputError("git timed out; narrow the request") from exc
        return proc.returncode, proc.stdout

    def _sha(self, args: dict[str, Any]) -> tuple[str, str]:
        ref = args.get("ref", "head")
        if ref is None:
            ref = "head"
        if ref not in ("base", "head"):
            raise ToolInputError("ref must be 'base' or 'head'")
        return ref, (self.base_sha if ref == "base" else self.head_sha)

    # -- dispatch ------------------------------------------------------------------------
    def call(self, call: ToolCall) -> ToolResult:
        """Run one tool call; errors become ``is_error`` results (never exceptions)."""
        self.calls += 1
        handlers = {
            "list_files": self.list_files, "read_file": self.read_file, "grep": self.grep,
            "get_diff": self.get_diff, "get_issue": self.get_issue, "list_tests": self.list_tests,
        }
        if self.crates_text:
            handlers["list_crates"] = self.list_crates
        handler = handlers.get(call.name)
        if handler is None:
            return ToolResult(call.id, call.name, f"unknown tool {call.name[:60]!r}; available: {', '.join(handlers)}, submit_test", True)
        if call.parse_error:
            return ToolResult(call.id, call.name, call.parse_error, True)
        try:
            text = handler(dict(call.arguments))
            return ToolResult(call.id, call.name, truncate(text, self.max_result_chars))
        except ToolInputError as exc:
            return ToolResult(call.id, call.name, str(exc), True)

    def list_files(self, args: dict[str, Any]) -> str:
        _check_keys(args, {"ref", "path", "recursive"})
        ref, sha = self._sha(args)
        path = safe_path(args.get("path", ""), allow_empty=True)
        recursive = args.get("recursive", False)
        if not isinstance(recursive, bool):
            raise ToolInputError("recursive must be a boolean")
        argv = ["ls-tree", "-z", "-l"] + (["-r"] if recursive else []) + [sha]
        if path:
            argv += ["--", path + "/"]
        rc, out = self._git(*argv)
        if rc != 0:
            raise ToolInputError(f"cannot list {path or '/'} at {ref}")
        entries = [e for e in out.decode("utf-8", "replace").split("\0") if e]
        if not entries:
            raise ToolInputError(f"{path or '/'} is not a directory at {ref} (or it is empty)")
        lines = []
        for e in entries[:MAX_LIST_ENTRIES]:
            meta, _, name = e.partition("\t")
            fields = meta.split()
            kind = fields[1] if len(fields) > 1 else "?"
            size = fields[3] if len(fields) > 3 else "-"
            lines.append(f"{name}/" if kind == "tree" else (f"{name}  ({size} bytes)" if kind == "blob" else f"{name}  [{kind}]"))
        more = f"\n[... {len(entries) - MAX_LIST_ENTRIES} more entries]" if len(entries) > MAX_LIST_ENTRIES else ""
        return f"{path or '/'} at {ref}:\n" + "\n".join(lines) + more

    def read_file(self, args: dict[str, Any]) -> str:
        _check_keys(args, {"ref", "path", "start_line", "end_line"})
        ref, sha = self._sha(args)
        path = safe_path(args.get("path"))
        start = _optional_int(args, "start_line") or 1
        end = _optional_int(args, "end_line")
        if start < 1 or (end is not None and end < start):
            raise ToolInputError("need 1 <= start_line <= end_line")
        rc, kind = self._git("cat-file", "-t", f"{sha}:{path}")
        kind_s = kind.decode().strip()
        if rc != 0:
            raise ToolInputError(f"{path} does not exist at {ref}")
        if kind_s == "tree":
            raise ToolInputError(f"{path} is a directory; use list_files")
        if kind_s != "blob":
            raise ToolInputError(f"{path} is not a regular file at {ref}")
        rc, size = self._git("cat-file", "-s", f"{sha}:{path}")
        if rc != 0 or int(size.decode().strip() or 0) > MAX_BLOB_BYTES:
            raise ToolInputError(f"{path} is larger than {MAX_BLOB_BYTES} bytes; use grep to find the relevant part")
        rc, data = self._git("show", f"{sha}:{path}")
        if rc != 0:
            raise ToolInputError(f"cannot read {path} at {ref}")
        if b"\0" in data[:8000]:
            raise ToolInputError(f"{path} is a binary file")
        lines = data.decode("utf-8", "replace").splitlines()
        total = len(lines)
        if end is None:
            end = start + MAX_LINES_PER_READ - 1
        end = min(end, total, start + MAX_LINES_PER_READ - 1)
        if total and start > total:
            raise ToolInputError(f"{path} has only {total} lines")
        body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(start, end + 1)) if total else "(empty file)"
        header = f"{path} at {ref}, lines {start}-{end} of {total}"
        body = truncate(body, self.max_result_chars - 400)
        return header + "\n" + untrusted(f"{ref}:{path}", body, self.nonce)

    def grep(self, args: dict[str, Any]) -> str:
        _check_keys(args, {"ref", "pattern", "path_glob", "ignore_case"})
        ref, sha = self._sha(args)
        pattern = _string(args, "pattern")
        if not pattern or len(pattern) > MAX_PATTERN_CHARS or _CTRL_RE.search(pattern):
            raise ToolInputError(f"pattern must be 1-{MAX_PATTERN_CHARS} printable characters")
        argv = ["grep", "-n", "-I", "--no-color", "-E"]
        ic = args.get("ignore_case", False)
        if not isinstance(ic, bool):
            raise ToolInputError("ignore_case must be a boolean")
        if ic:
            argv.append("-i")
        argv += ["-e", pattern, sha]
        if args.get("path_glob"):
            argv += ["--", f":(glob){safe_glob(args['path_glob'])}"]
        rc, out = self._git(*argv)
        if rc == 1:
            return f"no matches for {pattern!r} at {ref}"
        if rc != 0:
            raise ToolInputError("invalid pattern or path_glob")
        prefix = sha + ":"
        lines = []
        for raw in out.decode("utf-8", "replace").splitlines():
            line = raw[len(prefix):] if raw.startswith(prefix) else raw
            lines.append(line if len(line) <= 300 else line[:300] + "…")
        shown = lines[:MAX_GREP_LINES]
        more = f"\n[... {len(lines) - MAX_GREP_LINES} more matches; narrow the pattern or path_glob]" if len(lines) > MAX_GREP_LINES else ""
        return f"{len(lines)} match(es) at {ref}:\n" + untrusted(f"grep@{ref}", "\n".join(shown), self.nonce) + more

    def get_diff(self, args: dict[str, Any]) -> str:
        _check_keys(args, {"path"})
        if args.get("path"):
            path = safe_path(args["path"])
            rc, out = self._git("diff", "-U3", "--no-color", "--no-ext-diff", self.base_sha, self.head_sha, "--", path)
            if rc != 0:
                raise ToolInputError("cannot diff that path")
            text = out.decode("utf-8", "replace")
            return untrusted(f"diff:{path}", text or f"(no changes to {path})", self.nonce)
        rc, stat = self._git("diff", "--numstat", "--no-color", self.base_sha, self.head_sha)
        rc2, out = self._git("diff", "-U3", "--no-color", "--no-ext-diff", self.base_sha, self.head_sha)
        if rc != 0 or rc2 != 0:
            raise ToolInputError("cannot compute the diff")
        files = []
        for line in stat.decode("utf-8", "replace").splitlines():
            a, d, p = (line.split("\t", 2) + ["", "", ""])[:3]
            files.append(f"  {p}  (+{a} -{d})")
        head = "Changed files (base -> head):\n" + "\n".join(files[:200])
        budget = max(1000, self.max_result_chars - len(head) - 300)
        return head + "\n\n" + untrusted("diff", truncate(out.decode("utf-8", "replace"), budget), self.nonce)

    def get_issue(self, args: dict[str, Any]) -> str:
        _check_keys(args, set())
        return self.issue_text or "(no pull request or issue text is available)"

    def list_tests(self, args: dict[str, Any]) -> str:
        _check_keys(args, set())
        rc, out = self._git("ls-tree", "-r", "-z", "--name-only", self.head_sha)
        if rc != 0:
            raise ToolInputError("cannot list files")
        paths = [p for p in out.decode("utf-8", "replace").split("\0") if p and self.is_test_module(p)]
        shown = "\n".join(paths[:MAX_LIST_ENTRIES]) or "(no test modules found)"
        more = f"\n[... {len(paths) - MAX_LIST_ENTRIES} more]" if len(paths) > MAX_LIST_ENTRIES else ""
        return (self.test_info + "\n\n" if self.test_info else "") + f"Test modules at head ({len(paths)}):\n" + shown + more


    def list_crates(self, args: dict[str, Any]) -> str:
        _check_keys(args, set())
        return self.crates_text


def _check_keys(args: dict[str, Any], allowed: set[str]) -> None:
    extra = set(args) - allowed
    if extra:
        raise ToolInputError(f"unexpected argument(s): {', '.join(sorted(map(str, extra)))[:200]}")


def list_head_files(repo: str, sha: str) -> list[str]:
    """All file paths at ``sha`` (used to build the module index)."""
    proc = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=off", "ls-tree", "-r", "-z", "--name-only", sha],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_git_env(), shell=False, timeout=GIT_TIMEOUT)
    if proc.returncode != 0:
        return []
    return [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]


def read_blob_text(repo: str, sha: str, path: str) -> str | None:
    """Text of ``path`` at ``sha`` (None if missing, binary or too large)."""
    try:
        PurePosixPath(safe_path(path))
    except ToolInputError:
        return None
    proc = subprocess.run(["git", "-C", str(repo), "cat-file", "-s", f"{sha}:{path}"], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=_git_env(), shell=False, timeout=GIT_TIMEOUT)
    if proc.returncode != 0 or int(proc.stdout.decode().strip() or 0) > MAX_BLOB_BYTES:
        return None
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{path}"], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=_git_env(), shell=False, timeout=GIT_TIMEOUT)
    if proc.returncode != 0 or b"\0" in proc.stdout[:8000]:
        return None
    return proc.stdout.decode("utf-8", "replace")
