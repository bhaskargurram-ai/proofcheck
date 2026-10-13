"""A stand-in for ``python -m pytest`` used by the test suite (pytest is optional).

It accepts the arguments proofcheck's pytest runner passes
(``--junitxml=PATH``, ``--rootdir=.``, ``-q``, ``-p X``, ``-o X``) plus test
file paths, runs the files with stdlib unittest, and writes a JUnit report
with pytest-style ``classname`` values (``tests.test_x.Class``).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from proofcheck.unittest_junit import run, write_junit  # noqa: E402


def main(argv: list[str]) -> int:
    junit = None
    files = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("--junitxml="):
            junit = arg.split("=", 1)[1]
        elif arg in ("-p", "-o"):
            skip_next = True
        elif arg.startswith("-"):
            continue
        else:
            files.append(arg)
    if junit is None:
        print("fake_pytest: --junitxml is required", file=sys.stderr)
        return 4
    records = run(files, os.getcwd())
    write_junit(records, junit)
    return 1 if any(r["outcome"] in ("failure", "error") for r in records) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
