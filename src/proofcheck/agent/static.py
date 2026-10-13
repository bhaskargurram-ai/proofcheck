"""Static validation of agent-written test files, and import repair.

Nothing here executes the candidate. Checks:

* the file parses (``ast.parse``) and is not absurdly large;
* it defines at least one test the configured runner will collect
  (``unittest.TestCase`` subclasses when the project uses unittest;
  ``test_*`` functions or ``Test*`` classes are also fine under pytest);
* relative imports are rejected (the file is stored in a new directory);
* imports of the repository's own modules must resolve **at head**. Near
  misses are repaired by edit distance, as in Otter (arXiv 2502.05368):
  a misspelled module path is replaced by the closest real one, a misspelled
  name by the closest top-level name in that module (imported ``as`` the
  original name, so the test body is untouched), and a name that lives in a
  different module is imported from there;
* a small denylist of process/network modules and calls. This is a lint
  that catches obviously off-task code, **not a sandbox**: the real
  protection is that candidates only run in a job without secrets.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable

MAX_CODE_CHARS = 40_000
MIN_CODE_CHARS = 10

DENY_MODULES = {
    "subprocess", "socket", "ctypes", "multiprocessing", "pty", "requests", "httpx", "aiohttp", "urllib3",
    "ftplib", "smtplib", "telnetlib", "poplib", "imaplib", "webbrowser", "paramiko", "asyncio.subprocess",
    "urllib.request", "http.client", "http.server", "xmlrpc", "socketserver",
}
DENY_CALLS = {
    "os.system", "os.popen", "os.fork", "os.forkpty", "os.kill", "os.killpg", "os.execv", "os.execve", "os.execl",
    "os.execle", "os.execlp", "os.execlpe", "os.execvp", "os.execvpe", "os.spawnl", "os.spawnle", "os.spawnlp",
    "os.spawnlpe", "os.spawnv", "os.spawnve", "os.spawnvp", "os.spawnvpe", "os.posix_spawn", "os.posix_spawnp",
    "eval", "exec", "__import__",
}

_STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"pytest", "unittest", "_pytest"}


def levenshtein(a: str, b: str) -> int:
    """Classic edit distance (insert/delete/substitute, cost 1)."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def closest(name: str, candidates: Iterable[str], max_ratio: float = 0.34) -> str | None:
    """The unique closest candidate within ``max_ratio * len(name)`` edits (at least 1, at most 6)."""
    limit = max(1, min(6, int(len(name) * max_ratio)))
    best: list[tuple[int, str]] = []
    for c in candidates:
        if abs(len(c) - len(name)) > limit:
            continue
        d = levenshtein(name.lower(), c.lower())
        if d <= limit:
            best.append((d, c))
    if not best:
        return None
    best.sort()
    if len(best) > 1 and best[0][0] == best[1][0]:
        return None  # ambiguous
    return best[0][1]


# --------------------------------------------------------------------------- module index


@dataclass
class ModuleIndex:
    """The Python modules of the repository at one commit.

    ``modules`` maps dotted names to file paths (``pkg/__init__.py`` for a
    package; ``""`` for a namespace package directory). ``read`` returns a
    module's source (used lazily to list its top-level names).
    """

    modules: dict[str, str]
    read: Callable[[str], str | None] = lambda path: None
    _names: dict[str, tuple[set[str], bool]] = field(default_factory=dict)

    @classmethod
    def from_paths(cls, paths: Iterable[str], read: Callable[[str], str | None] = lambda p: None) -> "ModuleIndex":
        modules: dict[str, str] = {}
        paths = list(paths)
        src_is_package = "src/__init__.py" in paths
        for p in paths:
            if not p.endswith(".py"):
                continue
            for root in ("", "src/"):
                if root and not p.startswith(root):
                    continue
                if not root and p.startswith("src/") and not src_is_package:
                    continue  # src layout: importable without the "src." prefix
                rel = p[len(root):]
                parts = rel[:-3].split("/")
                if not all(part.isidentifier() for part in parts[:-1]) or not parts[-1].isidentifier():
                    continue
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                    if not parts:
                        continue
                    modules[".".join(parts)] = p
                else:
                    modules.setdefault(".".join(parts), p)
                for i in range(1, len(parts)):  # parent packages (regular or namespace)
                    modules.setdefault(".".join(parts[:i]), "")
        return cls(modules, read)

    @property
    def top_levels(self) -> set[str]:
        return {m.split(".", 1)[0] for m in self.modules}

    def has_module(self, name: str) -> bool:
        return name in self.modules

    def names_in(self, module: str) -> tuple[set[str], bool]:
        """Top-level names defined in ``module`` and whether the module is 'open' (star import / __getattr__)."""
        if module in self._names:
            return self._names[module]
        names: set[str] = set()
        open_ = False
        prefix = module + "."
        names.update(m[len(prefix):] for m in self.modules if m.startswith(prefix) and "." not in m[len(prefix):])
        path = self.modules.get(module, "")
        src = self.read(path) if path else None
        if path and src is None:
            open_ = True  # cannot read it: do not judge
        if src:
            try:
                tree = ast.parse(src)
            except (SyntaxError, ValueError):
                tree = None
                open_ = True
            if tree is not None:
                for node in tree.body:
                    names.update(_defined_names(node))
                    if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
                        open_ = True
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__getattr__":
                        open_ = True
                    if isinstance(node, (ast.If, ast.Try)):  # conditional definitions
                        for sub in ast.walk(node):
                            if isinstance(sub, ast.stmt):
                                names.update(_defined_names(sub))
        self._names[module] = (names, open_)
        return names, open_

    def modules_defining(self, name: str) -> list[str]:
        """Modules (non-test first) whose top level defines ``name``."""
        hits = []
        for m, path in self.modules.items():
            if not path:
                continue
            names, _ = self.names_in(m)
            if name in names:
                hits.append(m)
        hits.sort(key=lambda m: (any(p.startswith("test") for p in m.split(".")), len(m), m))
        return hits


