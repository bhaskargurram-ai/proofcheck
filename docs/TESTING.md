# Testing proofcheck

proofcheck is tested at five levels. The first four run offline (no network,
no GitHub, no LLM) and run in CI on every push and pull request; the fifth
runs against a real GitHub repository and is started by hand.

| Level | Where | Runs in CI | Needs |
|---|---|---|---|
| 1. Unit and integration tests | `tests/` | yes, Python 3.10–3.13 | `git` |
| 2. Workflow invariants | `tests/test_workflows.py`, actionlint | yes (the invariants) | PyYAML; actionlint optional |
| 3. Offline end-to-end scenarios | `tests/test_e2e_scenarios.py`, `e2e/run.py --local` | yes | `git` |
| 4. Packaging | `package` job in `.github/workflows/ci.yml` | yes | `build`, `twine` (from PyPI) |
| 5. End-to-end on GitHub | `e2e/run.py --repo OWNER/NAME` | no (manual) | a test repository, a token, optionally an LLM key |

## 1. Unit and integration tests

```bash
pip install -e ".[dev]"
python -m pytest -q
python -m unittest discover -s tests      # the same suite without pytest
```

Every test is a `unittest.TestCase`, so both runners execute the same suite.
Tests that need a repository build real throwaway git repositories and
worktrees in a temporary directory; nothing touches the network.

What they cover:

- claim extraction, evidence rules and verdicts (`test_claims.py`, `test_evidence.py`);
- fail-to-pass classification with the pytest, unittest and jest runners, JUnit
  parsing, touched-test detection (`test_failtopass.py`, `test_junit.py`);
  without pytest installed the pytest runner is exercised through
  `tests/fake_pytest.py`;
- issue reproduction, benchmarks, scope flags, the diff evidence map
  (`test_repro.py`, `test_scope.py`, `test_ablation.py`);
- the result schema validator and the card renderer, including hostile PR
  text (`test_card.py`), and the card's machine-readable data block: escaping,
  size cap, truncation, and that only the trailing block is parsed
  (`test_carddata.py`);
- the GitHub client with a fake transport (`test_github_api.py`), the CLI
  commands end to end on temporary repositories (`test_cli.py`,
  `test_integration_cmds.py`), `proofcheck init`;
- the agent: configuration, static checks, read-only tools, prompts, the
  tool-use client for both wire formats, propose → verify → merge with a
  scripted model (`test_agent_*.py`);
- the e2e harness itself (`test_e2e_harness.py`, see level 5).

## 2. Workflow invariants

`tests/test_workflows.py` parses every workflow in `.github/workflows/` and
`examples/workflows/` and checks the security model of
[SECURITY_MODEL.md](SECURITY_MODEL.md) as code: no `pull_request_target`, no
`${{ }}` inside `run:` scripts, `persist-credentials: false` on every checkout,
the privileged comment workflow never checks out or installs the PR, the
agent's key-holding job never runs PR code, and so on. It is skipped when
PyYAML is not installed (`pip install -e ".[dev]"` installs it).

