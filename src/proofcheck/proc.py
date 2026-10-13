"""Subprocess helpers: argument-list execution, timeouts, and environment scrubbing.

Everything proofcheck executes on behalf of a pull request (tests, issue
reproduction snippets, benchmarks) is untrusted code. These helpers make sure
that code:

* is never interpreted by a shell (argument lists only, ``shell=False``);
* is killed together with its whole process group when it times out;
* does not inherit secrets from proofcheck's own environment.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Mapping, Sequence

#: Substrings that mark an environment variable as sensitive. Matching is
#: case-insensitive and applies to the variable *name*.
SENSITIVE_MARKERS: tuple[str, ...] = ("TOKEN", "SECRET", "KEY", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH")

#: Maximum number of bytes of stdout/stderr kept per stream.
MAX_OUTPUT_BYTES = 1_000_000


def default_python() -> str:
    """The interpreter used for tests and reproducers when none is configured.

    ``$PROOFCHECK_PYTHON`` if set, else ``python``/``python3`` from ``PATH``
    (the project's environment, where its dependencies are installed), else
    the interpreter running proofcheck.
    """
    return os.environ.get("PROOFCHECK_PYTHON") or shutil.which("python") or shutil.which("python3") or sys.executable


def is_sensitive_env_name(name: str) -> bool:
    """Return True if an environment variable name looks like it holds a secret."""
    upper = name.upper()
    return any(marker in upper for marker in SENSITIVE_MARKERS)


def scrub_env(environ: Mapping[str, str] | None = None, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy of ``environ`` without secret-looking variables.

    Args:
        environ: Source environment (defaults to ``os.environ``).
        extra: Variables to add after scrubbing (these are trusted, e.g. PYTHONPATH).
    """
    source = os.environ if environ is None else environ
    clean = {k: v for k, v in source.items() if not is_sensitive_env_name(k)}
    if extra:
        clean.update(extra)
    return clean


@dataclass
class CommandResult:
    """Outcome of a finished (or killed) subprocess."""

    args: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False
    launch_error: str | None = None

    @property
    def ok(self) -> bool:
        """True when the process ran to completion with exit code 0."""
        return not self.timed_out and self.launch_error is None and self.returncode == 0


def _decode(data: bytes | None) -> str:
    if not data:
        return ""
    return data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")


def _kill_group(proc: subprocess.Popen, quiet: bool = False) -> None:
    """SIGKILL the process group started for ``proc`` (POSIX) or the process itself."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        elif not quiet:  # pragma: no cover - windows
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        if quiet:
            return
        try:
            proc.kill()
        except OSError:
            pass


def run_command(
    args: Sequence[str],
    cwd: str | os.PathLike[str],
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    stdin_data: bytes | None = None,
) -> CommandResult:
    """Run ``args`` (never through a shell) and capture its output.

    On timeout the whole process group is killed so that grandchildren
    (e.g. test workers) do not linger.
    """
    argv = [str(a) for a in args]
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=dict(env) if env is not None else None,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=(os.name == "posix"),
        )
    except (OSError, ValueError) as exc:
        return CommandResult(argv, None, "", "", time.monotonic() - start, launch_error=str(exc))
    try:
        out, err = proc.communicate(input=stdin_data, timeout=timeout)
        # Reap anything the command left running in the background (its
        # process group), so untrusted code cannot outlive its run.
        _kill_group(proc, quiet=True)
        return CommandResult(argv, proc.returncode, _decode(out), _decode(err), time.monotonic() - start)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - extremely unlikely
            out, err = b"", b""
        return CommandResult(argv, proc.returncode, _decode(out), _decode(err), time.monotonic() - start, timed_out=True)
