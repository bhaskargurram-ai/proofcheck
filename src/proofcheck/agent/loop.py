"""``proofcheck agent run``: propose, verify and refine in one process.

After the first propose/verify pass, every variant whose candidate is not
strong evidence gets the execution outcome (classification and a tail of the
test output, as untrusted data) and may submit a revised test, for up to
``agent.max_rounds`` rounds. This is the "execution feedback" loop that
the split CI workflow cannot do.

**Local or trusted use only.** Here the candidate tests, which are written by
a model that read untrusted PR/issue text, run on this machine in the same
process tree that holds ``PROOFCHECK_LLM_API_KEY``. The test environment is
scrubbed of secret-looking variables, but that is not a sandbox (a test can
read its parent's memory or environment through /proc, for example). In CI,
use ``agent propose`` and ``agent verify`` in separate jobs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..runners import TestRunner
from . import FAIL_TO_PASS, STATUS_ERROR, STATUS_SKIPPED
from .config import AgentConfig
from .llm_tools import LLMConfigError, LLMToolError, ToolLLM, Turn
from .prompts import feedback_message
from .propose import CAND_ERROR, SUBMITTED, Proposal, VariantSession, propose, redact, update_manifest, write_proposal
from .verify import CandidateOutcome, build_section, run_candidates

RUN_WARNING = (
    "WARNING: `proofcheck agent run` executes LLM-written test code on this machine, in the same process tree that "
    "holds PROOFCHECK_LLM_API_KEY. Use it only locally or in trusted contexts. In CI, run `proofcheck agent propose` "
    "and `proofcheck agent verify` in separate jobs (verify without secrets)."
)


@dataclass
class AgentRun:
    proposal: Proposal
    outcomes: list[CandidateOutcome] = field(default_factory=list)
    rounds: int = 0
    notes: list[str] = field(default_factory=list)
    section: dict[str, Any] = field(default_factory=dict)


def _submitted(sessions: Sequence[VariantSession]) -> list[tuple[dict[str, Any], str]]:
    return [({"variant": s.variant, "file": s.test_path, "rationale": s.rationale, "round": s.round}, s.code)
            for s in sessions if s.status == SUBMITTED]


def _refine(session: VariantSession, outcome: CandidateOutcome | None, budget: Any, max_tool_calls: int) -> bool:
    """Feed the execution outcome back and let the model revise. Returns True on a new accepted test."""
    snapshot = (session.status, session.code, session.rationale, session.targets_claim, list(session.repairs))
    if outcome is not None:
        details = f"{outcome.message}\n\n{outcome.output}"[-4000:]
        session.turns.append(Turn("user", text=feedback_message(outcome.classification, details, session.tools.nonce)))
    else:
        session.turns.append(Turn("user", text="proofcheck: no test was accepted yet. Call submit_test with a complete test file."))
    session.status = "no_submission"
    session.error = ""
    try:
        session.run(budget, max_tool_calls=max_tool_calls)
    except LLMConfigError:
        raise
    except LLMToolError as exc:
        session.error = redact(str(exc), session.secrets)
    if session.status == SUBMITTED and session.code != snapshot[1]:
        return True
    session.status, session.code, session.rationale, session.targets_claim, session.repairs = snapshot
    return False


def run_agent(
    repo: str | Path,
    base: str,
    head: str,
    *,
    agent_cfg: AgentConfig,
    runner: TestRunner,
    llm_factory: Callable[[], ToolLLM],
    env: Mapping[str, str],
    pr_title: str = "",
    pr_body: str = "",
    issues: Mapping[str, str] | None = None,
    result: dict[str, Any] | None = None,
    setup_command: Sequence[str] | None = None,
    secrets_: Sequence[str] = (),
    out_dir: str | Path | None = None,
    log: Callable[[str], None] = lambda s: None,
) -> AgentRun:
    """Propose, verify, then refine for up to ``agent_cfg.max_rounds`` rounds."""
    proposal = propose(repo, base, head, agent_cfg=agent_cfg, runner=runner, llm_factory=llm_factory, pr_title=pr_title,
                       pr_body=pr_body, issues=issues, result=result, secrets_=secrets_, log=log)
    run = AgentRun(proposal)
    manifest = proposal.manifest
    if manifest["status"] in (STATUS_SKIPPED, STATUS_ERROR):
        run.section = build_section(manifest, [], rounds=0)
        if out_dir:
            write_proposal(out_dir, proposal)
        return run
    mb, head_sha = manifest["merge_base"], manifest["head"]
    latest: dict[str, CandidateOutcome] = {}
    outcomes = run_candidates(runner, repo, mb, head_sha, _submitted(proposal.sessions), env, agent_cfg.test_timeout,
                              setup_command, run.notes)
    run.outcomes.extend(outcomes)
    latest.update({o.variant: o for o in outcomes})
    run.rounds = 1
    for rnd in range(1, agent_cfg.max_rounds + 1):
        if any(o.classification == FAIL_TO_PASS for o in run.outcomes):
            break
        if proposal.budget.exhausted():
            run.notes.append(f"refinement stopped: {proposal.budget.exhausted()}")
            break
        revised: list[VariantSession] = []
        for s in proposal.sessions:
            if s.status == CAND_ERROR:
                continue
            s.round = rnd
            log(f"agent: refining {s.variant} (round {rnd + 1}) ...")
            if _refine(s, latest.get(s.variant), proposal.budget, agent_cfg.max_tool_calls):
                revised.append(s)
        if not revised:
            break
        new = run_candidates(runner, repo, mb, head_sha, _submitted(revised), env, agent_cfg.test_timeout, setup_command, run.notes)
        run.outcomes.extend(new)
        latest.update({o.variant: o for o in new})
        run.rounds = rnd + 1
        for o in new:
            log(f"agent: {o.variant} round {rnd + 1}: {o.classification}")
    update_manifest(manifest, proposal.sessions, proposal.budget)
    if out_dir:
        write_proposal(out_dir, proposal)
    run.section = build_section(manifest, run.outcomes, run.notes, run.rounds)
    return run
