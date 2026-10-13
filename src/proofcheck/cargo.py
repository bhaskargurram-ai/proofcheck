"""Rust support: the ``cargo`` test runner.

Integration tests (``<crate>/tests/<name>.rs``) are run per target with
``cargo test -p <package> --test <name> --no-fail-fast -- --test-threads=1``.
Inline unit tests (``#[cfg(test)] mod tests { ... }`` inside ``src/``) are run
with ``cargo test -p <package> --lib <module>::tests::`` (``--bins`` for a
crate without a library), after the changed test module has been
transplanted from the head version of the file into the base worktree (see
:func:`proofcheck.rustsrc.transplant_test_modules`).

libtest's stable, human-readable output is parsed (``test name ... ok``);
``--format json`` needs a nightly toolchain and is not used. A target that
does not compile is reported as one pseudo-test without ``::`` in its id
(like a Python module that fails to import) with ``kind = "build"``, so the
fail-to-pass check can say "fails on base because it does not compile there".

Test ids:

* integration test: ``<file>::<libtest name>``, e.g. ``crates/foo/tests/cli.rs::parses_flags``
* inline test:      ``<package>::<libtest name>``, e.g. ``foo::parser::tests::rejects_empty``

All worktrees (base, head, ablation) share one ``CARGO_TARGET_DIR`` so that
dependencies compile once: the runner's ``target_dir``, else
``$CARGO_TARGET_DIR``, else ``<tmp>/proofcheck-cargo-target``.

Sharing a target directory between checkouts of the same workspace has a
trap: cargo's unit hashes do not depend on where a path package lives, and
its freshness check compares source *mtimes* with the artifacts. A base
worktree created before the head build would therefore look up to date and
silently reuse the head's binaries. Whenever cargo is about to run in a
different worktree than the previous run that used the same target
directory, every file of that worktree gets a fresh mtime first
(:func:`ensure_fresh`), so the workspace's own crates are rebuilt while
registry dependencies stay cached.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from . import git as gitmod
from . import rustsrc
from .junit import ERROR, FAILED, PASSED, SKIPPED, TestOutcome
from .proc import run_command
from .runners import RunResult, TestRunner, _matches_any, _tail, added_lines_from_diff

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib

#: Default per-run timeout for cargo projects (compiling a workspace is slow).
CARGO_DEFAULT_TIMEOUT = 1800.0
DEFAULT_TARGET_DIR_NAME = "proofcheck-cargo-target"

_TEST_LINE_RE = re.compile(r"^test (\S+?)(?: - should panic)? \.\.\. ?(.*)$")
_STATUS_RE = re.compile(r"(?:^|\s)(ok|FAILED|ignored)(?:, .*)?$")
_FAIL_HDR_RE = re.compile(r"^---- (\S+) std(?:out|err) ----$")
_PANIC_RE = re.compile(r"^thread '([^']*)'(?: \(\d+\))? panicked at (.*?):?$")
_ERROR_RE = re.compile(r"^error(\[E\d+\])?: (.*)$")
_ERROR_SKIP = ("could not compile", "aborting due to", "test failed, to rerun", "target failed", "targets failed")


def default_target_dir() -> str:
    return str(Path(tempfile.gettempdir()) / DEFAULT_TARGET_DIR_NAME)


MARKER_NAME = ".proofcheck-last-worktree"
_SKIP_DIRS = frozenset({".git", "target", "node_modules", ".venv"})


def worktree_root(workdir: Path) -> Path:
    """The enclosing git checkout (directory holding ``.git``), else ``workdir``."""
    d = workdir.resolve()
    for cand in (d, *d.parents):
        if (cand / ".git").exists():
            return cand
    return d


def ensure_fresh(workdir: Path, target_dir: str) -> bool:
    """Bump the mtime of every file in ``workdir``'s checkout if another checkout used ``target_dir`` last.

    Returns True when files were touched. See the module docstring for why.
    """
    root = worktree_root(Path(workdir))
    marker = Path(target_dir) / MARKER_NAME
    try:
        last = marker.read_text(encoding="utf-8").strip()
    except OSError:
        last = ""
    touched = False
    if last != str(root):
        now = time.time()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                p = os.path.join(dirpath, name)
                try:
                    if not os.path.islink(p):
                        os.utime(p, (now, now))
                except OSError:
                    pass
        touched = True
    try:
        marker.write_text(str(root) + "\n", encoding="utf-8")
    except OSError:
        pass
    return touched


# --------------------------------------------------------------------------- manifests

@dataclass
class CrateInfo:
    """What the runner needs from a crate's ``Cargo.toml``."""

    dir: str  # crate directory relative to the project ("" for the project root)
    package: str
    has_lib: bool
    bins: list[str] = field(default_factory=list)
    #: path relative to the crate dir -> [[test]] target name
    test_targets: dict[str, str] = field(default_factory=dict)
    autotests: bool = True


