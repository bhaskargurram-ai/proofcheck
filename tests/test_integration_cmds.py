"""`proofcheck summary`, `init`, `context --pr` and verdict labels (mocked HTTP)."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

import _helpers  # noqa: F401

from click.testing import CliRunner

from proofcheck import cli
from proofcheck.card import CARD_MARKER
from proofcheck.ci import context_from_api, write_summary
from proofcheck.config import parse_config_text
from proofcheck.github_api import VERDICT_LABELS, GitHubClient
from proofcheck.httpclient import HTTPResponse
from proofcheck.init_cmd import (AGENT_PATH, COMMENT_PATH, CONFIG_PATH, EVIDENCE_PATH, InitError, agent_caller,
                                 comment_caller, detect_runner, evidence_caller, init_repo)

RESULT = {"schema": "proofcheck/result", "version": 1, "base": "a" * 40, "head": "b" * 40, "verdict": "partial",
          "pr_number": 7, "claims": [{"type": "fixes_issue", "text": "fixes #1", "status": "unverified",
                                      "evidence": "no test <script>alert(1)</script>"}]}


def tmpdir(test):
    d = Path(tempfile.mkdtemp(prefix="proofcheck-int-"))
    test.addCleanup(shutil.rmtree, d, True)
    return d


class SummaryTest(unittest.TestCase):
    def test_writes_validated_card_to_step_summary(self):
        d = tmpdir(self)
        (d / "result.json").write_text(json.dumps(RESULT))
        summary = d / "summary.md"
        summary.write_text("earlier step\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
        env["GITHUB_STEP_SUMMARY"] = str(summary)
        with mock.patch.dict(os.environ, env, clear=True):
            res = CliRunner().invoke(cli.main, ["summary", "--json", str(d / "result.json")], catch_exceptions=False)
        self.assertEqual(res.exit_code, 0, res.output)
        text = summary.read_text()
        self.assertTrue(text.startswith("earlier step\n" + CARD_MARKER))  # appended, not overwritten
        self.assertIn("Some claims are backed by evidence", text)
        self.assertNotIn("<script>", text)

    def test_stdout_without_summary_file_and_rejects_forgery(self):
        d = tmpdir(self)
        (d / "result.json").write_text(json.dumps(RESULT))
        with mock.patch("builtins.print") as p:
            self.assertIsNone(write_summary(d / "result.json", env={}))
        self.assertIn(CARD_MARKER, p.call_args[0][0])
        (d / "bad.json").write_text(json.dumps(dict(RESULT, verdict="totally-verified")))
        res = CliRunner().invoke(cli.main, ["summary", "--json", str(d / "bad.json"), "--out", str(d / "o.md")])
        self.assertNotEqual(res.exit_code, 0)
        self.assertIn("could not write summary", res.output)
        self.assertFalse((d / "o.md").exists())


class FakeLabelsAPI:
    """In-memory GitHub labels + PR API."""

    def __init__(self, repo_labels=(), pr_labels=(), fail_create=False):
        self.repo_labels = {n: {"name": n} for n in repo_labels}
        self.pr_labels = list(pr_labels)
        self.calls = []
        self.fail_create = fail_create

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("api.github.com", 1)[1].split("?", 1)[0]
        payload = json.loads(body) if body else None
        self.calls.append((method, path, payload))
        if path.startswith("/repos/acme/w/labels/") and method == "GET":
            name = unquote(path.rsplit("/", 1)[1])
            return HTTPResponse(200 if name in self.repo_labels else 404, b"{}")
        if path == "/repos/acme/w/labels" and method == "POST":
            if self.fail_create:
                return HTTPResponse(403, b'{"message": "forbidden"}')
            if payload["name"] in self.repo_labels:
                return HTTPResponse(422, b"{}")
            self.repo_labels[payload["name"]] = payload
            return HTTPResponse(201, json.dumps(payload).encode())
        if path == "/repos/acme/w/issues/7/labels" and method == "GET":
            return HTTPResponse(200, json.dumps([{"name": n} for n in self.pr_labels]).encode())
        if path == "/repos/acme/w/issues/7/labels" and method == "POST":
            for n in payload["labels"]:
                if n not in self.repo_labels:
                    return HTTPResponse(422, b"{}")
                self.pr_labels.append(n)
            return HTTPResponse(200, json.dumps([{"name": n} for n in self.pr_labels]).encode())
        if path.startswith("/repos/acme/w/issues/7/labels/") and method == "DELETE":
            name = unquote(path.rsplit("/", 1)[1])
            if name not in self.pr_labels:
                return HTTPResponse(404, b"{}")
            self.pr_labels.remove(name)
            return HTTPResponse(200, b"[]")
        return HTTPResponse(404, b"{}")

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


class LabelsTest(unittest.TestCase):
    def client(self, fake):
        return GitHubClient(token="tok", repo="acme/w", transport=fake)

    def test_creates_missing_label_and_adds_it(self):
        fake = FakeLabelsAPI(pr_labels=["bug"])
        self.assertEqual(self.client(fake).sync_verdict_label(7, "evidence"), (["proofcheck: verified"], []))
        self.assertEqual(fake.repo_labels["proofcheck: verified"]["color"], "1a7f37")
        self.assertEqual(fake.pr_labels, ["bug", "proofcheck: verified"])
        # label names with ':' and spaces are URL-encoded in paths
        self.assertIn(("GET", "/repos/acme/w/labels/proofcheck%3A%20verified", None), fake.calls)

    def test_idempotent_and_removes_only_stale_verdict_labels(self):
        fake = FakeLabelsAPI(repo_labels=list(VERDICT_LABELS.values()) + ["proofcheck:agent"],
                             pr_labels=["proofcheck: partial", "proofcheck: verified", "proofcheck:agent", "bug"])
        c = self.client(fake)
        self.assertEqual(c.sync_verdict_label(7, "contradicted"),
                         (["proofcheck: contradicted"], ["proofcheck: partial", "proofcheck: verified"]))
        self.assertEqual(sorted(fake.pr_labels), ["bug", "proofcheck: contradicted", "proofcheck:agent"])
        n = len(fake.writes())
        self.assertEqual(c.sync_verdict_label(7, "contradicted"), ([], []))  # second run: no writes
        self.assertEqual(len(fake.writes()), n)
        # "error" carries no verdict label: stale ones are removed, others kept
        self.assertEqual(c.sync_verdict_label(7, "error"), ([], ["proofcheck: contradicted"]))
        self.assertEqual(sorted(fake.pr_labels), ["bug", "proofcheck:agent"])

    def test_comment_command_sets_label_and_survives_label_errors(self):
        d = tmpdir(self)
        (d / "result.json").write_text(json.dumps(RESULT))
        fake = FakeLabelsAPI(fail_create=True)
        comments = []

        def transport(method, url, headers, body, timeout):
            if "/comments" in url:
                if method == "GET":
                    return HTTPResponse(200, b"[]")
                comments.append(json.loads(body)["body"])
                return HTTPResponse(201, b'{"id": 5}')
            if url.endswith("/pulls/7/files?per_page=100&page=1"):
                return HTTPResponse(200, b"[]")
            return fake(method, url, headers, body, timeout)

        env = {"GITHUB_TOKEN": "tok", "GITHUB_REPOSITORY": "acme/w"}
        with mock.patch.dict(os.environ, env), mock.patch("proofcheck.github_api.urllib_transport", transport), \
                mock.patch("proofcheck.cli.env_client", lambda e=None: GitHubClient("tok", "acme/w", transport=transport)):
            res = CliRunner().invoke(cli.main, ["comment", "--json", str(d / "result.json"), "--labels"],
                                     catch_exceptions=False)
        self.assertEqual(res.exit_code, 0, res.output)  # a label failure never fails the comment
        self.assertEqual(len(comments), 1)
        self.assertIn("could not update labels", res.output)

        fake2 = FakeLabelsAPI()

        def transport2(method, url, headers, body, timeout):
            if "/comments" in url or "/files" in url:
                return transport(method, url, headers, body, timeout)
            return fake2(method, url, headers, body, timeout)

        with mock.patch.dict(os.environ, env), \
                mock.patch("proofcheck.cli.env_client", lambda e=None: GitHubClient("tok", "acme/w", transport=transport2)):
            res = CliRunner().invoke(cli.main, ["comment", "--json", str(d / "result.json"), "--labels"],
                                     catch_exceptions=False)
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertEqual(fake2.pr_labels, ["proofcheck: partial"])


class ContextFromApiTest(unittest.TestCase):
    def test_context_pr_reads_title_and_body_from_api(self):
        def transport(method, url, headers, body, timeout):
            if url.endswith("/repos/acme/w/pulls/7"):
                return HTTPResponse(200, json.dumps({"title": "Fix add", "body": "Fixes #3"}).encode())
            if url.endswith("/repos/acme/w/issues/3"):
                return HTTPResponse(200, json.dumps({"body": "```python\nprint(1)\n```"}).encode())
            return HTTPResponse(404, b"{}")

        ctx = context_from_api(7, env={"GITHUB_REPOSITORY": "acme/w", "GITHUB_TOKEN": "t"}, transport=transport)
        self.assertEqual((ctx.title, ctx.body, ctx.number, ctx.repo), ("Fix add", "Fixes #3", 7, "acme/w"))

        d = tmpdir(self)
        from proofcheck.github_api import GitHubClient as GC

        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/w", "GITHUB_TOKEN": "t"}), \
                mock.patch("proofcheck.ci.env_client", lambda env=None, transport=None: GC("t", "acme/w", transport=transport_fn)), \
                mock.patch("proofcheck.cli.env_client", lambda env=None: GC("t", "acme/w", transport=transport_fn)):
            transport_fn = transport
            res = CliRunner().invoke(cli.main, ["context", "--pr", "7", "--out", str(d / "ctx.json")], catch_exceptions=False)
        self.assertEqual(res.exit_code, 0, res.output)
        data = json.loads((d / "ctx.json").read_text())
        self.assertEqual((data["title"], data["number"], list(data["issues"])), ("Fix add", 7, ["#3"]))
        res = CliRunner().invoke(cli.main, ["context", "--pr", "7; rm -rf /", "--out", str(d / "x.json")])
        self.assertNotEqual(res.exit_code, 0)


class InitTest(unittest.TestCase):
    def test_detect_runner(self):
        cases = [
            ({"package.json": json.dumps({"devDependencies": {"jest": "^29"}})}, "jest"),
            ({"package.json": json.dumps({"scripts": {"test": "jest --ci"}})}, "jest"),
            ({"pyproject.toml": "[tool.pytest.ini_options]\n"}, "pytest"),
            ({"pyproject.toml": "[project]\noptional-dependencies = {dev = ['pytest>=7']}\n"}, "pytest"),
            ({"pytest.ini": "[pytest]\n"}, "pytest"),
            ({"setup.cfg": "[tool:pytest]\n"}, "pytest"),
            ({"tests/test_x.py": "import unittest\n"}, "pytest"),
            ({"package.json": "{}", "x.py": ""}, "unittest"),
            ({"pyproject.toml": "[project]\nname='x'\n"}, "unittest"),
        ]
        for files, want in cases:
            with self.subTest(files=files):
                d = tmpdir(self)
                for name, text in files.items():
                    (d / name).parent.mkdir(parents=True, exist_ok=True)
                    (d / name).write_text(text)
                self.assertEqual(detect_runner(d)[0], want)

    def test_writes_files_and_refuses_overwrite(self):
        d = tmpdir(self)
        (d / "tests").mkdir()
        (d / "tests" / "test_a.py").write_text("def test_a(): pass\n")
        res = init_repo(d, ref="v1.0.0")
        self.assertEqual(res.runner, "pytest")
        self.assertEqual(res.written, [EVIDENCE_PATH, COMMENT_PATH, CONFIG_PATH])
        self.assertFalse((d / AGENT_PATH).exists())
        ev = (d / EVIDENCE_PATH).read_text()
        self.assertIn("uses: bhaskargurram-ai/proofcheck/.github/workflows/evidence.yml@v1.0.0", ev)
        self.assertIn("proofcheck-ref: v1.0.0", ev)
        self.assertNotIn("pull_request_target:", ev + (d / COMMENT_PATH).read_text())
        cfg = parse_config_text((d / CONFIG_PATH).read_text(), "proofcheck.toml")
        self.assertEqual((cfg.runner, cfg.ablation.enabled, cfg.ablation.max_hunks), ("pytest", True, 12))

        (d / CONFIG_PATH).write_text("# mine\n")
        with self.assertRaises(InitError) as cm:
            init_repo(d, agent=True)
        self.assertIn("proofcheck.toml", str(cm.exception))
        self.assertEqual((d / CONFIG_PATH).read_text(), "# mine\n")  # untouched
        self.assertFalse((d / AGENT_PATH).exists())  # nothing written at all

        res = init_repo(d, agent=True, force=True)
        self.assertIn(AGENT_PATH, res.written)
        self.assertNotEqual((d / CONFIG_PATH).read_text(), "# mine\n")
        steps = "\n".join(res.next_steps)
        self.assertIn("PROOFCHECK_LLM_API_KEY", steps)
        self.assertIn("PROOFCHECK_LLM_MODEL", steps)

    def test_agent_flag_enables_agent_in_config(self):
        # proofcheck-agent.yml only proposes tests when [agent] is enabled, so
        # `init --agent` must enable it; plain `init` must leave it off.
        from proofcheck.agent.config import parse_agent_config
        from proofcheck.config import tomllib

        with_agent, without = tmpdir(self), tmpdir(self)
        init_repo(with_agent, agent=True)
        init_repo(without)
        on = tomllib.loads((with_agent / CONFIG_PATH).read_text())
        off = tomllib.loads((without / CONFIG_PATH).read_text())
        self.assertTrue(parse_agent_config(on.get("agent"), "proofcheck.toml").enabled)
        self.assertFalse(parse_agent_config(off.get("agent"), "proofcheck.toml").enabled)
        self.assertIsNotNone(parse_config_text((with_agent / CONFIG_PATH).read_text(), "proofcheck.toml"))

    def test_cli_and_bad_ref(self):
        d = tmpdir(self)
        (d / "package.json").write_text(json.dumps({"devDependencies": {"jest": "29"}}))
        res = CliRunner().invoke(cli.main, ["init", "--repo", str(d), "--agent", "--ref", "0123456789abcdef0123456789abcdef01234567"])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("Detected test runner: jest", res.output)
        self.assertIn("jest-junit", res.output)
        self.assertIn("gh secret set PROOFCHECK_LLM_API_KEY", res.output)
        self.assertIn('node-version: "20"', (d / EVIDENCE_PATH).read_text())
        self.assertIn("install-command: 'npm ci'", (d / AGENT_PATH).read_text())
        res = CliRunner().invoke(cli.main, ["init", "--repo", str(d)])
        self.assertNotEqual(res.exit_code, 0)
        self.assertIn("refusing to overwrite", res.output)
        for bad in ("v1\nevil: true", "${{ github.token }}", "../x", "-x"):
            with self.assertRaises(InitError, msg=bad):
                init_repo(tmpdir(self), ref=bad)

    def test_examples_match_generated_callers(self):
        ex = _helpers.ROOT / "examples" / "workflows"
        self.assertEqual((ex / "proofcheck.yml").read_text(), evidence_caller("v1.0.0", "pytest"))
        self.assertEqual((ex / "proofcheck-comment.yml").read_text(), comment_caller("v1.0.0"))
        self.assertEqual((ex / "proofcheck-agent.yml").read_text(), agent_caller("v1.0.0", "pytest"))


if __name__ == "__main__":
    unittest.main()
