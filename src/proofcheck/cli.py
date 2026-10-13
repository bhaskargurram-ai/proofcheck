"""Command line interface: ``proofcheck run`` and ``proofcheck comment``."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from . import __version__
from . import claims as C
from .card import harness_warning, render_card
from .check import PRContext, context_from_event, run_check
from .config import ConfigError, load_config_file, load_config_from_ref
from .github_api import GitHubError, env_client, harness_changes, pr_number_from_event, validate_number
from .llm import extractor_from_env
from .result import ResultValidationError, validate_result

#: Maximum size of result.json accepted by ``proofcheck comment``.
MAX_RESULT_BYTES = 5_000_000
#: Maximum characters kept from one issue body.
MAX_ISSUE_CHARS = 100_000


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _split_title_body(text: str) -> tuple[str, str]:
    """A PR body file may start with ``Title: ...`` or a ``# heading`` line."""
    lines = text.splitlines()
    if lines and (lines[0].lower().startswith("title:") or lines[0].startswith("# ")):
        first = lines[0].split(":", 1)[1] if lines[0].lower().startswith("title:") else lines[0][2:]
        return first.strip(), "\n".join(lines[1:]).strip()
    return "", text


def _fetch_issues(ctx: PRContext, limit: int) -> None:
    """Add bodies of issues referenced by ``fixes #N`` claims to ``ctx.issues``."""
    try:
        client = env_client()
        for num in C.referenced_issue_numbers(C.extract_claims(ctx.title, ctx.body), ctx.repo)[:limit]:
            issue = client.get_issue(num)
            if issue and not issue.get("pull_request"):
                ctx.issues[f"#{num}"] = str(issue.get("body") or "")[:MAX_ISSUE_CHARS]
    except (GitHubError, OSError, ValueError) as exc:
        click.echo(f"warning: could not fetch issues: {exc}", err=True)


