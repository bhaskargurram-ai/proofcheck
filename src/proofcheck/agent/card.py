"""The "Agent evidence (experimental)" section of the proof card.

Everything in ``result['agent']`` is untrusted: the model wrote the test
code and the rationale, and the PR's code produced the test messages. Text
goes through the card's escaping helpers; the winning test is shown inside a
fenced code block whose fence is longer than any backtick run in the code,
after stripping control and bidi characters, so it cannot break out of the
block or inject HTML, mentions or links.
"""

from __future__ import annotations

import re
from typing import Any

from ..card import _clean, _defuse, code, escape_md
from . import (
    ERROR,
    FAIL_TO_PASS,
    FAIL_TO_PASS_WEAK,
    FAILS_ON_HEAD,
    INVALID,
    NO_SUBMISSION,
    PASS_TO_PASS,
    STATUS_ERROR,
    STATUS_EVIDENCE,
    STATUS_INCONCLUSIVE,
    STATUS_NO_EVIDENCE,
    STATUS_SKIPPED,
    STATUS_WEAK,
)

MAX_CODE_LINES = 150
MAX_CODE_CHARS = 8_000

HEADLINES = {
    STATUS_EVIDENCE: "✅ An agent-written reproduction test **fails on the base commit and passes on the PR head**.",
    STATUS_WEAK: ("⚠️ **Weak:** an agent-written test fails on base only because a name or import is missing there, "
                  "so it shows the API is new rather than that the bug is fixed."),
    STATUS_NO_EVIDENCE: ("⚠️ The agent did not find a test that fails on base and passes on head. This is **no evidence**, "
                         "not a sign that the PR is wrong: agents often fail to write a reproduction test."),
    STATUS_INCONCLUSIVE: ("❔ The agent's tests fail on the PR head. Agent-written tests can be wrong, so this is "
                          "**inconclusive**, not a contradiction."),
    STATUS_ERROR: "❔ Agent evidence could not be gathered.",
    STATUS_SKIPPED: "⏭️ Skipped.",
}
CLASS_LABELS = {
    FAIL_TO_PASS: "✅ fail → pass",
    FAIL_TO_PASS_WEAK: "⚠️ fail → pass (weak)",
    PASS_TO_PASS: "⚠️ pass → pass",
    FAILS_ON_HEAD: "❔ fails on head",
    ERROR: "❔ error",
    INVALID: "❔ rejected",
    NO_SUBMISSION: "❔ no test",
}

_STRIP_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩﻿]")


def safe_code_block(src: Any, lang: str = "python") -> str:
    """A fenced code block that untrusted ``src`` cannot escape."""
    s = _STRIP_RE.sub("", str(src or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    "))
    lines = s.rstrip().split("\n")
    truncated = False
    if len(lines) > MAX_CODE_LINES:
        lines, truncated = lines[:MAX_CODE_LINES], True
    s = "\n".join(lines)
    if len(s) > MAX_CODE_CHARS:
        s, truncated = s[:MAX_CODE_CHARS], True
    if truncated:
        s += ("\n// … truncated; see result.json" if lang == "rust" else "\n# … truncated; see result.json")
    longest = max((len(m) for m in re.findall(r"`+|~+", s)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{s}\n{fence}"


def _tokens(usage: dict[str, Any]) -> str:
    tin = int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_tokens", 0)) + int(usage.get("cache_write_tokens", 0))
    return f"{tin:,} in / {int(usage.get('output_tokens', 0)):,} out tokens"


def agent_section(result: dict[str, Any]) -> list[str]:
    """Markdown lines for the agent section (empty when the result has none)."""
    a = result.get("agent")
    if not a:
        return []
    out = ["### 🤖 Agent evidence (experimental)", ""]
    status = a.get("status", STATUS_ERROR)
    rust = a.get("language") == "rust"
    head = HEADLINES.get(status, HEADLINES[STATUS_ERROR])
    if rust and status == STATUS_WEAK:
        head = ("⚠️ **Weak:** an agent-written Rust test fails on base only because it does not compile there "
                "(it uses an item the PR adds), so it shows the API is new rather than that the bug is fixed.")
    if status in (STATUS_SKIPPED, STATUS_ERROR) and a.get("reason"):
        head += f" {escape_md(a['reason'], 200)}"
    out.append(head)
    target = a.get("target_claim")
    if target:
        out.append(f"<sub>Looking for evidence for: {escape_md(target, 80)}</sub>")
    cands = a.get("candidates") or []
    if cands:
        out += ["", "| Variant | Result | Detail |", "|---|---|---|"]
        for c in cands[:12]:
            variant = escape_md(c.get("variant", ""), 40) + (f" (round {int(c.get('round', 0)) + 1})" if c.get("round") else "")
            out.append(f"| {variant} | {CLASS_LABELS.get(c.get('classification'), '❔')} | {escape_md(c.get('message', ''), 160)} |")
    meta = []
    if a.get("model"):
        meta.append(f"model {code(a['model'], 60)}" + (f" via {escape_md(a.get('provider', ''), 20)}" if a.get("provider") else ""))
    if a.get("variants"):
        meta.append(f"{len(a['variants'])} prompt variant{'s' if len(a['variants']) != 1 else ''}")
    if a.get("rounds", 1) > 1:
        meta.append(f"{a['rounds']} rounds")
    usage = a.get("usage") or {}
    if usage.get("requests"):
        meta.append(f"{int(usage['requests'])} requests, {_tokens(usage)}")
    if a.get("cost_usd") is not None:
        meta.append(f"≈ ${a['cost_usd']:.4f} (from configured prices)")
    elif usage.get("requests"):
        meta.append("cost not computed (no prices configured)")
    if meta:
        out += ["", "- " + " · ".join(meta)]
    w = a.get("winner")
    if w and w.get("code"):
        label = "Reproduction test" if w.get("classification") == FAIL_TO_PASS else "Best candidate (weak)"
        out += ["", "<details>", f"<summary>{label}: <code>{_defuse(_clean(w.get('file', ''), 120))}</code></summary>", ""]
        if w.get("rationale"):
            out += [f"_Agent's rationale (unverified):_ {escape_md(w['rationale'], 400)}", ""]
        out += [safe_code_block(w["code"], "rust" if rust or str(w.get("file", "")).endswith(".rs") else "python"),
                "", "</details>"]
        if w.get("classification") == FAIL_TO_PASS:
            out += ["", "> Commit this test to keep the evidence: it is not part of the PR until someone adds it."]
    notes = (a.get("notes") or [])[:4]
    if notes:
        out.append("")
        out += [f"- {escape_md(n, 200)}" for n in notes]
    out += ["", "<sub>Agent tests are written by an LLM with read-only access to the repository and run by proofcheck "
            "on base and head. Only the executed outcome counts, and a failed attempt never counts against the PR.</sub>", ""]
    return out
