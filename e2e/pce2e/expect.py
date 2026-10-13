"""Match a card's data block (and labels / card text) against a scenario's expectations.

Every check returns a human-readable failure string; an empty list means the
scenario passed. Missing sections count as zero / ``"none"``.
"""

from __future__ import annotations

from typing import Any

#: Card wording accepted in ``agent_status`` lists, mapped to the data block's values.
AGENT_ALIASES = {
    "verified (agent test)": "evidence",
    "evidence": "evidence",
    "weak": "weak_evidence",
    "weak_evidence": "weak_evidence",
    "no evidence": "no_evidence",
    "no_evidence": "no_evidence",
    "inconclusive": "inconclusive",
    "error": "error",
    "skipped": "skipped",
    "none": "none",
    "absent": "none",
}
LABEL_PREFIX = "proofcheck: "


def _allowed(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def _fmt(values: list[str]) -> str:
    return values[0] if len(values) == 1 else "one of " + ", ".join(values)


def _counts(data: dict[str, Any], section: str) -> dict[str, Any]:
    s = data.get(section)
    return s if isinstance(s, dict) else {}


def _int(d: dict[str, Any], key: str) -> int:
    v = d.get(key, 0)
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _claim_matches(want: dict[str, Any], got: dict[str, Any]) -> bool:
    if got.get("type") not in _allowed(want["type"]):
        return False
    if "status" in want and got.get("status") not in _allowed(want["status"]):
        return False
    if "verified_by" in want:
        allowed = _allowed(want["verified_by"])
        if (got.get("verified_by") or "") not in allowed:
            return False
    if "text_contains" in want and not all(t.lower() in str(got.get("text", "")).lower() for t in _allowed(want["text_contains"])):
        return False
    return True


def evaluate(expected: dict[str, Any], data: dict[str, Any] | None, card: str | None = None,
             labels: list[str] | None = None) -> list[str]:
    """Return the list of failed expectations (empty: all met)."""
    if data is None:
        return ["no proofcheck data block found on the card"]
    fails: list[str] = []
    tests = _counts(data, "tests")
    ablation = _counts(data, "ablation")

    def check_in(key: str, actual: Any) -> None:
        if key in expected:
            allowed = _allowed(expected[key])
            if actual not in allowed:
                fails.append(f"{key}: expected {_fmt(allowed)}, got {actual!r}")

    check_in("verdict", data.get("verdict"))
    check_in("tests_status", tests.get("status", "none") if tests else "none")
    check_in("ablation_status", ablation.get("status", "none") if ablation else "none")

    bounds = {
        "fail_to_pass": (tests, "fail_to_pass"), "pass_to_pass": (tests, "pass_to_pass"),
        "fail_on_head": (tests, "fail_on_head"), "ablation_required": (ablation, "required"),
        "ablation_not_exercised": (ablation, "not_exercised"),
    }
    for name, (section, field) in bounds.items():
        actual = _int(section, field)
        lo, hi = expected.get(f"min_{name}"), expected.get(f"max_{name}")
        if lo is not None and actual < lo:
            fails.append(f"{name}: expected at least {lo}, got {actual}")
        if hi is not None and actual > hi:
            fails.append(f"{name}: expected at most {hi}, got {actual}")

    if "agent_status" in expected:
        allowed = [AGENT_ALIASES.get(a.lower(), a) for a in _allowed(expected["agent_status"])]
        agent = data.get("agent")
        actual = agent.get("status", "none") if isinstance(agent, dict) else "none"
        if actual not in allowed:
            fails.append(f"agent_status: expected {_fmt(allowed)}, got {actual!r}")

    flags = data.get("flags") if isinstance(data.get("flags"), list) else []
    for f in expected.get("flags", []):
        if f not in flags:
            fails.append(f"flags: expected {f!r} among {flags}")
    for f in expected.get("absent_flags", []):
        if f in flags:
            fails.append(f"flags: did not expect {f!r}")

    repro = data.get("repro") if isinstance(data.get("repro"), list) else []
    for r in expected.get("repro", []):
        if r not in repro:
            fails.append(f"repro: expected a snippet with status {r!r}, got {repro}")

    if "warnings" in expected:
        has = _int(data, "warnings") > 0
        if has != expected["warnings"]:
            fails.append(f"warnings: expected {'a' if expected['warnings'] else 'no'} trusted warning on the card, "
                         f"got {_int(data, 'warnings')}")

    claims = [c for c in (data.get("claims") or []) if isinstance(c, dict)]
    if "claim_count" in expected and len(claims) != expected["claim_count"]:
        fails.append(f"claim_count: expected {expected['claim_count']}, got {len(claims)}")
    used: set[int] = set()
    for want in expected.get("claims", []):
        idx = next((i for i, c in enumerate(claims) if i not in used and _claim_matches(want, c)), None)
        if idx is None:
            same_type = [f"{c.get('type')}={c.get('status')}" + (f" (by {c['verified_by']})" if c.get("verified_by") else "")
                         for c in claims if c.get("type") in _allowed(want["type"])]
            fails.append(f"claims: no claim matching {want}; claims of that type: {same_type or 'none'}")
        else:
            used.add(idx)

    if "label" in expected:
        allowed = _allowed(expected["label"])
        ours = sorted(lbl for lbl in (labels or []) if lbl.startswith(LABEL_PREFIX))
        if allowed == [""]:
            if ours:
                fails.append(f"label: expected no proofcheck label, got {ours}")
        elif not any(a in ours for a in allowed):
            fails.append(f"label: expected {_fmt(allowed)}, got {ours or 'none'}")

    text = card or ""
    for s in expected.get("card_contains", []):
        if s not in text:
            fails.append(f"card_contains: {s!r} not found in the card")
    for s in expected.get("card_not_contains", []):
        if s in text:
            fails.append(f"card_not_contains: {s!r} found in the card")
    return fails
