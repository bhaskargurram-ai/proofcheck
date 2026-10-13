# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Agent: projects and Rust.** The agent writes its candidates in the
  project whose source the PR mainly changes (`[[project]]` tables), with that
  project's runner, module index, test style and working directory. For cargo
  projects it writes an integration test
  `<crate>/tests/proofcheck_agent_<variant>.rs` in the crate with the most
  changed `.rs` lines, with Rust prompts (public API only, `use <lib>::...`), a
  read-only `list_crates` tool, Rust static checks (balanced brackets, a
  `#[test]` fn, a reference to the crate, a denylist for processes, network,
  `unsafe`/FFI, `include!`/`env!` and `env::var`) and repairs (`use crate::`,
  top-level `use super::`, hyphenated package names). `verify` re-validates the
  manifest's project and directory against its own configuration, re-runs the
  Rust checks, and classifies a candidate that only fails to compile on base
  as weak. `--runner` accepts `cargo`; the manifest and the result's `agent`
  section gain additive `language`/`project` fields. The per-candidate
  timeout defaults to the project's timeout for cargo (1800 s). The reusable
  `agent.yml` workflow gains a `timeout-minutes` input for the verify job
  (default 60, was a fixed 30).

## [0.2.0] - Unreleased

### Added

- **Agent evidence (experimental, opt-in):** `proofcheck agent propose |
  verify | run | needed`. When a `fixes #N` claim has no deterministic
  evidence, an LLM with read-only, git-plumbing tools (no exec, write or
  network) proposes reproduction tests through a schema-validated
  `submit_test` call, using heterogeneous prompt variants
  (`reproduce-issue`, `exercise-diff`, `mimic-existing-tests`, optional
  `localize-first`), static validation and Otter-style import repair.
  `verify` (no LLM, no secrets) runs each candidate on base and head and
  classifies it as `fail_to_pass`, `fail_to_pass_weak`, `pass_to_pass`,
  `fails_on_head` (inconclusive) or `error`; a strong candidate upgrades the
  claim to "verified (agent test)", and agent evidence never yields ❌.
  Tool-use client for the Anthropic Messages API and OpenAI-compatible chat
  completions with retries, budgets and token/cost accounting; `[agent]`
  config table; card section "Agent evidence (experimental)". See
  `docs/AGENT.md`.
- **Diff evidence map (hunk ablation):** every non-test hunk of the PR is
  reverted on its own in a temporary head worktree (`git apply -R
  --unidiff-zero`) and the fail→pass tests and behaviour-changing issue
  reproducers are re-run. Hunks are *required* (reverting breaks the
  evidence), *not exercised* (nothing measured depends on them) or *skipped*.
  New files, deletions and renames are handled; binary, test and
  documentation files are not ablated. Deterministic order, hunk cap
  (`ablation.max_hunks`, 12), total time budget (`ablation.time_budget_s`,
  300 s), per-run timeouts, a clean-worktree baseline run and guaranteed
  cleanup. New card section, summary line ("3/4 changed hunks are required by
  the evidence"), an `unexercised` scope note, the `ablation` key in
  result.json (strictly validated on the comment side), the `[ablation]`
  config table and `proofcheck run --ablation/--no-ablation`.
- **Reusable workflows** `evidence.yml`, `agent.yml` and `comment.yml`
  (`on: workflow_call`), so a repository needs only three short caller
  workflows. proofcheck is installed from its own repository at
  `proofcheck-ref`, never from the pull request.
- **`proofcheck init [--agent] [--force] [--ref]`:** detects the test runner
  (pytest, jest, unittest) and writes the caller workflows and a commented
  `proofcheck.toml`; refuses to overwrite existing files.
- **`proofcheck summary`:** renders the validated card into
  `$GITHUB_STEP_SUMMARY`, so it is visible on fork PRs without a write token.
- **Verdict labels:** `proofcheck comment --labels` sets one of
  `proofcheck: verified | partial | contradicted | no evidence`, creating the
  label if needed and removing stale ones (idempotent).
- `proofcheck context --pr N` reads the PR text from the API (for
  `workflow_run` jobs).
- Issue reproducers: doctest-style `>>>` sessions (in `pycon`, `doctest`,
  `python`, `py` or unlabelled fences) are converted into scripts that print
  each expression like the interactive prompt.
- `release.yml` (PyPI trusted publishing and a GitHub release from the
  CHANGELOG) and `pages.yml` (deploys `docs/site/` on public repositories).
- Workflow security invariants are tested (`tests/test_workflows.py`).

### Changed

- This repository's own `proofcheck.yml`, `proofcheck-comment.yml` and new
  `proofcheck-agent.yml` call the local reusable workflows; the examples in
  `examples/workflows/` are the output of `proofcheck init`.
- `action.yml`: new commands `summary`, `agent-propose`, `agent-verify`; new
  inputs `ablation`, `labels`, `llm-*`, `repo-dir`; the job summary is
  rendered from the validated result instead of `card.md`; branding icon.
- `docs/SECURITY_MODEL.md` describes the three trust zones.

## [0.1.0] - 2026-10-13

Initial release.

### Added

- **Fail-to-pass test check:** the changed test files run in git worktrees of
  the base commit (with the head versions of the tests copied in) and of the
  head commit. Each touched test is classified as `fail_to_pass`,
  `pass_to_pass`, `fail_on_head`, `error` or `skipped`. AST-based detection
  ensures that untouched, pre-existing tests are not counted as evidence.
- Test runners: `pytest` (default), stdlib `unittest` (with a bundled JUnit
  XML bridge) and experimental `jest` (via `jest-junit`).
- Rule-based claim extraction for issue references, performance numbers,
  "no behaviour change", "adds tests" and "backwards compatible".
- Optional, off-by-default LLM claim extraction (OpenAI-compatible or
  Anthropic Messages API) with strict JSON validation and quote checking.
- Claim to evidence matching with documented rules; optional
  `benchmark_command` for verifying speed-up claims.
- Issue reproduction: fenced `python` blocks from referenced issues run on
  base and head with timeouts and a scrubbed environment.
- Scope check: workflow, dependency-manifest, binary and large-diff flags,
  plus a labelled heuristic for files unrelated to the description.
- Markdown proof card with escaping of all PR-derived text, and a validated
  JSON result (`proofcheck/result` v1).
- CLI: `proofcheck context`, `proofcheck run` and `proofcheck comment`
  (marker-based upsert, head-SHA check, harness-change warning).
- Composite GitHub Action and a two-workflow setup (`pull_request` +
  `workflow_run`), with a security model document.
- A demo script that builds a repository with a real fix and a fake fix.
