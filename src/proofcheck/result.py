"""The JSON result document and its strict validator.

``result.json`` is produced in the *unprivileged* workflow, where untrusted
PR code also runs, and is consumed by the *privileged* comment workflow. PR
code could have rewritten the file, so the comment side treats it as hostile
input: :func:`validate_result` checks every field's type, enumerations and
length, drops unknown keys, and the card is re-rendered from the validated
data with escaping (the pre-rendered ``card.md`` is never posted).
"""

from __future__ import annotations

import re
from typing import Any, Callable

from . import __version__
from .claims import CLAIM_TYPES

SCHEMA_ID = "proofcheck/result"
SCHEMA_VERSION = 1

VERDICTS = ("evidence", "partial", "no_evidence", "contradicted", "error")
CLAIM_STATUSES = ("verified", "unverified", "contradicted")
TEST_STATUSES = ("evidence", "no_evidence", "fail_on_head", "no_tests", "error", "skipped")
TEST_CLASSES = ("fail_to_pass", "pass_to_pass", "fail_on_head", "error", "skipped")
REPRO_STATUSES = ("reproduced_change", "unchanged", "no_snippets", "error", "not_found")
SNIPPET_STATUSES = ("behavior_changed", "unchanged", "error")
FLAG_KINDS = ("workflow", "dependency", "binary", "large_diff", "unmentioned", "unexercised")
ABLATION_STATUSES = ("ran", "not_applicable", "no_hunks", "baseline_failed", "error")
HUNK_STATUSES = ("required", "not_exercised", "skipped")
HUNK_KINDS = ("modify", "add", "delete", "rename")

MAX_STR = 2000
MAX_ITEMS = 200
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ResultValidationError(ValueError):
    """Raised when a result document does not match the schema."""


def _err(path: str, msg: str) -> ResultValidationError:
    return ResultValidationError(f"{path}: {msg}")


def _str(v: Any, path: str, max_len: int = MAX_STR, optional: bool = False) -> str | None:
    if v is None and optional:
        return None
    if not isinstance(v, str):
        raise _err(path, "expected string")
    return v[:max_len]


def _int(v: Any, path: str, optional: bool = False, minimum: int | None = None) -> int | None:
    if v is None and optional:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise _err(path, "expected integer")
    if minimum is not None and v < minimum:
        raise _err(path, f"must be >= {minimum}")
    return v


def _num(v: Any, path: str, optional: bool = False) -> float | None:
    if v is None and optional:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _err(path, "expected number")
    if v != v or v in (float("inf"), float("-inf")):
        raise _err(path, "expected finite number")
    return float(v)


def _bool(v: Any, path: str) -> bool:
    if not isinstance(v, bool):
        raise _err(path, "expected boolean")
    return v


def _enum(v: Any, allowed: tuple[str, ...], path: str) -> str:
    if v not in allowed:
        raise _err(path, f"must be one of {', '.join(allowed)}")
    return v


def _dict(v: Any, path: str, optional: bool = False) -> dict | None:
    if v is None and optional:
        return None
    if not isinstance(v, dict):
        raise _err(path, "expected object")
    return v


def _list(v: Any, path: str, item: Callable[[Any, str], Any], max_items: int = MAX_ITEMS) -> list:
    if v is None:
        return []
    if not isinstance(v, list):
        raise _err(path, "expected array")
    return [item(x, f"{path}[{i}]") for i, x in enumerate(v[:max_items])]


def _sha(v: Any, path: str, optional: bool = False) -> str | None:
    s = _str(v, path, 64, optional)
    if s is None:
        return None
    if not _SHA_RE.match(s):
        raise _err(path, "expected a hex commit SHA")
    return s


def _details(v: Any, path: str) -> dict[str, Any]:
    d = _dict(v, path, optional=True) or {}
    out: dict[str, Any] = {}
    for k, val in list(d.items())[:20]:
        if not isinstance(k, str) or len(k) > 40:
            continue
        if isinstance(val, bool) or val is None:
            out[k] = val
        elif isinstance(val, (int, float)):
            out[k] = _num(val, f"{path}.{k}")
        elif isinstance(val, str):
            out[k] = val[:300]
    return out


