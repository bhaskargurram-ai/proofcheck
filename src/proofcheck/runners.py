"""Pluggable test runners.

A runner knows three things about an ecosystem:

1. which changed files are *test-related* (copied from head into the base
   worktree) and which of those are *runnable test modules*;
2. how to build a command that runs a set of test files and writes a JUnit
   XML report;
3. optionally, which individual tests in a changed file were touched by the
   diff (so that untouched, pre-existing tests are not mistaken for new
   evidence).

Built-in runners: ``pytest`` (default), ``unittest`` (stdlib; useful when
pytest is not installed), ``jest`` (experimental, via ``jest-junit``) and
``cargo`` (Rust; see :mod:`proofcheck.cargo`).
"""

from __future__ import annotations

import ast
import fnmatch
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .junit import JUnitParseError, TestOutcome, parse_junit_file
from .proc import CommandResult, default_python, run_command

JUNIT_PLACEHOLDER = "{junitxml}"
TESTS_PLACEHOLDER = "{tests}"


@dataclass
class RunResult:
    """Outcome of one runner invocation (one worktree)."""

    outcomes: dict[str, TestOutcome] = field(default_factory=dict)
    command: list[str] = field(default_factory=list)
    returncode: int | None = None
    timed_out: bool = False
    error: str | None = None  # set when no usable report was produced
    output_tail: str = ""
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    p = PurePosixPath(path)
    for pat in patterns:
        if fnmatch.fnmatchcase(path, pat) or fnmatch.fnmatchcase(p.name, pat):
            return True
        # Support "**/" prefixes matching zero directories.
        if pat.startswith("**/") and fnmatch.fnmatchcase(path, pat[3:]):
            return True
    return False


def _tail(text: str, limit: int = 4000) -> str:
    return text[-limit:] if len(text) > limit else text


