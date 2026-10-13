"""Render the proof card (Markdown) from a *validated* result document.

Everything that can originate from the pull request -- claim text, quoted
sentences, test names, file paths, test output -- is untrusted. Before it is
placed in the card it is:

* stripped of control characters and truncated;
* HTML-escaped (``<``, ``>``, ``&``), so no raw HTML, comments or the card
  marker can be injected;
* Markdown that could create links, images or formatting (``[ ] ! * _ ~ ` # |``
  and backslash) is backslash-escaped;
* defused for ``@mentions`` and ``#123`` / ``owner/repo#1`` references (a
  zero-width joiner is inserted so GitHub does not notify people or
  cross-link issues);
* defused for bare URLs (no autolinks);
* pipe-escaped inside tables, with newlines collapsed.
"""

from __future__ import annotations

import re
from typing import Any

CARD_MARKER = "<!-- proofcheck:card -->"
#: GitHub's comment limit is 65536 characters; stay well below it.
MAX_CARD_CHARS = 60_000
MAX_TEST_ROWS = 25

ZWJ = "\u200d"
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
_MD_SPECIAL_RE = re.compile(r"([\\`*_\[\]#!~|])")
_MENTION_RE = re.compile(r"@(?=[A-Za-z0-9])")
_URL_RE = re.compile(r"(?i)\b(https?|ftp|file|javascript|data)(:)")
_WWW_RE = re.compile(r"(?i)\bwww\.")

ICONS = {"verified": "✅", "unverified": "⚠️", "contradicted": "❌"}
LABELS = {"verified": "verified", "unverified": "no evidence", "contradicted": "contradicted"}
TEST_ICONS = {"fail_to_pass": "✅", "pass_to_pass": "⚠️", "fail_on_head": "❌", "error": "❔", "skipped": "⏭️"}
TEST_LABELS = {
    "fail_to_pass": "fail → pass",
    "pass_to_pass": "pass → pass",
    "fail_on_head": "fails on head",
    "error": "error",
    "skipped": "skipped",
}
VERDICT_HEADLINES = {
    "evidence": "✅ The PR's claims are backed by evidence",
    "partial": "⚠️ Some claims are backed by evidence, others are not",
    "no_evidence": "⚠️ No evidence found for the PR's claims",
    "contradicted": "❌ Evidence contradicts the PR",
    "error": "⚠️ proofcheck could not complete its checks",
}


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _clean(text: Any, limit: int) -> str:
    s = "" if text is None else str(text)
    s = _CONTROL_RE.sub(" ", s.replace("\r", " "))
    s = re.sub(r"\s+", " ", s).strip()
    return _truncate(s, limit)


def _defuse(s: str) -> str:
    """Neutralize HTML, mentions and links in already-cleaned text (no Markdown escaping)."""
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = _MENTION_RE.sub("@" + ZWJ, s)
    s = _URL_RE.sub(lambda m: m.group(1) + ZWJ + m.group(2), s)
    s = _WWW_RE.sub(lambda m: m.group(0)[:3] + ZWJ + ".", s)
    return s


def escape_md(text: Any, limit: int = 300) -> str:
    """Escape untrusted text for use in Markdown prose or a table cell."""
    s = _clean(text, limit)
    s = _MD_SPECIAL_RE.sub(r"\\\1", s)
    s = _defuse(s)
    # "#123" and "owner/repo#123" cross-references: break them up.
    s = re.sub(r"\\#(?=\d)", "#" + ZWJ, s)
    return s


def code(text: Any, limit: int = 160) -> str:
    """Render untrusted text as an inline code span that is safe inside a table cell."""
    s = _clean(text, limit).replace("`", "'")
    if not s:
        return "` `"
    s = s.replace("|", "\\|")
    # Inside a code span HTML and mentions are inert; only the pipe needs escaping.
    return f"`{s}`"


def _sha(s: Any) -> str:
    s = str(s or "")
    return s[:7] if re.fullmatch(r"[0-9a-f]{7,64}", s) else "?"