def _claim(v: Any, path: str) -> dict[str, Any]:
    d = _dict(v, path)
    return {
        "type": _enum(d.get("type"), CLAIM_TYPES, f"{path}.type"),
        "text": _str(d.get("text", ""), f"{path}.text", 300),
        "source": _str(d.get("source", ""), f"{path}.source", 1000),
        "origin": _enum(d.get("origin", "rules"), ("rules", "llm"), f"{path}.origin"),
        "details": _details(d.get("details"), f"{path}.details"),
        "status": _enum(d.get("status"), CLAIM_STATUSES, f"{path}.status"),
        "evidence": _str(d.get("evidence", ""), f"{path}.evidence", 1000),
    }


def _claim_with_source(v: Any, path: str) -> dict[str, Any]:
    """A claim, plus the optional ``verified_by`` marker set when agent evidence verified it."""
    out = _claim(v, path)
    if isinstance(v, dict) and v.get("verified_by") is not None:
        out["verified_by"] = _enum(v.get("verified_by"), ("agent",), f"{path}.verified_by")
    return out


def _agent(v: Any, path: str) -> dict[str, Any] | None:
    """The optional ``agent`` section (agent-written reproduction tests)."""
    from .agent.schema import validate_agent_section

    return validate_agent_section(v, path)


def _test(v: Any, path: str) -> dict[str, Any]:
    d = _dict(v, path)
    return {
        "test_id": _str(d.get("test_id"), f"{path}.test_id", 500),
        "file": _str(d.get("file", ""), f"{path}.file", 500),
        "classification": _enum(d.get("classification"), TEST_CLASSES, f"{path}.classification"),
        "base": _str(d.get("base", ""), f"{path}.base", 20),
        "head": _str(d.get("head", ""), f"{path}.head", 20),
        "touched": _bool(d.get("touched", True), f"{path}.touched"),
        "message": _str(d.get("message", ""), f"{path}.message", 500),
    }


def _run(v: Any, path: str) -> dict[str, Any]:
    d = _dict(v, path, optional=True) or {}
    if not d:
        return {}
    return {
        "command": _list(d.get("command"), f"{path}.command", lambda x, p: _str(x, p, 500), 100),
        "returncode": _int(d.get("returncode"), f"{path}.returncode", optional=True),
        "timed_out": _bool(d.get("timed_out", False), f"{path}.timed_out"),
        "error": _str(d.get("error"), f"{path}.error", 1000, optional=True),
        "duration": _num(d.get("duration", 0), f"{path}.duration"),
        "output_tail": _str(d.get("output_tail", ""), f"{path}.output_tail", 4000),
    }


def _tests(v: Any, path: str) -> dict[str, Any] | None:
    d = _dict(v, path, optional=True)
    if d is None:
        return None
    counts_in = _dict(d.get("counts", {}), f"{path}.counts") or {}
    counts = {k: _int(counts_in.get(k, 0), f"{path}.counts.{k}", minimum=0) for k in TEST_CLASSES}
    old = _dict(d.get("old_tests_on_head", {}), f"{path}.old_tests_on_head") or {}
    old_out: dict[str, Any] = {}
    if old:
        old_out = {
            "ran": _bool(old.get("ran", False), f"{path}.old_tests_on_head.ran"),
            "files": _list(old.get("files"), f"{path}.old_tests_on_head.files", lambda x, p: _str(x, p, 500)),
            "passed": _int(old.get("passed", 0), f"{path}.old_tests_on_head.passed", minimum=0),
            "failing": _list(old.get("failing"), f"{path}.old_tests_on_head.failing", lambda x, p: _str(x, p, 500), 50),
            "error": _str(old.get("error"), f"{path}.old_tests_on_head.error", optional=True),
        }
    return {
        "status": _enum(d.get("status"), TEST_STATUSES, f"{path}.status"),
        "runner": _str(d.get("runner", ""), f"{path}.runner", 40),
        "test_files": _list(d.get("test_files"), f"{path}.test_files", lambda x, p: _str(x, p, 500)),
        "test_modules": _list(d.get("test_modules"), f"{path}.test_modules", lambda x, p: _str(x, p, 500)),
        "counts": counts,
        "tests": _list(d.get("tests"), f"{path}.tests", _test, 500),
        "notes": _list(d.get("notes"), f"{path}.notes", lambda x, p: _str(x, p, 1000), 20),
        "base_run": _run(d.get("base_run"), f"{path}.base_run"),
        "head_run": _run(d.get("head_run"), f"{path}.head_run"),
        "old_tests_on_head": old_out,
    }