def read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


def crate_info(workdir: Path, crate_dir: str) -> CrateInfo | None:
    """Parse ``<workdir>/<crate_dir>/Cargo.toml``; None if it is missing or has no ``[package]``."""
    root = workdir / crate_dir if crate_dir else workdir
    data = read_manifest(root / "Cargo.toml")
    if not data or not isinstance(data.get("package"), dict) or not isinstance(data["package"].get("name"), str):
        return None
    pkg = data["package"]
    has_lib = isinstance(data.get("lib"), dict) or (root / "src" / "lib.rs").is_file()
    bins: list[str] = []
    for b in data.get("bin") or []:
        if isinstance(b, dict) and isinstance(b.get("name"), str):
            bins.append(b["name"])
    if not bins and (root / "src" / "main.rs").is_file():
        bins.append(pkg["name"])
    tests: dict[str, str] = {}
    for t in data.get("test") or []:
        if isinstance(t, dict) and isinstance(t.get("name"), str):
            tests[str(t.get("path") or f"tests/{t['name']}.rs").lstrip("./")] = t["name"]
    autotests = pkg.get("autotests", True) is not False
    return CrateInfo(crate_dir, pkg["name"], has_lib, bins, tests, autotests)


def find_crate_dir(workdir: Path, path: str) -> str | None:
    """Nearest ancestor directory of ``path`` (relative to ``workdir``) with a ``[package]`` manifest."""
    parts = PurePosixPath(path).parts[:-1]
    for k in range(len(parts), -1, -1):
        d = "/".join(parts[:k])
        if crate_info(workdir, d) is not None:
            return d
    return None


def module_path(rel_to_src: str) -> str:
    """``parser/mod.rs`` -> ``parser``; ``a/b.rs`` -> ``a::b``; ``lib.rs`` -> ``""``."""
    parts = list(PurePosixPath(rel_to_src).with_suffix("").parts)
    if parts and parts[-1] in ("lib", "main", "mod"):
        parts = parts[:-1]
    return "::".join(parts)


# --------------------------------------------------------------------------- classification

def _parts(path: str) -> tuple[str, ...]:
    return PurePosixPath(path).parts


def is_integration_test_related(path: str) -> bool:
    """Anything under a ``tests/`` directory that is not inside ``src/`` (tests, helpers, fixtures)."""
    parts = _parts(path)
    for k, p in enumerate(parts[:-1]):
        if p == "tests":
            return not any(q in ("src", "target") for q in parts[:k])
    return False


def is_integration_test_module(path: str) -> bool:
    """``<crate>/tests/<name>.rs`` or ``<crate>/tests/<name>/main.rs``."""
    if not path.endswith(".rs") or not is_integration_test_related(path):
        return False
    parts = _parts(path)
    if len(parts) >= 2 and parts[-2] == "tests":
        return True
    return len(parts) >= 3 and parts[-3] == "tests" and parts[-1] == "main.rs"