def _summary_line(result: dict[str, Any]) -> str:
    claims = result.get("claims") or []
    parts = []
    if claims:
        v = sum(c["status"] == "verified" for c in claims)
        x = sum(c["status"] == "contradicted" for c in claims)
        parts.append(f"{v}/{len(claims)} claims verified" + (f", {x} contradicted" if x else ""))
    else:
        parts.append("no checkable claims in the PR text")
    tests = result.get("tests")
    if tests:
        c = tests.get("counts", {})
        if tests["status"] == "no_tests":
            parts.append("no tests added or changed")
        elif tests["status"] == "error" and not tests.get("tests"):
            parts.append("test run failed")
        else:
            parts.append(f"{c.get('fail_to_pass', 0)} fail→pass, {c.get('pass_to_pass', 0)} pass→pass, {c.get('fail_on_head', 0)} failing on head")
    ablation_line = _ablation_summary(result)
    if ablation_line:
        parts.append(ablation_line)
    scope = result.get("scope")
    if scope:
        flags = len(scope.get("flags") or [])
        parts.append(f"{scope.get('file_count', 0)} files (+{scope.get('total_additions', 0)}/−{scope.get('total_deletions', 0)})"
                     + (f", {flags} scope note{'s' if flags != 1 else ''}" if flags else ""))
    return " · ".join(parts)


def _claims_section(result: dict[str, Any]) -> list[str]:
    out = ["### Claims", ""]
    claims = result.get("claims") or []
    if not claims:
        out += ["_No checkable claims (issue references, performance numbers, \"adds tests\", \"no behaviour change\", "
                "\"backwards compatible\") were found in the PR title or description._", ""]
        return out
    out += ["| | Claim | Evidence |", "|---|---|---|"]
    for c in claims:
        icon = ICONS.get(c["status"], "⚠️")
        label = LABELS.get(c["status"], c["status"])
        claim = f"**{escape_md(c['text'], 80)}**"
        if c.get("source") and _clean(c["source"], 400) != _clean(c["text"], 400):
            claim += f"<br><sub>“{escape_md(c['source'], 160)}”</sub>"
        if c.get("origin") == "llm":
            claim += " <sub>(LLM-extracted)</sub>"
        out.append(f"| {icon} {label} | {claim} | {escape_md(c.get('evidence', ''), 300)} |")
    out.append("")
    return out


def _tests_section(result: dict[str, Any]) -> list[str]:
    tests = result.get("tests")
    if not tests:
        return []
    out = ["### Tests: do the changed tests fail before the change and pass after it?", ""]
    status = tests["status"]
    if status == "no_tests":
        out.append("⚠️ **No test evidence:** this PR does not add or change any test.")
    elif status == "evidence":
        out.append("✅ At least one changed test **fails on the base commit and passes on the PR head**: it exercises the change.")
    elif status == "no_evidence":
        out.append("⚠️ The changed tests **pass on the base commit too**, so they do not demonstrate the change.")
    elif status == "fail_on_head":
        out.append("❌ Some changed tests **fail on the PR head**.")
    elif status == "error":
        out.append("❔ The tests could not be run to completion; see notes.")
    rows = [t for t in tests.get("tests", []) if t.get("touched", True)]
    if rows:
        out += ["", "| Result | Test | Base | Head |", "|---|---|---|---|"]
        for t in rows[:MAX_TEST_ROWS]:
            icon = TEST_ICONS.get(t["classification"], "❔")
            out.append(f"| {icon} {TEST_LABELS.get(t['classification'], t['classification'])} | {code(t['test_id'], 120)} "
                       f"| {escape_md(t['base'], 12)} | {escape_md(t['head'], 12)} |")
        if len(rows) > MAX_TEST_ROWS:
            out.append(f"| | _…and {len(rows) - MAX_TEST_ROWS} more (see result.json)_ | | |")
    old = tests.get("old_tests_on_head") or {}
    notes = list(tests.get("notes") or [])
    if old.get("ran"):
        if old.get("failing"):
            notes.append(f"The base versions of {len(old.get('files', []))} modified test file(s) fail on head "
                         f"({len(old['failing'])} failing): existing expectations were changed.")
        else:
            notes.append(f"The base versions of modified test files still pass on head ({old.get('passed', 0)} passed).")
    for key, label in (("head_run", "head"), ("base_run", "base")):
        run = tests.get(key) or {}
        if run.get("error"):
            notes.append(f"{label} run: {run['error']}")
    if notes:
        out.append("")
        out += [f"- {escape_md(n, 300)}" for n in notes[:8]]
    out.append("")
    return out


