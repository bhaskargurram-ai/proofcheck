"""Validation of the agent's files: the candidates manifest and the ``agent``
section of ``result.json``.

``result.json`` travels from the unprivileged verify job to the privileged
comment workflow, so the ``agent`` section is validated as strictly as the
rest of the document (types, enumerations, lengths; unknown keys dropped).
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

from . import AGENT_STATUSES, CANDIDATE_CLASSES, MANIFEST_SCHEMA, MANIFEST_VERSION, VARIANTS
from .config import AgentConfigError, validate_test_dir

MAX_MANIFEST_BYTES = 2_000_000
MAX_CANDIDATE_BYTES = 200_000
MAX_CODE_IN_RESULT = 12_000


class ManifestError(ValueError):
    pass


def _r():  # lazy import: proofcheck.result imports this module lazily too
    from .. import result as R

    return R


def validate_candidate_path(path: Any, test_dir: str) -> str:
    """A candidate file must be ``<test_dir>/test_*.py`` with a safe relative path."""
    if not isinstance(path, str) or not path:
        raise ManifestError("candidate file must be a non-empty string")
    p = PurePosixPath(path)
    if path.startswith("/") or "\\" in path or any(part in ("", ".", "..") for part in path.split("/")):
        raise ManifestError(f"unsafe candidate path {path[:100]!r}")
    if str(p.parent) != test_dir or not p.name.startswith("test_") or p.suffix != ".py" or len(path) > 300:
        raise ManifestError(f"candidate path must be {test_dir}/test_*.py, not {path[:100]!r}")
    if not all(c.isalnum() or c in "_." for c in p.name):
        raise ManifestError(f"candidate file name has unexpected characters: {p.name[:60]!r}")
    return path


def _usage(v: Any, path: str) -> dict[str, int]:
    R = _r()
    d = R._dict(v, path, optional=True) or {}
    keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "requests")
    return {k: R._int(d.get(k, 0), f"{path}.{k}", minimum=0) for k in keys}


def load_manifest(candidates_dir: str | Path) -> dict[str, Any]:
    """Load and validate ``manifest.json`` from a candidates directory."""
    R = _r()
    p = Path(candidates_dir) / "manifest.json"
    if not p.is_file():
        raise ManifestError(f"{p} not found")
    if p.stat().st_size > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest.json is too large")
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ManifestError(f"manifest.json is not valid JSON: {exc}") from exc
    try:
        d = R._dict(d, "$")
        if d.get("schema") != MANIFEST_SCHEMA or d.get("version") != MANIFEST_VERSION:
            raise ManifestError("not a proofcheck agent manifest (schema/version mismatch)")
        try:
            test_dir = validate_test_dir(d.get("test_dir"))
        except AgentConfigError as exc:
            raise ManifestError(str(exc)) from exc
        out = {
            "schema": MANIFEST_SCHEMA, "version": MANIFEST_VERSION,
            "status": R._str(d.get("status", ""), "$.status", 40),
            "reason": R._str(d.get("reason", ""), "$.reason", 500),
            "base": R._sha(d.get("base"), "$.base"), "head": R._sha(d.get("head"), "$.head"),
            "merge_base": R._sha(d.get("merge_base"), "$.merge_base", optional=True),
            "provider": R._str(d.get("provider", ""), "$.provider", 40),
            "model": R._str(d.get("model", ""), "$.model", 120),
            "style": R._str(d.get("style", ""), "$.style", 20),
            "test_dir": test_dir,
            "target_claim": R._str(d.get("target_claim", ""), "$.target_claim", 200),
            "target_issue": R._int(d.get("target_issue"), "$.target_issue", optional=True, minimum=1),
            "variants": [v for v in R._list(d.get("variants"), "$.variants", lambda x, q: R._str(x, q, 40), 10) if v in VARIANTS],
            "usage": _usage(d.get("usage"), "$.usage"),
            "cost_usd": R._num(d.get("cost_usd"), "$.cost_usd", optional=True),
            "candidates": R._list(d.get("candidates"), "$.candidates", lambda x, q: _manifest_candidate(x, q, test_dir), 10),
        }
    except R.ResultValidationError as exc:
        raise ManifestError(f"invalid manifest: {exc}") from exc
    return out


def _manifest_candidate(v: Any, path: str, test_dir: str) -> dict[str, Any]:
    R = _r()
    d = R._dict(v, path)
    variant = R._enum(d.get("variant"), VARIANTS, f"{path}.variant")
    status = R._enum(d.get("status"), ("submitted", "no_submission", "invalid", "error"), f"{path}.status")
    file = d.get("file") or ""
    if status == "submitted":
        file = validate_candidate_path(file, test_dir)
    else:
        file = ""
    return {
        "variant": variant, "status": status, "file": file,
        "round": R._int(d.get("round", 0), f"{path}.round", minimum=0),
        "rationale": R._str(d.get("rationale", ""), f"{path}.rationale", 2000),
        "targets_claim": R._str(d.get("targets_claim", ""), f"{path}.targets_claim", 200),
        "tool_calls": R._int(d.get("tool_calls", 0), f"{path}.tool_calls", minimum=0),
        "repairs": R._list(d.get("repairs"), f"{path}.repairs", lambda x, q: R._str(x, q, 300), 20),
        "errors": R._list(d.get("errors"), f"{path}.errors", lambda x, q: R._str(x, q, 300), 20),
        "error": R._str(d.get("error", ""), f"{path}.error", 500),
    }


def read_candidate(candidates_dir: str | Path, rel: str) -> str:
    """Read a candidate file, refusing anything outside the directory or too large."""
    root = Path(candidates_dir).resolve()
    target = (Path(candidates_dir) / rel).resolve()
    if root not in target.parents:
        raise ManifestError(f"candidate path escapes the candidates directory: {rel[:100]!r}")
    if target.is_symlink() or not target.is_file():
        raise ManifestError(f"candidate file missing: {rel[:100]!r}")
    if target.stat().st_size > MAX_CANDIDATE_BYTES:
        raise ManifestError(f"candidate file too large: {rel[:100]!r}")
    return target.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- result.json


def _agent_test(v: Any, path: str) -> dict[str, Any]:
    R = _r()
    d = R._dict(v, path)
    return {
        "test_id": R._str(d.get("test_id", ""), f"{path}.test_id", 300),
        "base": R._str(d.get("base", ""), f"{path}.base", 20),
        "head": R._str(d.get("head", ""), f"{path}.head", 20),
        "base_kind": R._str(d.get("base_kind", ""), f"{path}.base_kind", 60),
        "message": R._str(d.get("message", ""), f"{path}.message", 500),
    }


def _agent_candidate(v: Any, path: str) -> dict[str, Any]:
    R = _r()
    d = R._dict(v, path)
    return {
        "variant": R._enum(d.get("variant"), VARIANTS, f"{path}.variant"),
        "round": R._int(d.get("round", 0), f"{path}.round", minimum=0),
        "file": R._str(d.get("file", ""), f"{path}.file", 300),
        "classification": R._enum(d.get("classification"), CANDIDATE_CLASSES, f"{path}.classification"),
        "message": R._str(d.get("message", ""), f"{path}.message", 500),
        "rationale": R._str(d.get("rationale", ""), f"{path}.rationale", 1000),
        "tests": R._list(d.get("tests"), f"{path}.tests", _agent_test, 20),
    }


def _winner(v: Any, path: str) -> dict[str, Any] | None:
    R = _r()
    d = R._dict(v, path, optional=True)
    if d is None:
        return None
    return {
        "variant": R._enum(d.get("variant"), VARIANTS, f"{path}.variant"),
        "file": R._str(d.get("file", ""), f"{path}.file", 300),
        "classification": R._enum(d.get("classification"), CANDIDATE_CLASSES, f"{path}.classification"),
        "code": R._str(d.get("code", ""), f"{path}.code", MAX_CODE_IN_RESULT),
        "rationale": R._str(d.get("rationale", ""), f"{path}.rationale", 1000),
    }


def validate_agent_section(v: Any, path: str = "$.agent") -> dict[str, Any] | None:
    """Validate the ``agent`` key of result.json (``None`` when absent)."""
    R = _r()
    d = R._dict(v, path, optional=True)
    if d is None:
        return None
    return {
        "status": R._enum(d.get("status"), AGENT_STATUSES, f"{path}.status"),
        "reason": R._str(d.get("reason", ""), f"{path}.reason", 500),
        "provider": R._str(d.get("provider", ""), f"{path}.provider", 40),
        "model": R._str(d.get("model", ""), f"{path}.model", 120),
        "target_claim": R._str(d.get("target_claim", ""), f"{path}.target_claim", 200),
        "variants": R._list(d.get("variants"), f"{path}.variants", lambda x, q: R._enum(x, VARIANTS, q), 10),
        "rounds": R._int(d.get("rounds", 1), f"{path}.rounds", minimum=0),
        "usage": _usage(d.get("usage"), f"{path}.usage"),
        "cost_usd": R._num(d.get("cost_usd"), f"{path}.cost_usd", optional=True),
        "candidates": R._list(d.get("candidates"), f"{path}.candidates", _agent_candidate, 20),
        "winner": _winner(d.get("winner"), f"{path}.winner"),
        "upgraded_claims": R._list(d.get("upgraded_claims"), f"{path}.upgraded_claims",
                                   lambda x, q: R._int(x, q, minimum=0), 50),
        "notes": R._list(d.get("notes"), f"{path}.notes", lambda x, q: R._str(x, q, 500), 10),
    }