def load_context(path: str) -> PRContext:
    """Load a context file written by ``proofcheck context``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise click.ClickException("context file must contain a JSON object")
    issues = data.get("issues") or {}
    if not isinstance(issues, dict):
        raise click.ClickException("context.issues must be an object")
    number = data.get("number")
    return PRContext(
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        issues={str(k)[:40]: str(v)[:MAX_ISSUE_CHARS] for k, v in list(issues.items())[:20]},
        repo=str(data["repo"]) if data.get("repo") else None,
        number=validate_number(number) if number is not None else None,
    )


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="proofcheck")
def main() -> None:
    """proofcheck: evidence, not authorship.

    Checks whether a pull request does what it claims by running its tests on
    the base and head commits, replaying issue reproducers and matching the
    PR's claims to the evidence.
    """


@main.command("run")
@click.option("--repo", default=".", show_default=True, type=click.Path(exists=True, file_okay=False), help="Git repository.")
@click.option("--base", required=True, help="Base ref (e.g. origin/main or a SHA).")
@click.option("--head", default="HEAD", show_default=True, help="Head ref (the PR).")
@click.option("--title", default=None, help="PR title (overrides the event payload / body file).")
@click.option("--pr-body-file", type=click.Path(exists=True, dir_okay=False), help="File with the PR description.")
@click.option("--issue-body-file", type=click.Path(exists=True, dir_okay=False), multiple=True,
              help="File with an issue body to reproduce (repeatable).")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False),
              help="Config file. Default: proofcheck.toml / [tool.proofcheck] read from the BASE commit.")
@click.option("--runner", type=click.Choice(["pytest", "unittest", "jest"]), default=None, help="Override the test runner.")
@click.option("--test-command", default=None, help="Override the test command (shell-style string, never run through a shell).")
@click.option("--event", "event_path", type=click.Path(dir_okay=False), default=None,
              help="GitHub event payload (default: $GITHUB_EVENT_PATH when set).")
@click.option("--context-file", type=click.Path(exists=True, dir_okay=False), default=None,
              help="PR context written by `proofcheck context` (title, body, issue bodies).")
@click.option("--fetch-issues/--no-fetch-issues", default=False, show_default=True,
              help="Fetch referenced issues from the GitHub API in this process. Prefer `proofcheck context` in CI, "
                   "so no token is present while PR code runs.")
@click.option("--llm/--no-llm", "use_llm", default=None, help="Use the optional LLM claim extractor (needs PROOFCHECK_LLM_* env).")
@click.option("--skip-tests", is_flag=True, help="Skip the fail-to-pass test run.")
@click.option("--ablation/--no-ablation", "ablation", default=None,
              help="Build the diff evidence map (revert each changed hunk and re-run the evidence). Default: from config (on).")
@click.option("--out", "out_path", type=click.Path(dir_okay=False), default=None, help="Write the Markdown card here.")
@click.option("--json", "json_path", type=click.Path(dir_okay=False), default=None, help="Write the JSON result here.")
@click.option("--fail-on", type=click.Choice(["never", "contradicted", "no-evidence"]), default="never", show_default=True,
              help="Exit non-zero for this verdict (or worse).")
def run_cmd(repo: str, base: str, head: str, title: str | None, pr_body_file: str | None, issue_body_file: tuple[str, ...],
            config_path: str | None, runner: str | None, test_command: str | None, event_path: str | None,
            context_file: str | None, fetch_issues: bool, use_llm: bool | None, skip_tests: bool, out_path: str | None, json_path: str | None,
            fail_on: str, ablation: bool | None = None) -> None:
    """Gather evidence for a pull request and write the proof card."""
    import shlex

    from . import git as gitmod

    try:
        if config_path:
            cfg = load_config_file(config_path)
        else:
            cfg = load_config_from_ref(repo, gitmod.resolve_ref(repo, base))
    except (ConfigError, ValueError, gitmod.GitError) as exc:
        raise click.ClickException(f"config error: {exc}") from exc
    if runner:
        cfg.runner = runner
    if test_command:
        cfg.test_command = shlex.split(test_command)
    if ablation is not None:
        cfg.ablation.enabled = ablation

    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    if context_file:
        ctx = load_context(context_file)
    elif event_path or in_actions:
        ctx = context_from_event(event_path)
    else:
        ctx = PRContext(repo=os.environ.get("GITHUB_REPOSITORY") or None)
    if pr_body_file:
        t, b = _split_title_body(_read_text(pr_body_file))
        ctx.title, ctx.body = t or ctx.title, b
    if title is not None:
        ctx.title = title
    for f in issue_body_file:
        ctx.issues["issue-file" if len(issue_body_file) == 1 else f"issue-file:{Path(f).name}"] = _read_text(f)

    if fetch_issues and ctx.repo:
        _fetch_issues(ctx, cfg.max_issues)

    llm = None
    if use_llm or (use_llm is None and cfg.llm.enabled):
        llm = extractor_from_env(model=cfg.llm.model, base_url=cfg.llm.base_url, provider=cfg.llm.provider, timeout=cfg.llm.timeout)
        if llm is None:
            click.echo("warning: LLM extraction requested but PROOFCHECK_LLM_PROVIDER/API_KEY/MODEL are not set", err=True)

    try:
        raw = run_check(repo, base, head, cfg, ctx, llm=llm, skip_tests=skip_tests)
    except gitmod.GitError as exc:
        raise click.ClickException(str(exc)) from exc
    result = validate_result(raw)
    card = render_card(result)
    if out_path:
        Path(out_path).write_text(card, encoding="utf-8")
    if json_path:
        Path(json_path).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if not out_path:
        click.echo(card)
    click.echo(f"proofcheck verdict: {result['verdict']}", err=True)

    bad = {"never": set(), "contradicted": {"contradicted"}, "no-evidence": {"contradicted", "no_evidence", "error"}}[fail_on]
    if result["verdict"] in bad:
        sys.exit(1)


@main.command("context")
@click.option("--event", "event_path", type=click.Path(dir_okay=False), default=None,
              help="GitHub event payload (default: $GITHUB_EVENT_PATH).")
@click.option("--max-issues", default=5, show_default=True, type=click.IntRange(0, 20), help="Maximum issues to fetch.")
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False), help="Where to write context.json.")
@click.option("--pr", "pr_number", default=None,
              help="Read the PR title/body from the GitHub API instead of the event payload (e.g. in a workflow_run job).")
def context_cmd(event_path: str | None, max_issues: int, out_path: str, pr_number: str | None = None) -> None:
    """Collect PR title/body and referenced issue bodies into a JSON file.

    Run this in a CI step *before* any PR code executes, with GITHUB_TOKEN
    available only to this step; then pass the file to `proofcheck run
    --context-file`, which needs no token.
    """
    if pr_number is not None:
        from .ci import context_from_api

        try:
            ctx = context_from_api(validate_number(pr_number))
        except GitHubError as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        ctx = context_from_event(event_path)
    if ctx.repo:
        _fetch_issues(ctx, max_issues)
    data = {"repo": ctx.repo, "number": ctx.number, "title": ctx.title, "body": ctx.body, "issues": ctx.issues}
    Path(out_path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    click.echo(f"wrote {out_path} ({len(ctx.issues)} issue(s))", err=True)


@main.command("comment")
@click.option("--json", "json_path", required=True, type=click.Path(exists=True, dir_okay=False), help="result.json from `proofcheck run`.")
@click.option("--pr", "pr_number", default=None, help="PR number (default: from result.json or the event payload).")
@click.option("--repo", "repo_slug", default=None, help="owner/repo (default: $GITHUB_REPOSITORY).")
@click.option("--bot-login", default=None, help="Only update existing cards authored by this login (default: any Bot account).")
@click.option("--expect-head-sha", default=None,
              help="Refuse to comment unless result.json and the PR's current head both match this commit "
                   "(pass github.event.workflow_run.head_sha).")
@click.option("--check-pr-files/--no-check-pr-files", default=True, show_default=True,
              help="Ask the GitHub API whether the PR changes workflows/test harness files and add a warning if so.")
@click.option("--dry-run", is_flag=True, help="Print the card instead of posting it.")
@click.option("--labels/--no-labels", default=False, show_default=True,
              help="Also set a `proofcheck: <verdict>` label on the PR (created if missing; stale ones removed).")
def comment_cmd(json_path: str, pr_number: str | None, repo_slug: str | None, bot_login: str | None,
                expect_head_sha: str | None, check_pr_files: bool, dry_run: bool, labels: bool = False) -> None:
    """Post or update the proof card comment on a pull request.

    The result file is treated as untrusted: it is validated against the
    schema and the card is re-rendered from it (card.md is never posted).
    """
    p = Path(json_path)
    if p.stat().st_size > MAX_RESULT_BYTES:
        raise click.ClickException("result.json is too large")
    try:
        result = validate_result(json.loads(p.read_text(encoding="utf-8")))
    except (ValueError, ResultValidationError) as exc:
        raise click.ClickException(f"invalid result.json: {exc}") from exc
    try:
        if pr_number is not None:
            number = validate_number(pr_number)
        elif result.get("pr_number"):
            number = validate_number(result["pr_number"])
        else:
            number = pr_number_from_event()
            if number is None:
                raise click.ClickException("PR number not given (--pr) and not found in result.json or the event payload")
    except GitHubError as exc:
        raise click.ClickException(str(exc)) from exc
    card = render_card(result)
    if dry_run:
        click.echo(card)
        return
    env = dict(os.environ)
    if repo_slug:
        env["GITHUB_REPOSITORY"] = repo_slug
    if not env.get("GITHUB_TOKEN"):
        raise click.ClickException("GITHUB_TOKEN is not set")
    try:
        client = env_client(env)
        if check_pr_files:
            changed = harness_changes(client.list_pull_files(number))
            if changed:
                card = render_card(result, trusted_warnings=[harness_warning(changed)])
        if expect_head_sha:
            expected = expect_head_sha.strip().lower()
            if result["head"] != expected:
                raise click.ClickException("result.json head commit does not match --expect-head-sha")
            pr = client.get_pull(number)
            if str((pr.get("head") or {}).get("sha", "")).lower() != expected:
                raise click.ClickException(f"PR #{number} head does not match --expect-head-sha (PR updated, or wrong PR number)")
        action, cid = client.upsert_card_comment(number, card, bot_login=bot_login)
    except GitHubError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{action} proof card comment {cid} on #{number}")
    if labels:
        try:
            added, removed = client.sync_verdict_label(number, result["verdict"])
        except GitHubError as exc:
            click.echo(f"warning: could not update labels: {exc}", err=True)
        else:
            click.echo(f"labels: added {added or 'none'}, removed {removed or 'none'}")


@main.command("summary")
@click.option("--json", "json_path", required=True, type=click.Path(exists=True, dir_okay=False), help="result.json to render.")
@click.option("--out", "out_path", type=click.Path(dir_okay=False), default=None,
              help="Append the card here (default: $GITHUB_STEP_SUMMARY, else stdout).")
def summary_cmd(json_path: str, out_path: str | None) -> None:
    """Render the proof card from result.json into the job summary.

    Needs no token, so it also works for pull requests from forks. The result
    is validated and the card re-rendered from it, as in `proofcheck comment`.
    """
    from .ci import write_summary

    try:
        dest = write_summary(json_path, out_path)
    except (ValueError, ResultValidationError, OSError) as exc:
        raise click.ClickException(f"could not write summary: {exc}") from exc
    if dest:
        click.echo(f"wrote proof card to {dest}", err=True)


@main.command("init")
@click.option("--repo", default=".", show_default=True, type=click.Path(exists=True, file_okay=False), help="Repository to set up.")
@click.option("--agent", is_flag=True, help="Also add the (experimental, opt-in) agent evidence workflow.")
@click.option("--force", is_flag=True, help="Overwrite existing files.")
@click.option("--ref", default=None, help="proofcheck release (tag or commit SHA) the workflows use. Default: this version's tag.")
@click.option("--runner", type=click.Choice(["pytest", "unittest", "jest"]), default=None, help="Override runner detection.")
def init_cmd(repo: str, agent: bool, force: bool, ref: str | None, runner: str | None) -> None:
    """Write caller workflows and a commented proofcheck.toml into a repository."""
    from .init_cmd import InitError, init_repo

    try:
        res = init_repo(repo, agent=agent, force=force, ref=ref, runner=runner)
    except InitError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Detected test runner: {res.runner} ({res.reason})")
    for path in res.written:
        click.echo(f"  wrote {path}")
    click.echo("")
    click.echo("Next steps:")
    for i, step in enumerate(res.next_steps, 1):
        click.echo(f"  {i}. {step}")


if __name__ == "__main__":  # pragma: no cover
    main()