def _repro_section(result: dict[str, Any]) -> list[str]:
    repros = result.get("repro") or []
    if not repros:
        return []
    out = ["### Issue reproduction", ""]
    for r in repros:
        raw_label = str(r["issue"])
        if raw_label.startswith("issue-file"):
            label = "Issue (from file" + (f" {code(raw_label.split(':', 1)[1], 40)}" if ":" in raw_label else "") + ")"
        else:
            label = escape_md(raw_label, 60)
        if r["status"] in ("no_snippets", "not_found"):
            out.append(f"- **{label}**: {escape_md(r.get('note') or 'nothing to run', 200)}")
            continue
        for s in r.get("snippets", []):
            b, h = s["base"], s["head"]
            if s["status"] == "behavior_changed":
                verdict = "🔁 behaviour changed"
            elif s["status"] == "unchanged":
                verdict = "⚠️ unchanged"
            else:
                verdict = "❔ error"
            out.append(f"- **{label}**, snippet {s['index']}: {verdict} — base exit {b.get('exit_code')}"
                       f"{' (timeout)' if b.get('timed_out') else ''} {code(b.get('last_line', ''), 80)}"
                       f" → head exit {h.get('exit_code')}{' (timeout)' if h.get('timed_out') else ''} {code(h.get('last_line', ''), 80)}")
    out.append("")
    return out


def _bench_section(result: dict[str, Any]) -> list[str]:
    b = result.get("benchmark")
    if not b:
        return []
    out = ["### Benchmark", ""]
    if b.get("error"):
        out.append(f"- ❔ {escape_md(b['error'], 200)}")
    elif b.get("speedup") is not None:
        out.append(f"- {code(' '.join(b.get('command', [])), 100)}: median {b['base_median']:.3f}s on base → "
                   f"{b['head_median']:.3f}s on head (**{b['speedup']:.2f}×**, {b.get('runs')} runs each)")
    out.append("")
    return out


def _scope_section(result: dict[str, Any]) -> list[str]:
    scope = result.get("scope")
    if not scope:
        return []
    out = ["### Scope", ""]
    out.append(f"- {scope.get('file_count', 0)} files changed (+{scope.get('total_additions', 0)} / −{scope.get('total_deletions', 0)} lines)")
    for f in scope.get("flags") or []:
        paths = f.get("paths") or []
        shown = ", ".join(code(p, 80) for p in paths[:8])
        more = f" and {len(paths) - 8} more" if len(paths) > 8 else ""
        prefix = "🔎" if f.get("heuristic") else "⚠️"
        out.append(f"- {prefix} {escape_md(f.get('message', ''), 200)}" + (f" {shown}{more}" if shown else ""))
    out.append("")
    return out


HUNK_ICONS = {"required": "✅", "not_exercised": "⚠️", "skipped": "⏭️"}
HUNK_LABELS = {"required": "required", "not_exercised": "not exercised", "skipped": "skipped"}
MAX_HUNK_ROWS = 25


def _ablation_summary(result: dict[str, Any]) -> str:
    from .ablation import summary_text

    return summary_text(result.get("ablation") or {})


