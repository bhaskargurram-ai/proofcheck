# proofcheck end-to-end harness

`e2e/` drives proofcheck the way a maintainer meets it: real pushes, a real
issue, a real pull request, the real workflows on GitHub, and the proof card
the comment workflow posts. It is not part of the installed package.

For each scenario the harness pushes two branches, opens an issue (if the
scenario has one) and a PR, waits for the proof card whose machine-readable
data block names the pushed head commit, checks the block against the
scenario's expectations, and then closes the PR and issue and deletes both
branches. Everything runs in parallel: all PRs are opened first, then polled
together.

```bash
# Offline: build every scenario's commits locally and run `proofcheck run` on them.
python e2e/run.py --scenarios e2e/scenarios/python-toy --local [--runner unittest]

# Show what an online run would push and open (no token, no network).
python e2e/run.py --scenarios e2e/scenarios/python-toy --dry-run

# Online, against a repository that has proofcheck installed.
export GITHUB_TOKEN=...            # see "Token" below; never printed
python e2e/run.py --repo OWNER/NAME --scenarios e2e/scenarios/python-toy \
    [--only real-fix,fake-fix] [--keep-open] [--timeout 3600] [--require-agent]
```

The online and local runs write `report.md` and `report.json` (online) or
`report-local.md` / `report-local.json` (local) into `e2e/` (change with
`--report-dir`). The exit code is 0 when every scenario passed, 1 when any
failed, errored or timed out, and 2 for usage errors.

Requirements: Python 3.10+, `git`, and `requests` for the online mode
(`pip install requests`). The harness imports `parse_card_data` from this
checkout's `src/` (or an installed proofcheck).

## Options

| Option | Meaning |
|---|---|
| `--repo OWNER/NAME` | Target repository (online mode). |
| `--scenarios DIR` | Scenario set, e.g. `e2e/scenarios/python-toy`. |
| `--only NAME` | Run only these scenarios (repeatable, or comma-separated). |
| `--keep-open` | Leave PRs, issues and branches in place for inspection. |
| `--timeout S` | Seconds to wait for all cards (default 3600). |
| `--dry-run` | Build the commits locally and print the plan. |
| `--local` | Offline end-to-end run with `proofcheck run`; `--runner`, `--keep-local DIR`. |
| `--require-agent` | The target runs the agent workflow: scenarios that wait for the agent fail if no agent section arrives. |
| `--bot-login LOGIN` | Only accept cards posted by this login (default: any Bot account, e.g. `github-actions[bot]`). |
| `--poll-interval`, `--max-poll-interval` | Polling starts at 15 s and backs off ×1.5 up to 60 s. |
| `--prefix` | Branch prefix (default `proofcheck-e2e`). |
| `--api-url`, `--server-url` | For GitHub Enterprise Server. |

## The target repository

Install proofcheck in a test repository with `proofcheck init` (add `--agent`
to exercise the agent scenarios), commit the generated workflows on the
default branch, and make sure:

- **The evidence workflow runs for PRs into any branch.** The harness opens
  each PR against its own base branch (below), not the default branch. The
  generated `proofcheck.yml` uses `on: pull_request` without a branch filter;
  if you added `branches: [main]`, add `proofcheck-e2e/**` too.
- **The toy package is testable.** The `python-toy` set adds a package at
  `e2e_toy/` with tests in `e2e_toy/tests/` (unittest-style, so pytest and
  stdlib unittest both run them). Either
  - the repository is a pytest (or unittest) project at the root: the
    default configuration finds `e2e_toy/tests/test_*.py`, and the toy imports
    work because proofcheck puts the worktree root on `PYTHONPATH`; or
  - the configuration declares the toy as its own project (proofcheck ≥ 1.0
    multi-project support):

    ```toml
    [[project]]
    name = "e2e-toy"
    path = "e2e_toy"
    runner = "pytest"
    ```

    Use `pytest` here: the tests import `e2e_toy.…`, which pytest resolves from
    the package layout (`e2e_toy/__init__.py`, `e2e_toy/tests/__init__.py`).
    This layout is not covered by the offline run in this repository.
  The configuration is read from the base commit, i.e. from the target's
  default branch.
- **Workflows on the default branch.** `proofcheck-comment.yml` and
  `proofcheck-agent.yml` are triggered by `workflow_run`, which GitHub only
  starts from workflow files on the default branch. They find the PR from the
  artifact and check it against `workflow_run.head_sha` and the API, so PRs
  into a non-default base branch are handled the same way as PRs into `main`.

### Token

Use a personal access token (classic: `repo` and `workflow` scopes;
fine-grained: Contents, Issues, Pull requests and **Workflows** read/write)
or a GitHub App installation token with the same permissions.

- Do **not** use the `GITHUB_TOKEN` of an Actions run: GitHub does not start
  workflows for pull requests opened with it, so no card would ever arrive.
- The `workflow-change` scenario pushes a file under `.github/workflows/`,
  which needs the `workflow` scope / Workflows permission. Without it that
  scenario errors at push time (the others are unaffected).
- With an App token the PR author is the App, which the agent's default
  `run-for: trusted` gate does not trust; the agent scenarios therefore add the
  `proofcheck:agent` opt-in label.

The token is passed to git through `GIT_CONFIG_*` environment variables as an
`http.extraheader` (never in a URL or on a command line), only ever sent in the
`Authorization` header of API requests, and redacted (with its base64 forms)
from every log line, error and report.

