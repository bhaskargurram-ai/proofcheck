"""``proofcheck agent ...`` commands (experimental)."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

import click

from .. import git as gitmod
from ..config import Config, ConfigError, load_config_file, load_config_from_ref
from ..proc import scrub_env
from ..projects import Project, build_projects
from ..runners import RUNNER_NAMES
from . import VARIANTS
from .config import AgentConfig, AgentConfigError, load_agent_config_file, load_agent_config_from_ref
from .llm_tools import LLMConfigError, client_from_env

MAX_RESULT_BYTES = 5_000_000


def _load_configs(repo: str, base: str, config_path: str | None) -> tuple[Config, AgentConfig]:
    try:
        if config_path:
            return load_config_file(config_path), load_agent_config_file(config_path)
        ref = gitmod.resolve_ref(repo, base)
        return load_config_from_ref(repo, ref), load_agent_config_from_ref(repo, ref)
    except (ConfigError, AgentConfigError, ValueError, gitmod.GitError) as exc:
        raise click.ClickException(f"config error: {exc}") from exc


def _projects(cfg: Config, repo: str, runner: str | None, test_command: str | None) -> list[Project]:
    """The configured test projects; ``--runner``/``--test-command`` override the root project's."""
    if runner:
        cfg.runner = runner
    if test_command:
        cfg.test_command = shlex.split(test_command)
    try:
        return build_projects(cfg, repo)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _apply_overrides(acfg: AgentConfig, variants: str | None, max_tool_calls: int | None, when: str | None,
                     max_rounds: int | None = None) -> None:
    if variants:
        if variants.isdigit():
            n = int(variants)
            if not 1 <= n <= len(VARIANTS):
                raise click.BadParameter(f"must be 1-{len(VARIANTS)}", param_hint="--variants")
            acfg.variants = list(VARIANTS[:n])
        else:
            names = [v.strip() for v in variants.split(",") if v.strip()]
            bad = [v for v in names if v not in VARIANTS]
            if bad or not names:
                raise click.BadParameter(f"unknown variant(s) {', '.join(bad)}; choose from {', '.join(VARIANTS)}",
                                         param_hint="--variants")
            acfg.variants = names
    if max_tool_calls is not None:
        acfg.max_tool_calls = max_tool_calls
    if when:
        acfg.when = when
    if max_rounds is not None:
        acfg.max_rounds = max_rounds