def _snippet_run(v: Any, path: str) -> dict[str, Any]:
    d = _dict(v, path)
    return {
        "exit_code": _int(d.get("exit_code"), f"{path}.exit_code", optional=True),
        "timed_out": _bool(d.get("timed_out", False), f"{path}.timed_out"),
        "error": _str(d.get("error"), f"{path}.error", 500, optional=True),
        "stdout_tail": _str(d.get("stdout_tail", ""), f"{path}.stdout_tail", 1500),
        "stderr_tail": _str(d.get("stderr_tail", ""), f"{path}.stderr_tail", 1500),
        "last_line": _str(d.get("last_line", ""), f"{path}.last_line", 300),
    }


def _snippet(v: Any, path: str) -> dict[str, Any]:
    d = _dict(v, path)
    return {
        "index": _int(d.get("index"), f"{path}.index", minimum=0),
        "status": _enum(d.get("status"), SNIPPET_STATUSES, f"{path}.status"),
        "code_preview": _str(d.get("code_preview", ""), f"{path}.code_preview", 200),
        "base": _snippet_run(d.get("base"), f"{path}.base"),
        "head": _snippet_run(d.get("head"), f"{path}.head"),
    }


def _repro(v: Any, path: str) -> dict[str, Any]:
    d = _dict(v, path)
    return {
        "issue": _str(d.get("issue"), f"{path}.issue", 120),
        "status": _enum(d.get("status"), REPRO_STATUSES, f"{path}.status"),
        "note": _str(d.get("note", ""), f"{path}.note", 500),
        "snippets": _list(d.get("snippets"), f"{path}.snippets", _snippet, 10),
    }


def _bench(v: Any, path: str) -> dict[str, Any] | None:
    d = _dict(v, path, optional=True)
    if d is None:
        return None
    times = lambda x, p: _num(x, p)  # noqa: E731
    return {
        "command": _list(d.get("command"), f"{path}.command", lambda x, p: _str(x, p, 500), 100),
        "runs": _int(d.get("runs", 0), f"{path}.runs", minimum=0),
        "base_times": _list(d.get("base_times"), f"{path}.base_times", times, 1000),
        "head_times": _list(d.get("head_times"), f"{path}.head_times", times, 1000),
        "base_median": _num(d.get("base_median"), f"{path}.base_median", optional=True),
        "head_median": _num(d.get("head_median"), f"{path}.head_median", optional=True),
        "speedup": _num(d.get("speedup"), f"{path}.speedup", optional=True),
        "error": _str(d.get("error"), f"{path}.error", 500, optional=True),
    }


def _file(v: Any, path: str) -> dict[str, Any]:
    d = _dict(v, path)
    return {
        "path": _str(d.get("path"), f"{path}.path", 500),
        "status": _str(d.get("status", "M"), f"{path}.status", 2),
        "old_path": _str(d.get("old_path"), f"{path}.old_path", 500, optional=True),
        "additions": _int(d.get("additions", 0), f"{path}.additions", minimum=0),
        "deletions": _int(d.get("deletions", 0), f"{path}.deletions", minimum=0),
        "binary": _bool(d.get("binary", False), f"{path}.binary"),
    }


def _flag(v: Any, path: str) -> dict[str, Any]:
    d = _dict(v, path)
    return {
        "kind": _enum(d.get("kind"), FLAG_KINDS, f"{path}.kind"),
        "message": _str(d.get("message", ""), f"{path}.message", 500),
        "paths": _list(d.get("paths"), f"{path}.paths", lambda x, p: _str(x, p, 500)),
        "heuristic": _bool(d.get("heuristic", False), f"{path}.heuristic"),
    }


def _scope(v: Any, path: str) -> dict[str, Any] | None:
    d = _dict(v, path, optional=True)
    if d is None:
        return None
    files = _list(d.get("files"), f"{path}.files", _file, 3000)
    return {
        "files": files,
        "file_count": _int(d.get("file_count", len(files)), f"{path}.file_count", minimum=0),
        "total_additions": _int(d.get("total_additions", 0), f"{path}.total_additions", minimum=0),
        "total_deletions": _int(d.get("total_deletions", 0), f"{path}.total_deletions", minimum=0),
        "flags": _list(d.get("flags"), f"{path}.flags", _flag, 20),
    }


