"""The three modes of the harness.

``online``  push each scenario to a real repository that has proofcheck
            installed, open an issue and a PR, wait for the proof card,
            check its data block, clean up (unless ``--keep-open``).
``local``   build the same two commits in a throwaway repository and run
            ``proofcheck run`` on them (no network, no token): an offline
            end-to-end check of the scenarios' expectations.
``dry-run`` build the commits locally and print what the online run would do.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from proofcheck.carddata import parse_card_data
from proofcheck.github_api import VERDICT_LABELS

from . import E2E_DIR, ROOT
from .expect import evaluate
from .github import GitHub, GitHubError, find_card
from .gitops import Git, GitError, Redactor, auth_env, build_branches, diffstat
from .report import Report, ScenarioResult, write_report
from .scenario import Scenario, ScenarioError, ScenarioSet, load_set, substitute

LOCAL_ISSUE_NUMBER = 2
LOCAL_PR_NUMBER = 1
LOCAL_REPO = "proofcheck-e2e/local"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Log:
    """Timestamped, redacted progress lines; also kept per scenario for the report."""

    def __init__(self, redact: Callable[[object], str] | None = None, stream: Any = None, quiet: bool = False) -> None:
        self.redact = redact or Redactor()
        self.stream = stream if stream is not None else sys.stdout
        self.quiet = quiet

    def __call__(self, msg: object, result: ScenarioResult | None = None) -> None:
        line = self.redact(msg)
        stamped = f"[{time.strftime('%H:%M:%S')}]" + (f" [{result.name}]" if result else "") + f" {line}"
        if result is not None:
            result.log.append(stamped)
        if not self.quiet:
            print(stamped, file=self.stream, flush=True)


@dataclass
class Options:
    repo: str = ""
    timeout: float = 3600
    keep_open: bool = False
    poll_initial: float = 15
    poll_max: float = 60
    bot_login: str | None = None
    prefix: str = "proofcheck-e2e"
    server_url: str = "https://github.com"
    remote_url: str | None = None
    label_wait: float = 90
    require_agent: bool = False


def _expectation_summary(exp: dict[str, Any]) -> str:
    return ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in exp.items())


# --------------------------------------------------------------------------- local / dry run


def _local_repo(tmp: Path, redact: Callable[[object], str]) -> Git:
    git = Git(tmp / "repo", redact=redact)
    git.init()
    (git.cwd / "README.md").write_text("Stand-in for the target repository's default branch.\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    git("branch", "-M", "main")
    return git


def _proofcheck_cmd() -> tuple[list[str], dict[str, str]]:
    """How to run this checkout's proofcheck: ``python -m proofcheck`` with ``src`` first on PYTHONPATH."""
    env = dict(os.environ)
    src = ROOT / "src"
    if (src / "proofcheck").is_dir():
        env["PYTHONPATH"] = str(src) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return [sys.executable, "-m", "proofcheck"], env


