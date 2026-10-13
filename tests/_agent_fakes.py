"""Test doubles for the agent: a scripted LLM transport and a tiny repository with a real bug fix."""

from __future__ import annotations

import itertools
import json
import sys

import _helpers  # noqa: F401  (sets up sys.path)
from _helpers import GitRepo

from proofcheck.httpclient import HTTPResponse
from proofcheck.runners import UnittestRunner

_ids = itertools.count(1)


def anthropic_reply(calls, text="", usage=(120, 30), stop=None):
    content = [{"type": "text", "text": text}] if text else []
    for name, args in calls:
        content.append({"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": args})
    return {"id": "msg", "type": "message", "role": "assistant", "content": content,
            "stop_reason": stop or ("tool_use" if calls else "end_turn"),
            "usage": {"input_tokens": usage[0], "output_tokens": usage[1], "cache_read_input_tokens": 10}}


def openai_reply(calls, text="", usage=(120, 30), stop=None):
    msg = {"role": "assistant", "content": text or None}
    if calls:
        msg["tool_calls"] = [{"id": f"call_{next(_ids)}", "type": "function",
                              "function": {"name": n, "arguments": json.dumps(a)}} for n, a in calls]
    return {"choices": [{"index": 0, "message": msg, "finish_reason": stop or ("tool_calls" if calls else "stop")}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}


class ScriptedTransport:
    """Answers each request with the next scripted step.

    A step is a list of ``(tool_name, arguments)`` pairs (one assistant turn
    with those tool calls), a string (a text-only turn), or an
    ``HTTPResponse`` (returned as is, e.g. a 429).
    """

    def __init__(self, provider, steps):
        self.provider = provider
        self.steps = list(steps)
        self.requests = []

    def __call__(self, method, url, headers, body, timeout):
        payload = json.loads(body)
        self.requests.append({"method": method, "url": url, "headers": dict(headers), "payload": payload})
        if not self.steps:
            raise AssertionError("the scripted LLM ran out of steps")
        step = self.steps.pop(0)
        if isinstance(step, HTTPResponse):
            return step
        if isinstance(step, str):
            calls, text = [], step
        else:
            calls, text = step, ""
        reply = anthropic_reply(calls, text) if self.provider == "anthropic" else openai_reply(calls, text)
        return HTTPResponse(200, json.dumps(reply).encode())

    def request_text(self, i):
        """All text of request ``i`` (for asserting what the model was shown)."""
        return json.dumps(self.requests[i]["payload"])


TEXTUTIL_BASE = '''"""Small text helpers."""


def word_count(text):
    """Number of words in ``text``."""
    return len(text.split(" "))


def shout(text):
    return text.upper()
'''

TEXTUTIL_HEAD = '''"""Small text helpers."""


def word_count(text):
    """Number of words in ``text``."""
    return len(text.split())


def normalize(text):
    """Collapse runs of whitespace."""
    return " ".join(text.split())


def shout(text):
    return text.upper()
'''

EXISTING_TEST = '''import unittest

from textutil import shout


class ShoutTest(unittest.TestCase):
    def test_shout(self):
        self.assertEqual(shout("hi"), "HI")
'''

ISSUE = """word_count is wrong with repeated spaces.

```python
from textutil import word_count
assert word_count("hello  world") == 2   # returns 3
```
"""

PR_BODY = "Fix word_count with repeated whitespace\n\nFixes #7. Also adds normalize()."


def make_textutil_repo() -> GitRepo:
    """main: buggy word_count(); branch fix: fixed word_count() plus a new normalize() (no test added)."""
    r = GitRepo()
    r.commit({"textutil.py": TEXTUTIL_BASE, "tests/test_textutil.py": EXISTING_TEST}, "base")
    r.branch("fix")
    r.commit({"textutil.py": TEXTUTIL_HEAD}, "fix word_count")
    r.checkout("main")
    return r


def unittest_runner() -> UnittestRunner:
    return UnittestRunner(command=[sys.executable, "{unittest_junit}", "--junitxml", "{junitxml}", "{tests}"])


def ut(body: str, imports: str = "from textutil import word_count") -> str:
    return f"import unittest\n\n{imports}\n\n\nclass ReproTest(unittest.TestCase):\n{body}"


# Candidate tests: one per verify classification.
STRONG = ut('    def test_repeated_spaces(self):\n        self.assertEqual(word_count("hello  world"), 2)\n',
            imports="from textutils import word_count")  # typo on purpose: import repair fixes it
WEAK = ut('    def test_normalize(self):\n        self.assertEqual(normalize("a  b"), "a b")\n',
          imports="from textutil import normalize")
PASS_BOTH = ut('    def test_shout(self):\n        self.assertEqual(shout("a"), "A")\n', imports="from textutil import shout")
FAILS_HEAD = ut('    def test_wrong_expectation(self):\n        self.assertEqual(word_count("hello  world"), 3)\n')


def submit(code: str, name: str = "test_issue_7.py") -> tuple[str, dict]:
    return ("submit_test", {"filename": name, "code": code, "rationale": "word_count splits on single spaces.",
                            "targets_claim": "fixes #7"})