def _defined_names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.add(node.name)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            out.update(n.id for n in ast.walk(t) if isinstance(n, ast.Name))
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
        out.add(node.target.id)
    elif isinstance(node, ast.Import):
        out.update((a.asname or a.name.split(".", 1)[0]) for a in node.names)
    elif isinstance(node, ast.ImportFrom):
        out.update((a.asname or a.name) for a in node.names if a.name != "*")
    return out


# --------------------------------------------------------------------------- validation


@dataclass
class StaticReport:
    ok: bool
    code: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)
    test_names: list[str] = field(default_factory=list)


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else ""
    return ""


def _is_testcase_base(base: ast.expr) -> bool:
    name = _dotted(base)
    return name.endswith("TestCase") or name.endswith("IsolatedAsyncioTestCase")


def find_tests(tree: ast.Module, style: str) -> list[str]:
    """Names of tests the runner would collect (``Class.test_x`` or ``test_x``)."""
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test") and style == "pytest":
            found.append(node.name)
        elif isinstance(node, ast.ClassDef):
            is_case = any(_is_testcase_base(b) for b in node.bases)
            if not is_case and not (style == "pytest" and node.name.startswith("Test")):
                continue
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    found.append(f"{node.name}.{item.name}")
    return found


def _denylist(tree: ast.Module) -> list[str]:
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in DENY_MODULES or a.name.split(".", 1)[0] in DENY_MODULES:
                    problems.append(f"line {node.lineno}: importing {a.name!r} is not allowed in reproduction tests")
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mod = node.module
            if mod in DENY_MODULES or mod.split(".", 1)[0] in DENY_MODULES or any(f"{mod}.{a.name}" in DENY_MODULES for a in node.names):
                problems.append(f"line {node.lineno}: importing from {mod!r} is not allowed in reproduction tests")
            if mod == "os" and any(f"os.{a.name}" in DENY_CALLS for a in node.names):
                problems.append(f"line {node.lineno}: process-control functions from 'os' are not allowed")
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in DENY_CALLS:
                problems.append(f"line {node.lineno}: calling {name}() is not allowed in reproduction tests")
    return problems


def _replace_span(lines: list[str], node: ast.stmt, new_stmts: list[str]) -> None:
    """Replace the source span of ``node`` with ``new_stmts`` (same indentation)."""
    start, end = node.lineno - 1, (node.end_lineno or node.lineno) - 1
    col, end_col = node.col_offset, node.end_col_offset or len(lines[end])
    indent = lines[start][:col]
    prefix = lines[start][:col]
    suffix = lines[end][end_col:]
    text = ("\n" + indent).join(new_stmts)
    # Keep the newline structure: collapse the old span into one logical line.
    new_line = prefix + text + suffix
    lines[start:end + 1] = [new_line]