def _read_result(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    from ..result import ResultValidationError, validate_result

    p = Path(path)
    if p.stat().st_size > MAX_RESULT_BYTES:
        raise click.ClickException("result.json is too large")
    try:
        return validate_result(json.loads(p.read_text(encoding="utf-8")))
    except (ValueError, ResultValidationError) as exc:
        raise click.ClickException(f"invalid result.json: {exc}") from exc


def _pr_text(context_file: str | None, pr_body_file: str | None, title: str | None,
             issue_body_file: tuple[str, ...]) -> tuple[str, str, dict[str, str]]:
    from ..cli import _read_text, _split_title_body, load_context

    pr_title, pr_body, issues = "", "", {}
    if context_file:
        ctx = load_context(context_file)
        pr_title, pr_body, issues = ctx.title, ctx.body, dict(ctx.issues)
    if pr_body_file:
        t, b = _split_title_body(_read_text(pr_body_file))
        pr_title, pr_body = t or pr_title, b
    if title is not None:
        pr_title = title
    for f in issue_body_file:
        issues["issue-file" if len(issue_body_file) == 1 else f"issue-file:{Path(f).name}"] = _read_text(f)
    return pr_title, pr_body, issues


def _llm_factory(cfg: Config, acfg: AgentConfig):
    def factory():
        try:
            return client_from_env(provider=cfg.llm.provider, model=cfg.llm.model, base_url=cfg.llm.base_url,
                                   timeout=acfg.request_timeout, max_retries=acfg.max_retries,
                                   max_output_tokens=acfg.max_output_tokens, temperature=acfg.temperature)
        except LLMConfigError as exc:
            raise click.ClickException(f"LLM not configured: {exc}") from exc
    return factory


def _secrets() -> list[str]:
    return [v for k, v in os.environ.items() if k.startswith("PROOFCHECK_LLM_API_KEY") and v]


def _minimal_result(repo: str, base: str, head: str, source: str) -> dict[str, Any]:
    from ..result import new_result

    b, h = gitmod.resolve_ref(repo, base), gitmod.resolve_ref(repo, head)
    return new_result(repo=None, pr_number=None, base=b, head=h, merge_base=gitmod.merge_base(repo, b, h),
                      verdict="no_evidence", claims=[], tests=None, repro=[], benchmark=None, scope=None, notes=[],
                      config_source=source)


def _write_outputs(result: dict[str, Any], json_path: str | None, out_path: str | None) -> dict[str, Any]:
    from ..card import render_card
    from ..result import validate_result

    result = validate_result(result)
    if json_path:
        Path(json_path).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    card = render_card(result)
    if out_path:
        Path(out_path).write_text(card, encoding="utf-8")
    else:
        click.echo(card)
    return result


def _disabled(acfg: AgentConfig, enable: bool) -> bool:
    if acfg.enabled or enable:
        return False
    click.echo("agent: disabled ([agent] enabled = false in the base commit's config); pass --enable to run anyway", err=True)
    return True


_common = [
    click.option("--repo", default=".", show_default=True, type=click.Path(exists=True, file_okay=False), help="Git repository."),
    click.option("--base", required=True, help="Base ref (e.g. origin/main or a SHA)."),
    click.option("--head", default="HEAD", show_default=True, help="Head ref (the PR)."),
    click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False),
                 help="Config file. Default: proofcheck.toml / [tool.proofcheck] read from the BASE commit."),
    click.option("--runner", type=click.Choice(list(RUNNER_NAMES)), default=None,
                 help="Override the root project's test runner ([[project]] tables keep theirs)."),
    click.option("--test-command", default=None, help="Override the test command (never run through a shell)."),
]

_text_opts = [
    click.option("--context-file", type=click.Path(exists=True, dir_okay=False), default=None,
                 help="PR context written by `proofcheck context` (title, body, issue bodies)."),
    click.option("--pr-body-file", type=click.Path(exists=True, dir_okay=False), help="File with the PR description."),
    click.option("--title", default=None, help="PR title."),
    click.option("--issue-body-file", type=click.Path(exists=True, dir_okay=False), multiple=True,
                 help="File with the issue text (repeatable)."),
    click.option("--variants", default=None, help=f"Number of variants (1-{len(VARIANTS)}) or a comma list of: {', '.join(VARIANTS)}."),
    click.option("--max-tool-calls", type=click.IntRange(1, 100), default=None, help="Tool-call budget per variant."),
    click.option("--when", type=click.Choice(["missing-evidence", "always"]), default=None,
                 help="With --result: only work on 'fixes' claims that lack evidence (default), or always."),
    click.option("--enable", is_flag=True, help="Run even if [agent] enabled = false in the config."),
]


def _apply(opts):
    def deco(f):
        for o in reversed(opts):
            f = o(f)
        return f
    return deco


@click.group("agent")
def agent() -> None:
    """Experimental: LLM-written reproduction tests, verified by execution.

    `propose` (needs PROOFCHECK_LLM_* env; never executes repository code),
    `verify` (no LLM; runs the candidates on base and head), and `run`
    (both plus refinement rounds; local/trusted use only). See docs/AGENT.md.
    """


@agent.command("propose")
@_apply(_common)
@_apply(_text_opts)
@click.option("--result", "result_path", type=click.Path(exists=True, dir_okay=False), default=None,
              help="result.json from `proofcheck run`: decides which 'fixes' claim needs evidence.")
