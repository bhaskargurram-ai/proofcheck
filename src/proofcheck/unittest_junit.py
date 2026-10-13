"""Run stdlib ``unittest`` test files and write a JUnit XML report.

This is the test command behind ``runner = "unittest"``. It lets projects
that use plain ``unittest`` (and environments where pytest is not installed)
produce the per-test outcomes proofcheck needs::

    python -m proofcheck.unittest_junit --junitxml report.xml tests/test_foo.py

The module depends only on the standard library so that it can also be run
directly as a script (``python path/to/unittest_junit.py ...``).

Test ids are ``<dotted.module>.<Class>::<method>``; a module that fails to
import is reported as a single errored test named after the module (this
mirrors how pytest reports collection errors).
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
import time
import traceback
import unittest
import xml.etree.ElementTree as ET
from typing import Any


def module_name_for(path: str, root: str) -> str:
    """Convert ``tests/test_x.py`` (relative to ``root``) into ``tests.test_x``."""
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    if rel.endswith(".py"):
        rel = rel[:-3]
    return rel.replace(os.sep, ".").replace("/", ".")


class _Collector(unittest.TestResult):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict[str, Any]] = []
        self._start = 0.0

    def startTest(self, test: unittest.TestCase) -> None:  # noqa: N802 - unittest API
        super().startTest(test)
        self._start = time.monotonic()

    def _record(self, test: Any, outcome: str, message: str = "") -> None:
        tid = test.id()
        classname, _, name = tid.rpartition(".")
        self.records.append(
            {"classname": classname, "name": name, "outcome": outcome, "message": message, "time": time.monotonic() - self._start}
        )

    def addSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().addSuccess(test)
        self._record(test, "passed")

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        super().addFailure(test, err)
        self._record(test, "failure", self._exc_info_to_string(err, test))

    def addError(self, test: Any, err: Any) -> None:  # noqa: N802
        super().addError(test, err)
        self._record(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:  # noqa: N802
        super().addSkip(test, reason)
        self._record(test, "skipped", reason)

    def addExpectedFailure(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        super().addExpectedFailure(test, err)
        self._record(test, "skipped", "expected failure")

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().addUnexpectedSuccess(test)
        self._record(test, "failure", "unexpected success")


def run(paths: list[str], root: str) -> list[dict[str, Any]]:
    """Load and run the given test files; return per-test records."""
    if root not in sys.path:
        sys.path.insert(0, root)
    loader = unittest.TestLoader()
    records: list[dict[str, Any]] = []
    for path in paths:
        mod = module_name_for(path, root)
        # Like pytest's default "prepend" import mode: the test file's own
        # directory is importable, so sibling helper modules resolve.
        test_dir = os.path.dirname(os.path.abspath(path))
        if test_dir not in sys.path:
            sys.path.insert(1, test_dir)
        try:
            module = importlib.import_module(mod)
            suite = loader.loadTestsFromModule(module)
        except BaseException as exc:  # noqa: BLE001 - report import errors as test errors
            if isinstance(exc, KeyboardInterrupt):
                raise
            records.append({"classname": "", "name": mod, "outcome": "error", "message": traceback.format_exc(), "time": 0.0})
            continue
        result = _Collector()
        suite.run(result)
        records.extend(result.records)
    return records


def write_junit(records: list[dict[str, Any]], path: str) -> None:
    """Write ``records`` as a JUnit XML file."""
    suite = ET.Element("testsuite", name="unittest", tests=str(len(records)))
    for r in records:
        case = ET.SubElement(suite, "testcase", classname=r["classname"], name=r["name"], time=f"{r['time']:.3f}")
        if r["outcome"] in ("failure", "error"):
            child = ET.SubElement(case, r["outcome"], message=(r["message"].strip().splitlines() or [""])[-1][:500])
            child.text = r["message"]
        elif r["outcome"] == "skipped":
            ET.SubElement(case, "skipped", message=r["message"][:500])
    root = ET.Element("testsuites")
    root.append(suite)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--junitxml", required=True, help="where to write the JUnit XML report")
    parser.add_argument("--root", default=os.getcwd(), help="import root (default: current directory)")
    parser.add_argument("paths", nargs="+", help="test files to run")
    ns = parser.parse_args(argv)
    records = run(ns.paths, ns.root)
    write_junit(records, ns.junitxml)
    failed = sum(r["outcome"] in ("failure", "error") for r in records)
    print(f"{len(records)} tests, {failed} failed/errored")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