class TestRunner:
    """Base class for test runners. Subclasses set the class attributes."""

    __test__ = False  # not a pytest test class

    name = "base"
    #: Files that should be copied from head into the base worktree.
    test_patterns: tuple[str, ...] = ()
    #: Test-related files that are runnable test modules.
    module_patterns: tuple[str, ...] = ()
    default_command: tuple[str, ...] = ()
    #: Per-run timeout used when the configuration does not set one (None: the global default).
    default_timeout: float | None = None

    def __init__(
        self,
        command: Sequence[str] | None = None,
        test_patterns: Sequence[str] | None = None,
        module_patterns: Sequence[str] | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        if command:
            self.command = list(command)
        else:
            self.command = list(self.default_command)
            if self.command and self.command[0] == "python":
                self.command[0] = default_python()
        if test_patterns:
            self.test_patterns = tuple(test_patterns)
        if module_patterns:
            self.module_patterns = tuple(module_patterns)
        self.extra_env = dict(extra_env or {})

    # -- file classification ---------------------------------------------------
    def is_test_file(self, path: str) -> bool:
        """True if ``path`` is test-related (test module, fixture, conftest...)."""
        return _matches_any(path, self.test_patterns) or self.is_test_module(path)

    def is_test_module(self, path: str) -> bool:
        """True if ``path`` is a runnable test module."""
        return _matches_any(path, self.module_patterns)

    def touched_tests(self, path: str, head_source: str, changed_lines: set[int] | None) -> set[tuple[str | None, str]] | None:
        """Return ``{(class_name, test_name)}`` touched by the diff, or None if unknown.

        ``changed_lines`` is the set of head line numbers added/modified by the
        PR, or ``None`` when the whole file is new.
        """
        return None

    def test_belongs_to(self, test_id: str, path: str) -> bool:
        """True if ``test_id`` (from the JUnit report) comes from file ``path``."""
        return True

    def test_regions(self, path: str, source: str) -> list[tuple[int, int]] | None:
        """Line ranges (1-based, inclusive) of test code inside a *non-test* file.

        Used by the diff evidence map to leave inline test code alone (Rust
        ``#[cfg(test)]`` modules). ``None`` means the file could not be analysed.
        """
        return []

    def inline_test_units(self, repo: str | Path, base: str, head: str, changed: Sequence[Any], subdir: str = ".") -> tuple[list[Any], list[str]]:
        """Tests that live inside changed *source* files (Rust inline test modules).

        Returns ``(units, notes)``; each unit has ``path`` (project-relative),
        ``base_text`` (bytes written into the base worktree), ``touched`` and
        ``modules``. Runners without inline tests return ``([], [])``.
        """
        return [], []

    # -- execution ---------------------------------------------------------------
    def build_command(self, test_files: Sequence[str], junit_path: str) -> list[str]:
        """Expand placeholders in the configured command."""
        cmd: list[str] = []
        has_junit = any(JUNIT_PLACEHOLDER in part for part in self.command)
        has_tests = TESTS_PLACEHOLDER in self.command
        for part in self.command:
            if part == TESTS_PLACEHOLDER:
                cmd.extend(test_files)
            else:
                cmd.append(part.replace(JUNIT_PLACEHOLDER, junit_path))
        if not has_junit:
            cmd.extend(self.junit_args(junit_path))
        if not has_tests:
            cmd.extend(test_files)
        return cmd

    def junit_args(self, junit_path: str) -> list[str]:
        """Arguments appended when the command lacks a ``{junitxml}`` placeholder."""
        return []

    def env_for(self, workdir: Path, junit_path: str) -> dict[str, str]:
        """Runner-specific environment additions."""
        return {}

    def run_selected(self, workdir: Path, test_files: Sequence[str], test_ids: Sequence[str], env: Mapping[str, str],
                     timeout: float) -> RunResult:
        """Run (at least) ``test_ids`` from ``test_files``. The default runs the whole files."""
        return self.run(workdir, test_files, env, timeout)

    def run(self, workdir: Path, test_files: Sequence[str], env: Mapping[str, str], timeout: float) -> RunResult:
        """Run ``test_files`` inside ``workdir`` and parse the JUnit report."""
        tmp = Path(tempfile.mkdtemp(prefix="proofcheck-junit-"))
        junit_path = str(tmp / "report.xml")
        try:
            cmd = self.build_command(list(test_files), junit_path)
            full_env = dict(env)
            full_env.update(self.extra_env)
            for key, value in self.env_for(workdir, junit_path).items():
                if key == "PYTHONPATH" and full_env.get(key):
                    # Worktree first, then any user-configured entries.
                    value = value + os.pathsep + full_env[key]
                full_env[key] = value
            res: CommandResult = run_command(cmd, cwd=workdir, env=full_env, timeout=timeout)
            out = RunResult(
                command=cmd,
                returncode=res.returncode,
                timed_out=res.timed_out,
                output_tail=_tail(res.stdout + ("\n" if res.stdout and res.stderr else "") + res.stderr),
                duration=res.duration,
            )
            if res.launch_error:
                out.error = f"could not start test command: {res.launch_error}"
                return out
            if not os.path.exists(junit_path):
                reason = "timed out" if res.timed_out else f"exited with code {res.returncode}"
                out.error = f"test command {reason} without writing a JUnit report"
                return out
            try:
                out.outcomes = parse_junit_file(junit_path)
            except JUnitParseError as exc:
                out.error = str(exc)
            if res.timed_out and out.error is None:
                out.error = "test command timed out"
            return out
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- python

_PY_TEST_PATTERNS = ("test_*.py", "*_test.py", "conftest.py", "tests/*", "test/*", "*/tests/*", "*/test/*", "testing/*")
_PY_MODULE_PATTERNS = ("test_*.py", "*_test.py")


def python_module_for(path: str) -> str:
    """``tests/test_x.py`` -> ``tests.test_x`` (the prefix JUnit classnames use)."""
    p = path[:-3] if path.endswith(".py") else path
    return p.replace("/", ".")


def python_touched_tests(head_source: str, changed_lines: set[int] | None) -> set[tuple[str | None, str]] | None:
    """Find test functions/methods whose source lines intersect ``changed_lines``.

    Returns ``None`` when the file cannot be parsed. A brand-new file
    (``changed_lines is None``) marks every test as touched. A change to a
    class body or decorator line marks the enclosing test as touched.
    """
    try:
        tree = ast.parse(head_source)
    except (SyntaxError, ValueError):
        return None
    touched: set[tuple[str | None, str]] = set()

    def span(node: ast.AST) -> range:
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])  # type: ignore[attr-defined]
        end = getattr(node, "end_lineno", None) or node.lineno  # type: ignore[attr-defined]
        return range(start, end + 1)

    def consider(cls: str | None, fn: ast.AST) -> None:
        name = getattr(fn, "name", "")
        if not name.startswith("test"):
            return
        if changed_lines is None or any(line in changed_lines for line in span(fn)):
            touched.add((cls, name))

    def visit_class(cls: ast.ClassDef) -> None:
        for item in cls.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                consider(cls.name, item)
            elif isinstance(item, ast.ClassDef):  # nested test classes
                visit_class(item)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            consider(None, node)
        elif isinstance(node, ast.ClassDef):
            visit_class(node)
    return touched


