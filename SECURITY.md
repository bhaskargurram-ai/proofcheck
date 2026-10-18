# Security policy

## Supported versions

Security fixes go into the latest release of proofcheck on PyPI (`proofcheck-ai`). Older releases are not patched; please upgrade.

## Reporting a vulnerability

Please report security problems privately, not in a public issue:

- through GitHub's private vulnerability reporting: https://github.com/bhaskargurram-ai/proofcheck/security/advisories/new
- or by email to gurrambhaskar.ai@gmail.com

Include the version, how to reproduce the problem, and what an attacker could do with it. You should get a reply within 7 days. Once a fix is released, the advisory is published with credit to the reporter unless you ask otherwise.

## Scope

The README's [security model](https://github.com/bhaskargurram-ai/proofcheck#security-model) describes what proofcheck trusts and what it doesn't, including how its GitHub workflows handle untrusted pull requests and where API keys are used. A way to break those guarantees, for example running untrusted code in a job that holds a secret, is in scope.
