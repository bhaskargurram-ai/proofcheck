"""``proofcheck agent propose``: ask an LLM for reproduction-test candidates.

For every prompt variant a fresh conversation is run in which the model may
only use the read-only tools of :mod:`proofcheck.agent.tools` and deliver a
test through ``submit_test``. Accepted files are written to the candidates
directory together with ``manifest.json`` and a redacted transcript.

Nothing from the repository is executed here, so this step can hold the API
key. Running the candidates is :mod:`proofcheck.agent.verify`'s job, in a
separate process (in CI: a separate job without secrets).
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .. import __version__
from .. import claims as C
from .. import git as gitmod
from ..runners import TestRunner
from . import MANIFEST_SCHEMA, MANIFEST_VERSION, STATUS_ERROR, STATUS_SKIPPED, VARIANTS
from .config import AgentConfig
from .llm_tools import LLMConfigError, LLMToolError, Reply, ToolCall, ToolLLM, ToolResult, Turn, Usage
from .prompts import FORCE_SUBMIT, NUDGE, TaskContext, initial_message, issue_block, rejection_message, system_prompt
from .static import ModuleIndex, StaticReport, validate_test_code
from .tools import SUBMIT_TOOL, RepoTools, list_head_files, read_blob_text, validate_submission

MAX_TRANSCRIPT_FIELD = 2_000
MAX_HISTORY_CHARS = 120_000
KEEP_RECENT_TOOL_TURNS = 6

# Candidate statuses in the manifest.
SUBMITTED = "submitted"
NO_SUBMISSION = "no_submission"
INVALID = "invalid"
CAND_ERROR = "error"
CANDIDATE_STATUSES = (SUBMITTED, NO_SUBMISSION, INVALID, CAND_ERROR)

_SECRET_PATTERNS = [
    re.compile(r"sk-(?:ant-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]


def redact(text: str, secrets_: Sequence[str] = ()) -> str:
    """Remove known secret values and common token formats from ``text``."""
    for s in secrets_:
        if s and len(s) >= 6:
            text = text.replace(s, "[REDACTED]")
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def _clip(text: str, limit: int = MAX_TRANSCRIPT_FIELD) -> str:
    return text if len(text) <= limit else text[:limit] + f"…[+{len(text) - limit} chars]"


def variant_filename(variant: str) -> str:
    return "test_" + variant.replace("-", "_") + ".py"


# --------------------------------------------------------------------------- budget


@dataclass
class Budget:
    """Run-wide token/cost budget shared by all variants and rounds."""

    max_total_tokens: int
    max_cost_usd: float = 0.0
    price_in: float | None = None
    price_out: float | None = None
    usage: Usage = field(default_factory=Usage)

    @classmethod
    def from_config(cls, cfg: AgentConfig) -> "Budget":
        return cls(cfg.max_total_tokens, cfg.max_cost_usd, cfg.price_input_per_mtok, cfg.price_output_per_mtok)

    def cost(self) -> float | None:
        return self.usage.cost(self.price_in, self.price_out)

    def exhausted(self) -> str | None:
        if self.usage.total_tokens >= self.max_total_tokens:
            return f"token budget exhausted ({self.usage.total_tokens} >= {self.max_total_tokens})"
        cost = self.cost()
        if self.max_cost_usd and cost is not None and cost >= self.max_cost_usd:
            return f"cost budget exhausted (${cost:.4f} >= ${self.max_cost_usd:.2f})"
        return None


# --------------------------------------------------------------------------- one variant


@dataclass
class VariantSession:
    """The conversation for one prompt variant (kept in memory for refinement rounds)."""

    variant: str
    llm: ToolLLM
    tools: RepoTools
    system: str
    turns: list[Turn]
    test_path: str
    style: str
    index: ModuleIndex | None
    max_tool_calls: int
    secrets: Sequence[str] = ()
    round: int = 0
    tool_calls_used: int = 0
    status: str = NO_SUBMISSION
    code: str = ""
    rationale: str = ""
    targets_claim: str = ""
    suggested_filename: str = ""
    repairs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    error: str = ""
    usage: Usage = field(default_factory=Usage)
    transcript: list[dict[str, Any]] = field(default_factory=list)

    # -- transcript ----------------------------------------------------------------------
    def _log(self, entry: dict[str, Any]) -> None:
        entry = {"variant": self.variant, "round": self.round, **entry}
        self.transcript.append(json.loads(redact(json.dumps(entry), self.secrets)))

    def _log_reply(self, reply: Reply) -> None:
        self._log({"role": "assistant", "text": _clip(reply.text), "stop_reason": reply.stop_reason,
                   "tool_calls": [{"name": c.name[:60], "arguments": _clip(json.dumps(c.arguments))} for c in reply.tool_calls],
                   "usage": reply.usage.to_dict()})

    def _log_results(self, results: list[ToolResult]) -> None:
        self._log({"role": "tool", "results": [{"name": r.name[:60], "is_error": r.is_error, "content": _clip(r.content, 1500)}
                                               for r in results]})

    # -- submission ----------------------------------------------------------------------
    def _submit(self, call: ToolCall) -> tuple[bool, str]:
        if call.parse_error:
            self.errors = [call.parse_error]
            return False, rejection_message(self.errors)
        args, errors = validate_submission(call.arguments)
        if args is None:
            self.errors = errors
            self.status = INVALID
            return False, rejection_message(errors)
        report: StaticReport = validate_test_code(args["code"], self.style, self.index)
        if not report.ok:
            self.errors = report.errors
            self.status = INVALID
            return False, rejection_message(report.errors)
        self.status = SUBMITTED
        self.code = report.code
        self.rationale = args["rationale"].strip()
        self.targets_claim = args["targets_claim"].strip()
        self.suggested_filename = args["filename"].strip()
        self.repairs = list(report.repairs)
        self.warnings = list(report.warnings)
        self.errors = []
        note = f"Accepted. proofcheck stored the file as {self.test_path} and will run it on base and head."
        if report.repairs:
            note += " Imports were repaired: " + "; ".join(report.repairs)
        return True, note

    # -- history compaction ----------------------------------------------------------------
    def _compact(self) -> None:
        """Elide old tool outputs once the history gets long (keeps requests bounded)."""
        sizes = sum(len(r.content) for t in self.turns if t.role == "tool" for r in t.results)
        if sizes <= MAX_HISTORY_CHARS:
            return
        tool_turns = [t for t in self.turns if t.role == "tool"]
        for t in tool_turns[:-KEEP_RECENT_TOOL_TURNS]:
            for r in t.results:
                if len(r.content) > 300:
                    r.content = r.content[:200] + "\n[older tool output elided to save context; call the tool again if needed]"

    # -- the loop ------------------------------------------------------------------------
    def run(self, budget: Budget, max_tool_calls: int | None = None) -> None:
        """Converse until a test is accepted or a budget runs out."""
        limit = self.max_tool_calls if max_tool_calls is None else max_tool_calls
        used_at_start = self.tool_calls_used
        forced_attempts = 0
        nudges = 0
        accepted = False
        specs = self.tools.specs() + [SUBMIT_TOOL]
        while not accepted:
            why = budget.exhausted()
            if why:
                self.error = why
                break
            over = self.tool_calls_used - used_at_start >= limit
            if over:
                if forced_attempts >= 3:
                    self.error = self.error or "tool-call budget exhausted without an accepted test"
                    break
                forced_attempts += 1
            self._compact()
            reply = self.llm.chat(self.system, self.turns, specs, force_tool=SUBMIT_TOOL.name if over else None)
            self.usage.add(reply.usage)
            budget.usage.add(reply.usage)
            self.turns.append(Turn("assistant", text=reply.text, tool_calls=list(reply.tool_calls)))
            self._log_reply(reply)
            if not reply.tool_calls:
                nudges += 1
                if nudges > 3:
                    self.error = "the model stopped without submitting a test"
                    break
                self.turns.append(Turn("user", text=FORCE_SUBMIT if over else NUDGE))
                continue
            results: list[ToolResult] = []
            for call in reply.tool_calls:
                if call.name == SUBMIT_TOOL.name:
                    if accepted:
                        results.append(ToolResult(call.id, call.name, "Ignored: a test was already accepted.", True))
                        continue
                    ok, msg = self._submit(call)
                    accepted = accepted or ok
                    results.append(ToolResult(call.id, call.name, msg, not ok))
                elif accepted:
                    results.append(ToolResult(call.id, call.name, "Ignored: a test was already accepted.", True))
                elif self.tool_calls_used - used_at_start >= limit:
                    results.append(ToolResult(call.id, call.name, "Tool budget exhausted. Call submit_test now.", True))
                else:
                    results.append(self.tools.call(call))
                self.tool_calls_used += 1
            self.turns.append(Turn("tool", results=results))
            self._log_results(results)
            if not accepted and self.tool_calls_used - used_at_start >= limit and not over:
                self.turns.append(Turn("user", text=FORCE_SUBMIT))
        if accepted:
            self.error = ""

    def to_manifest_entry(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "status": self.status if self.status in CANDIDATE_STATUSES else NO_SUBMISSION,
            "file": self.test_path if self.status == SUBMITTED else "",
            "round": self.round,
            "rationale": _clip(redact(self.rationale, self.secrets), 2000),
            "targets_claim": _clip(redact(self.targets_claim, self.secrets), 200),
            "suggested_filename": _clip(self.suggested_filename, 200),
            "tool_calls": self.tool_calls_used,
            "repairs": [_clip(r, 300) for r in self.repairs[:20]],
            "warnings": [_clip(w, 300) for w in self.warnings[:20]],
            "errors": [_clip(redact(e, self.secrets), 300) for e in self.errors[:20]],
            "error": _clip(redact(self.error, self.secrets), 500),
            "usage": self.usage.to_dict(),
        }


# --------------------------------------------------------------------------- context


def detect_style(repo: str | Path, head_sha: str, runner_name: str, paths: Sequence[str] | None = None) -> str:
    """``unittest`` when the project runs unittest (or shows no sign of pytest), else ``pytest``."""
    if runner_name == "unittest":
        return "unittest"
    paths = list(paths if paths is not None else list_head_files(str(repo), head_sha))
    names = set(paths)
    if any(p == "conftest.py" or p.endswith("/conftest.py") for p in paths) or "pytest.ini" in names:
        return "pytest"
    for cfg_file, marker in (("pyproject.toml", "[tool.pytest"), ("setup.cfg", "[tool:pytest]"), ("tox.ini", "[pytest]")):
        if cfg_file in names and marker in (read_blob_text(str(repo), head_sha, cfg_file) or ""):
            return "pytest"
    tests = [p for p in paths if re.search(r"(^|/)(test_[^/]*|[^/]*_test)\.py$", p)][:30]
    uses_pytest = uses_unittest = 0
    for p in tests:
        src = read_blob_text(str(repo), head_sha, p) or ""
        uses_pytest += bool(re.search(r"^\s*(import pytest|from pytest\b)", src, re.M))
        uses_unittest += bool(re.search(r"^\s*(import unittest|from unittest\b)", src, re.M))
    if uses_unittest and not uses_pytest:
        return "unittest"
    return "pytest"


def runner_description(runner: TestRunner, style: str) -> str:
    if runner.name == "unittest":
        return "proofcheck's unittest runner (the file is imported as a module and its TestCase classes are run)"
    return "pytest" + (" (unittest.TestCase classes are collected too)" if style == "unittest" else "")


def pick_target(claims: Sequence[dict[str, Any]], when: str, have_result: bool) -> tuple[dict[str, Any] | None, str]:
    """Choose the ``fixes`` claim to work on. Returns ``(claim, skip_reason)``."""
    fixes = [c for c in claims if c.get("type") == C.FIXES_ISSUE]
    if have_result and when == "missing-evidence":
        if not fixes:
            return None, "the PR makes no 'fixes #N' claim"
        missing = [c for c in fixes if c.get("status", "unverified") == "unverified"]
        if not missing:
            return None, "deterministic evidence already covers (or contradicts) every 'fixes' claim"
        return missing[0], ""
    return (fixes[0] if fixes else None), ""


def target_text(claim: dict[str, Any] | None) -> tuple[str, int | None]:
    if not claim:
        return "the bug the pull request says it fixes", None
    num = (claim.get("details") or {}).get("number")
    if isinstance(num, (int, float)) and not isinstance(num, bool) and num > 0:
        return f"fixes #{int(num)}", int(num)
    return re.sub(r"\s+", " ", str(claim.get("text") or "fixes the linked issue"))[:120], None


@dataclass
class Proposal:
    manifest: dict[str, Any]
    sessions: list[VariantSession]
    budget: Budget


def _base_manifest(base_sha: str, head_sha: str, merge_base: str, cfg: AgentConfig) -> dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA, "version": MANIFEST_VERSION, "tool_version": __version__,
        "status": "proposed", "reason": "", "base": base_sha, "head": head_sha, "merge_base": merge_base,
        "provider": "", "model": "", "style": "", "test_dir": cfg.test_dir, "target_claim": "", "target_issue": None,
        "variants": [], "candidates": [], "usage": Usage().to_dict(), "cost_usd": None,
        "budgets": {"max_tool_calls": cfg.max_tool_calls, "max_total_tokens": cfg.max_total_tokens,
                    "max_output_tokens": cfg.max_output_tokens, "max_cost_usd": cfg.max_cost_usd},
    }


def propose(
    repo: str | Path,
    base: str,
    head: str,
    *,
    agent_cfg: AgentConfig,
    runner: TestRunner,
    llm_factory: Callable[[], ToolLLM],
    pr_title: str = "",
    pr_body: str = "",
    issues: Mapping[str, str] | None = None,
    result: dict[str, Any] | None = None,
    variants: Sequence[str] | None = None,
    secrets_: Sequence[str] = (),
    log: Callable[[str], None] = lambda s: None,
) -> Proposal:
    """Run every prompt variant once and return the manifest and live sessions.

    ``llm_factory`` is only called when there is work to do, so a skipped run
    never needs LLM configuration.
    """
    repo = Path(repo)
    base_sha = gitmod.resolve_ref(repo, base)
    head_sha = gitmod.resolve_ref(repo, head)
    mb = gitmod.merge_base(repo, base_sha, head_sha)
    manifest = _base_manifest(base_sha, head_sha, mb, agent_cfg)
    budget = Budget.from_config(agent_cfg)

    if result is not None:
        claims = list(result.get("claims") or [])
    else:
        claims = [c.to_dict() for c in C.extract_claims(pr_title, pr_body)]
    target, skip = pick_target(claims, agent_cfg.when, result is not None)
    if skip:
        manifest.update(status=STATUS_SKIPPED, reason=skip)
        return Proposal(manifest, [], budget)
    tclaim, tissue = target_text(target)
    manifest.update(target_claim=tclaim, target_issue=tissue)
    if runner.name not in ("pytest", "unittest"):
        manifest.update(status=STATUS_ERROR, reason=f"the agent only supports Python test runners (pytest, unittest), not {runner.name}")
        return Proposal(manifest, [], budget)

    llm = llm_factory()
    manifest.update(provider=llm.provider, model=llm.model)
    paths = list_head_files(str(repo), head_sha)
    style = detect_style(repo, head_sha, runner.name, paths)
    manifest["style"] = style
    index = ModuleIndex.from_paths(paths, read=lambda p: read_blob_text(str(repo), head_sha, p))
    changed = [f.path for f in gitmod.changed_files(repo, mb, head_sha)]

    chosen_issues = dict(issues or {})
    if tissue is not None and f"#{tissue}" in chosen_issues:
        chosen_issues = {f"#{tissue}": chosen_issues[f"#{tissue}"]}
    nonce = secrets.token_hex(8)
    names = list(variants or agent_cfg.variants)
    manifest["variants"] = names
    sessions: list[VariantSession] = []
    for variant in names:
        if variant not in VARIANTS:
            raise ValueError(f"unknown variant {variant!r}")
        test_path = f"{agent_cfg.test_dir}/{variant_filename(variant)}"
        ctx = TaskContext(base_sha=mb, head_sha=head_sha, test_path=test_path, style=style,
                          runner_desc=runner_description(runner, style), target_claim=tclaim, pr_title=pr_title,
                          pr_body=pr_body, issues=chosen_issues, changed_files=changed)
        tools = RepoTools(str(repo), mb, head_sha, nonce, issue_text=issue_block(ctx, nonce),
                          is_test_module=runner.is_test_module,
                          test_info=f"Tests run with {ctx.runner_desc}. Test style for your file: {style}.")
        session = VariantSession(variant, llm, tools, system_prompt(ctx, nonce, agent_cfg.max_tool_calls),
                                 [Turn("user", text=initial_message(variant, ctx, nonce))], test_path, style, index,
                                 agent_cfg.max_tool_calls, secrets=secrets_)
        sessions.append(session)
        log(f"agent: variant {variant} ...")
        try:
            session.run(budget)
        except LLMConfigError:
            raise
        except LLMToolError as exc:
            session.status = CAND_ERROR if session.status != SUBMITTED else session.status
            session.error = redact(str(exc), secrets_)
        log(f"agent: variant {variant}: {session.status}" + (f" ({session.error})" if session.error else ""))
    update_manifest(manifest, sessions, budget)
    return Proposal(manifest, sessions, budget)


def update_manifest(manifest: dict[str, Any], sessions: Sequence[VariantSession], budget: Budget) -> None:
    manifest["candidates"] = [s.to_manifest_entry() for s in sessions]
    manifest["usage"] = budget.usage.to_dict()
    manifest["cost_usd"] = budget.cost()


def write_proposal(out_dir: str | Path, proposal: Proposal) -> Path:
    """Write ``manifest.json``, candidate files and ``transcript.jsonl`` into ``out_dir``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    root = out.resolve()
    for s in proposal.sessions:
        if s.status != SUBMITTED:
            continue
        target = (out / s.test_path).resolve()
        if root not in target.parents:  # pragma: no cover - test_path is validated config
            raise ValueError(f"refusing to write outside {out}: {s.test_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(s.code, encoding="utf-8")
    with (out / "transcript.jsonl").open("w", encoding="utf-8") as fh:
        for s in proposal.sessions:
            for entry in s.transcript:
                fh.write(json.dumps(entry) + "\n")
    path = out / "manifest.json"
    path.write_text(json.dumps(proposal.manifest, indent=2) + "\n", encoding="utf-8")
    return path
