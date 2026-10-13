#!/usr/bin/env python3
"""Build a tiny demo repository with one real fix and one "fake" fix.

Usage::

    python examples/demo/make_demo_repo.py /tmp/proofcheck-demo

Creates::

    /tmp/proofcheck-demo/
        repo/            git repo; branches: main, fix-real, fix-fake
        issue-1.md       the bug report (contains a ```python reproducer)
        pr-real.md       description of the real fix
        pr-fake.md       description of the fake fix

* ``main``      -- ``word_count()`` splits on a single space, so
                   ``word_count("hello  world")`` returns 3.
* ``fix-real``  -- splits on any whitespace and adds a regression test that
                   fails on main and passes on the branch.
* ``fix-fake``  -- reformats the function without fixing it and adds a test
                   that passes on main too (it does not exercise the bug),
                   while claiming to fix the issue and to be 2x faster.

The tests are plain ``unittest`` TestCases, so they run under pytest (the
default runner) and under ``--runner unittest`` (no pytest needed).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

GIT_ID = ["-c", "user.name=Demo Author", "-c", "user.email=demo@example.invalid", "-c", "commit.gpgsign=false"]

BUGGY = '''"""Tiny text utilities."""


def word_count(text: str) -> int:
    """Return the number of words in ``text``."""
    if not text:
        return 0
    return len(text.split(" "))
'''

FIXED = '''"""Tiny text utilities."""


def word_count(text: str) -> int:
    """Return the number of words in ``text`` (any whitespace separates words)."""
    return len(text.split())
'''

FAKE = '''"""Tiny text utilities."""


def word_count(text: str) -> int:
    """Return the number of words in ``text``.

    Optimized implementation.
    """
    if not text:
        return 0
    words = text.split(" ")
    return len(words)
'''

TESTS_MAIN = '''import unittest

from textutils import word_count


class WordCountTest(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(word_count("hello world"), 2)

    def test_empty(self):
        self.assertEqual(word_count(""), 0)
'''

TESTS_REAL = TESTS_MAIN + '''
    def test_repeated_whitespace(self):
        self.assertEqual(word_count("hello  world"), 2)
        self.assertEqual(word_count(" tabs\\tand\\nnewlines "), 3)
'''

TESTS_FAKE = TESTS_MAIN + '''
    def test_three_words(self):
        self.assertEqual(word_count("one two three"), 3)
'''

ISSUE = '''word_count() miscounts text with repeated spaces

```python
from textutils import word_count

result = word_count("hello  world")
assert result == 2, f"expected 2 words, got {result}"
print("ok")
```
'''

PR_REAL = '''Title: Count words separated by any whitespace

Fixes #1. `word_count` now uses `str.split()` without an argument, so repeated
spaces, tabs and newlines are handled. Adds a regression test.
This is backwards compatible.
'''

PR_FAKE = '''Title: Fix word_count and optimize it

Fixes #1. Rewrote word_count for clarity and it is now 2x faster.
Adds tests for the fix.
'''


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *GIT_ID, "-C", str(repo), *args], check=True, stdout=subprocess.DEVNULL)


def commit(repo: Path, files: dict[str, str], message: str) -> None:
    for name, content in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def build(dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    repo = dest / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    commit(repo, {"textutils.py": BUGGY, "tests/test_textutils.py": TESTS_MAIN, ".gitignore": "__pycache__/\n"}, "Initial commit")

    git(repo, "checkout", "-q", "-b", "fix-real", "main")
    commit(repo, {"textutils.py": FIXED, "tests/test_textutils.py": TESTS_REAL}, "Count words separated by any whitespace")

    git(repo, "checkout", "-q", "-b", "fix-fake", "main")
    commit(repo, {"textutils.py": FAKE, "tests/test_textutils.py": TESTS_FAKE}, "Fix word_count and optimize it")
    git(repo, "checkout", "-q", "main")

    (dest / "issue-1.md").write_text(ISSUE, encoding="utf-8")
    (dest / "pr-real.md").write_text(PR_REAL, encoding="utf-8")
    (dest / "pr-fake.md").write_text(PR_FAKE, encoding="utf-8")
    return repo


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    dest = Path(argv[1]).resolve()
    repo = build(dest)
    print(f"demo repository created at {repo}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
