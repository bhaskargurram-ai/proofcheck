# Agent evidence (experimental)

proofcheck's main checks are deterministic: they run the tests a pull request
adds, replay reproducers from the linked issue, and compare the PR description
with what they measured. Many real PRs say "Fixes #123" but add no test and
link an issue without a runnable snippet. For those PRs the card can only say
**no evidence**.

Agent evidence is an opt-in attempt to close that gap. An LLM, given
**read-only** access to the repository, tries to write a *reproduction test*
for the issue. proofcheck then runs that test itself on the base commit and on
the PR head. The model never decides anything: only the executed outcome
counts.

> **Status: experimental.** Off by default. Python projects only (pytest or
> unittest). The success rate on real projects has **not been measured** yet.

## Honest expectations

Writing a test that fails before a fix and passes after it is hard, even for
the best current systems. The Otter paper (arXiv
[2502.05368](https://arxiv.org/abs/2502.05368)) and related work such as
SWT-bench and Issue2Test report fail-to-pass success on roughly **a third of
issues (about 31–37%)** for the strongest approaches they evaluate, at a cost
of well under a dollar per issue. proofcheck borrows several of their ideas
(localise first, heterogeneous prompts, import repair, a self-check before
submitting), but it is a different system on different repositories and
**its own success rate is unknown**.

So expect the agent to fail on most PRs. That is why a failed attempt always
reads as "no evidence found", never as "the PR is wrong", and why agent
evidence can upgrade a claim but can never produce ❌.

## What it does

```
            PR text, issue text            repository (git objects only)
                    │                                   │
                    ▼                                   ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ propose (holds the API key, runs NO repository code)          │
  │  for each prompt variant:                                     │
  │    model ⇄ read-only tools (git show / ls-tree / grep / diff) │
  │    model → submit_test(code)  → schema + static checks        │
  │                                 + import repair               │
  └──────────────────────────────────────────────────────────────┘
                    │ candidates/ (manifest.json, test files, transcript)
                    ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ verify (no LLM, no secrets): run each candidate on base and   │
  │ on head with the project's test runner → classify → merge     │
  │ into result.json → card                                       │
  └──────────────────────────────────────────────────────────────┘
```

1. **Gate.** By default (`when = "missing-evidence"`) the agent only runs when
   `result.json` from `proofcheck run` has a `fixes #N` claim that is still
   unverified: no fail-to-pass test and no behaviour-changing issue
   reproducer. `proofcheck agent needed --json result.json` answers this for
   a workflow's gate job.
2. **Propose.** Each prompt variant gets its own conversation. The model can
   only call the read-only tools below and `submit_test`. A rejected
   submission (syntax error, no test, unknown import, denied module) is sent
   back with the reasons and the model may try again.
3. **Verify.** Every accepted candidate is written into fresh git worktrees of
   the merge base and of the head commit and run with the configured runner,
   exactly like the PR's own tests in the fail-to-pass check.
4. **Merge.** The outcome goes into `result.json` under `agent`. If at least
   one candidate is strong evidence, the matching unverified `fixes #N` claim
   becomes ✅ **verified (agent test)** with the evidence text "an
   agent-written reproduction test fails on base and passes on head". The card
   gets a "🤖 Agent evidence (experimental)" section with the winning test in
   a collapsed block. **Commit that test** if you want to keep the evidence.

### Tools the model can use

| Tool | Implemented with | Notes |
|---|---|---|
| `list_files(ref, path, recursive)` | `git ls-tree` | `ref` is `base` or `head`, nothing else |
| `read_file(ref, path, start_line, end_line)` | `git cat-file`, `git show REF:path` | ≤ 400 lines per call; blobs > 1 MB and binary files refused |
| `grep(ref, pattern, path_glob, ignore_case)` | `git grep -E` | ≤ 80 matches shown |
| `get_diff(path?)` | `git diff` merge-base..head | file list with line counts, then the (truncated) diff |
| `get_issue()` | — | PR title/body and issue text, as untrusted data |
| `list_tests()` | `git ls-tree` + runner patterns | existing test modules and how tests run |
| `submit_test(filename, code, rationale, targets_claim)` | — | the only output channel; strict schema |

Paths chosen by the model are validated: relative, no `..` or empty
segments, no backslashes or control characters, no leading `-` or `:` (git
pathspec magic), nothing under `.git`. Every result is truncated (8,000
characters by default) and file contents are wrapped in per-run random
delimiters that mark them as untrusted.

### Prompt variants

Sampling one prompt several times at a higher temperature gives similar
answers; Otter++ found that a few *different* prompts do better. proofcheck's
variants approach the same task from different sides, and all of them
localise first and end with the same self-check ("will this fail on base
*because of the bug*, not because of an import? will it pass on head?").

| Variant | Entry point |
|---|---|
| `reproduce-issue` (default) | Start from the issue text and its snippet; find the focal function; copy an existing test's imports. |
| `exercise-diff` (default) | Start from the diff; find the input that makes base and head behave differently; test the entry point that reaches it. |
| `mimic-existing-tests` (default) | Start from the closest existing test file; write the issue's case in exactly that style. |
| `localize-first` (optional 4th) | Gather evidence and write an explicit plan (focal code, input, base vs head behaviour, test to imitate), review it, then write. |

The test style follows the project: if it runs tests with the stdlib
`unittest` runner, or shows no sign of pytest (no `conftest.py`, no pytest
configuration, existing tests import `unittest`), the model must write a
`unittest.TestCase`; otherwise plain pytest functions are expected.
Candidates are stored as `tests/proofcheck_agent/test_<variant>.py`
(configurable with `test_dir`), whatever file name the model suggests.

### Static checks and import repair

Before a submission is accepted (nothing is executed):

* it must parse and contain at least one test the runner will collect;
* relative imports are rejected (the file lives in its own directory);
* imports of the repository's own modules must exist **at head**. As in
  Otter, near misses are repaired by edit distance: `from shop.carts import
  total` becomes `from shop.cart import total`; a misspelled name is imported
  from the right module `as` the name the test uses; a name that lives in a
  different module is imported from there. Every repair is recorded in the
  manifest;
* a small denylist rejects process and network modules (`subprocess`,
  `socket`, `urllib.request`, `requests`, …) and calls such as `os.system`,
  `eval` and `exec`. This is a lint that catches off-task code, **not a
  sandbox**.

### Classification

| Class | Meaning | Effect |
|---|---|---|
| `fail_to_pass` | Passes on head; fails on base with a behavioural failure (an assertion, or an exception raised by the project's code) | **Strong.** Upgrades the matching `fixes #N` claim to ✅ verified (agent test). |
| `fail_to_pass_weak` | Passes on head; fails on base only because a name is missing there (`ImportError`, `ModuleNotFoundError`, or `AttributeError`/`NameError`/argument `TypeError` raised in the test itself) | Weak. Shown on the card, claim stays ⚠️. Shows the API is new, not that a bug is fixed. |
| `pass_to_pass` | Passes on both | No evidence. |
| `fails_on_head` | Some test fails on head | **Inconclusive.** Agent tests can be wrong; never a contradiction. |
| `error` | The runner produced no usable report | No evidence. |

Failures are told apart by reading the JUnit failure message, type and
traceback: pytest's `assert` messages, `E   ImportError: ...` lines in
collection errors, and the file the exception was raised in.

## Safety model

The agent adds two new risks to proofcheck's [security model](SECURITY_MODEL.md):
an API key, and model output that is shaped by untrusted text (the PR, the
issue, the repository). The design keeps the key and untrusted code apart.

* **The key never meets PR code.** `propose` holds the key but never
  executes, imports, installs or checks out anything from the repository: all
  tools read git objects. `verify` runs the candidates (and therefore the PR's
  code, which they import) but needs no key and no LLM. In CI they are
  **separate jobs**, so separate virtual machines: code running in `verify`
  cannot read memory, files or environment of `propose`. The `verify` job
  should reference no secrets and use `permissions: contents: read`.
* **No dangerous tools.** The model has no write, exec or network tool. Its
  only output is `submit_test`, validated against a strict schema (exactly
  four string fields with length caps) and the static checks above.
* **Prompt injection.** PR text, issue text, file contents and test output
  are passed as data inside random, per-run delimiters (forged delimiters are
  stripped), and the system prompt tells the model never to follow
  instructions inside them. An injection can still steer *which test* the
  model writes, but a test only counts if it actually fails on base and passes
  on head when proofcheck runs it, and a bad test can only lead to "no
  evidence" or "inconclusive", never to ❌.
* **The card is re-rendered from validated data.** The `agent` section of
  `result.json` is validated like the rest (types, enumerations, lengths,
  unknown keys dropped) by the privileged comment workflow. Model-written
  text is escaped; the test code is shown in a fenced block whose fence is
  longer than any backtick run in the code, with control and bidi characters
  removed.
* **Configuration comes from the base commit**, so a PR cannot enable the
  agent for itself or raise its budgets. API keys in the config file are
  rejected.
* **Transcripts are redacted.** `transcript.jsonl` has the API key and common
  token formats removed and long fields truncated.

### Why `proofcheck agent run` is for local or trusted use only

`agent run` does everything in one process: it proposes, runs the
candidates, feeds the outcome back to the model and asks for a revision.
That execution feedback helps, but it means LLM-written test code (and the
PR code it imports) runs on the same machine, as the same user, as the
process that holds `PROOFCHECK_LLM_API_KEY`. proofcheck removes secret-looking
variables from the test environment, but that is not a sandbox: the code can
read `/proc/<parent pid>/environ`, the parent's memory, or files the user can
read. Use `agent run` on your own machine, on PRs you have looked at. In CI,
use `agent propose` and `agent verify` in separate jobs. The command prints a
warning every time it runs.

## Usage

### Local

```bash
export PROOFCHECK_LLM_PROVIDER=anthropic        # or: openai (any OpenAI-compatible endpoint)
export PROOFCHECK_LLM_MODEL=<model id>          # required: proofcheck has no default model
export PROOFCHECK_LLM_API_KEY=...               # not needed for a local http://localhost endpoint
# export PROOFCHECK_LLM_BASE_URL=https://...    # optional

proofcheck run --base origin/main --head HEAD --pr-body-file pr.md \
  --issue-body-file issue.md --json result.json --out card.md

# propose + verify + up to 2 refinement rounds (local/trusted only)
proofcheck agent run --enable --base origin/main --head HEAD \
  --pr-body-file pr.md --issue-body-file issue.md \
  --result result.json --json result.json --out card.md --out-dir candidates
```

`--enable` overrides `[agent] enabled = false`. `--variants 4` (or
`--variants reproduce-issue,localize-first`), `--max-tool-calls N`,
`--max-rounds N` and `--when always` override the configuration for one run.

### CI (two jobs)

```bash
# job "propose": has the API key; checks out the PR head as DATA only
#   (no install, no tests, no build)
proofcheck agent propose --repo . --base "$BASE_SHA" --head "$HEAD_SHA" \
  --context-file context.json --result result.json --out-dir candidates
# upload candidates/ as an artifact

# job "verify": NO secrets, permissions: contents: read; installs the project
proofcheck agent verify --repo . --base "$BASE_SHA" --head "$HEAD_SHA" \
  --candidates candidates --result result.json \
  --json proofcheck-out/result.json --out proofcheck-out/card.md
# upload proofcheck-out/ (result.json, card.md, pr_number, head_sha) as proofcheck-result

# gate job helper: prints true/false, and writes needed=/reason= to $GITHUB_OUTPUT
proofcheck agent needed --json result.json --github-output
```

`candidates/` contains `manifest.json` (schema `proofcheck/agent-candidates`
v1), the accepted test files under their repository-relative paths (for
example `candidates/tests/proofcheck_agent/test_reproduce_issue.py`) and
`transcript.jsonl`. `verify` checks that the manifest was produced for the
same base and head commits, re-validates every path, and refuses files
outside `test_dir`.

`propose` exits 0 and writes a manifest with `"status": "skipped"` when the
agent is disabled in the base commit's config or no `fixes` claim needs
evidence, so a workflow can always run `verify` afterwards. It exits non-zero
only for usage errors (for example `PROOFCHECK_LLM_MODEL` not set).

## Cost controls

* `max_tool_calls` per variant and round (default 25). When it is used up the
  model is forced (via `tool_choice`) to call `submit_test`.
* `max_output_tokens` per response (default 4096).
* `max_total_tokens` over the whole run (default 1,500,000 input + output),
  and `max_cost_usd` when prices are configured. The run stops cleanly when a
  budget is reached.
* Old tool outputs are elided from the conversation once it grows past about
  120,000 characters. With the Anthropic API, prompt-cache breakpoints are set
  on the system prompt and the newest message, so repeated history is read
  from the cache.
* Token usage (input, output, cache reads/writes, requests) is always
  recorded in the manifest, `result.json` and the card. A cost in USD is shown
  **only** when you configure prices (`price_input_per_mtok`,
  `price_output_per_mtok`); cached tokens are charged at the full input price,
  so the figure is an upper bound.

## Configuration

In `proofcheck.toml` (or `[tool.proofcheck.agent]` in `pyproject.toml`), read
from the **base** commit:

```toml
[agent]
enabled = false            # master switch (also: --enable on the command line)
variants = 3               # 1-4, or a list: ["reproduce-issue", "exercise-diff", "mimic-existing-tests", "localize-first"]
max_rounds = 2             # refinement rounds for `proofcheck agent run` (0-5)
max_tool_calls = 25        # per variant and round (1-100)
when = "missing-evidence"  # or "always"
test_dir = "tests/proofcheck_agent"
max_output_tokens = 4096
max_total_tokens = 1500000
max_cost_usd = 0           # 0 = no cost cap; only enforced when prices are set
# price_input_per_mtok = 3.0    # USD per million input tokens (your provider's price)
# price_output_per_mtok = 15.0  # USD per million output tokens
# temperature = 0.2        # omitted from requests unless set
request_timeout = 120      # seconds per LLM request
max_retries = 4            # retries for HTTP 408/409/425/429/500/502/503/504/529 and network errors, with backoff and Retry-After
test_timeout = 120         # seconds per candidate run on each commit

[llm]                      # optional fallbacks; environment variables win
provider = "anthropic"
model = "<model id>"
base_url = ""
```

Environment variables: `PROOFCHECK_LLM_PROVIDER` (`anthropic` or `openai`),
`PROOFCHECK_LLM_MODEL` (required), `PROOFCHECK_LLM_API_KEY`,
`PROOFCHECK_LLM_BASE_URL` (optional). The key is never read from a file.

## Limitations

* **Python only** (pytest or unittest runners). Jest projects get an `error`
  status.
* **One claim per run**: the first `fixes #N` claim that lacks evidence.
* **The model can read the fix.** A test that fails on base and passes on head
  proves that the PR changed this behaviour, in the direction the test
  expects. It does not prove that the new behaviour is what the issue asked
  for: read the test before trusting the ✅.
* **Agent tests can be wrong or flaky.** `fails_on_head` is reported as
  inconclusive, and a single run per commit cannot detect flakiness.
* **Weak/strong is a heuristic** based on exception types and where they were
  raised.
* **The denylist is not a sandbox.** Candidates, like the PR's own tests, must
  only run in an unprivileged environment.
* **Costs are estimates** from your configured prices, and the success rate is
  unmeasured.