class _PythonRunner(TestRunner):
    test_patterns = _PY_TEST_PATTERNS
    module_patterns = _PY_MODULE_PATTERNS

    def touched_tests(self, path: str, head_source: str, changed_lines: set[int] | None) -> set[tuple[str | None, str]] | None:
        if not path.endswith(".py"):
            return None
        return python_touched_tests(head_source, changed_lines)

    def test_belongs_to(self, test_id: str, path: str) -> bool:
        module = python_module_for(path)
        classname = test_id.split("::", 1)[0] if "::" in test_id else test_id
        return classname == module or classname.startswith(module + ".")

    def env_for(self, workdir: Path, junit_path: str) -> dict[str, str]:
        # Make the worktree's code win over any copy of the project that is
        # installed in the environment (e.g. an editable install of the checkout).
        parts = []
        if (workdir / "src").is_dir():
            parts.append(str(workdir / "src"))
        parts.append(str(workdir))
        return {"PYTHONPATH": os.pathsep.join(parts), "PYTHONDONTWRITEBYTECODE": "1"}


class PytestRunner(_PythonRunner):
    """Runs tests with pytest and ``--junitxml``."""

    name = "pytest"
    # --continue-on-collection-errors: a new test module that cannot import on
    # base (expected for new code) must not stop the other modules from running.
    default_command = ("python", "-m", "pytest", "-p", "no:cacheprovider", "-q", "-o", "junit_family=xunit2",
                       "--continue-on-collection-errors")

    def junit_args(self, junit_path: str) -> list[str]:
        # --rootdir=. makes JUnit classnames rootdir-relative dotted paths
        # (tests/test_x.py -> "tests.test_x"), which python_module_for() expects.
        return [f"--junitxml={junit_path}", "--rootdir=."]


class UnittestRunner(_PythonRunner):
    """Runs stdlib unittest test files via :mod:`proofcheck.unittest_junit`.

    The helper script is copied to a private temporary directory and run by
    path, so the project under test needs neither pytest nor proofcheck
    installed.
    """

    name = "unittest"
    default_command = ("python", "{unittest_junit}", "--junitxml", JUNIT_PLACEHOLDER, TESTS_PLACEHOLDER)

    def build_command(self, test_files: Sequence[str], junit_path: str) -> list[str]:
        cmd = super().build_command(test_files, junit_path)
        if any("{unittest_junit}" in c for c in cmd):
            helper_dir = Path(junit_path).parent
            helper = helper_dir / "proofcheck_unittest_junit.py"
            shutil.copyfile(Path(__file__).with_name("unittest_junit.py"), helper)
            cmd = [c.replace("{unittest_junit}", str(helper)) for c in cmd]
        return cmd


