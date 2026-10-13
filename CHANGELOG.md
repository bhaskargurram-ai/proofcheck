# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

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
