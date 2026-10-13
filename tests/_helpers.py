"""Shared test helpers: import path setup and throwaway git repositories."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

TESTS_DIR = Path(__file__).resolve().parent
FAKE_PYTEST = TESTS_DIR / "fake_pytest.py"

GIT_ID = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *GIT_ID, "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return out.stdout.strip()


class GitRepo:
    """A temporary git repository with helpers to write files and commit."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="proofcheck-test-"))
        self.path = self.tmp / "repo"
        self.path.mkdir()
        git(self.path, "init", "-q", "-b", "main")

    def write(self, files: dict[str, str | bytes | None]) -> None:
        for name, content in files.items():
            p = self.path / name
            if content is None:
                if p.exists():
                    p.unlink()
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                p.write_bytes(content)
            else:
                p.write_text(content, encoding="utf-8")

    def commit(self, files: dict[str, str | bytes | None], message: str = "commit") -> str:
        self.write(files)
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "-m", message)
        return git(self.path, "rev-parse", "HEAD")

    def branch(self, name: str, start: str = "main") -> None:
        git(self.path, "checkout", "-q", "-b", name, start)

    def checkout(self, name: str) -> None:
        git(self.path, "checkout", "-q", name)

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


BUGGY = '''def add(a, b):
    return a - b


def mul(a, b):
    return a * b
'''

FIXED = '''def add(a, b):
    return a + b


def mul(a, b):
    return a * b
'''

BASE_TESTS = '''import unittest

from calc import mul


class MulTest(unittest.TestCase):
    def test_mul(self):
        self.assertEqual(mul(2, 3), 6)
'''

GOOD_TESTS = (BASE_TESTS + '''

class AddTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
''').replace("from calc import mul", "from calc import add, mul")

WEAK_TESTS = BASE_TESTS + '''
    def test_mul_zero(self):
        self.assertEqual(mul(0, 5), 0)
'''


def make_calc_repo() -> GitRepo:
    """main: buggy add(); branches good (fix + fail-to-pass test), weak (no fix,
    test passes on both), broken (test fails on head), notests (fix only)."""
    r = GitRepo()
    r.commit({"calc.py": BUGGY, "tests/test_calc.py": BASE_TESTS}, "base")
    r.branch("good")
    r.commit({"calc.py": FIXED, "tests/test_calc.py": GOOD_TESTS}, "fix add")
    r.branch("weak", "main")
    r.commit({"calc.py": BUGGY.replace("return a * b", "return b * a"), "tests/test_calc.py": WEAK_TESTS}, "weak")
    r.branch("broken", "main")
    r.commit({"tests/test_calc.py": GOOD_TESTS}, "test only")
    r.branch("notests", "main")
    r.commit({"calc.py": FIXED}, "fix only")
    r.checkout("main")
    return r


class RepoTestCase(unittest.TestCase):
    """Base class providing a fresh calc repo per test."""

    def setUp(self) -> None:
        self.repo = make_calc_repo()
        self.addCleanup(self.repo.cleanup)


def scrubbed_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env