def is_rust_source(path: str) -> bool:
    """A ``.rs`` file under a ``src/`` directory (may contain inline tests)."""
    parts = _parts(path)
    return path.endswith(".rs") and "src" in parts[:-1] and not is_integration_test_related(path)


# --------------------------------------------------------------------------- output parsing

@dataclass
class LibtestRun:
    """Parsed output of one ``cargo test`` invocation."""

    results: dict[str, tuple[str, str, str]] = field(default_factory=dict)  # name -> (outcome, message, detail)
    started: bool = False  # a test binary printed "running N tests"
    finished: bool = False  # a "test result:" summary was printed
    crashed: str | None = None  # name of a test that started but never reported


def parse_libtest(stdout: str) -> LibtestRun:
    run = LibtestRun()
    pending: str | None = None
    failures: dict[str, list[str]] = {}
    current: str | None = None
    for raw in stdout.splitlines():
        line = raw.rstrip("\r")
        m = _FAIL_HDR_RE.match(line)
        if m:
            current = m.group(1)
            failures.setdefault(current, [])
            continue
        if line in ("failures:", "successes:"):
            current = None
            continue
        if current is not None:
            # Captured output of a failed test: anything may appear here.
            failures[current].append(line)
            continue
        if line.startswith("running ") and line.endswith((" tests", " test")):
            run.started = True
            pending = None
            continue
        if line.startswith("test result: "):
            run.finished = True
            pending = None
            continue
        m = _TEST_LINE_RE.match(line)
        if m:
            name, rest = m.group(1), m.group(2)
            st = _STATUS_RE.search(rest) if rest else None
            if st and (rest == st.group(0).strip() or rest.startswith(st.group(1))):
                run.results[name] = (_outcome(st.group(1)), "", "")
                pending = None
            else:
                pending = name
            continue
        if pending is not None:
            st = _STATUS_RE.search(line.strip())
            if st:
                run.results[pending] = (_outcome(st.group(1)), "", "")
                pending = None
    if pending is not None and pending not in run.results:
        run.crashed = pending
    for name, lines in failures.items():
        if name in run.results and run.results[name][0] == FAILED:
            run.results[name] = (FAILED, _panic_message(lines), "\n".join(lines).strip()[-2000:])
    return run


def _outcome(word: str) -> str:
    return {"ok": PASSED, "FAILED": FAILED}.get(word, SKIPPED)


def _panic_message(lines: Sequence[str]) -> str:
    for k, line in enumerate(lines):
        m = _PANIC_RE.match(line.strip())
        if m:
            msg = []
            for nxt in lines[k + 1:]:
                if not nxt.strip() or nxt.startswith(("stack backtrace:", "note: ")):
                    break
                msg.append(nxt.strip())
            text = " ".join(msg) or "panicked"
            return f"{text} (at {m.group(2)})"[:2000]
    for line in lines:
        if line.strip():
            return line.strip()[:2000]
    return "test failed"


def first_compile_error(stderr: str) -> str:
    """The first meaningful ``error...`` line of cargo/rustc output, with its location."""
    lines = stderr.splitlines()
    for k, line in enumerate(lines):
        m = _ERROR_RE.match(line.strip())
        if not m or any(s in m.group(2) for s in _ERROR_SKIP):
            continue
        text = line.strip()
        for nxt in lines[k + 1:k + 3]:
            if nxt.strip().startswith("--> "):
                text += f" ({nxt.strip()[4:]})"
                break
        return text[:500]
    for line in lines:
        if line.strip().startswith("error"):
            return line.strip()[:500]
    return "the test target failed to build"


# --------------------------------------------------------------------------- runner

@dataclass
class _Target:
    """One ``cargo test`` invocation."""

    module: str  # the file the target was selected for (project-relative)
    args: list[str]  # cargo args after the base command, before "--"
    filters: list[str]
    id_prefix: str  # prefix of the ids of this target's tests
    name_prefixes: list[str] = field(default_factory=list)  # keep only tests whose libtest name starts with one of these
    error: str | None = None  # the target could not even be resolved