def _hunk(v: Any, path: str) -> dict[str, Any]:
    d = _dict(v, path)
    return {
        "file": _str(d.get("file"), f"{path}.file", 500),
        "old_path": _str(d.get("old_path"), f"{path}.old_path", 500, optional=True),
        "kind": _enum(d.get("kind", "modify"), HUNK_KINDS, f"{path}.kind"),
        "location": _str(d.get("location", ""), f"{path}.location", 1100),
        "start": _int(d.get("start", 0), f"{path}.start", minimum=0),
        "end": _int(d.get("end", 0), f"{path}.end", minimum=0),
        "added": _int(d.get("added", 0), f"{path}.added", minimum=0),
        "removed": _int(d.get("removed", 0), f"{path}.removed", minimum=0),
        "status": _enum(d.get("status"), HUNK_STATUSES, f"{path}.status"),
        "broken_by": _list(d.get("broken_by"), f"{path}.broken_by", lambda x, p: _str(x, p, 500), 10),
        "reason": _str(d.get("reason", ""), f"{path}.reason", 500),
        "duration": _num(d.get("duration", 0), f"{path}.duration"),
    }


def _ablation(v: Any, path: str) -> dict[str, Any] | None:
    d = _dict(v, path, optional=True)
    if d is None:
        return None
    count = lambda k: _int(d.get(k, 0), f"{path}.{k}", minimum=0)  # noqa: E731
    return {
        "status": _enum(d.get("status"), ABLATION_STATUSES, f"{path}.status"),
        "evidence": _list(d.get("evidence"), f"{path}.evidence", lambda x, p: _str(x, p, 500), 60),
        "hunks": _list(d.get("hunks"), f"{path}.hunks", _hunk, 100),
        "total_hunks": count("total_hunks"),
        "tested": count("tested"),
        "required": count("required"),
        "not_exercised": count("not_exercised"),
        "skipped": count("skipped"),
        "untested_over_cap": count("untested_over_cap"),
        "budget_s": _num(d.get("budget_s", 0), f"{path}.budget_s"),
        "elapsed_s": _num(d.get("elapsed_s", 0), f"{path}.elapsed_s"),
        "budget_exhausted": _bool(d.get("budget_exhausted", False), f"{path}.budget_exhausted"),
        "notes": _list(d.get("notes"), f"{path}.notes", lambda x, p: _str(x, p, 500), 10),
    }


def validate_result(data: Any) -> dict[str, Any]:
    """Validate a result document and return a sanitized copy.

    Raises :class:`ResultValidationError` on any schema violation.
    """
    d = _dict(data, "$")
    if d.get("schema") != SCHEMA_ID:
        raise _err("$.schema", f"expected {SCHEMA_ID!r}")
    if d.get("version") != SCHEMA_VERSION:
        raise _err("$.version", f"unsupported version (expected {SCHEMA_VERSION})")
    repo = _str(d.get("repo"), "$.repo", 200, optional=True)
    if repo is not None and not _SLUG_RE.match(repo):
        raise _err("$.repo", "invalid owner/repo")
    return {
        "schema": SCHEMA_ID,
        "version": SCHEMA_VERSION,
        "tool_version": _str(d.get("tool_version", ""), "$.tool_version", 40),
        "repo": repo,
        "pr_number": _int(d.get("pr_number"), "$.pr_number", optional=True, minimum=1),
        "base": _sha(d.get("base"), "$.base"),
        "head": _sha(d.get("head"), "$.head"),
        "merge_base": _sha(d.get("merge_base"), "$.merge_base", optional=True),
        "verdict": _enum(d.get("verdict"), VERDICTS, "$.verdict"),
        "claims": _list(d.get("claims"), "$.claims", _claim_with_source, 50),
        "tests": _tests(d.get("tests"), "$.tests"),
        "agent": _agent(d.get("agent"), "$.agent"),
        "repro": _list(d.get("repro"), "$.repro", _repro, 10),
        "benchmark": _bench(d.get("benchmark"), "$.benchmark"),
        "scope": _scope(d.get("scope"), "$.scope"),
        "notes": _list(d.get("notes"), "$.notes", lambda x, p: _str(x, p, 1000), 20),
        "config_source": _str(d.get("config_source", ""), "$.config_source", 200),
        "ablation": _ablation(d.get("ablation"), "$.ablation"),
    }


def new_result(**fields: Any) -> dict[str, Any]:
    """Create a result document with the schema header filled in."""
    doc: dict[str, Any] = {"schema": SCHEMA_ID, "version": SCHEMA_VERSION, "tool_version": __version__}
    doc.update(fields)
    return doc