def local_scenario(git: Git, sset: ScenarioSet, s: Scenario, out_dir: Path, log: Log, runner: str | None = None,
                   ablation: bool = True, timeout: float = 900) -> ScenarioResult:
    """Build the scenario's commits in ``git``'s repository and run ``proofcheck run`` on them."""
    r = ScenarioResult(name=s.name, description=s.description)
    t0 = time.monotonic()
    try:
        r.base_branch, r.head_branch = f"e2e/{s.name}-base", f"e2e/{s.name}"
        r.base_sha, r.head_sha = build_branches(git, sset, s, "main", r.base_branch, r.head_branch)
        issue = LOCAL_ISSUE_NUMBER if s.has_issue else None
        r.issue_number, r.pr_number = issue, LOCAL_PR_NUMBER
        out_dir.mkdir(parents=True, exist_ok=True)
        ctx = {"repo": LOCAL_REPO, "number": LOCAL_PR_NUMBER, "title": substitute(s.title, issue),
               "body": substitute(s.body, issue), "issues": {f"#{issue}": s.issue_body} if issue else {}}
        (out_dir / "context.json").write_text(json.dumps(ctx, indent=2), encoding="utf-8")
        cmd, env = _proofcheck_cmd()
        cmd += ["run", "--repo", str(git.cwd), "--base", r.base_sha, "--head", r.head_sha,
                "--context-file", str(out_dir / "context.json"), "--out", str(out_dir / "card.md"),
                "--json", str(out_dir / "result.json"), "--ablation" if ablation else "--no-ablation"]
        if runner:
            cmd += ["--runner", runner]
        log(f"proofcheck run {r.base_sha[:8]}..{r.head_sha[:8]}" + (f" (runner {runner})" if runner else ""), r)
        res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
        if res.returncode != 0 or not (out_dir / "card.md").is_file():
            raise RuntimeError(f"proofcheck run exited {res.returncode}: {(res.stderr or res.stdout)[-1500:]}")
        card = (out_dir / "card.md").read_text(encoding="utf-8")
        r.data = parse_card_data(card)
        r.failures = evaluate(s.expectations("local"), r.data, card=card, labels=None)
        r.status = "pass" if not r.failures else "fail"
        log(f"verdict {r.data.get('verdict') if r.data else '?'}: {r.status}" +
            (f" ({len(r.failures)} failed expectation(s))" if r.failures else ""), r)
        for f in r.failures:
            log(f"  ✗ {f}", r)
    except (GitError, ScenarioError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        r.status, r.error = "error", log.redact(exc)
        log(f"error: {r.error}", r)
    r.duration_s = time.monotonic() - t0
    return r


def run_local(sset: ScenarioSet, scenarios: Sequence[Scenario], log: Log, runner: str | None = None,
              keep_dir: Path | None = None, ablation: bool = True) -> Report:
    report = Report(mode="local", scenario_set=sset.name, started=_now())
    report.notes.append("Offline run: label, trusted-warning and agent expectations are only checked online; "
                        "[expected_local] overrides apply.")
    tmp = Path(keep_dir) if keep_dir else Path(tempfile.mkdtemp(prefix="proofcheck-e2e-local-"))
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        git = _local_repo(tmp, log.redact)
        for s in scenarios:
            report.results.append(local_scenario(git, sset, s, tmp / "out" / s.name, log, runner=runner, ablation=ablation))
    finally:
        if keep_dir is None:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            log(f"kept the local repository and outputs in {tmp}")
    report.finished = _now()
    return report


def run_dry(sset: ScenarioSet, scenarios: Sequence[Scenario], log: Log, opts: Options) -> Report:
    report = Report(mode="dry-run", scenario_set=sset.name, repo=opts.repo or None, started=_now())
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    tmp = Path(tempfile.mkdtemp(prefix="proofcheck-e2e-dry-"))
    try:
        git = _local_repo(tmp, log.redact)
        for s in scenarios:
            r = ScenarioResult(name=s.name, description=s.description)
            report.results.append(r)
            r.base_branch, r.head_branch = f"{opts.prefix}/{s.name}-base-{stamp}", f"{opts.prefix}/{s.name}-{stamp}"
            try:
                base, head = build_branches(git, sset, s, "main", r.base_branch, r.head_branch)
            except (GitError, ScenarioError) as exc:
                r.status, r.error = "error", log.redact(exc)
                log(f"error: {r.error}", r)
                continue
            r.status = "planned"
            target = opts.repo or "OWNER/NAME"
            log(f"would push {r.base_branch} (default branch + the set's base files) and {r.head_branch} to {target}", r)
            for row in diffstat(git, base, head):
                log(f"  change: {row}", r)
            if s.has_issue:
                log(f"would open issue {s.issue_title!r} ({len(s.issue_body or '')} chars)", r)
            log(f"would open {'draft ' if s.draft else ''}PR {substitute(s.title, 123)!r} into {r.base_branch}"
                + (" (the new issue's number is shown as 123)" if s.has_issue else ""), r)
            for line in substitute(s.body, 123).strip().splitlines()[:6]:
                log(f"  | {line}", r)
            if s.labels:
                log(f"would add labels {s.labels}", r)
            log("would wait for the proof card whose data block names the pushed head"
                + (f", then up to {s.agent_grace}s for the agent section" if s.wait_agent else ""), r)
            log(f"would check: {_expectation_summary(s.expectations('online', opts.require_agent))}", r)
            log(f"would close the PR{' and the issue' if s.has_issue else ''} and delete both branches"
                if not opts.keep_open else "would leave everything open (--keep-open)", r)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    report.finished = _now()
    return report


# --------------------------------------------------------------------------- online


@dataclass
class _Run:
    scenario: Scenario
    result: ScenarioResult
    t0: float
    card: str | None = None
    first_seen: float | None = None
    issue_created: bool = False
    pr_created: bool = False
    branches_pushed: tuple[str, ...] = ()


class OnlineRunner:
    """Opens every scenario's PR first, then polls them together, then cleans up."""

    def __init__(self, sset: ScenarioSet, scenarios: Sequence[Scenario], gh: GitHub, opts: Options, log: Log,
                 workdir: Path, token: str = "", clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.sset, self.scenarios, self.gh, self.opts, self.log = sset, list(scenarios), gh, opts, log
        self.workdir, self.token, self.clock, self.sleep = Path(workdir), token, clock, sleep
        self.runs: list[_Run] = []
        self.leftover_branches: list[str] = []
        self.git: Git | None = None
        self.report = Report(mode="online", scenario_set=sset.name, repo=opts.repo, started=_now())

    # -- setup -------------------------------------------------------------------------
    def _clone(self, default_branch: str) -> Git:
        env = auth_env(self.token, self.opts.server_url) if self.token else {}
        git = Git(self.workdir / "repo", env=env, redact=self.log.redact)
        git.init()
        remote = self.opts.remote_url or f"{self.opts.server_url.rstrip('/')}/{self.opts.repo}.git"
        git("remote", "add", "origin", remote)
        self.log(f"fetching {self.opts.repo}@{default_branch}")
        git("fetch", "-q", "--depth=1", "--no-tags", "origin",
            f"+refs/heads/{default_branch}:refs/remotes/origin/{default_branch}")
        return git

    def _open(self, git: Git, run: _Run, default_branch: str, stamp: str) -> None:
        s, r = run.scenario, run.result
        r.base_branch, r.head_branch = f"{self.opts.prefix}/{s.name}-base-{stamp}", f"{self.opts.prefix}/{s.name}-{stamp}"
        r.base_sha, r.head_sha = build_branches(git, self.sset, s, f"refs/remotes/origin/{default_branch}",
                                                r.base_branch, r.head_branch)
        run.branches_pushed = (r.head_branch, r.base_branch)  # registered before the push: cleanup tolerates 404
        git("push", "-q", "origin", f"refs/heads/{r.base_branch}:refs/heads/{r.base_branch}",
            f"refs/heads/{r.head_branch}:refs/heads/{r.head_branch}")
        self.log(f"pushed {r.base_branch} ({r.base_sha[:8]}) and {r.head_branch} ({r.head_sha[:8]})", r)
        issue = None
        if s.has_issue:
            data = self.gh.create_issue(s.issue_title or s.name, s.issue_body or "")
            issue = int(data["number"])
            r.issue_number, run.issue_created = issue, True
            self.log(f"opened issue #{issue}", r)
        pr = self.gh.create_pull(substitute(s.title, issue), substitute(s.body, issue), r.head_branch, r.base_branch, s.draft)
        r.pr_number, r.pr_url, run.pr_created = int(pr["number"]), str(pr.get("html_url") or ""), True
        self.log(f"opened PR #{r.pr_number} {r.pr_url}", r)
        pr_head = str((pr.get("head") or {}).get("sha") or "")
        if pr_head and pr_head != r.head_sha:
            raise GitHubError(f"PR #{r.pr_number} head is {pr_head[:12]}, expected {r.head_sha[:12]}")
        if s.labels:
            self.gh.add_labels(r.pr_number, s.labels)
            self.log(f"added labels {s.labels}", r)

    # -- waiting -------------------------------------------------------------------------
    def _finish(self, run: _Run, comment: dict[str, Any], data: dict[str, Any]) -> None:
        r, s = run.result, run.scenario
        run.card = str(comment.get("body") or "")
        r.data = data
        # The comment step sets the label right after upserting the card: wait
        # (briefly) until the label matching the card's verdict is there.
        want = VERDICT_LABELS.get(str(data.get("verdict"))) if "label" in s.expected else None
        deadline = self.clock() + (self.opts.label_wait if want else 0)
        while True:
            try:
                r.labels = sorted(self.gh.issue_labels(r.pr_number or 0))
            except GitHubError as exc:
                self.log(f"could not read labels: {exc}", r)
                break
            if not want or want in r.labels or self.clock() >= deadline:
                break
            self.sleep(5)
        r.failures = evaluate(s.expectations("online", self.opts.require_agent), data, card=run.card, labels=r.labels)
        r.status = "pass" if not r.failures else "fail"
        self.log(f"card found: verdict {data.get('verdict')}, agent {(data.get('agent') or {}).get('status', 'none')}: "
                 f"{r.status}", r)
        for f in r.failures:
            self.log(f"  ✗ {f}", r)

    def _poll_once(self, run: _Run) -> bool:
        """Check one PR; True when it is finished."""
        r, s = run.result, run.scenario
        try:
            comments = self.gh.issue_comments(r.pr_number or 0)
        except GitHubError as exc:
            self.log(f"listing comments failed: {exc}", r)
            if exc.status in (401, 404):
                r.status, r.error = "error", str(exc)
                return True
            return False
        comment, data = find_card(comments, r.head_sha, self.opts.bot_login)
        if data is None:
            return False
        if not s.wait_agent or data.get("agent"):
            self._finish(run, comment, data)
            return True
        now = self.clock()
        if run.first_seen is None:
            run.first_seen = now
            run.card = str(comment.get("body") or "")
            r.data = data
            self.log(f"card found (verdict {data.get('verdict')}); waiting up to {s.agent_grace}s for the agent section", r)
        if now - run.first_seen >= s.agent_grace:
            self.log("no agent section within the grace period; checking the card as it is", r)
            self._finish(run, comment, data)
            return True
        return False

    def _wait_all(self) -> None:
        pending = [run for run in self.runs if run.result.status == "pending"]
        deadline = self.clock() + self.opts.timeout
        delay = self.opts.poll_initial
        if pending:
            self.log(f"waiting for {len(pending)} proof card(s) (timeout {self.opts.timeout:.0f}s)")
        while pending:
            for run in list(pending):
                if self._poll_once(run):
                    pending.remove(run)
            if not pending:
                break
            if self.clock() >= deadline:
                for run in pending:
                    r = run.result
                    r.status = "timeout"
                    r.error = f"no proof card for head {(r.head_sha or '')[:12]} within {self.opts.timeout:.0f}s"
                    if run.first_seen is not None and r.data is not None:
                        r.error = f"card found but no agent section within {self.opts.timeout:.0f}s"
                    try:
                        r.diagnostics = self.gh.runs_for_sha(r.head_sha or "")
                    except GitHubError as exc:
                        self.log(f"could not list workflow runs: {exc}", r)
                    self.log(r.error, r)
                    for d in r.diagnostics:
                        self.log(f"  workflow run {d.get('name')}: {d.get('status')}/{d.get('conclusion')}", r)
                break
            self.sleep(max(1.0, min(delay, deadline - self.clock())))
            delay = min(delay * 1.5, self.opts.poll_max)

    # -- cleanup -------------------------------------------------------------------------
    def _cleanup(self, run: _Run) -> None:
        r = run.result
        if self.opts.keep_open:
            if run.pr_created or run.issue_created or run.branches_pushed:
                r.cleanup.append("kept open (--keep-open)")
            return
        steps: list[tuple[str, Callable[[], Any]]] = []
        if run.pr_created and r.pr_number:
            steps.append((f"closed PR #{r.pr_number}", lambda: self.gh.close_pull(r.pr_number or 0)))
        if run.issue_created and r.issue_number:
            steps.append((f"closed issue #{r.issue_number}", lambda: self.gh.close_issue(r.issue_number or 0)))
        for branch in run.branches_pushed:
            steps.append((f"deleted branch {branch}", lambda b=branch: self.gh.delete_branch(b)))
        for label, fn in steps:
            try:
                out = fn()
                r.cleanup.append(label if out is not False else label + " (already gone)")
            except Exception as exc:  # cleanup must go on
                r.cleanup.append(f"FAILED: {label.replace('closed', 'close').replace('deleted', 'delete')}: {self.log.redact(exc)}")
                if label.startswith("deleted branch "):
                    self.leftover_branches.append(label[len("deleted branch "):])
            self.log(r.cleanup[-1], r)

    def _delete_leftover_branches(self) -> None:
        """Retry refused API branch deletions with one `git push --delete`; report what remains."""
        if not self.leftover_branches:
            return
        branches = list(dict.fromkeys(self.leftover_branches))
        if self.git is not None:
            try:
                self.git("push", "-q", "origin", "--delete", *branches)
                self.log(f"deleted {len(branches)} branch(es) with git push --delete after the API refused")
                return
            except GitError as exc:
                self.log(f"git push --delete also failed: {self.log.redact(exc)}")
        self.report.notes.append(
            f"{len(branches)} branch(es) could not be deleted (the token may not be allowed to delete refs). "
            "Delete them with: git push origin --delete " + " ".join(branches))

    def run(self) -> Report:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        for s in self.scenarios:
            res = ScenarioResult(name=s.name, description=s.description)
            self.runs.append(_Run(s, res, self.clock()))
            self.report.results.append(res)
        try:
            info = self.gh.get_repo()
            default = str(info.get("default_branch") or "main")
            git = self._clone(default)
            self.git = git
            for run in self.runs:
                try:
                    self._open(git, run, default, stamp)
                except (GitError, GitHubError, ScenarioError, KeyError, ValueError, TypeError) as exc:
                    run.result.status, run.result.error = "error", self.log.redact(exc)
                    self.log(f"error while opening: {run.result.error}", run.result)
            self._wait_all()
        except (GitError, GitHubError) as exc:
            self.report.notes.append(f"aborted: {self.log.redact(exc)}")
            self.log(f"aborted: {exc}")
            for run in self.runs:
                if run.result.status == "pending":
                    run.result.status, run.result.error = "error", "run aborted"
        finally:
            for run in self.runs:
                if run.result.status == "pending":
                    run.result.status, run.result.error = "error", "interrupted"
                self._cleanup(run)
                run.result.duration_s = self.clock() - run.t0
            self._delete_leftover_branches()
            self.report.finished = _now()
        return self.report


# --------------------------------------------------------------------------- CLI


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="e2e/run.py", description="proofcheck end-to-end harness (see e2e/README.md).")
    p.add_argument("--repo", help="Target repository OWNER/NAME (with proofcheck installed). Required online.")
    p.add_argument("--scenarios", required=True, help="Scenario set directory, e.g. e2e/scenarios/python-toy.")
    p.add_argument("--only", action="append", default=[], metavar="NAME",
                   help="Run only this scenario (repeatable or comma-separated).")
    p.add_argument("--keep-open", action="store_true", help="Leave PRs, issues and branches in place.")
    p.add_argument("--timeout", type=float, default=3600, help="Seconds to wait for all cards (default 3600).")
    p.add_argument("--dry-run", action="store_true", help="Build the commits locally and print what would happen.")
    p.add_argument("--local", action="store_true", help="Offline: run `proofcheck run` on the scenarios' commits locally.")
    p.add_argument("--runner", choices=["pytest", "unittest"], default=None, help="Local mode: override the test runner.")
    p.add_argument("--keep-local", metavar="DIR", default=None, help="Local mode: keep the repository and outputs in DIR.")
    p.add_argument("--report-dir", default=str(E2E_DIR), help="Where report.md / report.json go (default: e2e/).")
    p.add_argument("--require-agent", action="store_true",
                   help="The target runs the agent workflow: scenarios that wait for the agent fail without an agent section.")
    p.add_argument("--bot-login", default=None, help="Only accept cards by this login (default: any Bot account).")
    p.add_argument("--poll-interval", type=float, default=15, help="First polling interval in seconds (backs off to --max-poll-interval).")
    p.add_argument("--max-poll-interval", type=float, default=60)
    p.add_argument("--prefix", default="proofcheck-e2e", help="Branch name prefix (default proofcheck-e2e).")
    p.add_argument("--api-url", default="https://api.github.com")
    p.add_argument("--server-url", default="https://github.com")
    args = p.parse_args(argv)
    args.only = [n.strip() for item in args.only for n in item.split(",") if n.strip()]
    if args.dry_run and args.local:
        p.error("--dry-run and --local are exclusive")
    if not (args.dry_run or args.local) and not args.repo:
        p.error("--repo OWNER/NAME is required (or use --local / --dry-run)")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    redact = Redactor([token])
    log = Log(redact)
    try:
        sset = load_set(args.scenarios)
        scenarios = sset.select(args.only)
    except ScenarioError as exc:
        print(f"error: {redact(exc)}", file=sys.stderr)
        return 2
    log(f"scenario set {sset.name}: {', '.join(s.name for s in scenarios)}")
    opts = Options(repo=args.repo or "", timeout=args.timeout, keep_open=args.keep_open, poll_initial=args.poll_interval,
                   poll_max=args.max_poll_interval, bot_login=args.bot_login, prefix=args.prefix, server_url=args.server_url,
                   require_agent=args.require_agent)
    if args.dry_run:
        report = run_dry(sset, scenarios, log, opts)
        log(f"dry run: {len(scenarios)} scenario(s) planned; nothing was pushed or opened")
        return 0 if report.ok else 1
    if args.local:
        report = run_local(sset, scenarios, log, runner=args.runner,
                           keep_dir=Path(args.keep_local) if args.keep_local else None)
        stem = "report-local"
    else:
        if not token:
            print("error: set GITHUB_TOKEN (or GH_TOKEN) to a token that can push branches, open issues/PRs and "
                  "delete branches in the target repository", file=sys.stderr)
            return 2
        workdir = Path(tempfile.mkdtemp(prefix="proofcheck-e2e-"))
        try:
            gh = GitHub(args.repo, token, api_url=args.api_url, redact=redact)
            report = OnlineRunner(sset, scenarios, gh, opts, log, workdir, token=token).run()
        except GitHubError as exc:
            print(f"error: {redact(exc)}", file=sys.stderr)
            return 2
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        stem = "report"
    md, js = write_report(report, args.report_dir, stem)
    log(f"{'PASS' if report.ok else 'FAIL'}: {report.summary()}; wrote {md} and {js}")
    return 0 if report.ok else 1
