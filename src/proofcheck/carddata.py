"""Machine-readable data block at the end of the proof card.

The card ends with one HTML comment::

    <!-- proofcheck:data {"v":1,"head":"<40-hex>","verdict":"evidence",...} -->

GitHub does not render it, but anything that reads the comment body through
the API (the end-to-end harness in ``e2e/``, bots, dashboards) can find the
verdict, the claim statuses and the counts without scraping Markdown.

Safety:

* the block is built only from fields of a *validated* result document
  (:func:`proofcheck.result.validate_result`); the comment workflow
  re-renders the card, and therefore the block, from validated data;
* the JSON is compact and ASCII-only, and every ``-``, ``<``, ``>``, ``&``
  and ``@`` is written as a ``\\u`` escape, so no sequence inside it can end the comment
  (``-->``, ``--!>``) or open markup; JSON parsers read the escapes back
  transparently;
* claim texts are cleaned and cut to 80 characters, lists are capped, and the
  encoded block never exceeds :data:`MAX_DATA_CHARS` (claims are dropped
  first, then optional sections, when it would);
* :func:`parse_card_data` only accepts the block when it is the *last* thing
  in the card, so a look-alike block that PR-controlled text manages to place
  earlier in the card (for example inside a code span) is never picked up.
"""

from __future__ import annotations

import json
import re
from typing import Any

DATA_PREFIX = "<!-- proofcheck:data "
DATA_SUFFIX = " -->"
#: Version of the data block format (``"v"``).
DATA_VERSION = 1
#: Hard cap for the whole block, prefix and suffix included.
MAX_DATA_CHARS = 4000
MAX_CLAIMS = 20
MAX_CLAIM_TEXT = 80

#: Written as ``\\u`` escapes: comment terminators, markup, and ``@`` so that even
#: a raw copy of the body cannot carry a live @mention.
_ESCAPED = "-<>&@"
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f  ‪-‮⁦-⁩]")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_TRAILING_RE = re.compile(r"(?:\A|\n)<!-- proofcheck:data (\{[^\n]*\}) -->\s*\Z")


def _text(value: Any, limit: int) -> str:
    s = _CONTROL_RE.sub(" ", "" if value is None else str(value))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _sha(value: Any) -> str | None:
    return value if isinstance(value, str) and _SHA_RE.match(value) else None


def build_card_data(result: dict[str, Any], trusted_warnings: int = 0) -> dict[str, Any]:
    """The data block's content for a validated ``result``.

    ``trusted_warnings`` is the number of warnings the privileged side placed
    on the card (e.g. "this PR modifies the workflow that produced the card").
    """
    data: dict[str, Any] = {
        "v": DATA_VERSION,
        "tool": _text(result.get("tool_version"), 20),
        "pr": result.get("pr_number") if _count(result.get("pr_number")) else None,
        "head": _sha(result.get("head")),
        "base": _sha(result.get("base")),
        "merge_base": _sha(result.get("merge_base")),
        "verdict": _text(result.get("verdict"), 20),
    }
    claims = []
    for c in (result.get("claims") or [])[:MAX_CLAIMS]:
        item = {"type": _text(c.get("type"), 30), "text": _text(c.get("text"), MAX_CLAIM_TEXT),
                "status": _text(c.get("status"), 20)}
        if c.get("verified_by"):
            item["verified_by"] = _text(c.get("verified_by"), 20)
        claims.append(item)
    data["claims"] = claims
    tests = result.get("tests")
    if tests:
        counts = tests.get("counts") or {}
        data["tests"] = {"status": _text(tests.get("status"), 20),
                         **{k: _count(counts.get(k)) for k in ("fail_to_pass", "pass_to_pass", "fail_on_head", "error", "skipped")}}
    else:
        data["tests"] = None
    ablation = result.get("ablation")
    if ablation:
        data["ablation"] = {"status": _text(ablation.get("status"), 20),
                            **{k: _count(ablation.get(k)) for k in ("total_hunks", "tested", "required", "not_exercised", "skipped")}}
    else:
        data["ablation"] = None
    data["repro"] = [_text(s.get("status"), 20) for r in (result.get("repro") or [])[:10] for s in (r.get("snippets") or [])[:5]]
    agent = result.get("agent")
    if agent:
        winner = agent.get("winner") or {}
        data["agent"] = {"status": _text(agent.get("status"), 20),
                         "winner": _text(winner.get("classification"), 20) or None,
                         "candidates": len(agent.get("candidates") or [])}
    else:
        data["agent"] = None
    scope = result.get("scope") or {}
    data["flags"] = sorted({_text(f.get("kind"), 20) for f in (scope.get("flags") or [])})
    data["files"] = _count(scope.get("file_count"))
    data["warnings"] = _count(trusted_warnings)
    return data


def encode_card_data(data: dict[str, Any]) -> str:
    """Compact JSON that is safe inside an HTML comment (no ``-``, ``<``, ``>``, ``&``, ``@``)."""
    s = json.dumps(data, separators=(",", ":"), ensure_ascii=True, sort_keys=False, allow_nan=False)
    # Negative numbers never occur (counts are validated >= 0), so every "-"
    # is inside a string, where "-" decodes back to "-".
    for ch in _ESCAPED:
        s = s.replace(ch, f"\\u{ord(ch):04x}")
    return s


def card_data_block(result: dict[str, Any], trusted_warnings: int = 0) -> str:
    """The ``<!-- proofcheck:data ... -->`` line for a validated result, at most MAX_DATA_CHARS long."""
    data = build_card_data(result, trusted_warnings)
    budget = MAX_DATA_CHARS - len(DATA_PREFIX) - len(DATA_SUFFIX)
    payload = encode_card_data(data)
    # Too big (only possible with many long claims): shorten claim texts,
    # then drop claims from the end, then drop the claim list altogether.
    if len(payload) > budget:
        for c in data["claims"]:
            c["text"] = _text(c["text"], 24)
        payload = encode_card_data(data)
    while len(payload) > budget and data["claims"]:
        data["claims"].pop()
        data["claims_truncated"] = True
        payload = encode_card_data(data)
    if len(payload) > budget:  # pragma: no cover - fixed-size fields only
        data = {k: data[k] for k in ("v", "head", "base", "verdict")}
        payload = encode_card_data(data)
    return DATA_PREFIX + payload + DATA_SUFFIX


def parse_card_data(markdown: Any) -> dict[str, Any] | None:
    """Return the data block of a proof card, or None.

    Only a block at the very end of the card (trailing whitespace allowed)
    counts. Returns None for cards rendered before the block existed, for
    malformed JSON and for anything that is not a JSON object with ``"v"``.
    """
    if not isinstance(markdown, str):
        return None
    m = _TRAILING_RE.search(markdown[-(MAX_DATA_CHARS + 64):])
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("v"), int):
        return None
    return data
