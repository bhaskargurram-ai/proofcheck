<p align="center">
  <img src="https://raw.githubusercontent.com/bhaskargurram-ai/proofcheck/main/docs/assets/banner.svg" alt="proofcheck: Evidence, not authorship." width="880">
</p>

<p align="center">
  <b>proofcheck checks whether a pull request does what it claims.</b><br>
  It runs the PR's tests before and after the change, replays the linked issue's reproducer,<br>
  and matches every claim in the description to the evidence it measured.
</p>

<p align="center">
  <a href="https://github.com/bhaskargurram-ai/proofcheck/actions/workflows/ci.yml"><img src="https://github.com/bhaskargurram-ai/proofcheck/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
  <a href="https://github.com/bhaskargurram-ai/proofcheck/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue" alt="Python 3.10 to 3.13">
  <a href="https://pypi.org/project/proofcheck-ai/"><img src="https://img.shields.io/pypi/v/proofcheck-ai" alt="PyPI: proofcheck-ai"></a>
  <img src="https://img.shields.io/badge/agent-experimental-orange" alt="Agent: experimental">
</p>

It makes no attempt to guess whether a human or an AI wrote the code. It only looks at evidence.

<p align="center">
  <img src="https://raw.githubusercontent.com/bhaskargurram-ai/proofcheck/main/docs/images/proof-card.png" alt="proofcheck 0.2 on one of its own pull requests: 'fixes #1' verified by 5 tests that fail on the base commit and pass on the PR head, plus the issue's reproducer; 'backwards compatible' marked as no evidence; the diff evidence map shows all 6 changed hunks are required by specific tests." width="760">
  <br><sub>A real card: proofcheck 0.2 run on one of its own pull requests (#2). Running it there also caught a bug in proofcheck itself: quoted example phrases in the description were being counted as claims.</sub>
</p>

> [!NOTE]
> **Status: 1.0.0.** Supported runners: pytest, stdlib `unittest` and Rust `cargo` (integration tests and inline `#[cfg(test)]` modules), including repositories with several projects in subfolders. Jest is experimental. The agent that writes reproduction tests is **experimental and off by default**. Feedback and runners for other ecosystems are welcome.

## Contents

- [Why evidence instead of AI detection?](#why-evidence-instead-of-ai-detection)
- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [What's on the card](#whats-on-the-card)
- [Diff evidence map](#diff-evidence-map)
- [Agent evidence (experimental)](#agent-evidence-experimental)
- [Security model](#security-model)
- [How proofcheck compares](#how-proofcheck-compares)
- [Configuration](#configuration)
- [CLI reference](#cli-reference)
- [FAQ](#faq)
- [Limitations](#limitations)
- [Roadmap](#roadmap)
- [Contributing](#contributing) · [License](#license)

## Why evidence instead of AI detection?

Maintainers increasingly get pull requests that *look* right: tidy code, a confident description, "Fixes #123", a new test. Some come from people and some from AI tools. Classifying a PR as AI-generated answers a question maintainers don't need answered, and false positives hurt real contributors.

proofcheck asks the questions a careful reviewer asks first:

- **Does the new test fail without the fix and pass with it?** A test that also passes on the base commit doesn't exercise the change, however plausible it looks. This is the *fail-to-pass* criterion used to validate fixes in benchmarks such as SWE-bench.
- **Does the issue's reproducer behave differently after the change?**
- **Which parts of the diff does that evidence actually depend on?**
- **Do the description's claims hold up?** "Fixes #N", "adds tests", "3x faster", "no behaviour change", "backwards compatible".
- **Does the diff touch anything surprising,** such as CI workflows, dependency manifests or binaries?

A good human PR and a good AI-assisted PR both pass. A sloppy PR from either source gets a card that shows exactly what is missing. ⚠️ means **no evidence**, not **wrong**.

<p align="center">
  <img src="https://raw.githubusercontent.com/bhaskargurram-ai/proofcheck/main/docs/assets/evidence-ladder.svg" alt="The evidence ladder from strongest to weakest: the PR's own test fails on base and passes on head; the issue's reproducer behaves differently on head; an agent-written test fails on base and passes on head; a changed hunk is required by that evidence. Below the line, no evidence: the changed test passes on both, or there are no tests or reproducers." width="880">
</p>

## How it works

<p align="center">
  <img src="https://raw.githubusercontent.com/bhaskargurram-ai/proofcheck/main/docs/assets/how-it-works.svg" alt="Pipeline: a pull request's description, diff and linked issue feed four evidence sources (changed tests on base and head, issue reproducer replay, diff evidence map, and an opt-in agent reproduction test). Claims parsed from the description are matched to that evidence, and the result is posted as one proof card comment with a label: verified, partial, contradicted or no evidence." width="880">
</p>

1. **Claims.** A rule-based extractor reads the PR title and description and finds claims such as `Fixes #12`, `adds tests` or `2x faster`. It ignores code blocks, quotes and unfilled templates. An optional LLM extractor can add claims, but never verdicts.
2. **Evidence.** proofcheck checks out the base and head commits as git worktrees and gathers evidence from up to four sources:
   - **Changed tests on base and head.** The PR's added or modified tests run against the base code (with the new tests copied in) and against the head. A test that fails before and passes after is *fail → pass*.
   - **Issue reproducer replay.** Python snippets from the linked issue run on both commits; proofcheck records whether the behaviour changed.
   - **Diff evidence map.** Each changed hunk is undone in turn and the fail → pass evidence is rerun, which shows which hunks the evidence depends on.
   - **Agent reproduction test** *(opt-in, experimental)*. If a `fixes #N` claim still has no evidence, an LLM writes a candidate test and proofcheck runs it on base and head itself.
3. **Verdicts.** Fixed rules match each claim to the evidence: ✅ verified, ❌ contradicted, or ⚠️ no evidence.
4. **Card.** The result is posted as a single PR comment that is updated in place, written to the job summary, and summarised in a label: `proofcheck: verified`, `partial`, `contradicted` or `no evidence`.

## Quick start

### 1. Install

```bash
pip install proofcheck-ai             # the command and the import are `proofcheck`
# or, straight from GitHub:
pip install "git+https://github.com/bhaskargurram-ai/proofcheck@v1.0.1"
```

### 2. Generate the workflows

From the root of your repository:

```bash
proofcheck init            # add --agent to also set up the experimental agent
```

`init` detects your test runner (pytest, Jest or unittest), writes the caller workflows below plus a commented `proofcheck.toml`, refuses to overwrite existing files unless you pass `--force`, and prints the next steps. Commit the files and open a pull request.

### 3. The workflows it writes

proofcheck runs as separate workflows so that untrusted PR code and privileged tokens never share a job ([why](#security-model)). Each caller is a few lines that point at a reusable workflow in this repository. Pin `@v1.0.1`, or a full commit SHA if you want to be strict.

**`.github/workflows/proofcheck.yml`** gathers evidence. It is unprivileged and is where PR code runs.

```yaml
name: proofcheck

on:
  pull_request:
    types: [opened, synchronize, reopened, edited]

permissions:
  contents: read
  issues: read   # read linked issues on private repositories (read-only)

concurrency:
  group: proofcheck-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  evidence:
    uses: bhaskargurram-ai/proofcheck/.github/workflows/evidence.yml@v1.0.1
    with:
      proofcheck-ref: v1.0.1
      python-version: "3.12"
      # Installs the project under test; this runs PR code (unprivileged job).
      install-command: 'pip install -e ".[dev]" pytest || pip install pytest'
      fail-on: never   # or "contradicted" / "no-evidence" to fail the check
```

**`.github/workflows/proofcheck-comment.yml`** posts the card and the label. It is privileged and never runs PR code.

```yaml
name: proofcheck comment

on:
  workflow_run:
    workflows: ["proofcheck"]   # must match the `name:` of proofcheck.yml
    types: [completed]

permissions: {}

jobs:
  comment:
    if: github.event.workflow_run.event == 'pull_request' && github.event.workflow_run.conclusion != 'cancelled'
    permissions:
      contents: read
      actions: read          # download the artifact of the triggering run
      pull-requests: write   # upsert the comment and the verdict label
    uses: bhaskargurram-ai/proofcheck/.github/workflows/comment.yml@v1.0.1
    with:
      proofcheck-ref: v1.0.1
      labels: true
```

**`.github/workflows/proofcheck-agent.yml`** *(optional, experimental)* tries to write a reproduction test when a `fixes #N` claim has no evidence. It runs only for PRs labelled `proofcheck:agent` or opened by owners, members and collaborators, unless you set `run-for: everyone`.

```yaml
name: proofcheck agent

on:
  workflow_run:
    workflows: ["proofcheck"]   # must match the `name:` of proofcheck.yml
    types: [completed]

permissions: {}

jobs:
  agent:
    if: github.event.workflow_run.event == 'pull_request' && github.event.workflow_run.conclusion != 'cancelled'
    permissions:
      contents: read
      actions: read
      pull-requests: read
      issues: read
    uses: bhaskargurram-ai/proofcheck/.github/workflows/agent.yml@v1.0.1
    with:
      proofcheck-ref: v1.0.1
      llm-provider: ${{ vars.PROOFCHECK_LLM_PROVIDER }}
      llm-model: ${{ vars.PROOFCHECK_LLM_MODEL }}
      llm-base-url: ${{ vars.PROOFCHECK_LLM_BASE_URL }}
      llm-reasoning-effort: ${{ vars.PROOFCHECK_LLM_REASONING_EFFORT }}
      python-version: "3.12"
      install-command: 'pip install -e ".[dev]" pytest || pip install pytest'
      run-for: trusted   # or "everyone" (spends API credits on any fork PR)
    secrets:
      llm-api-key: ${{ secrets.PROOFCHECK_LLM_API_KEY }}

  comment:
    needs: agent
    if: needs.agent.outputs.verified == 'true'
    permissions:
      contents: read
      actions: read
      pull-requests: write
    uses: bhaskargurram-ai/proofcheck/.github/workflows/comment.yml@v1.0.1
    with:
      proofcheck-ref: v1.0.1
      # Trusted values: the PR number comes from the gate job (API data, no PR
      # code ran there) and the artifact from this run's verify job.
      pr-number: ${{ needs.agent.outputs.pr-number }}
      artifact-run-id: ${{ github.run_id }}
      labels: true
```

For the agent, add a repository **secret** `PROOFCHECK_LLM_API_KEY` and repository **variables** `PROOFCHECK_LLM_PROVIDER` (`anthropic` or `openai`) and `PROOFCHECK_LLM_MODEL` (optional: `PROOFCHECK_LLM_BASE_URL`, and `PROOFCHECK_LLM_REASONING_EFFORT`: OpenAI's GPT-6 Luna and Sol only call tools through Chat Completions with `none`); `init --agent` also sets `[agent] enabled = true` in `proofcheck.toml`. The key is only ever passed to the propose job, which never runs PR code. The agent workflow posts its own updated card through the same privileged comment workflow, so the comment workflow only listens for the evidence run.

On fork PRs the evidence workflow has no write token, so the card also appears in the run's **job summary** even before the comment workflow posts it.

<details>
<summary><b>Run it locally instead</b></summary>

```bash
proofcheck run --repo . --base origin/main --head HEAD \
    --pr-body-file pr.md --issue-body-file issue.md \
    --out card.md --json result.json
```

Or try the bundled demo, which builds a small repository with a real fix and a plausible-looking fake fix and runs proofcheck on both:

```bash
examples/demo/run_demo.sh /tmp/proofcheck-demo
```

The demo uses pytest when it is installed and otherwise falls back to the stdlib `unittest` runner. Set `PROOFCHECK_RUNNER=pytest` to force pytest.

</details>

<details>
<summary><b>Use the composite action directly</b></summary>

The `bhaskargurram-ai/proofcheck@v1.0.1` composite action is still available for custom setups. Its `command` input selects `context`, `run`, `summary`, `comment`, `agent-propose` or `agent-verify`. See [`action.yml`](https://github.com/bhaskargurram-ai/proofcheck/blob/main/action.yml) and the full examples in [`examples/workflows/`](https://github.com/bhaskargurram-ai/proofcheck/tree/main/examples/workflows). The reusable workflows above wrap the same steps and are the recommended setup.

</details>

## What's on the card

| Section | What it shows |
|---|---|
| **Verdict** | One line: ✅ evidence backs the PR, ⚠️ partial or no evidence, ❌ evidence contradicts it. The comment workflow applies the matching `proofcheck:` label. |
| **Claims** | Every claim found in the title and description, with the quoted source sentence and the evidence for or against it. |
| **Tests** | Each test the diff touched, classified by its result on base and on head. |
| **Issue reproduction** | Each snippet from the linked issue: behaviour changed, unchanged, or error. |
| **Diff evidence map** | Which changed hunks the evidence depends on. See [below](#diff-evidence-map). |
| **Agent evidence** | Only when the agent ran: verdict, model, variants tried, cost, and the winning test. |
| **Scope** | Changes to CI config, dependency manifests, binaries, very large diffs, and hunks no evidence depends on. |
| **Data block** | Invisible: a `<!-- proofcheck:data {...} -->` comment at the end with the verdict, claim statuses and counts as JSON, for bots and the end-to-end harness. Read it with `proofcheck.carddata.parse_card_data`. See [docs/TESTING.md](https://github.com/bhaskargurram-ai/proofcheck/blob/main/docs/TESTING.md#the-cards-data-block). |

<details>
<summary><b>Fail-to-pass tests in detail</b></summary>

1. proofcheck finds test files added or modified between the merge base and the PR head. For pytest these are `test_*.py`, `*_test.py`, `conftest.py` and anything under `tests/` or `test/`. The patterns are configurable.
2. It creates a temporary **git worktree of the base commit** and copies **only the changed test files** from the head into it. It also creates a worktree of the head commit.
3. It runs the changed test modules in both worktrees and reads per-test outcomes from JUnit XML.
4. It classifies every test the diff **touched**, using the AST to find which test functions changed:

| Classification | Base | Head | Meaning |
|---|---|---|---|
| ✅ `fail_to_pass` | fail/error | pass | The test exercises the change. This is strong evidence. |
| ⚠️ `pass_to_pass` | pass | pass | The test does not exercise the change. |
| ❌ `fail_on_head` | any | fail/error | The PR's own tests fail. |
| ❔ `error` / ⏭️ `skipped` | | | No usable outcome. |

A new test module that cannot be imported on base (because the code it tests doesn't exist yet) counts as fail-to-pass for its tests. Pre-existing tests in a modified file that the diff did not touch are reported but **not counted**. If the PR changes no tests, the card says *"⚠️ No test evidence"*.

When the PR claims *no behaviour change* or *backwards compatibility*, proofcheck also runs the **base versions of modified test files against the head code** to check whether the old expectations still hold.

</details>

<details>
<summary><b>Claims and the rules that decide them</b></summary>

The rule-based extractor (always on) ignores code blocks, inline code, quotes and HTML comments such as unfilled PR templates.

| Claim type | Examples |
|---|---|
| `fixes_issue` | `Fixes #123`, `closes #4, #5`, `resolves owner/repo#9`, issue URLs |
| `performance` | `3x faster`, `2.5× speedup`, `40% faster`, `from 162 s to 0.22 s`, `reduces memory by 30%` |
| `no_behavior_change` | `no functional change`, `refactor only`, `does not change behavior` |
| `adds_tests` | `adds tests`, `added a regression test`, `includes unit tests` |
| `backwards_compatible` | `backwards compatible`, `no breaking changes` |

| Claim | ✅ verified when | ❌ contradicted when |
|---|---|---|
| fixes #N | ≥ 1 fail-to-pass test, **or** the issue's reproducer fails on base and succeeds on head, **or** (experimental) an agent-written test fails on base and passes on head | changed tests fail on head, **or** the issue's reproducer fails identically on base and head. Agent evidence never contradicts. |
| adds tests | test functions were added or changed and ran | the diff changes no test files |
| no behaviour change | never; absence of change cannot be proven | fail-to-pass tests exist, a reproducer's behaviour changed, or base versions of modified tests fail on head |
| backwards compatible | never; it is only reported as "not contradicted" | base versions of modified tests fail on head |
| performance (time) | with `benchmark_command`, the measured speedup is ≥ 50 % of the claimed improvement (claimed 3x needs ≥ 2x) | the head is measurably slower |
| performance (memory) | not measured yet, so it is always ⚠️ | |

Everything else is ⚠️ *no evidence*.

</details>

<details>
<summary><b>Issue reproduction</b></summary>

For every `fixes #N` that points at the same repository, the `context` step fetches the issue body before any PR code runs. Locally, pass `--issue-body-file`. proofcheck extracts fenced `python` and `py` code blocks, and converts doctest-style `>>>` sessions into scripts. Each snippet runs on base and on head:

- in a subprocess with a timeout, with the worktree on `PYTHONPATH`, from a throwaway directory;
- with an environment that has secret-looking variables removed (`*TOKEN*`, `*SECRET*`, `*KEY*`, `*PASSWORD*`, …);
- comparing exit code, stdout and stderr, with worktree paths and memory addresses normalised.

Each snippet is reported as `behavior_changed`, `unchanged` or `error`.

</details>

<details>
<summary><b>Scope flags</b></summary>

- ⚠️ changes to `.github/workflows/`, `action.yml` and other CI config
- ⚠️ dependency manifests and lockfiles (`requirements*.txt`, `pyproject.toml`, `setup.py`, `package.json`, `*.lock`, …)
- ⚠️ binary files
- ⚠️ diffs larger than `large_diff_threshold` changed lines
- ⚠️ hunks that no evidence depends on (from the [diff evidence map](#diff-evidence-map))
- 🔎 *heuristic:* non-test files whose path shares no word with the PR title or description, flagged only when the description mentions other changed files

</details>

<details>
<summary><b>The two demo cards, in full</b></summary>

This is real output of [`examples/demo`](https://github.com/bhaskargurram-ai/proofcheck/tree/main/examples/demo) (from proofcheck 0.1.0). The demo bug is that `word_count("hello  world")` returns 3. Two PRs claim to fix it.

**PR A: a real fix.** Its description says *"Fixes #1 … Adds a regression test. This is backwards compatible."*

> ## proofcheck: ⚠️ Some claims are backed by evidence, others are not
>
> 2/3 claims verified · 1 fail→pass, 0 pass→pass, 0 failing on head · 2 files (+6/−4)
>
> | | Claim | Evidence |
> |---|---|---|
> | ✅ verified | **fixes #‍1**<br><sub>“Fixes #‍1.”</sub> | 1 test fails on base and passes on head; issue reproducer fails on base (exit 1) and succeeds on head |
> | ✅ verified | **Adds a regression test**<br><sub>“Adds a regression test.”</sub> | 1 test added/changed in 1 file |
> | ⚠️ no evidence | **backwards compatible**<br><sub>“This is backwards compatible.”</sub> | base versions of modified tests still pass on head (2 passed); not a full compatibility check |
>
> ✅ fail → pass: `tests.test_textutils.WordCountTest::test_repeated_whitespace` (base failed, head passed)
>
> Issue reproduction: 🔁 behaviour changed. Base exit 1 `AssertionError: expected 2 words, got 3` → head exit 0 `ok`

**PR B: a plausible-looking fake.** Its description says *"Fixes #1. Rewrote word_count for clarity and it is now 2x faster. Adds tests for the fix."* The code is reformatted but the bug is still there, and the new test also passes on the unfixed code.

> ## proofcheck: ❌ Evidence contradicts the PR
>
> 1/3 claims verified, 1 contradicted · 0 fail→pass, 1 pass→pass, 0 failing on head · 2 files (+9/−2)
>
> | | Claim | Evidence |
> |---|---|---|
> | ❌ contradicted | **fixes #‍1**<br><sub>“Fixes #‍1.”</sub> | the issue reproducer still fails on head exactly as on base (exit 1) |
> | ⚠️ no evidence | **2x faster**<br><sub>“Rewrote word\_count for clarity and it is now 2x faster.”</sub> | unverified: no benchmark evidence (configure benchmark\_command to measure it) |
> | ✅ verified | **Adds tests**<br><sub>“Adds tests for the fix.”</sub> | 1 test added/changed in 1 file |
>
> ⚠️ pass → pass: `tests.test_textutils.WordCountTest::test_three_words` (passed on base and head)
>
> Issue reproduction: ⚠️ unchanged. Base exit 1 `AssertionError: expected 2 words, got 3` → head exit 1, same error

</details>

## Diff evidence map

A fail → pass test proves the PR changed *something* the test checks. The diff evidence map shows **which parts of the diff** that evidence depends on. It is deterministic and uses no LLM.

For each hunk in the changed non-test files, proofcheck takes a fresh worktree of the head, reverse-applies **just that hunk**, and reruns only the fail → pass tests and any reproducers whose behaviour changed:

| Status | Meaning |
|---|---|
| **required** | With this hunk undone, some evidence fails again. The tests depend on it. |
| **not exercised** | All evidence still passes without it. Nothing measured depends on this change. |
| **skipped** | The hunk could not be reversed cleanly, or the time budget ran out. |

The card shows a table of `file:lines`, status and which evidence broke, with a summary such as *"3/4 changed hunks are required by the tests"*. Unexercised hunks also get a ⚠️ scope note (*"changed, but no evidence depends on it"*). That is a useful way to spot unrelated changes riding along in a PR. It is not proof that they are wrong: refactors, logging and docs are often unexercised by design. In Python files, a hunk that changes only comments, docstrings or formatting (the code's syntax tree is the same without it) is marked 📝 *docs only*, is not run and gets no scope note.

The map runs only when there is at least one fail → pass test or behaviour-changing reproducer, and is capped by `ablation.max_hunks` (default 12) and `ablation.time_budget_s` (default 300 s). Turn it off with `--no-ablation` or `[ablation] enabled = false`.

## Agent evidence (experimental)

Many real PRs say "Fixes #123" but add no test, and many issues have no runnable reproducer. For exactly that case, proofcheck can ask an LLM to **write a reproduction test**, then run it on base and head itself. The agent is **off by default**.

<p align="center">
  <img src="https://raw.githubusercontent.com/bhaskargurram-ai/proofcheck/main/docs/assets/agent-loop.svg" alt="The agent loop: a fixes #N claim with no evidence; localize code with read-only tools; three prompt variants (reproduce-issue, exercise-diff, mimic-existing-tests); static checks and import repair; then, in a separate job with no secrets, run each candidate on base and head. A fail to pass candidate marks the claim verified (agent test); anything else means no evidence found, never contradicted." width="880">
</p>

- **Propose** (`proofcheck agent propose`). The model gets read-only tools (`list_files`, `read_file`, `grep`, `get_diff`, `get_issue`, `list_tests`) implemented with git plumbing. It cannot write files, run code or reach the network; its only output is a schema-validated `submit_test` call. Several **different prompt variants** run (by default `reproduce-issue`, `exercise-diff`, `mimic-existing-tests`), because varied prompts find more reproductions than resampling one. Candidates must parse, contain a test function, live under `tests/proofcheck_agent/`, and import modules that exist at head; near-miss module names are repaired by edit distance.
- **Verify** (`proofcheck agent verify`). No LLM. Each candidate runs on base and head through the same engine as the PR's own tests:

| Result | Meaning | Effect on the card |
|---|---|---|
| `fail_to_pass` | fails on base with an assertion-type failure, passes on head | `fixes #N` becomes ✅ **verified (agent test)** |
| `fail_to_pass_weak` | fails on base only with `ImportError`, `AttributeError` or `NameError` | reported as weak; the symbol just didn't exist before |
| `pass_to_pass` | passes on both | no evidence |
| `fails_on_head` | fails on head | inconclusive: the agent's test may be wrong |
| `error` | could not run | no evidence |

**Agent evidence can never produce ❌.** An LLM-written test that fails on head says more about the test than about the PR, so the card says "no evidence found" instead.

**Expect it to fail often.** Published research on generating issue-reproducing tests (Otter / Otter++, [arXiv:2502.05368](https://arxiv.org/abs/2502.05368); see also SWT-bench) reports fail-to-pass success on roughly **a third of issues** (about 31–37 %), at under $0.10 per issue in that setup. We have not benchmarked proofcheck's agent; its token use and cost are recorded on every card. When it succeeds, the card shows the test in a collapsible block: **commit it** to keep the evidence.

**Projects and Rust.** In a repository with several `[[project]]` tables, the agent works in the project whose source the PR mainly changes, and runs its candidates there with that project's runner. For a cargo project it writes a Rust **integration test** (`<crate>/tests/proofcheck_agent_<variant>.rs`) in the crate with the most changed lines, using only the crate's **public API** (`use <crate>::...`); private items and inline `#[cfg(test)]` modules are out of reach, and a candidate that only fails on base because it does not compile there counts as weak evidence. Rust candidates have their own static checks (balanced brackets, a `#[test]` fn, no processes, network, `unsafe` or environment reads). See [docs/AGENT.md](https://github.com/bhaskargurram-ai/proofcheck/blob/main/docs/AGENT.md#rust-cargo-projects).

`proofcheck agent run` chains propose and verify with up to `agent.max_rounds` refinement rounds that feed execution output back to the model. Use it **only locally or in trusted contexts**: it runs untrusted code in the same process that holds the API key. In CI, the agent workflow keeps propose and verify in separate jobs. Details are in [docs/AGENT.md](https://github.com/bhaskargurram-ai/proofcheck/blob/main/docs/AGENT.md).

## Security model

Running PR code safely is the core problem. proofcheck splits the work into three trust zones, connected only by artifacts:

<p align="center">
  <img src="https://raw.githubusercontent.com/bhaskargurram-ai/proofcheck/main/docs/assets/security-model.svg" alt="Three trust zones. Zone 1, evidence job on pull_request: runs PR code, read-only token, no secrets. Zone 2, agent (opt-in): the propose job holds the LLM key but never executes code; the verify job runs candidates and PR code with no secrets on a separate VM. Zone 3, comment job on workflow_run: holds a write token, never checks out PR code, validates result.json and re-renders the card. No job both runs PR code and holds a secret or a write token." width="880">
</p>

- **Untrusted code runs only in jobs without secrets.** The evidence job runs on `pull_request` with `contents: read`, `persist-credentials: false`, a scrubbed environment, timeouts, and config read from the **base** commit. The agent's verify job has no secrets either.
- **The LLM key never meets PR code.** The propose job reads the PR's files as data through git and never installs or runs anything. Separate jobs run on separate VMs, so code in the verify job cannot read the key from the propose job's memory. The agent only runs for PRs labelled `proofcheck:agent` or from owners, members and collaborators, unless you opt in to everyone.
- **The privileged comment job never checks out or executes PR code.** It treats the artifact as hostile: it validates the PR number and the `result.json` schema, re-renders the card itself (the uploaded `card.md` is ignored), checks that the result matches the PR's current head SHA, and warns on the card when the PR modifies workflows or test-harness files.
- **Never use `pull_request_target` together with a checkout of the PR head.** That combination runs attacker code with a write token and your secrets.
- **All PR-derived text is escaped** on the card: HTML, @mentions, issue references, links, table pipes and control characters.
- **Prompt injection.** The rule-based path has nothing to inject into. PR text, issue text and file contents reach an LLM only as delimited, untrusted data; the model has no write, exec or network tools; its output is schema-validated and can't produce a ❌, and generated code only runs in a job without secrets.

proofcheck raises the bar for low-effort PRs. It is **not** a sandbox and not a defence against a determined attacker: a malicious PR can still tamper with its own evidence inside the unprivileged job. Read [docs/SECURITY_MODEL.md](https://github.com/bhaskargurram-ai/proofcheck/blob/main/docs/SECURITY_MODEL.md) for the full threat model.

## How proofcheck compares

Different tools answer different questions. This table is our best reading of each category as of October 2026; corrections are welcome.

| | AI code reviewers<br><sub>e.g. Copilot code review, CodeRabbit, Greptile, Qodo</sub> | Slop detectors<br><sub>e.g. anti-slop, Slopper</sub> | [reprove](https://pypi.org/project/reprove/)<br><sub>0.1.3</sub> | **proofcheck**<br><sub>1.0.0</sub> |
|---|---|---|---|---|
| Main question | Is this code good? | Is this PR low-effort or AI-generated? | Are the changed lines tested? | **Does the PR do what it claims?** |
| How | Reads the diff and comments | Scores signals about the PR and author | Executes: diff coverage, mutation testing, LLM hypotheses run as pytest | **Executes:** tests on base and head, issue reproducers, hunk ablation |
| Runs the PR's tests | No | No | Yes | Yes |
| Checks the description's claims | Partly, by reading | No | No | **Yes, against measured evidence** |
| Fail → pass on base vs head | No | No | No | **Yes** |
| Replays the issue's reproducer | No | No | No | **Yes** |
| Mutation testing | No | No | **Yes** | No |
| LLM-written tests, executed | No | No | Yes (hypotheses) | Yes (reproduction tests, opt-in) |
| Judges authorship | No | Yes | No | **No, by design** |
| Workflow split for fork PRs | n/a (hosted services) | Varies | Single `pull_request` workflow | Three zones: evidence, agent, privileged comment |
| Languages | Many | Many | Python | Python first; Jest experimental |

These tools are complementary. An AI reviewer comments on code quality and design, which proofcheck doesn't do at all. reprove's mutation testing measures how strong the tests are, which proofcheck doesn't do either. proofcheck focuses on one thing: whether the evidence backs the PR's claims.

## Configuration

Put a `proofcheck.toml` at the repository root, or a `[tool.proofcheck]` table in `pyproject.toml`. **The file is read from the PR's base commit**, so a PR cannot change how it is checked. `proofcheck init` writes a commented starting point.

<details open>
<summary><b>All keys</b></summary>

```toml
runner = "pytest"                      # pytest | unittest | jest | cargo
test_command = "python -m pytest -x"   # optional; {junitxml} and {tests} placeholders supported (cargo: a prefix, default "cargo test")
test_patterns = ["tests/*", "test_*.py", "*_test.py", "conftest.py"]   # files copied from head into base
test_module_patterns = ["test_*.py", "*_test.py"]                     # files that are run
timeout = 600                          # seconds per test run (cargo projects: 1800 unless set)
setup_command = ""                     # optional, run in each worktree before tests (e.g. a build step)
cargo_target_dir = ""                  # cargo: shared target dir (default $CARGO_TARGET_DIR, else <tmp>/proofcheck-cargo-target)
large_diff_threshold = 1000            # changed lines
max_issues = 5

# Optional benchmark for performance claims: runs N times on base and head
# (interleaved) and compares median wall time.
benchmark_command = ["python", "benchmarks/bench_parse.py"]
benchmark_runs = 5
benchmark_timeout = 600

[test_env]                             # extra env vars for test runs
APP_ENV = "test"

[repro]
enabled = true
timeout = 60
python = "python"                      # interpreter for issue snippets

[ablation]                             # diff evidence map
enabled = true
max_hunks = 12
time_budget_s = 300
exclude = ["*.md", "*.rst", "*.txt", "docs/*", "LICENSE*", "CHANGELOG*", ".github/*"]

[agent]                                # experimental reproduction-test agent
enabled = false
when = "missing-evidence"              # or "always"
variants = 3                           # 4 adds "localize-first"
max_rounds = 2                         # refinement rounds, `agent run` only
max_tool_calls = 25                    # per variant
test_dir = "tests/proofcheck_agent"
# test_timeout = 120                   # seconds per candidate and commit (default 120; cargo: the project's timeout)
max_output_tokens = 4096               # per model response
max_total_tokens = 1500000             # input + output over the whole run
max_cost_usd = 0                       # 0 = no cap; enforced only when prices are set
# price_input_per_mtok = 0.0           # USD per million tokens, to report cost
# price_output_per_mtok = 0.0
# temperature = 0.0                    # omitted from requests unless set
request_timeout = 120
max_retries = 4

[llm]                                  # optional LLM claim extraction
enabled = false                        # the API key is never read from this file
provider = "openai"                    # or "anthropic"
model = ""
base_url = ""

[[project]]                            # optional, repeatable: a test project in a subdirectory
name = "python-sdk"                    # prefixes test ids when there is more than one project
path = "adapters/python"               # relative POSIX path, no ".."; "." for the root
runner = "pytest"                      # also: test_command, setup_command, test_paths, timeout, test_env, cargo_target_dir
```

</details>

**Several projects in one repository.** Without `[[project]]` tables the top-level keys describe the one project at the repository root, exactly as before. Each `[[project]]` table adds a project in a subdirectory (or replaces the root project when `path = "."`; the top-level `runner`/`test_command`/`setup_command`/`test_patterns` must then move into that table). Every changed file belongs to the deepest project that contains it, is classified by that project's runner, and its tests run with the project directory as the working directory in the base and head worktrees (Python: `<project>/src` and `<project>` go on `PYTHONPATH`). With more than one project, test ids are prefixed with the project name (`python-sdk:tests.test_client::test_x`), the card lists each project's status, and `result.json` gains `tests.projects`. `proofcheck init` detects projects (the root plus `pyproject.toml`, `setup.cfg`, `package.json` with a Jest test script and cargo workspace roots up to three levels deep, skipping `examples/`, `fixtures/`, `vendor/`, `packaging/`, `docs/` and the like) and writes the tables when it finds more than one.

**Rust (`runner = "cargo"`).** Changed integration tests (`<crate>/tests/<name>.rs`) run per target with `cargo test -p <package> --test <name> --no-fail-fast -- --test-threads=1`; libtest's plain output is parsed (no nightly flags). When a PR changes a `#[cfg(test)] mod tests { ... }` block inside a `src/` file, only that module is transplanted from the head version into the base version of the file and run with `cargo test -p <package> --lib <module>::tests::` on both commits; files whose test module cannot be separated from the code by proofcheck's small lexer are skipped with a note. A test target that does not compile on the base commit counts as fail → pass, and the card says it failed only because it did not compile there (the API is new). Base, head and the diff evidence map share one `CARGO_TARGET_DIR` so dependencies compile once; the evidence workflow installs the toolchain from `rust-toolchain.toml` and uses `Swatinem/rust-cache` in the unprivileged job only. Allow 60–120 minutes (`timeout-minutes`) for large workspaces.

**Environment variables**

| Variable | Purpose |
|---|---|
| `PROOFCHECK_LLM_PROVIDER` | `anthropic` (Messages API) or `openai` (any OpenAI-compatible chat endpoint) |
| `PROOFCHECK_LLM_API_KEY` | API key. Refused in config files and stripped from every subprocess. |
| `PROOFCHECK_LLM_MODEL` | Model id. **Required**; there is no default. |
| `PROOFCHECK_LLM_BASE_URL` | Optional custom endpoint |
| `PROOFCHECK_PYTHON` | Interpreter for tests and reproducers (default: `python` on `PATH`) |

The optional LLM claim extractor sends PR text as untrusted data inside random delimiters, gives the model no tools, and accepts only strict JSON. Every claim must quote text that actually appears in the PR, and LLM-found claims can only add things to check.

## CLI reference

```text
proofcheck init      [--repo .] [--agent] [--force] [--ref v1.0.1]
proofcheck context   --out context.json [--max-issues 5]
proofcheck run       --base REF [--head HEAD] [--repo .] [options]
proofcheck summary   --json result.json
proofcheck comment   --json result.json --pr N [options]
proofcheck agent propose --repo DIR --base REF --head REF --out-dir DIR
                         [--context-file F] [--result result.json] [--config F]
proofcheck agent verify  --repo DIR --base REF --head REF --candidates DIR
                         --result result.json --json OUT --out card.md
proofcheck agent run     (propose + verify + refinement; local or trusted use only)
```

| Command | What it does |
|---|---|
| `init` | Detects the test runner and writes the caller workflows and a commented `proofcheck.toml`. |
| `context` | Collects the PR text and referenced issues. Run it **before** any PR code. |
| `run` | Gathers evidence and writes `card.md` and `result.json`. |
| `summary` | Writes the card to `$GITHUB_STEP_SUMMARY`, or to stdout. Needs no token, so it works on fork PRs. |
| `comment` | Validates `result.json`, re-renders the card and creates or updates the PR comment. |
| `agent propose` | LLM with read-only tools writes candidate tests. Never executes code. |
| `agent verify` | Runs the candidates on base and head and merges the result. No LLM. |
| `agent needed` | Prints `true` when a `fixes` claim still lacks evidence (used by the agent workflow's gate). |
| `agent run` | Both, with refinement rounds. Runs untrusted code next to the API key, so use it only locally or in trusted contexts. |

<details>
<summary><b><code>run</code> and <code>comment</code> options</b></summary>

| `run` option | Purpose |
|---|---|
| `--runner pytest\|unittest\|jest\|cargo` | choose the test runner (default from config, else `pytest`) |
| `--test-command "…"` | override the test command (split like a shell, never run through one) |
| `--config FILE` | config file; by default config is read **from the base commit** |
| `--context-file FILE` | PR title, body and issues from `proofcheck context` |
| `--pr-body-file FILE` | PR description; a first line `Title: …` or `# …` is used as the title |
| `--issue-body-file FILE` | issue text to reproduce (repeatable) |
| `--ablation/--no-ablation` | build the diff evidence map (default: on) |
| `--fail-on never\|contradicted\|no-evidence` | exit 1 for that verdict or worse |
| `--llm/--no-llm` | enable the optional LLM claim extractor |
| `--skip-tests` | skip the fail-to-pass run |
| `--out FILE`, `--json FILE` | where to write the card and the result |

`comment` options: `--pr`, `--repo`, `--expect-head-sha` (refuse unless the PR head matches), `--bot-login`, `--no-check-pr-files`, `--dry-run`.

You can also run `python -m proofcheck …`.

</details>

## FAQ

<details>
<summary><b>Does proofcheck detect AI-generated code?</b></summary>

No, and it never will. It checks whether the evidence backs the PR's claims. A well-evidenced AI-assisted PR gets ✅; an unevidenced human PR gets ⚠️.

</details>

<details>
<summary><b>Does ✅ mean the PR is correct and safe to merge?</b></summary>

No. ✅ means the claim is backed by executed evidence: for example, a test that fails without the change and passes with it. It doesn't prove the test checks the *right* behaviour, and it doesn't review code quality or security. Review is still required.

</details>

<details>
<summary><b>What does ⚠️ "no evidence" mean?</b></summary>

That proofcheck found nothing that confirms or refutes the claim. It is not an accusation. Often the fix is simply to add a test that fails before the change.

</details>

<details>
<summary><b>Do I need an LLM?</b></summary>

No. Claim extraction, the test runs, issue replay and the diff evidence map are all deterministic. An LLM is only used if you enable the optional claim extractor or the experimental agent. Neither one decides a verdict: the extractor can only propose claims, which are then checked against what proofcheck ran, and agent evidence can upgrade a claim to ✅ but never mark one ❌.

</details>

<details>
<summary><b>Can a malicious PR fake a good card?</b></summary>

Partly. PR code runs in the evidence job, so it can tamper with its own results there. The comment workflow limits what a forged result can say (schema validation, re-rendering, head-SHA checks, harness-change warnings), but proofcheck is not a defence against a determined attacker. See [docs/SECURITY_MODEL.md](https://github.com/bhaskargurram-ai/proofcheck/blob/main/docs/SECURITY_MODEL.md).

</details>

<details>
<summary><b>Why several workflows instead of one?</b></summary>

So that no single job both runs PR code and holds a secret or a write token. A single `pull_request` workflow can't comment on fork PRs, and a single `pull_request_target` workflow that checks out the PR is the classic "pwn request".

</details>

<details>
<summary><b>Which languages are supported?</b></summary>

Python first: pytest (most complete) and stdlib `unittest`. Jest is experimental and needs `jest-junit`. Issue reproducers are Python only. The agent writes Python tests.

</details>

## Limitations

- A fail-to-pass test proves the PR changes behaviour the test checks, not that the test checks the *right* behaviour.
- Test runs share the CI job with PR code, so a malicious PR can tamper with its own results.
- proofcheck does not measure test strength: there is no mutation testing.
- The diff evidence map reverts one hunk at a time; hunks that only matter in combination may show as unexercised.
- Benchmarks on shared CI runners are noisy. Differences under 5 % are treated as noise.
- Issue reproducers are Python only and are compared with the previous behaviour, not with an expected output.
- The agent is experimental and often finds nothing. Its tests can be wrong, which is why they can never contradict a claim.
- The Jest runner is experimental and has not been tested against many real projects.
- Rust: only integration tests and inline `#[cfg(test)]` modules of changed files in existing crates are run (not doctests, benches or examples); `#[path]` module attributes, out-of-line `mod tests;` files and inline tests in new files are not handled. Build scripts and proc macros run on base and head like any test code.

## Roadmap

- Runners for **Vitest**, **Go** (`go test` + `go-junit-report`), cargo-nextest (JUnit, per-test timeouts) and a hardened Jest runner
- Optional mutation testing of changed lines, to report how strong the fail → pass tests are
- Benchmark integrations (pytest-benchmark, hyperfine, criterion) and memory measurements
- Reproducers in other languages, and expected-output assertions from issues
- A GitHub App mode that runs checks in isolated sandboxes instead of the PR's CI job
- Check-run annotations next to the PR comment

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](https://github.com/bhaskargurram-ai/proofcheck/blob/main/CONTRIBUTING.md). In short:

```bash
pip install -e ".[dev]"
python -m pytest            # or, without pytest: python -m unittest discover -s tests
```

The test suite builds real throwaway git repositories and worktrees, so it needs `git` on `PATH`. [docs/TESTING.md](https://github.com/bhaskargurram-ai/proofcheck/blob/main/docs/TESTING.md) describes every test level, including the packaging check and the end-to-end harness in [`e2e/`](https://github.com/bhaskargurram-ai/proofcheck/blob/main/e2e/README.md) that opens real pull requests against a test repository. Please report security issues privately through the repository's security advisory form rather than in a public issue.

## License

[MIT](https://github.com/bhaskargurram-ai/proofcheck/blob/main/LICENSE) © 2026 Bhaskar Gurram
