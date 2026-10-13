import os
import sys
import unittest
from unittest import mock

import _helpers
from _helpers import RepoTestCase

from proofcheck import git as gitmod
from proofcheck.proc import run_command, scrub_env
from proofcheck.repro import (
    BEHAVIOR_CHANGED,
    UNCHANGED,
    compare_runs,
    extract_python_blocks,
    reproduce_issue,
    run_snippet,
)

ISSUE = """add() is wrong

```python
from calc import add
assert add(2, 3) == 5, add(2, 3)
print("ok")
```

Interactive session (not a script):

```python
>>> add(1, 1)
0
```

```bash
pip install calc
```

~~~py
print("tilde fence")
~~~
"""


class ExtractTest(unittest.TestCase):
    def test_extract_python_blocks(self):
        blocks = extract_python_blocks(ISSUE)
        self.assertEqual(len(blocks), 2)
        self.assertIn("assert add(2, 3) == 5", blocks[0])
        self.assertEqual(blocks[1].strip(), 'print("tilde fence")')

    def test_limit_and_empty(self):
        text = "\n".join(f"```python\nprint({i})\n```" for i in range(10))
        self.assertEqual(len(extract_python_blocks(text, limit=3)), 3)
        self.assertEqual(extract_python_blocks(""), [])
        self.assertEqual(extract_python_blocks("```\nprint(1)\n```"), [])  # no language: not taken


class EnvScrubTest(unittest.TestCase):
    def test_scrub_env_removes_secrets(self):
        env = {"PATH": "/bin", "GITHUB_TOKEN": "t", "AWS_SECRET_ACCESS_KEY": "s", "my_api_key": "k", "DB_PASSWORD": "p",
               "HOME": "/h", "NPM_AUTH": "a"}
        clean = scrub_env(env, extra={"PYTHONPATH": "/x"})
        self.assertEqual(clean, {"PATH": "/bin", "HOME": "/h", "PYTHONPATH": "/x"})

    def test_snippet_cannot_see_secrets(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghs_supersecret", "PROOFCHECK_LLM_API_KEY": "sk-x"}):
            r = run_snippet("import os\nprint(sorted(k for k in os.environ if 'TOKEN' in k or 'KEY' in k))",
                            _helpers.ROOT, sys.executable, 30)
        self.assertEqual(r.exit_code, 0, r.stderr)
        self.assertEqual(r.stdout, "[]")

    def test_run_command_never_uses_a_shell(self):
        r = run_command(["echo", "$HOME; rm -rf /"], cwd=_helpers.ROOT)
        self.assertEqual(r.stdout.strip(), "$HOME; rm -rf /")

    def test_snippet_timeout(self):
        r = run_snippet("import time\ntime.sleep(20)", _helpers.ROOT, sys.executable, 1)
        self.assertTrue(r.timed_out)


class ReproduceTest(RepoTestCase):
    def test_reproduce_on_base_and_head(self):
        with gitmod.worktree(self.repo.path, "main") as b, gitmod.worktree(self.repo.path, "good") as h:
            rep = reproduce_issue("#1", ISSUE, b, h, sys.executable, 30)
            self.assertEqual(rep.status, "reproduced_change")
            first = rep.snippets[0]
            self.assertEqual(first.status, BEHAVIOR_CHANGED)
            self.assertEqual(first.base.exit_code, 1)
            self.assertIn("AssertionError", first.base.last_line())
            self.assertEqual((first.head.exit_code, first.head.stdout), (0, "ok"))
            self.assertEqual(rep.snippets[1].status, UNCHANGED)
            # worktree paths are normalized away, so identical output compares equal
            self.assertNotIn(str(b), first.base.stderr)

        with gitmod.worktree(self.repo.path, "main") as b, gitmod.worktree(self.repo.path, "weak") as h:
            rep = reproduce_issue("#1", ISSUE, b, h, sys.executable, 30)
            self.assertEqual(rep.status, "unchanged")

    def test_no_snippets(self):
        with gitmod.worktree(self.repo.path, "main") as b:
            rep = reproduce_issue("#2", "Just prose.", b, b, sys.executable, 30)
        self.assertEqual(rep.status, "no_snippets")

    def test_compare_runs_error(self):
        from proofcheck.repro import ERROR, SnippetRun

        self.assertEqual(compare_runs(SnippetRun(None, "", "", error="no python"), SnippetRun(0, "", "")), ERROR)


if __name__ == "__main__":
    unittest.main()
