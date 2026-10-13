"""``proofcheck agent verify``: run candidate tests on base and head (no LLM).

Each candidate file is written into a worktree of the merge base and one of
the head commit and run with the project's normal test runner (the same
runners and JUnit parsing as the fail-to-pass check). Classification:

``fail_to_pass``
    every test passes on head and at least one fails on base with a
    *behavioural* failure: an assertion, or an exception other than a
    missing-symbol error. **Strong evidence.**
``fail_to_pass_weak``
    passes on head, but on base it only fails because a name does not exist
    yet (``ImportError``/``ModuleNotFoundError``, ``AttributeError``,
    ``NameError``, a ``TypeError`` about unexpected/missing arguments, or a
    module that fails to import). Shows the API is new, not that a bug is fixed.
``pass_to_pass``
    passes on both commits: no evidence.
``fails_on_head``
    some test fails on head. Agent tests can be wrong, so this is
    **inconclusive** and never counted against the PR.
``error``
    the runner produced no usable report.

Agent evidence never contradicts a claim. A strong candidate upgrades a
matching unverified ``fixes #N`` claim to ``verified`` (marked
``verified_by: agent``); nothing else about the result changes.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import git as gitmod
from ..failtopass import FAIL_ON_HEAD as FT_FAIL_ON_HEAD
from ..failtopass import FAIL_TO_PASS as FT_FAIL_TO_PASS
from ..failtopass import PASS_TO_PASS as FT_PASS_TO_PASS
from ..failtopass import classify_runs, is_collection_error_id
from ..junit import ERROR as J_ERROR
from ..junit import FAILED as J_FAILED
from ..junit import TestOutcome
from ..proc import run_command
from ..runners import RunResult, TestRunner
from . import (
    AGENT_EVIDENCE_TEXT,
    ERROR,
    FAIL_TO_PASS,
    FAIL_TO_PASS_WEAK,
    FAILS_ON_HEAD,
    INVALID,
    NO_SUBMISSION,
    PASS_TO_PASS,
    STATUS_ERROR,
    STATUS_EVIDENCE,
    STATUS_INCONCLUSIVE,
    STATUS_NO_EVIDENCE,
    STATUS_SKIPPED,
    STATUS_WEAK,
)
from .schema import MAX_CODE_IN_RESULT, ManifestError, read_candidate

#: Exceptions that mean "this name does not exist on base" rather than "the bug".
MISSING_SYMBOL = {"ImportError", "ModuleNotFoundError", "AttributeError", "NameError", "UnboundLocalError"}
_SIGNATURE_RE = re.compile(r"unexpected keyword argument|positional argument|required (?:keyword-only |positional )?argument|takes no arguments")
_EXC_PREFIX_RE = re.compile(r"^\s*(?:E\s+)?([A-Za-z_][\w.]*?(?:Error|Exception|Exit|Interrupt|Warning|Failure))\b(?::|$)")
_E_LINE_RE = re.compile(r"^E\s+([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning))\b", re.M)
_TRAILER_RE = re.compile(r":\d+: ([A-Za-z_][\w.]*)\s*$")

STRONG = "behavioural"
WEAK = "missing_symbol"
UNKNOWN = "unknown"


def exception_name(o: TestOutcome | None) -> str:
    """Best-effort exception class name of a failure ('' if unknown)."""
    if o is None:
        return ""
    msg = (o.message or "").strip()
    first = msg.splitlines()[0] if msg else ""
    m = _EXC_PREFIX_RE.match(first)
    if m:
        return m.group(1).rsplit(".", 1)[-1]
    detail = getattr(o, "detail", "") or ""
    if first.startswith("assert ") or first == "assert" or re.match(r"^AssertionError\b", first):
        return "AssertionError"
    m = _TRAILER_RE.search(detail.strip().splitlines()[-1] if detail.strip() else "")
    if m:
        return m.group(1).rsplit(".", 1)[-1]
    m = _E_LINE_RE.search(detail)
    if m:
        return m.group(1).rsplit(".", 1)[-1]
    if "ImportError while importing test module" in detail:
        return "ImportError"
    # unittest_junit puts the last traceback line in the message.
    m = _EXC_PREFIX_RE.match(msg.splitlines()[-1]) if msg else None
    return m.group(1).rsplit(".", 1)[-1] if m else ""


_PYTEST_LOC_RE = re.compile(r"^(\S+?):\d+: [A-Za-z_][\w.]*\s*$", re.M)
_TB_LOC_RE = re.compile(r'File "([^"]+)", line \d+')


def raise_location(o: TestOutcome | None) -> str:
    """File in which the failure was raised, from the traceback ('' if unknown)."""
    detail = (getattr(o, "detail", "") or "") if o else ""
    tb = _TB_LOC_RE.findall(detail)
    if tb:
        return tb[-1].replace("\\", "/")
    locs = _PYTEST_LOC_RE.findall(detail)
    return locs[-1].replace("\\", "/") if locs else ""


def failure_kind(o: TestOutcome | None, test_path: str = "") -> str:
    """``behavioural`` (strong), ``missing_symbol`` (weak) or ``unknown``.

    ``AttributeError``/``NameError`` and argument ``TypeError``s are weak only
    when raised in the test file itself (the test touched a name that base
    lacks); raised inside the project's code they are the bug (strong).
    """
    name = exception_name(o)
    text = f"{o.message if o else ''}\n{getattr(o, 'detail', '') if o else ''}"
    if name in ("ImportError", "ModuleNotFoundError"):
        return WEAK
    if name in MISSING_SYMBOL or (name == "TypeError" and _SIGNATURE_RE.search(text)):
        if name == "AttributeError" and "'NoneType' object has no attribute" in text:
            return STRONG
        loc = raise_location(o)
        base = test_path.rsplit("/", 1)[-1]
        if loc and base and not loc.endswith(base):
            return STRONG
        return WEAK
    if name:
        return STRONG
    if o is not None and o.outcome == J_FAILED:
        return STRONG  # a plain test failure without a recognisable exception
    return UNKNOWN


@dataclass
class CandidateOutcome:
    variant: str
    file: str
    classification: str
    message: str = ""
    rationale: str = ""
    round: int = 0
    tests: list[dict[str, Any]] = field(default_factory=list)
    code: str = ""
    output: str = ""  # head/base output tail, for refinement feedback only (not stored in result.json)

    def to_dict(self) -> dict[str, Any]:
        return {"variant": self.variant, "round": self.round, "file": self.file, "classification": self.classification,
                "message": self.message[:500], "rationale": self.rationale[:1000], "tests": self.tests[:20]}


def classify_candidate(runner: TestRunner, path: str, base_run: RunResult, head_run: RunResult) -> tuple[str, str, list[dict[str, Any]]]:
    """Classify one candidate from its base and head runs: ``(classification, message, tests)``."""
    if not head_run.ok:
        return ERROR, f"head run: {head_run.error}", []
    tests = classify_runs(runner, [path], base_run, head_run, {path: None})
    rows: list[dict[str, Any]] = []
    base_coll = next((o for tid, o in base_run.outcomes.items()
                      if is_collection_error_id(tid) and runner.test_belongs_to(tid, path) and o.outcome == J_ERROR), None)
    for t in tests:
        base_o = base_run.outcomes.get(t.test_id)
        kind = ""
        if t.classification == FT_FAIL_TO_PASS:
            kind = failure_kind(base_o if base_o is not None else base_coll, path)
            if base_o is None and base_coll is not None and kind == STRONG:
                kind = WEAK  # the whole module failed to import on base
            if base_o is None and base_coll is None:
                kind = UNKNOWN
        rows.append({"test_id": t.test_id, "base": t.base, "head": t.head,
                     "base_kind": exception_name(base_o if base_o is not None else base_coll) if t.classification == FT_FAIL_TO_PASS else "",
                     "message": t.message[:500], "_cls": t.classification, "_kind": kind})
    if not rows:
        return ERROR, "the test runner reported no tests for the candidate", []
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    if any(r["_cls"] == FT_FAIL_ON_HEAD for r in rows):
        bad = next(r for r in rows if r["_cls"] == FT_FAIL_ON_HEAD)
        return FAILS_ON_HEAD, f"fails on head: {bad['message'][:300]}", clean
    if not base_run.ok:
        return ERROR, f"base run: {base_run.error}", clean
    f2p = [r for r in rows if r["_cls"] == FT_FAIL_TO_PASS]
    if any(r["_kind"] == STRONG for r in f2p):
        r = next(r for r in f2p if r["_kind"] == STRONG)
        return FAIL_TO_PASS, f"fails on base ({r['base_kind'] or 'failure'}) and passes on head", clean
    if f2p:
        r = f2p[0]
        return FAIL_TO_PASS_WEAK, (f"fails on base only with {r['base_kind'] or 'an error before the test body ran'}"
                                   f" (missing name or import), passes on head"), clean
    if any(r["_cls"] == FT_PASS_TO_PASS for r in rows):
        return PASS_TO_PASS, "passes on base and on head", clean
    return ERROR, "no test passed on head (all skipped or unrecognised)", clean


def _write(wt: Path, rel: str, code: str) -> Path:
    root = wt.resolve()
    target = (wt / rel).resolve()
    if root not in target.parents:
        raise ManifestError(f"candidate path escapes the worktree: {rel[:100]!r}")
    if target.is_symlink():
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(code, encoding="utf-8")
    return target


def _setup(setup_command: Sequence[str] | None, wt: Path, env: Mapping[str, str], timeout: float) -> str | None:
    if not setup_command:
        return None
    r = run_command(list(setup_command), cwd=wt, env=env, timeout=timeout)
    if not r.ok:
        return f"setup_command failed in {wt.name}: " + ("timed out" if r.timed_out else (r.launch_error or f"exit code {r.returncode}"))
    return None


def run_candidates(
    runner: TestRunner,
    repo: str | Path,
    merge_base: str,
    head_sha: str,
    candidates: Sequence[tuple[dict[str, Any], str]],
    env: Mapping[str, str],
    timeout: float,
    setup_command: Sequence[str] | None = None,
    notes: list[str] | None = None,
) -> list[CandidateOutcome]:
    """Run ``(manifest_entry, code)`` pairs on base and head worktrees and classify them."""
    out: list[CandidateOutcome] = []
    if not candidates:
        return out
    with gitmod.worktree(repo, merge_base, prefix="proofcheck-agent-base-") as bwt, \
            gitmod.worktree(repo, head_sha, prefix="proofcheck-agent-head-") as hwt:
        for wt in (bwt, hwt):
            err = _setup(setup_command, wt, env, timeout)
            if err and notes is not None:
                notes.append(err)
        for entry, code in candidates:
            rel = entry["file"]
            co = CandidateOutcome(entry["variant"], rel, ERROR, rationale=entry.get("rationale", ""),
                                  round=entry.get("round", 0), code=code)
            try:
                ast.parse(code)
            except SyntaxError as exc:
                co.classification, co.message = INVALID, f"syntax error at line {exc.lineno}"
                out.append(co)
                continue
            written = []
            try:
                written = [_write(hwt, rel, code), _write(bwt, rel, code)]
                head_run = runner.run(hwt, [rel], env, timeout)
                base_run = runner.run(bwt, [rel], env, timeout)
                co.classification, co.message, co.tests = classify_candidate(runner, rel, base_run, head_run)
                co.output = (f"--- head ---\n{head_run.output_tail[-1500:]}\n--- base ---\n{base_run.output_tail[-1500:]}")
            except ManifestError as exc:
                co.classification, co.message = ERROR, str(exc)
            finally:
                for w in written:
                    try:
                        w.unlink()
                    except OSError:
                        pass
            out.append(co)
    return out


_RANK = {FAIL_TO_PASS: 0, FAIL_TO_PASS_WEAK: 1, PASS_TO_PASS: 2, FAILS_ON_HEAD: 3, ERROR: 4, INVALID: 5, NO_SUBMISSION: 6}


def overall_status(outcomes: Sequence[CandidateOutcome]) -> str:
    classes = {o.classification for o in outcomes}
    if FAIL_TO_PASS in classes:
        return STATUS_EVIDENCE
    if FAIL_TO_PASS_WEAK in classes:
        return STATUS_WEAK
    if PASS_TO_PASS in classes:
        return STATUS_NO_EVIDENCE
    if FAILS_ON_HEAD in classes:
        return STATUS_INCONCLUSIVE
    if classes & {ERROR}:
        return STATUS_ERROR
    return STATUS_NO_EVIDENCE


def pick_winner(outcomes: Sequence[CandidateOutcome]) -> CandidateOutcome | None:
    """The best candidate worth showing (strong, else weak); shortest code wins ties."""
    good = [o for o in outcomes if o.classification in (FAIL_TO_PASS, FAIL_TO_PASS_WEAK)]
    if not good:
        return None
    return min(good, key=lambda o: (_RANK[o.classification], len(o.code), o.variant))


def build_section(manifest: dict[str, Any], outcomes: Sequence[CandidateOutcome], notes: Sequence[str] = (),
                  rounds: int = 1) -> dict[str, Any]:
    """The ``agent`` section of result.json."""
    cand_dicts = [o.to_dict() for o in outcomes]
    # Variants that produced nothing runnable are listed too.
    seen = {(o.variant, o.round) for o in outcomes}
    for c in manifest.get("candidates", []):
        if c["status"] != "submitted" and (c["variant"], c.get("round", 0)) not in seen:
            cls = INVALID if c["status"] == "invalid" else NO_SUBMISSION
            msg = c.get("error") or "; ".join(c.get("errors") or [])
            cand_dicts.append({"variant": c["variant"], "round": c.get("round", 0), "file": "", "classification": cls,
                               "message": msg[:500] or ("no test submitted" if cls == NO_SUBMISSION else "rejected"),
                               "rationale": "", "tests": []})
    status = overall_status(outcomes) if outcomes else (STATUS_ERROR if manifest.get("status") == "error" else STATUS_NO_EVIDENCE)
    if manifest.get("status") == STATUS_SKIPPED:
        status = STATUS_SKIPPED
    winner = pick_winner(outcomes)
    return {
        "status": status,
        "reason": manifest.get("reason", ""),
        "provider": manifest.get("provider", ""),
        "model": manifest.get("model", ""),
        "target_claim": manifest.get("target_claim", ""),
        "variants": list(manifest.get("variants", [])),
        "rounds": rounds,
        "usage": dict(manifest.get("usage") or {}),
        "cost_usd": manifest.get("cost_usd"),
        "candidates": cand_dicts,
        "winner": None if winner is None else {
            "variant": winner.variant, "file": winner.file, "classification": winner.classification,
            "code": winner.code[:MAX_CODE_IN_RESULT], "rationale": winner.rationale[:1000]},
        "upgraded_claims": [],
        "notes": list(notes)[:10],
    }


def recompute_verdict(result: dict[str, Any]) -> str:
    """Same rules as :func:`proofcheck.evidence.verdict`, on result dicts."""
    claims = result.get("claims") or []
    counts = (result.get("tests") or {}).get("counts") or {}
    if any(c.get("status") == "contradicted" for c in claims) or counts.get("fail_on_head", 0):
        return "contradicted"
    verified = any(c.get("status") == "verified" for c in claims) or counts.get("fail_to_pass", 0)
    unverified = any(c.get("status") == "unverified" for c in claims)
    if verified and not unverified:
        return "evidence"
    if verified:
        return "partial"
    return "no_evidence"


def merge_into_result(result: dict[str, Any], section: dict[str, Any], target_issue: int | None) -> dict[str, Any]:
    """Attach ``section`` as ``result['agent']`` and upgrade a matching ``fixes`` claim.

    Only an *unverified* ``fixes_issue`` claim is upgraded, and only by a
    strong (``fail_to_pass``) candidate. Contradicted claims stay
    contradicted; agent evidence never makes anything worse.
    """
    result = dict(result)
    claims = [dict(c) for c in (result.get("claims") or [])]
    upgraded: list[int] = []
    if section["status"] == STATUS_EVIDENCE:
        fixes = [i for i, c in enumerate(claims) if c.get("type") == "fixes_issue" and c.get("status") == "unverified"]
        if target_issue is not None:
            fixes = [i for i in fixes if (claims[i].get("details") or {}).get("number") == target_issue]
        elif len(fixes) > 1:
            fixes = fixes[:1]
        for i in fixes:
            claims[i]["status"] = "verified"
            claims[i]["verified_by"] = "agent"
            claims[i]["evidence"] = AGENT_EVIDENCE_TEXT + (f" ({section['winner']['file']})" if section.get("winner") else "")
            upgraded.append(i)
    elif section["status"] == STATUS_WEAK:
        for c in claims:
            if c.get("type") == "fixes_issue" and c.get("status") == "unverified":
                note = "an agent-written test fails on base only because a name is missing there (weak)"
                c["evidence"] = (c.get("evidence", "") + "; " + note).strip("; ")[:1000]
    section = dict(section, upgraded_claims=upgraded)
    result["claims"] = claims
    result["agent"] = section
    if upgraded and result.get("verdict") != "contradicted":
        result["verdict"] = recompute_verdict(result)
    return result


def verify_candidates_dir(
    repo: str | Path,
    base: str,
    head: str,
    candidates_dir: str | Path,
    runner: TestRunner,
    env: Mapping[str, str],
    timeout: float,
    setup_command: Sequence[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the manifest, run every submitted candidate and return ``(manifest, agent_section)``.

    Problems with the manifest or a mismatch between the manifest's commits
    and ``base``/``head`` give an ``error`` section rather than an exception.
    """
    from .schema import load_manifest

    base_sha = gitmod.resolve_ref(repo, base)
    head_sha = gitmod.resolve_ref(repo, head)
    mb = gitmod.merge_base(repo, base_sha, head_sha)
    try:
        manifest = load_manifest(candidates_dir)
    except ManifestError as exc:
        empty = {"status": STATUS_ERROR, "reason": f"cannot read the candidates: {exc}", "candidates": [], "variants": []}
        return empty, build_section(empty, [], rounds=0)
    if manifest["head"] != head_sha or manifest["base"] not in (base_sha, mb):
        manifest = dict(manifest, status=STATUS_ERROR,
                        reason="the candidates were proposed for different commits than the ones being verified")
        return manifest, build_section(manifest, [], rounds=0)
    if manifest["status"] in (STATUS_SKIPPED, STATUS_ERROR):
        return manifest, build_section(manifest, [], rounds=0)
    notes: list[str] = []
    pairs = []
    for c in manifest["candidates"]:
        if c["status"] != "submitted":
            continue
        try:
            pairs.append((c, read_candidate(candidates_dir, c["file"])))
        except ManifestError as exc:
            notes.append(str(exc)[:300])
    outcomes = run_candidates(runner, repo, mb, head_sha, pairs, env, timeout, setup_command, notes)
    rounds = 1 + max((c.get("round", 0) for c in manifest["candidates"]), default=0)
    return manifest, build_section(manifest, outcomes, notes, rounds)