def _check_imports(tree: ast.Module, lines: list[str], index: ModuleIndex, report: StaticReport) -> bool:
    """Validate/repair imports. Returns True if the source was modified."""
    tops = index.top_levels
    edits: list[tuple[ast.stmt, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level and node.level > 0:
            report.errors.append(f"line {node.lineno}: relative imports are not allowed; import from the package root "
                                 "(the test is stored in its own directory)")
            continue
        if isinstance(node, ast.Import):
            new_aliases, changed = [], False
            for a in node.names:
                fixed = _resolve_module(a.name, index, tops, report, node.lineno)
                if fixed is None:
                    new_aliases.append(a)
                    continue
                if fixed != a.name:
                    changed = True
                    # Keep the name the test body uses.
                    asname = a.asname or (a.name if "." not in a.name else None)
                    if asname is None and "." in a.name:
                        report.errors.append(f"line {node.lineno}: module {a.name!r} does not exist at head (did you mean {fixed!r}?)")
                        new_aliases.append(a)
                        continue
                    new_aliases.append(ast.alias(name=fixed, asname=asname if asname != fixed else None))
                    report.repairs.append(f"import {a.name} -> import {fixed}" + (f" as {asname}" if asname and asname != fixed else ""))
                else:
                    new_aliases.append(a)
            if changed:
                edits.append((node, [ast.unparse(ast.Import(names=new_aliases))]))
        elif isinstance(node, ast.ImportFrom) and node.module:
            module = _resolve_module(node.module, index, tops, report, node.lineno)
            if module is None:
                continue  # not a repository module (stdlib/third-party) or already reported
            stmts: dict[str, list[ast.alias]] = {module: []}
            changed = module != node.module
            if changed:
                report.repairs.append(f"from {node.module} import ... -> from {module} import ...")
            names, open_ = index.names_in(module)
            for a in node.names:
                if a.name == "*" or open_ or a.name in names or index.has_module(f"{module}.{a.name}"):
                    stmts[module].append(a)
                    continue
                near = closest(a.name, names)
                if near:
                    stmts[module].append(ast.alias(name=near, asname=a.asname or a.name))
                    report.repairs.append(f"from {module} import {a.name} -> from {module} import {near} as {a.asname or a.name}")
                    changed = True
                    continue
                elsewhere = index.modules_defining(a.name)
                if elsewhere:
                    target = min(elsewhere, key=lambda m: (levenshtein(m, module), elsewhere.index(m)))
                    stmts.setdefault(target, []).append(a)
                    report.repairs.append(f"from {module} import {a.name} -> from {target} import {a.name}")
                    changed = True
                    continue
                report.errors.append(f"line {node.lineno}: {a.name!r} is not defined in module {module!r} at head")
            if changed:
                new = [ast.unparse(ast.ImportFrom(module=m, names=al, level=0)) for m, al in stmts.items() if al]
                edits.append((node, new))
    if not edits:
        return False
    # Apply bottom-up so earlier line numbers stay valid.
    for node, new in sorted(edits, key=lambda e: (e[0].lineno, e[0].col_offset), reverse=True):
        _replace_span(lines, node, new)
    return True


def _installed(top: str) -> bool:
    """True if a top-level module of this name is importable here (a dependency, not a typo).

    ``find_spec`` on a top-level name locates the module without executing it.
    """
    try:
        return importlib.util.find_spec(top) is not None
    except (ImportError, ValueError):
        return False


def _resolve_module(name: str, index: ModuleIndex, tops: set[str], report: StaticReport, lineno: int) -> str | None:
    """Return the (possibly repaired) repository module for ``name``; None if not a repo module or unresolvable."""
    top = name.split(".", 1)[0]
    if top not in tops:
        if top in _STDLIB or _installed(top):
            return None
        near_top = closest(top, tops)
        if near_top is None:
            return None  # assume a third-party dependency
        name = near_top + name[len(top):]
        top = near_top
    if index.has_module(name):
        return name
    near = closest(name, [m for m in index.modules if m.split(".", 1)[0] == top], max_ratio=0.25)
    if near:
        return near
    # "pkg.mod.func" used as a module path? Try the longest existing prefix.
    parts = name.split(".")
    for i in range(len(parts) - 1, 0, -1):
        if index.has_module(".".join(parts[:i])):
            report.errors.append(f"line {lineno}: module {name!r} does not exist at head ({'.'.join(parts[:i])!r} does)")
            return None
    report.errors.append(f"line {lineno}: module {name!r} does not exist at head")
    return None


def validate_test_code(code: str, style: str, index: ModuleIndex | None) -> StaticReport:
    """Validate (and possibly repair) a candidate test module.

    ``style`` is ``"pytest"`` or ``"unittest"``. Returns a report whose
    ``code`` holds the repaired source.
    """
    report = StaticReport(ok=False, code=code)
    if not isinstance(code, str) or len(code.strip()) < MIN_CODE_CHARS:
        report.errors.append("the test file is empty")
        return report
    if len(code) > MAX_CODE_CHARS:
        report.errors.append(f"the test file is too long ({len(code)} characters, limit {MAX_CODE_CHARS})")
        return report
    if "\x00" in code:
        report.errors.append("the test file contains NUL bytes")
        return report
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        report.errors.append(f"syntax error at line {exc.lineno}: {exc.msg}")
        return report
    report.errors.extend(_denylist(tree))
    if style == "unittest":
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if any(m.split(".", 1)[0] == "pytest" for m in mods):
                    report.errors.append(f"line {node.lineno}: this project runs its tests with unittest; do not import pytest")
    report.test_names = find_tests(tree, style)
    if not report.test_names:
        if style == "unittest":
            report.errors.append("no tests found: define a unittest.TestCase subclass with test_* methods")
        else:
            report.errors.append("no tests found: define at least one test_* function (or a Test* class with test_* methods)")
    if index is not None:
        lines = code.split("\n")
        if _check_imports(tree, lines, index, report):
            repaired = "\n".join(lines)
            try:
                ast.parse(repaired)
                report.code = repaired
            except SyntaxError:  # pragma: no cover - defensive: never ship a broken repair
                report.errors.append("import repair produced invalid code; fix the imports manually")
    report.ok = not report.errors
    return report
