"""JUnit XML parsing (the lingua franca of test runners).

pytest (``--junitxml``), jest-junit, ``proofcheck.unittest_junit`` and most
other runners can emit this format. We only need per-test outcomes.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

PASSED = "passed"
FAILED = "failed"
ERROR = "error"
SKIPPED = "skipped"

#: Higher rank wins when the same test id appears more than once
#: (e.g. pytest reports a teardown error as a second entry).
_RANK = {SKIPPED: 0, PASSED: 1, FAILED: 2, ERROR: 3}

#: Upper bound on the size of a JUnit file we are willing to parse.
MAX_JUNIT_BYTES = 50_000_000


class JUnitParseError(ValueError):
    """Raised when a JUnit XML document cannot be parsed."""


@dataclass
class TestOutcome:
    """Outcome of a single test case."""

    __test__ = False  # not a pytest test class

    test_id: str
    outcome: str
    message: str = ""
    file: str = ""
    #: Tail of the failure/error body (traceback). Used to tell assertion
    #: failures from import errors when the message alone is not enough.
    detail: str = ""
    #: For pseudo-tests that stand for a whole test module/target: why it
    #: produced no tests ("build" = it does not compile). Empty otherwise.
    kind: str = ""


def test_id_for(classname: str, name: str) -> str:
    """Build the canonical test id used to match base and head runs."""
    classname = (classname or "").strip()
    name = (name or "").strip()
    return f"{classname}::{name}" if classname else name


def parse_junit_string(text: str | bytes) -> dict[str, TestOutcome]:
    """Parse a JUnit XML document into ``{test_id: TestOutcome}``."""
    if isinstance(text, str):
        text = text.encode("utf-8")
    if len(text) > MAX_JUNIT_BYTES:
        raise JUnitParseError("JUnit XML file is too large")
    if b"<!DOCTYPE" in text or b"<!ENTITY" in text:
        # Test output is untrusted; refuse DTDs outright (entity expansion attacks).
        raise JUnitParseError("JUnit XML with DTD/entities is not accepted")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise JUnitParseError(f"invalid JUnit XML: {exc}") from exc
    results: dict[str, TestOutcome] = {}
    for case in root.iter("testcase"):
        tid = test_id_for(case.get("classname", ""), case.get("name", ""))
        if not tid:
            continue
        outcome = PASSED
        message = ""
        detail = ""
        for child in case:
            tag = child.tag
            if tag in (FAILED, "failure"):
                outcome, message = FAILED, child.get("message") or (child.text or "")
                detail = (child.text or "").strip()[-2000:]
            elif tag == ERROR:
                outcome, message = ERROR, child.get("message") or (child.text or "")
                detail = (child.text or "").strip()[-2000:]
            elif tag == SKIPPED and outcome == PASSED:
                outcome, message = SKIPPED, child.get("message") or ""
        new = TestOutcome(tid, outcome, message.strip()[:2000], case.get("file", ""), detail)
        old = results.get(tid)
        if old is None or _RANK[new.outcome] > _RANK[old.outcome]:
            results[tid] = new
    return results


def parse_junit_file(path: str | Path) -> dict[str, TestOutcome]:
    """Parse a JUnit XML file from disk."""
    p = Path(path)
    if p.stat().st_size > MAX_JUNIT_BYTES:
        raise JUnitParseError("JUnit XML file is too large")
    return parse_junit_string(p.read_bytes())