def _ablation_section(result: dict[str, Any]) -> list[str]:
    """The "Diff evidence map" section (hunk ablation)."""
    a = result.get("ablation")
    if not a or a.get("status") in ("not_applicable", "no_hunks"):
        return []
    out = ["### Diff evidence map", ""]
    if a["status"] == "baseline_failed":
        out += ["❔ The evidence did not reproduce in a fresh head worktree, so the hunks were not ablated.", ""]
    elif a["status"] == "error":
        out += ["❔ The diff evidence map could not be completed; see notes.", ""]
    else:
        out += ["Each changed hunk was reverted on its own and the fail→pass tests / changed reproducers were re-run. "
                "✅ *required*: reverting it breaks the evidence. ⚠️ *not exercised*: nothing that was measured depends on it.", ""]
    rows = a.get("hunks") or []
    if rows:
        out += ["| Hunk | Status | Evidence that broke |", "|---|---|---|"]
        for h in rows[:MAX_HUNK_ROWS]:
            icon = HUNK_ICONS.get(h["status"], "❔")
            label = HUNK_LABELS.get(h["status"], h["status"])
            if h["status"] == "required":
                broke = ", ".join(code(b, 80) for b in h.get("broken_by", [])[:3])
                extra = len(h.get("broken_by", [])) - 3
                detail = broke + (f" and {extra} more" if extra > 0 else "")
            elif h["status"] == "skipped":
                detail = escape_md(h.get("reason", ""), 160)
            else:
                detail = "—"
            out.append(f"| {code(h.get('location') or h['file'], 120)} | {icon} {label} | {detail} |")
        if len(rows) > MAX_HUNK_ROWS:
            out.append(f"| _…and {len(rows) - MAX_HUNK_ROWS} more (see result.json)_ | | |")
    notes = list(a.get("notes") or [])
    if notes:
        out.append("")
        out += [f"- {escape_md(n, 300)}" for n in notes[:5]]
    out.append("")
    return out


def harness_warning(paths: list[str]) -> str:
    """Warning shown when the PR modifies the CI workflow or test harness that produced the card."""
    shown = ", ".join(code(p, 80) for p in paths[:6]) + (f" and {len(paths) - 6} more" if len(paths) > 6 else "")
    return ("> [!WARNING]\n> This PR modifies files that control how this check runs (" + shown + "). "
            "The results below were produced by the PR's own workflow run and could have been altered by the PR. "
            "Re-run proofcheck after reviewing those changes.")


def render_card(result: dict[str, Any], trusted_warnings: list[str] | None = None) -> str:
    """Render the Markdown proof card for a validated result.

    ``trusted_warnings`` are Markdown snippets produced by the privileged side
    (never from result data); they are placed verbatim above the summary.
    """
    verdict = result.get("verdict", "error")
    lines = [CARD_MARKER, f"## proofcheck: {VERDICT_HEADLINES.get(verdict, VERDICT_HEADLINES['error'])}", ""]
    for w in trusted_warnings or []:
        lines += [w, ""]
    lines += [_summary_line(result), ""]
    lines += _claims_section(result)
    lines += _tests_section(result)
    lines += _repro_section(result)
    lines += _ablation_section(result)
    lines += _bench_section(result)
    lines += _scope_section(result)
    for n in (result.get("notes") or [])[:5]:
        lines.append(f"> {escape_md(n, 300)}")
    if result.get("notes"):
        lines.append("")
    lines += [
        "---",
        "<sub>proofcheck judges <b>evidence, not authorship</b>: it runs the PR's tests on the base and head commits, "
        "replays issue reproducers and checks claims against what it measured. It does not guess whether a human or an AI "
        "wrote the code. ⚠️ means <i>no evidence</i>, not <i>wrong</i>. "
        f"Base <code>{_sha(result.get('merge_base') or result.get('base'))}</code> → head <code>{_sha(result.get('head'))}</code> · "
        f"proofcheck {escape_md(result.get('tool_version', ''), 20)}</sub>",
    ]
    card = "\n".join(lines) + "\n"
    if len(card) > MAX_CARD_CHARS:
        card = card[: MAX_CARD_CHARS - 200] + "\n\n_…card truncated; see result.json for the full report._\n"
    return card
