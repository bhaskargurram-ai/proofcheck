"""End-to-end CLI tests: `proofcheck run` on a real repo and `proofcheck comment`."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _helpers  # noqa: F401  (sets up sys.path)
from _helpers import RepoTestCase

from click.testing import CliRunner

from proofcheck import cli
from proofcheck.card import CARD_MARKER
from proofcheck.httpclient import HTTPResponse

UNITTEST_CMD = f"{sys.executable} {{unittest_junit}} --junitxml {{junitxml}} {{tests}}"


class RunCommandTest(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.out = Path(tempfile.mkdtemp(prefix="proofcheck-cli-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.out, ignore_errors=True))

    def invoke_run(self, head, body, *extra):
        (self.out / "pr.md").write_text(body, encoding="utf-8")
        args = ["run", "--repo", str(self.repo.path), "--base", "main", "--head", head, "--runner", "unittest",
                "--test-command", UNITTEST_CMD, "--pr-body-file", str(self.out / "pr.md"), "--no-fetch-issues",
                "--out", str(self.out / "card.md"), "--json", str(self.out / "result.json"), *extra]
        env = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
        with mock.patch.dict(os.environ, env, clear=True):
            res = CliRunner().invoke(cli.main, args, catch_exceptions=False)
        return res

    def test_run_good_pr(self):
        issue = self.out / "issue.md"
        issue.write_text("```python\nfrom calc import add\nassert add(1, 2) == 3\n```\n", encoding="utf-8")
        res = self.invoke_run("good", "Title: Fix add\n\nFixes #1. Adds a regression test.", "--issue-body-file", str(issue))
        self.assertEqual(res.exit_code, 0, res.output)
        result = json.loads((self.out / "result.json").read_text())
        self.assertEqual(result["verdict"], "evidence")
        self.assertEqual([c["status"] for c in result["claims"]], ["verified", "verified"])
        self.assertEqual(result["repro"][0]["status"], "reproduced_change")
        card = (self.out / "card.md").read_text()
        self.assertTrue(card.startswith(CARD_MARKER))
        self.assertIn("fail → pass", card)

    def test_run_weak_pr_and_fail_on(self):
        res = self.invoke_run("weak", "Fixes #1 and makes it 2x faster. Adds tests.", "--fail-on", "no-evidence")
        result = json.loads((self.out / "result.json").read_text())
        self.assertEqual(result["tests"]["status"], "no_evidence")
        self.assertEqual(result["verdict"], "partial")  # adds_tests verified, others not
        self.assertEqual(res.exit_code, 0)
        res = self.invoke_run("broken", "Fixes #1", "--fail-on", "contradicted")
        self.assertEqual(res.exit_code, 1)
        self.assertEqual(json.loads((self.out / "result.json").read_text())["verdict"], "contradicted")

    def test_benchmark_command(self):
        cfg = self.out / "proofcheck.toml"
        bench = ("import calc, time\n"
                 "time.sleep(0.3 if calc.add(1, 1) != 2 else 0.05)\n")
        (self.out / "bench.py").write_text(bench, encoding="utf-8")
        cfg.write_text(f'benchmark_command = ["{sys.executable}", "{self.out / "bench.py"}"]\nbenchmark_runs = 3\n'
                       f'[test_env]\nPYTHONPATH = "."\n', encoding="utf-8")
        res = self.invoke_run("good", "Fixes #1, now 3x faster.", "--config", str(cfg))
        self.assertEqual(res.exit_code, 0, res.output)
        result = json.loads((self.out / "result.json").read_text())
        perf = [c for c in result["claims"] if c["type"] == "performance"][0]
        self.assertEqual(perf["status"], "verified", (perf, result["benchmark"]))
        self.assertGreater(result["benchmark"]["speedup"], 2)


class CommentCommandTest(unittest.TestCase):
    def setUp(self):
        from test_card import sample_result

        self.dir = Path(tempfile.mkdtemp(prefix="proofcheck-comment-"))
        self.result_path = self.dir / "result.json"
        self.result_path.write_text(json.dumps(sample_result()), encoding="utf-8")

    def tearDown(self):
        __import__("shutil").rmtree(self.dir, ignore_errors=True)

    def test_comment_posts_rerendered_card(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append((method, url, body))
            if method == "GET":
                return HTTPResponse(200, b"[]")
            return HTTPResponse(201, b'{"id": 77}')

        env = {"GITHUB_TOKEN": "tok", "GITHUB_REPOSITORY": "acme/widgets"}
        with mock.patch.dict(os.environ, env), mock.patch("proofcheck.httpclient.urllib_transport", transport), \
                mock.patch("proofcheck.github_api.urllib_transport", transport):
            res = CliRunner().invoke(cli.main, ["comment", "--json", str(self.result_path)], catch_exceptions=False)
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("created proof card comment 77 on #7", res.output)
        method, url, body = calls[-1]
        self.assertEqual(method, "POST")
        self.assertTrue(json.loads(body)["body"].startswith(CARD_MARKER))

    def _fake_api(self, files=(), head_sha="b" * 40):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append((method, url, body))
            if method == "GET" and "/files" in url:
                return HTTPResponse(200, json.dumps([{"filename": f} for f in files]).encode())
            if method == "GET" and "/comments" in url:
                return HTTPResponse(200, b"[]")
            if method == "GET" and "/pulls/" in url:
                return HTTPResponse(200, json.dumps({"head": {"sha": head_sha}}).encode())
            return HTTPResponse(201, b'{"id": 78}')

        return transport, calls

    def _invoke_comment(self, transport, *extra):
        env = {"GITHUB_TOKEN": "tok", "GITHUB_REPOSITORY": "acme/widgets"}
        with mock.patch.dict(os.environ, env), mock.patch("proofcheck.github_api.urllib_transport", transport):
            return CliRunner().invoke(cli.main, ["comment", "--json", str(self.result_path), *extra])

    def test_comment_warns_when_pr_changes_the_harness(self):
        transport, calls = self._fake_api(files=[".github/workflows/proofcheck.yml", "tests/conftest.py", "src/x.py"])
        res = self._invoke_comment(transport)
        self.assertEqual(res.exit_code, 0, res.output)
        body = json.loads(calls[-1][2])["body"]
        self.assertIn("[!WARNING]", body)
        self.assertIn(".github/workflows/proofcheck.yml", body)
        self.assertIn("tests/conftest.py", body)

    def test_comment_expect_head_sha(self):
        transport, calls = self._fake_api(head_sha="c" * 40)
        res = self._invoke_comment(transport, "--expect-head-sha", "b" * 40)
        self.assertNotEqual(res.exit_code, 0)
        self.assertIn("does not match", res.output)
        self.assertFalse(any(m == "POST" for m, _, _ in calls))
        transport, calls = self._fake_api(head_sha="b" * 40)
        res = self._invoke_comment(transport, "--expect-head-sha", "b" * 40)
        self.assertEqual(res.exit_code, 0, res.output)
        res = self._invoke_comment(transport, "--expect-head-sha", "d" * 40)
        self.assertIn("result.json head commit does not match", res.output)

    def test_context_file_roundtrip(self):
        event = self.dir / "event.json"
        event.write_text(json.dumps({"pull_request": {"number": 3, "title": "Fix it", "body": "Fixes #9"}}), encoding="utf-8")
        out = self.dir / "context.json"

        def transport(method, url, headers, body, timeout):
            self.assertTrue(url.endswith("/repos/acme/widgets/issues/9"))
            return HTTPResponse(200, json.dumps({"body": "```python\nprint(1)\n```"}).encode())

        env = {"GITHUB_TOKEN": "tok", "GITHUB_REPOSITORY": "acme/widgets"}
        with mock.patch.dict(os.environ, env), mock.patch("proofcheck.github_api.urllib_transport", transport):
            res = CliRunner().invoke(cli.main, ["context", "--event", str(event), "--out", str(out)], catch_exceptions=False)
        self.assertEqual(res.exit_code, 0, res.output)
        ctx = cli.load_context(str(out))
        self.assertEqual((ctx.title, ctx.body, ctx.number, ctx.repo), ("Fix it", "Fixes #9", 3, "acme/widgets"))
        self.assertIn("print(1)", ctx.issues["#9"])

    def test_comment_rejects_tampered_result(self):
        doc = json.loads(self.result_path.read_text())
        doc["pr_number"] = "7 && curl evil"
        self.result_path.write_text(json.dumps(doc), encoding="utf-8")
        res = CliRunner().invoke(cli.main, ["comment", "--json", str(self.result_path), "--dry-run"])
        self.assertNotEqual(res.exit_code, 0)
        self.assertIn("invalid result.json", res.output)

    def test_comment_dry_run_and_bad_pr_arg(self):
        res = CliRunner().invoke(cli.main, ["comment", "--json", str(self.result_path), "--dry-run"])
        self.assertEqual(res.exit_code, 0)
        self.assertTrue(res.output.startswith(CARD_MARKER))
        res = CliRunner().invoke(cli.main, ["comment", "--json", str(self.result_path), "--pr", "../1", "--dry-run"])
        self.assertNotEqual(res.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