class CargoRunner(TestRunner):
    """Runs Rust tests with ``cargo test`` (integration tests and inline ``#[cfg(test)]`` modules)."""

    name = "cargo"
    default_command = ("cargo", "test")
    default_timeout = CARGO_DEFAULT_TIMEOUT

    def __init__(
        self,
        command: Sequence[str] | None = None,
        test_patterns: Sequence[str] | None = None,
        module_patterns: Sequence[str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        target_dir: str | None = None,
    ) -> None:
        super().__init__(command=command, test_patterns=test_patterns, module_patterns=module_patterns, extra_env=extra_env)
        self._custom_tests = bool(test_patterns)
        self._custom_modules = bool(module_patterns)
        self.target_dir = target_dir
        #: inline source file -> libtest name prefixes of its test modules (filled by run())
        self._inline_prefixes: dict[str, tuple[str, list[str]]] = {}

    # -- classification ----------------------------------------------------------
    def is_test_file(self, path: str) -> bool:
        if self._custom_tests:
            return _matches_any(path, self.test_patterns) or self.is_test_module(path)
        return is_integration_test_related(path)

    def is_test_module(self, path: str) -> bool:
        if self._custom_modules:
            return _matches_any(path, self.module_patterns)
        return is_integration_test_module(path)

    def touched_tests(self, path: str, head_source: str, changed_lines: set[int] | None) -> set[tuple[str | None, str]] | None:
        if not path.endswith(".rs"):
            return None
        try:
            names = rustsrc.touched_test_functions(head_source, changed_lines)
        except rustsrc.RustLexError:
            return None
        return {(None, n) for n in names}

    def test_regions(self, path: str, source: str) -> list[tuple[int, int]] | None:
        """Line ranges of ``#[cfg(test)]`` modules in a source file (None if it cannot be analysed)."""
        if not is_rust_source(path):
            return []
        try:
            return [(m.start_line, m.end_line) for m in rustsrc.analyze(source).test_modules]
        except rustsrc.RustLexError:
            return None

    def test_belongs_to(self, test_id: str, path: str) -> bool:
        if test_id == path or test_id.startswith(path + "::"):
            return True
        if not is_rust_source(path):
            return False
        pkg, _, name = test_id.partition("::")
        known = self._inline_prefixes.get(path)
        if known is not None:
            kpkg, prefixes = known
            return pkg == kpkg and any(name.startswith(p) for p in prefixes)
        parts = _parts(path)
        k = max(i for i, p in enumerate(parts[:-1]) if p == "src")
        mp = module_path("/".join(parts[k + 1:]))
        return bool(name) and (not mp or name.startswith(mp + "::"))

    # -- inline tests ------------------------------------------------------------
    def inline_test_units(
        self, repo: str | Path, base: str, head: str, changed: Sequence[gitmod.ChangedFile], subdir: str = "."
    ) -> tuple[list["InlineUnit"], list[str]]:
        """Changed ``#[cfg(test)]`` modules, transplanted into the base version of their files.

        ``changed`` holds project-relative paths; ``subdir`` is the project
        directory within the repository. Returns ``(units, notes)``.
        """
        units: list[InlineUnit] = []
        notes: list[str] = []
        for f in changed:
            if f.binary or f.status == "D" or not is_rust_source(f.path) or self.is_test_file(f.path):
                continue
            repo_path = _join(subdir, f.path)
            head_raw = gitmod.show_file(repo, head, repo_path)
            if head_raw is None or b"cfg(" not in head_raw:
                continue
            head_src = head_raw.decode("utf-8", errors="replace")
            if f.status in ("A", "C"):
                try:
                    has_tests = bool(rustsrc.analyze(head_src).test_modules)
                except rustsrc.RustLexError:
                    has_tests = True
                if has_tests:
                    notes.append(f"Inline tests in {repo_path} were not run: the file is new, so they cannot run on the base commit.")
                continue
            old_repo_path = _join(subdir, f.old_path) if f.old_path else repo_path
            base_raw = gitmod.show_file(repo, base, old_repo_path)
            if base_raw is None:
                continue
            diff = gitmod.diff_text(repo, base, head, [old_repo_path, repo_path] if old_repo_path != repo_path else [repo_path])
            lines = added_lines_from_diff(diff)
            try:
                tp = rustsrc.transplant_test_modules(base_raw.decode("utf-8", errors="replace"), head_src, lines)
            except rustsrc.RustLexError as exc:
                notes.append(f"Inline tests in {repo_path} could not be separated from the code ({exc}); "
                             "no fail→pass evidence for them.")
                continue
            if tp is None:
                continue
            units.append(InlineUnit(f.path, tp.text.encode("utf-8"), {(None, n) for n in tp.touched}, tp.modules))
        return units, notes

    # -- execution ---------------------------------------------------------------
    def _target_for(self, workdir: Path, path: str) -> _Target:
        crate_dir = find_crate_dir(workdir, path)
        info = crate_info(workdir, crate_dir) if crate_dir is not None else None
        if info is None:
            return _Target(path, [], [], path, error=f"no Cargo.toml with a [package] section found for {path}")
        rel = path[len(crate_dir) + 1:] if crate_dir else path
        if is_integration_test_module(path) or (self._custom_modules and self.is_test_module(path) and not is_rust_source(path)):
            parts = _parts(rel)
            name = info.test_targets.get(rel)
            if name is None:
                if not info.autotests:
                    return _Target(path, [], [], path, error=f"{path} is not a test target (autotests = false and no [[test]] entry)")
                name = parts[-2] if parts[-1] == "main.rs" and len(parts) >= 3 else PurePosixPath(rel).stem
            return _Target(path, ["-p", info.package, "--test", name], [], f"{path}::")
        # inline unit tests in a source file
        parts = _parts(rel)
        if len(parts) >= 3 and parts[0] == "src" and parts[1] == "bin":
            bin_name = PurePosixPath(parts[2]).stem
            selector = ["--bin", bin_name]
            mp = module_path("/".join(parts[3:])) if len(parts) > 3 else ""
        else:
            if not parts or parts[0] != "src":
                return _Target(path, [], [], path, error=f"{path} is not under the crate's src/ directory")
            if info.has_lib:
                selector = ["--lib"]
            elif info.bins:
                selector = ["--bins"]
            else:
                return _Target(path, [], [], path, error=f"crate {info.package} has no lib or bin target")
            mp = module_path("/".join(parts[1:]))
        try:
            src = (workdir / path).read_text(encoding="utf-8", errors="replace")
            mods = [m.qualname for m in rustsrc.analyze(src).test_modules]
        except (OSError, rustsrc.RustLexError):
            mods = []
        if mods:
            prefixes = [f"{mp}::{m}::" if mp else f"{m}::" for m in mods]
        else:
            prefixes = [f"{mp}::" if mp else "tests::"]
        self._inline_prefixes[path] = (info.package, prefixes)
        return _Target(path, ["-p", info.package, *selector], list(prefixes), f"{info.package}::", name_prefixes=prefixes)

    def env_for(self, workdir: Path, junit_path: str) -> dict[str, str]:
        return {"CARGO_TERM_COLOR": "never", "CARGO_TERM_PROGRESS_WHEN": "never", "RUST_BACKTRACE": "0"}

    def _env(self, env: Mapping[str, str]) -> dict[str, str]:
        full = dict(env)
        full.update(self.env_for(Path("."), ""))
        full["CARGO_TARGET_DIR"] = self.target_dir or env.get("CARGO_TARGET_DIR") or default_target_dir()
        full.update(self.extra_env)
        return full

    def run(self, workdir: Path, test_files: Sequence[str], env: Mapping[str, str], timeout: float) -> RunResult:
        return self._run(workdir, test_files, None, env, timeout)

    def run_selected(self, workdir: Path, test_files: Sequence[str], test_ids: Sequence[str], env: Mapping[str, str],
                     timeout: float) -> RunResult:
        """Run only ``test_ids`` (exact libtest names) from ``test_files``."""
        return self._run(workdir, test_files, list(test_ids), env, timeout)

    def _run(self, workdir: Path, test_files: Sequence[str], only: list[str] | None, env: Mapping[str, str],
             timeout: float) -> RunResult:
        workdir = Path(workdir)
        out = RunResult()
        full_env = self._env(env)
        Path(full_env["CARGO_TARGET_DIR"]).mkdir(parents=True, exist_ok=True)
        ensure_fresh(workdir, full_env["CARGO_TARGET_DIR"])
        deadline = time.monotonic() + timeout
        tails: list[str] = []
        errors: list[str] = []
        started = time.monotonic()
        for path in test_files:
            target = self._target_for(workdir, path)
            if target.error:
                out.outcomes[path] = TestOutcome(path, ERROR, f"does not build: {target.error}", path, "", "build")
                continue
            filters = list(target.filters)
            exact = False
            if only is not None:
                names = [tid[len(target.id_prefix):] for tid in only
                         if tid.startswith(target.id_prefix) and self.test_belongs_to(tid, path)]
                if not names:
                    continue
                filters, exact = names, True
            left = deadline - time.monotonic()
            if left <= 1:
                out.timed_out = True
                errors.append("time budget exhausted before all test targets ran")
                break
            cmd = [*self.command, *target.args, "--no-fail-fast", "--", "--test-threads=1"]
            if exact:
                cmd.append("--exact")
            cmd += filters
            res = run_command(cmd, cwd=workdir, env=full_env, timeout=left)
            out.command = out.command + (["&&"] if out.command else []) + cmd
            if res.returncode or out.returncode is None:
                out.returncode = res.returncode
            tails.append(res.stdout[-3000:] + ("\n" if res.stdout and res.stderr else "") + res.stderr[-3000:])
            if res.launch_error:
                out.error = f"could not start test command: {res.launch_error}"
                break
            parsed = parse_libtest(res.stdout)
            if res.timed_out:
                out.timed_out = True
                errors.append(f"cargo test timed out for {path}")
            if not parsed.started and not res.timed_out and res.returncode not in (0, None):
                msg = first_compile_error(res.stderr)
                out.outcomes[path] = TestOutcome(path, ERROR, f"does not compile: {msg}", path, _tail(res.stderr, 2000), "build")
                continue
            for name, (outcome, message, detail) in parsed.results.items():
                if target.name_prefixes and not any(name.startswith(p) for p in target.name_prefixes):
                    continue
                tid = target.id_prefix + name
                out.outcomes[tid] = TestOutcome(tid, outcome, message, path, detail)
            if parsed.crashed and (not target.name_prefixes or any(parsed.crashed.startswith(p) for p in target.name_prefixes)):
                tid = target.id_prefix + parsed.crashed
                out.outcomes[tid] = TestOutcome(tid, FAILED, "the test process crashed: " + _last_line(res.stderr), path,
                                                _tail(res.stderr, 2000))
        out.duration = time.monotonic() - started
        out.output_tail = _tail("\n".join(tails))
        if out.error is None and out.timed_out and not out.outcomes:
            out.error = "; ".join(errors) or "test command timed out"
        elif out.error is None and out.timed_out:
            out.error = "test command timed out"
        return out


def _last_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()[:300]
    return "no output"


def _join(subdir: str, path: str) -> str:
    return path if subdir in ("", ".") else f"{subdir.rstrip('/')}/{path}"


@dataclass
class InlineUnit:
    """A changed source file whose ``#[cfg(test)]`` module(s) were transplanted for the base run."""

    path: str  # project-relative path of the source file
    base_text: bytes  # new content of the file in the base worktree
    touched: set[tuple[str | None, str]]
    modules: list[str]


def cargo_available(env: Mapping[str, str] | None = None) -> bool:
    import shutil

    return shutil.which("cargo", path=(env or os.environ).get("PATH")) is not None
