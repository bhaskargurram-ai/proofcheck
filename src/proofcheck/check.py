"""Orchestrates all checks for one pull request and builds the result document."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from . import claims as C
from . import git as gitmod
from .benchmark import BenchmarkResult, run_benchmark
from .config import Config
from .evidence import assess_claims, verdict
from .failtopass import STATUS_SKIPPED, TestCheck
from .llm import ClaimExtractor, LLMError
from .proc import scrub_env
from .projects import ProjectSet, build_projects, run_projects_fail_to_pass, setup_projects
from .repro import IssueRepro, reproduce_issue
from .result import new_result
from .scope import analyze_scope


@dataclass
class PRContext:
    """The (untrusted) text of a pull request and of the issues it references."""

    title: str = ""
    body: str = ""
    #: label ("#12" or "issue-file") -> issue body
    issues: dict[str, str] = field(default_factory=dict)
    repo: str | None = None
    number: int | None = None


def _setup(projects: list, wt: Path, env: Mapping[str, str]) -> str | None:
    errors = setup_projects(projects, wt, env)
    return "; ".join(errors) if errors else None


def run_check(
    repo: str | Path,
    base: str,
    head: str,
    cfg: Config,
    ctx: PRContext,
    llm: ClaimExtractor | None = None,
    environ: Mapping[str, str] | None = None,
    skip_tests: bool = False,
) -> dict[str, Any]:
    """Run every check and return an (unvalidated) result document."""
    repo = Path(repo)
    base_sha = gitmod.resolve_ref(repo, base)
    head_sha = gitmod.resolve_ref(repo, head)
    mb = gitmod.merge_base(repo, base_sha, head_sha)
    notes: list[str] = []

    projects = build_projects(cfg, repo)
    # A single implicit root project is used directly; several projects are
    # seen through one runner-like view over repository-relative paths.
    runner = projects[0].runner if len(projects) == 1 and projects[0].is_root else ProjectSet(projects)
    changed = gitmod.changed_files(repo, mb, head_sha)

    claims = C.extract_claims(ctx.title, ctx.body)
    if llm is not None:
        try:
            claims = C.merge_claims(claims, llm.extract(ctx.title, ctx.body))
        except (LLMError, OSError, ValueError) as exc:
            notes.append(f"LLM claim extraction skipped: {exc}")

    scope = analyze_scope(changed, f"{ctx.title}\n{ctx.body}", runner.is_test_file, cfg.large_diff_threshold)
    env = scrub_env(environ, extra=cfg.test_env)

    tests: TestCheck | None = None
    repros: list[IssueRepro] = []
    bench: BenchmarkResult | None = None
    want_old = any(c.type in (C.NO_BEHAVIOR_CHANGE, C.BACKWARDS_COMPATIBLE) for c in claims)
    want_bench = bool(cfg.benchmark_command) and any(
        c.type == C.PERFORMANCE and c.details.get("metric") == "time" for c in claims
    )

    with gitmod.worktree(repo, mb, prefix="proofcheck-base-") as base_wt, gitmod.worktree(repo, head_sha, prefix="proofcheck-head-") as head_wt:
        for wt in (base_wt, head_wt):
            err = _setup(projects, wt, env)
            if err:
                notes.append(err)
        if cfg.repro_enabled:
            for label, body in list(ctx.issues.items())[: max(cfg.max_issues, 0)]:
                repros.append(reproduce_issue(label, body, base_wt, head_wt, cfg.repro_python, cfg.repro_timeout, environ))
        if want_bench:
            assert cfg.benchmark_command is not None
            bench = run_benchmark(cfg.benchmark_command, base_wt, head_wt, cfg.benchmark_runs, env, cfg.benchmark_timeout)
        if skip_tests:
            tests = TestCheck(status=STATUS_SKIPPED, runner=runner.name)
        else:
            # Runs last: it writes head versions of the test files into base_wt.
            tests = run_projects_fail_to_pass(projects, repo, mb, head_sha, changed, base_wt, head_wt, env,
                                              check_old_tests=want_old, multi=bool(cfg.projects))

    # Issues referenced but not available (e.g. cross-repo or fetch failed).
    for num in C.referenced_issue_numbers(claims, ctx.repo):
        if f"#{num}" not in ctx.issues and not any(k.startswith("issue-file") for k in ctx.issues) and cfg.repro_enabled and ctx.repo:
            repros.append(IssueRepro(f"#{num}", "not_found", note="Issue text was not available to proofcheck."))

    assessments = assess_claims(claims, tests if tests and tests.status != STATUS_SKIPPED else None, repros, bench)
    ablation = None
    if cfg.ablation.enabled and not skip_tests:
        from .ablation import ablation_scope_flag, run_ablation

        ablation = run_ablation(
            repo, mb, head_sha, changed, runner, tests, repros, ctx.issues, env, environ=environ,
            max_hunks=cfg.ablation.max_hunks, time_budget_s=cfg.ablation.time_budget_s,
            test_timeout=max(p.timeout for p in projects),
            repro_timeout=cfg.repro_timeout, python=cfg.repro_python, exclude=cfg.ablation.exclude,
            setup=lambda wt: _setup(projects, wt, env),
        )
        flag = ablation_scope_flag(ablation)
        if flag is not None:
            scope.flags.append(flag)
    return new_result(
        repo=ctx.repo,
        pr_number=ctx.number,
        base=base_sha,
        head=head_sha,
        merge_base=mb,
        verdict=verdict(assessments, tests),
        claims=[a.to_dict() for a in assessments],
        tests=tests.to_dict() if tests else None,
        repro=[r.to_dict() for r in repros],
        benchmark=bench.to_dict() if bench else None,
        scope=scope.to_dict(),
        notes=notes,
        config_source=cfg.source,
        ablation=ablation.to_dict() if ablation is not None else None,
    )


def context_from_event(event_path: str | None = None) -> PRContext:
    """Build a :class:`PRContext` from a GitHub ``pull_request`` event payload."""
    import json

    path = event_path or os.environ.get("GITHUB_EVENT_PATH")
    ctx = PRContext(repo=os.environ.get("GITHUB_REPOSITORY") or None)
    if not path or not os.path.exists(path):
        return ctx
    with open(path, encoding="utf-8") as fh:
        event = json.load(fh)
    pr = event.get("pull_request") or {}
    ctx.title = str(pr.get("title") or "")
    ctx.body = str(pr.get("body") or "")
    num = pr.get("number")
    if isinstance(num, int) and not isinstance(num, bool) and num > 0:
        ctx.number = num
    return ctx
