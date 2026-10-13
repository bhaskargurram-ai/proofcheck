# Contributing to proofcheck

Thanks for your interest! Bug reports, runner implementations for new
ecosystems, and improvements to the claim rules are all welcome.

## Development setup

```bash
git clone <your fork>
cd proofcheck
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

Run the tests with either runner:

```bash
python -m pytest
python -m unittest discover -s tests -v
```

The suite creates real temporary git repositories and worktrees, so `git`
must be on your `PATH`. Tests do not need network access, and they do not
need pytest to be installed: when pytest is missing, they exercise the
pytest runner through a small stand-in (`tests/fake_pytest.py`).

To try the whole pipeline end to end:

```bash
examples/demo/run_demo.sh /tmp/proofcheck-demo
```

## Guidelines

- **Keep runtime dependencies minimal.** The only runtime dependencies are
  `click` and the standard library (plus `tomli` on Python 3.10). HTTP goes
  through `urllib`.
- **Security first.** proofcheck runs untrusted code and renders untrusted
  text. Read [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md) before changing
  `proc.py`, `card.py`, `result.py`, `github_api.py`, `action.yml` or the
  workflows. Specifically:
  - never use `shell=True` or interpolate untrusted strings into a shell;
  - every piece of PR-derived text in the card must go through `escape_md()`
    or `code()`;
  - any new field in `result.json` must be validated in `result.py`.
- **Claims must stay checkable.** A new claim type needs a matching evidence
  rule in `evidence.py`, documented in the README table, with positive *and*
  negative regex tests.
- **Be honest in the card.** Missing evidence is ⚠️, never ❌. Only mark a
  claim ❌ when executed evidence contradicts it.
- Code style: type hints, docstrings on public functions, small modules.
  Tests use `unittest.TestCase`, so both `pytest` and `unittest` can run them.

## Adding a test runner

Subclass `proofcheck.runners.TestRunner` and set `test_patterns`,
`module_patterns` and `default_command`. Then implement `build_command()` /
`env_for()` so that the runner writes a JUnit XML report, and
`test_belongs_to()` so that test ids map back to files. Register the class in
`RUNNERS` and add an end-to-end test that uses a temporary git repository
(see `tests/test_failtopass.py`).

## Reporting security issues

Please use the repository's private security advisory form rather than a
public issue.