@click.option("--out-dir", required=True, type=click.Path(file_okay=False), help="Candidates directory to write.")
def propose_cmd(repo, base, head, config_path, runner, test_command, context_file, pr_body_file, title, issue_body_file,
                variants, max_tool_calls, when, enable, result_path, out_dir) -> None:
    """Ask the LLM for reproduction-test candidates (read-only tools; nothing is executed)."""
    from .propose import Proposal, _base_manifest, propose, write_proposal

    cfg, acfg = _load_configs(repo, base, config_path)
    _apply_overrides(acfg, variants, max_tool_calls, when)
    projects = _projects(cfg, repo, runner, test_command)
    result = _read_result(result_path)
    if _disabled(acfg, enable):
        b, h = gitmod.resolve_ref(repo, base), gitmod.resolve_ref(repo, head)
        m = _base_manifest(b, h, gitmod.merge_base(repo, b, h), acfg)
        m.update(status="skipped", reason="the agent is disabled in the configuration")
        path = write_proposal(out_dir, Proposal(m, [], None))  # type: ignore[arg-type]
        click.echo(f"wrote {path} (skipped)", err=True)
        return
    pr_title, pr_body, issues = _pr_text(context_file, pr_body_file, title, issue_body_file)
    try:
        proposal = propose(repo, base, head, agent_cfg=acfg, runner=projects[0].runner, llm_factory=_llm_factory(cfg, acfg),
                           pr_title=pr_title, pr_body=pr_body, issues=issues, result=result, secrets_=_secrets(),
                           log=lambda s: click.echo(s, err=True), projects=projects)
    except gitmod.GitError as exc:
        raise click.ClickException(str(exc)) from exc
    path = write_proposal(out_dir, proposal)
    m = proposal.manifest
    n = sum(c["status"] == "submitted" for c in m["candidates"])
    usage = m["usage"]
    click.echo(f"wrote {path}: {m['status']}{' (' + m['reason'] + ')' if m['reason'] else ''}; {n} candidate(s); "
               f"{usage['requests']} requests, {usage['input_tokens'] + usage['cache_read_tokens'] + usage['cache_write_tokens']} "
               f"input / {usage['output_tokens']} output tokens"
               + (f", ~${m['cost_usd']:.4f}" if m.get("cost_usd") is not None else ""), err=True)


@agent.command("verify")
@_apply(_common)
@click.option("--candidates", "candidates_dir", required=True, type=click.Path(exists=True, file_okay=False),
              help="Directory written by `proofcheck agent propose`.")
@click.option("--result", "result_path", type=click.Path(exists=True, dir_okay=False), default=None,
              help="result.json from `proofcheck run` to merge into (recommended).")
@click.option("--json", "json_path", type=click.Path(dir_okay=False), default=None, help="Write the merged result.json here.")
@click.option("--out", "out_path", type=click.Path(dir_okay=False), default=None, help="Write the Markdown card here.")
@click.option("--timeout", type=click.FloatRange(1, 3600), default=None,
              help="Per-candidate, per-commit test timeout (default: agent.test_timeout; 120 s, or the project's timeout for cargo).")
def verify_cmd(repo, base, head, config_path, runner, test_command, candidates_dir, result_path, json_path, out_path, timeout) -> None:
    """Run the candidates on base and head and merge the outcome into result.json (no LLM)."""
    from .verify import merge_into_result, verify_candidates_dir

    cfg, acfg = _load_configs(repo, base, config_path)
    projects = _projects(cfg, repo, runner, test_command)
    result = _read_result(result_path) or _minimal_result(repo, base, head, cfg.source)
    env = scrub_env(None, extra=cfg.test_env)
    try:
        manifest, section = verify_candidates_dir(repo, base, head, candidates_dir, None, env, timeout,
                                                  projects=projects, agent_cfg=acfg)
        if result.get("head") != gitmod.resolve_ref(repo, head):
            raise click.ClickException("result.json was produced for a different head commit")
    except gitmod.GitError as exc:
        raise click.ClickException(str(exc)) from exc
    merged = _write_outputs(merge_into_result(result, section, manifest.get("target_issue")), json_path, out_path)
    click.echo(f"agent evidence: {section['status']}; verdict: {merged['verdict']}", err=True)