## How a scenario becomes a PR

1. Fetch the default branch (shallow).
2. Branch `proofcheck-e2e/<scenario>-base-<timestamp>` from it and commit the
   set's `_base/files/` overlay (the toy package with its bugs and existing
   tests). The commit is made even if the overlay changes nothing.
3. Branch `proofcheck-e2e/<scenario>-<timestamp>` from that and commit the
   scenario's change.
4. Push both branches, open the issue (if any), open the PR from the second
   branch **into the first**, so the PR's diff is exactly the scenario change
   and nothing has to be merged into the target's default branch first.
5. Poll the PR's comments for a proofcheck card (by a Bot account) whose data
   block's `head` is the pushed commit. With `[wait] agent = true` it keeps
   polling until the card has an agent section or `agent_grace` seconds pass.
   Then it waits briefly for the verdict label.
6. Check the expectations, then (unless `--keep-open`) close the PR and the
   issue and delete both branches. Cleanup runs in a `finally` block, also on
   errors, timeouts and Ctrl-C. When no card arrives, the report lists the
   workflow runs for the head commit.

The harness never writes outside the set's `allowed_paths` (checked when
loading), never follows symlinks, runs git with argument lists (no shell),
with hooks disabled and with `GIT_DIR`/`GIT_WORK_TREE`-style variables
removed.

## Scenario sets

```
e2e/scenarios/<set>/
  set.toml               optional: description, allowed_paths (default ["e2e_toy/*"])
  _base/files/...        committed on the base branch
  <scenario>/            name: lowercase letters, digits, dashes
    scenario.toml
    files/...            overlay committed on the head branch, or
    patch.diff           a `git diff` applied with `git apply --index`
    issue.md             optional issue body (needs [issue] title)
```

`scenario.toml`:

```toml
description = "What this scenario demonstrates."
title = "Fix word_count with repeated whitespace"   # PR title; {issue} is replaced
body = """
Fixes #{issue}.
"""                                                  # PR body; only {issue} is substituted
draft = false
labels = ["proofcheck:agent"]                       # added to the PR after opening
delete = ["e2e_toy/old.py"]                         # files removed in the head commit

[issue]
title = "word_count counts empty words"             # body: issue.md

[wait]
agent = true          # keep polling for the agent section
agent_grace = 1200    # ...for at most this many seconds after the first card

[expected]
verdict = "evidence"                      # a string or a list of allowed values
label = "proofcheck: verified"            # "" = no proofcheck label
tests_status = "evidence"                 # evidence|no_evidence|fail_on_head|no_tests|error|skipped|none
min_fail_to_pass = 1                      # also max_fail_to_pass, min/max_pass_to_pass, min/max_fail_on_head
ablation_status = "ran"
min_ablation_required = 1                 # also max_ablation_required, min/max_ablation_not_exercised
agent_status = ["verified (agent test)", "no evidence", "none"]
flags = ["workflow"]                      # scope flag kinds that must be present
absent_flags = ["binary"]
repro = ["behavior_changed"]              # issue snippet statuses that must be present
warnings = true                           # the comment workflow's trusted warning (workflow/harness files changed)
claim_count = 1
claims = [{ type = "fixes_issue", status = "verified", verified_by = "agent", text_contains = "#" }]
card_contains = ["Diff evidence map"]
card_not_contains = ["<img"]

[expected_local]
# Overrides for --local. label, warnings and agent_status are online-only and
# are dropped in local mode unless set here.
verdict = "no_evidence"
```

`agent_status` takes the data block's values (`evidence`, `weak_evidence`,
`no_evidence`, `inconclusive`, `error`, `skipped`, `none` when there is no agent
section) or the card's wording (`verified (agent test)`, `weak`, `no evidence`,
`absent`). Missing test or ablation sections count as zero.

## The `python-toy` set

| Scenario | Change | Expected |
|---|---|---|
| `real-fix` | fixes `word_count`, adds a regression test; issue has a reproducer | `evidence`; fixes and adds-tests verified; ≥1 fail→pass; reproducer behaviour changed; every hunk required |
| `fake-fix` | rewrites `clamp` without fixing it, adds a test that passes on base too | `no_evidence`; fixes unverified; pass→pass only |
| `smuggled-hunk` (patch) | real `clamp` fix + test, plus a telemetry URL in `settings.py` | `evidence`; ≥1 hunk required, ≥1 not exercised; `unexercised` scope note |
| `agent-no-test` | real `chunk` fix, no test | offline `no_evidence`; online with the agent: `verified (agent test)` or `no evidence` |
| `no-behaviour-change` | "pure refactor, no behaviour change" that changes `slugify`'s output and the tests | `contradicted` |
| `prompt-injection` | comment-only change; issue and PR text with instructions to reviewers/LLMs, HTML, a link, an @mention and forged data blocks | `no_evidence`; nothing rendered; the harness reads the real data block |
| `workflow-change` | adds a manual-only no-op workflow `.github/workflows/proofcheck-e2e-dummy.yml` | `no_evidence`; `workflow` scope flag; trusted warning on the card |

The same expectations, minus the online-only ones, are checked offline by
`tests/test_e2e_scenarios.py`, which also takes `agent-no-test` through
`proofcheck agent propose`/`verify` with a scripted model.
