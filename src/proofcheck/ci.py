"""CI integration helpers: the job summary and API-based PR context.

* :func:`write_summary` renders the proof card from a *validated* result into
  ``$GITHUB_STEP_SUMMARY``. It needs no token, so the card is visible on the
  workflow run page even for pull requests from forks, where the unprivileged
  workflow cannot comment.
* :func:`context_from_api` builds the PR context (title, body) from the GitHub
  API instead of the event payload. Jobs triggered by ``workflow_run`` (the
  agent workflow) have no ``pull_request`` payload, and the API is a trusted
  source, unlike an artifact written next to PR code.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from .card import render_card
from .check import PRContext
from .github_api import env_client
from .result import validate_result

#: Maximum size of result.json accepted for rendering.
MAX_RESULT_BYTES = 5_000_000
#: GitHub limits one step summary to 1 MiB.
MAX_SUMMARY_BYTES = 1_000_000


def load_validated(path: str | Path) -> dict:
    """Read and validate result.json (raises ValueError / ResultValidationError)."""
    p = Path(path)
    if p.stat().st_size > MAX_RESULT_BYTES:
        raise ValueError("result.json is too large")
    return validate_result(json.loads(p.read_text(encoding="utf-8")))


def write_summary(json_path: str | Path, out_path: str | Path | None = None, env: Mapping[str, str] | None = None) -> str | None:
    """Append the card to ``out_path`` or ``$GITHUB_STEP_SUMMARY``; print it when neither is set.

    Returns the path written to, or ``None`` when the card was printed.
    """
    e = os.environ if env is None else env
    card = render_card(load_validated(json_path))
    dest = str(out_path) if out_path else e.get("GITHUB_STEP_SUMMARY") or None
    if dest is None:
        print(card, end="")
        return None
    data = card.encode("utf-8")[:MAX_SUMMARY_BYTES].decode("utf-8", errors="ignore")
    with open(dest, "a", encoding="utf-8") as fh:
        fh.write(data + "\n")
    return dest


def context_from_api(number: int, env: Mapping[str, str] | None = None, transport=None) -> PRContext:
    """PR title/body from ``GET /repos/{repo}/pulls/{number}``."""
    client = env_client(env, transport=transport)
    pr = client.get_pull(number)
    return PRContext(
        title=str(pr.get("title") or ""),
        body=str(pr.get("body") or ""),
        repo=client.repo,
        number=number,
    )