[actionlint](https://github.com/rhysd/actionlint) catches expression and
schema mistakes the invariants do not look for. It is not run in CI; run it
before changing a workflow:

```bash
actionlint .github/workflows/*.yml examples/workflows/*.yml
```

## 3. Offline end-to-end scenarios

The scenario set used on GitHub (level 5) can also run locally. The harness
builds each scenario's base and head commits in a throwaway repository exactly
as it would push them, runs `proofcheck run` on them as a subprocess, and checks
the card's data block against the scenario's expectations (the online-only
ones, label, trusted warning and agent status, are skipped;
`[expected_local]` overrides apply):

```bash
python e2e/run.py --scenarios e2e/scenarios/python-toy --local --runner unittest
python e2e/run.py --scenarios e2e/scenarios/python-toy --local --runner pytest   # needs pytest
```

`tests/test_e2e_scenarios.py` does the same inside the test suite and adds two
checks that need more than one process: the `workflow-change` card re-rendered
with the comment workflow's trusted warning, and the `agent-no-test` scenario
taken through `proofcheck agent propose` and `verify` with a scripted model, which
must turn the card into "verified (agent test)".

This level covers the whole `run` pipeline on realistic PRs, including issue
reproducers, the diff evidence map and hostile issue text, but not GitHub:
no workflows, artifacts, permissions, labels or comments.

## 4. Packaging

The `package` CI job builds the sdist and wheel with `python -m build`, runs
`twine check --strict`, checks that the sdist contains the package, installs
the wheel into a fresh virtualenv, and then, from outside the checkout:

- checks that the installed copy is the one that runs;
- runs `proofcheck --help`, `--version` and `agent --help`;
- runs `proofcheck init` in a new repository and checks the files it writes;
- runs the demo (`examples/demo/run_demo.sh`) with the installed proofcheck
  and the job's Python for the tests, as `evidence.yml` does, and checks the
  two verdicts and the data block.

To reproduce it locally (needs PyPI access for `build` and `twine`):

```bash
python -m pip install build twine
python -m build --outdir /tmp/pc-dist && python -m twine check --strict /tmp/pc-dist/*
python -m venv /tmp/pc-venv && /tmp/pc-venv/bin/pip install /tmp/pc-dist/*.whl
cd /tmp && /tmp/pc-venv/bin/proofcheck --help
```

## 5. End-to-end on GitHub

`e2e/run.py` exercises what no offline test can: the reusable workflows
running on GitHub's runners, the `pull_request` → `workflow_run` hand-off,
artifact upload and download, the comment workflow's validation and
re-rendering, comment upserts, verdict labels, the trusted warning for PRs
that change workflows, and (with an API key) the agent with a real model.

```bash
export GITHUB_TOKEN=...   # PAT or App token; see e2e/README.md (not an Actions GITHUB_TOKEN)
python e2e/run.py --repo OWNER/NAME --scenarios e2e/scenarios/python-toy
python e2e/run.py --repo OWNER/NAME --scenarios e2e/scenarios/python-toy --only real-fix --keep-open
python e2e/run.py --scenarios e2e/scenarios/python-toy --dry-run        # plan only
```

For every scenario it pushes a base branch (the target's default branch plus a
small toy package with known bugs) and a head branch (the scenario's change),
opens an issue and a PR into the base branch, waits for the card whose data
block names the pushed head, checks the verdict, claim statuses, labels, test,
ablation and agent results, and cleans up. It writes `e2e/report.md` and
`e2e/report.json`. The harness's own logic (scenario validation, patch and
overlay application, expectation matching, reports, the HTTP layer with
retries and pagination, the full online flow against a fake GitHub and a bare
git remote, cleanup on errors, timeouts and Ctrl-C, token redaction) is unit
tested offline in `tests/test_e2e_harness.py`.

The `python-toy` scenarios are: a real fix, a fake (pass → pass) fix, a fix
with a smuggled unrelated hunk, a fix without a test (for the agent), a
contradicted "no behaviour change" claim, a prompt-injection issue, and a
workflow-file change. See [e2e/README.md](../e2e/README.md) for the scenario
format, the target repository's requirements and the token permissions.

What it does not cover: forks (the harness opens same-repository PRs; fork PRs
differ in token permissions and in GitHub's approval gate for first-time
contributors), and the agent's behaviour is only checked against an allowed
set of outcomes, since an LLM may or may not find a reproduction test.

## The card's data block

Each card ends with an invisible comment that the harness, and any other
tool, can read instead of scraping the Markdown:

```
<!-- proofcheck:data {"v":1,"tool":"…","pr":4,"head":"<sha>","base":"<sha>","merge_base":"<sha>","verdict":"evidence",
  "claims":[{"type":"fixes_issue","text":"fixes #3","status":"verified"}],
  "tests":{"status":"evidence","fail_to_pass":1,"pass_to_pass":0,"fail_on_head":0,"error":0,"skipped":0},
  "ablation":{"status":"ran","total_hunks":2,"tested":2,"required":1,"not_exercised":1,"skipped":0},
  "repro":["behavior_changed"],"agent":null,"flags":["unexercised"],"files":3,"warnings":0} -->
```

(one line in the card; `-`, `<`, `>`, `&` and `@` appear as `\u` escapes.)
Parse it with `proofcheck.carddata.parse_card_data(comment_body)`, which
returns `None` for cards without a block and only accepts the block at the
very end of the card. It is built from the validated result, like the rest of
the card, and capped at 4000 characters.