class JestRunner(TestRunner):
    """Experimental: runs Jest with the ``jest-junit`` reporter.

    The project must have ``jest`` and ``jest-junit`` installed. Test ids use
    the test file path as classname so they can be matched to files.
    """

    name = "jest"
    test_patterns = ("*.test.js", "*.test.jsx", "*.test.ts", "*.test.tsx", "*.test.mjs", "*.test.cjs",
                     "*.spec.js", "*.spec.jsx", "*.spec.ts", "*.spec.tsx", "*.spec.mjs", "*.spec.cjs",
                     "__tests__/*", "*/__tests__/*", "__mocks__/*", "*/__mocks__/*", "__fixtures__/*", "*/__fixtures__/*")
    module_patterns = ("*.test.js", "*.test.jsx", "*.test.ts", "*.test.tsx", "*.test.mjs", "*.test.cjs",
                       "*.spec.js", "*.spec.jsx", "*.spec.ts", "*.spec.tsx", "*.spec.mjs", "*.spec.cjs",
                       "__tests__/*.js", "*/__tests__/*.js", "__tests__/*.ts", "*/__tests__/*.ts")
    default_command = ("npx", "--no-install", "jest", "--ci", "--reporters=default", "--reporters=jest-junit", "--runTestsByPath", TESTS_PLACEHOLDER)

    def build_command(self, test_files: Sequence[str], junit_path: str) -> list[str]:
        # jest-junit is configured through the environment (see env_for), so
        # no junit argument is appended.
        cmd: list[str] = []
        for part in self.command:
            if part == TESTS_PLACEHOLDER:
                cmd.extend(test_files)
            else:
                cmd.append(part.replace(JUNIT_PLACEHOLDER, junit_path))
        if TESTS_PLACEHOLDER not in self.command:
            cmd.extend(test_files)
        return cmd

    def env_for(self, workdir: Path, junit_path: str) -> dict[str, str]:
        return {
            "JEST_JUNIT_OUTPUT_DIR": str(Path(junit_path).parent),
            "JEST_JUNIT_OUTPUT_NAME": Path(junit_path).name,
            "JEST_JUNIT_CLASSNAME": "{filepath}",
            "JEST_JUNIT_TITLE": "{classname} {title}",
            "JEST_JUNIT_ADD_FILE_ATTRIBUTE": "true",
            "CI": "true",
        }

    def test_belongs_to(self, test_id: str, path: str) -> bool:
        classname = test_id.split("::", 1)[0]
        return classname.replace("\\", "/").endswith(path)


RUNNERS: dict[str, type[TestRunner]] = {
    "pytest": PytestRunner,
    "unittest": UnittestRunner,
    "jest": JestRunner,
}

#: Every runner name accepted in configuration and on the command line.
RUNNER_NAMES = ("pytest", "unittest", "jest", "cargo")


def _runner_class(name: str) -> type[TestRunner]:
    if name == "cargo":
        from .cargo import CargoRunner  # imported lazily: proofcheck.cargo imports this module

        return CargoRunner
    try:
        return RUNNERS[name]
    except KeyError:
        raise ValueError(f"unknown runner {name!r}; choose one of {', '.join(sorted(RUNNER_NAMES))}") from None


def get_runner(
    name: str,
    command: Sequence[str] | None = None,
    test_patterns: Sequence[str] | None = None,
    module_patterns: Sequence[str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    **options: Any,
) -> TestRunner:
    """Instantiate a runner by name. ``options`` are runner-specific (cargo: ``target_dir``)."""
    cls = _runner_class(name)
    return cls(command=command, test_patterns=test_patterns, module_patterns=module_patterns, extra_env=extra_env, **options)


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def added_lines_from_diff(diff: str) -> set[int]:
    """Return head-side line numbers that were added or modified in a ``-U0`` diff."""
    lines: set[int] = set()
    for line in diff.splitlines():
        m = _HUNK_RE.match(line)
        if not m:
            continue
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 1
        if count == 0:
            # Pure deletion: mark the line after the deletion point so the
            # enclosing test still counts as touched.
            lines.add(max(start, 1))
            lines.add(start + 1)
        else:
            lines.update(range(start, start + count))
    return lines