@agent.command("run")
@_apply(_common)
@_apply(_text_opts)
@click.option("--result", "result_path", type=click.Path(exists=True, dir_okay=False), default=None,
              help="result.json from `proofcheck run` to use and merge into.")
@click.option("--out-dir", type=click.Path(file_okay=False), default=None, help="Also write the candidates directory here.")
@click.option("--max-rounds", type=click.IntRange(0, 5), default=None, help="Refinement rounds (default: agent.max_rounds).")
@click.option("--json", "json_path", type=click.Path(dir_okay=False), default=None, help="Write the merged result.json here.")
@click.option("--out", "out_path", type=click.Path(dir_okay=False), default=None, help="Write the Markdown card here.")
def run_cmd(repo, base, head, config_path, runner, test_command, context_file, pr_body_file, title, issue_body_file,
            variants, max_tool_calls, when, enable, result_path, out_dir, max_rounds, json_path, out_path) -> None:
    """Propose + verify + refinement rounds in one process. LOCAL/TRUSTED USE ONLY."""
    from .loop import RUN_WARNING, run_agent
    from .verify import merge_into_result

    click.echo(RUN_WARNING, err=True)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        click.echo("WARNING: running inside GitHub Actions. Never use `agent run` on untrusted pull requests.", err=True)
    cfg, acfg = _load_configs(repo, base, config_path)
    _apply_overrides(acfg, variants, max_tool_calls, when, max_rounds)
    projects = _projects(cfg, repo, runner, test_command)
    result = _read_result(result_path)
    if _disabled(acfg, enable):
        return
    pr_title, pr_body, issues = _pr_text(context_file, pr_body_file, title, issue_body_file)
    env = scrub_env(None, extra=cfg.test_env)
    try:
        run = run_agent(repo, base, head, agent_cfg=acfg, runner=projects[0].runner, llm_factory=_llm_factory(cfg, acfg), env=env,
                        pr_title=pr_title, pr_body=pr_body, issues=issues, result=result, setup_command=cfg.setup_command,
                        secrets_=_secrets(), out_dir=out_dir, log=lambda s: click.echo(s, err=True), projects=projects)
    except gitmod.GitError as exc:
        raise click.ClickException(str(exc)) from exc
    base_result = result or _minimal_result(repo, base, head, cfg.source)
    merged = _write_outputs(merge_into_result(base_result, run.section, run.proposal.manifest.get("target_issue")),
                            json_path, out_path)
    click.echo(f"agent evidence: {run.section['status']} after {run.rounds} round(s); verdict: {merged['verdict']}", err=True)


@agent.command("needed")
@click.option("--json", "json_path", required=True, type=click.Path(exists=True, dir_okay=False), help="result.json from `proofcheck run`.")
@click.option("--when", type=click.Choice(["missing-evidence", "always"]), default="missing-evidence", show_default=True)
@click.option("--github-output", is_flag=True, help="Also append needed=... and reason=... to $GITHUB_OUTPUT.")
def needed_cmd(json_path, when, github_output) -> None:
    """Print 'true' if a 'fixes' claim lacks evidence (for the agent workflow's gate job)."""
    from .propose import pick_target

    result = _read_result(json_path) or {}
    claims = result.get("claims") or []
    target, skip = pick_target(claims, when, True)
    if when == "always" and target is None and not any(c.get("type") == "fixes_issue" for c in claims):
        skip = "the PR makes no 'fixes #N' claim"
    needed = not skip
    reason = skip or "a 'fixes' claim has no deterministic evidence"
    click.echo("true" if needed else "false")
    click.echo(f"agent needed: {needed} ({reason})", err=True)
    if github_output and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"needed={'true' if needed else 'false'}\n")
            fh.write("reason=" + reason.replace("\n", " ")[:200] + "\n")


def main() -> None:  # pragma: no cover - convenience for `python -m proofcheck.agent.cli`
    agent(prog_name="proofcheck agent")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
