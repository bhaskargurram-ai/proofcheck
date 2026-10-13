"""Agent evidence (experimental, opt-in): LLM-written reproduction tests.

When a pull request claims to fix an issue but the deterministic checks found
no evidence (no fail-to-pass test, no behaviour-changing issue reproducer),
an LLM agent can try to write a *reproduction test*. proofcheck then runs
that test itself on the base and head commits. The agent never decides
anything: only the executed outcome counts.

The work is split so that the API key and untrusted code never meet:

``propose`` (:mod:`proofcheck.agent.propose`)
    Holds the API key. The model gets **read-only** repository tools that are
    implemented with git plumbing (``git show``, ``git ls-tree``,
    ``git grep``); nothing from the repository is executed. Its only output
    channel is a schema-validated ``submit_test`` tool call.

``verify`` (:mod:`proofcheck.agent.verify`)
    No LLM and no key. Runs each candidate test on base and head with the
    normal test runners and classifies it.

``run`` (:mod:`proofcheck.agent.loop`)
    propose + verify + refinement rounds in one process. Local/trusted use
    only, because there the untrusted test code runs next to the API key.

See ``docs/AGENT.md`` for the design, the safety model and limitations.
"""

from __future__ import annotations

#: Prompt variants, in the order they run. The first three are the default.
VARIANTS = ("reproduce-issue", "exercise-diff", "mimic-existing-tests", "localize-first")
DEFAULT_VARIANT_COUNT = 3

# Candidate classifications (per candidate test file).
FAIL_TO_PASS = "fail_to_pass"  # fails on base for a behavioural reason, passes on head: strong
FAIL_TO_PASS_WEAK = "fail_to_pass_weak"  # fails on base only because a symbol is missing: weak
PASS_TO_PASS = "pass_to_pass"  # passes on both: no evidence
FAILS_ON_HEAD = "fails_on_head"  # fails on head: inconclusive (agent tests can be wrong)
ERROR = "error"  # could not be run / no report
INVALID = "invalid"  # rejected by static validation
NO_SUBMISSION = "no_submission"  # the model never submitted a valid test

CANDIDATE_CLASSES = (FAIL_TO_PASS, FAIL_TO_PASS_WEAK, PASS_TO_PASS, FAILS_ON_HEAD, ERROR, INVALID, NO_SUBMISSION)

# Overall status of the agent section in result.json.
STATUS_EVIDENCE = "evidence"
STATUS_WEAK = "weak_evidence"
STATUS_NO_EVIDENCE = "no_evidence"
STATUS_INCONCLUSIVE = "inconclusive"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"

AGENT_STATUSES = (STATUS_EVIDENCE, STATUS_WEAK, STATUS_NO_EVIDENCE, STATUS_INCONCLUSIVE, STATUS_ERROR, STATUS_SKIPPED)

#: Evidence text placed on a ``fixes #N`` claim upgraded by an agent test.
AGENT_EVIDENCE_TEXT = "an agent-written reproduction test fails on base and passes on head"

MANIFEST_SCHEMA = "proofcheck/agent-candidates"
MANIFEST_VERSION = 1
