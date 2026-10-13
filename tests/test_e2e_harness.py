"""Unit tests for the end-to-end harness in e2e/ (offline: fake HTTP session, local git repositories)."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from urllib.parse import urlparse

import _helpers  # noqa: F401  (sets up sys.path)
from _helpers import GIT_ID, ROOT

sys.path.insert(0, str(ROOT / "e2e"))

from pce2e import expect, harness, report  # noqa: E402
from pce2e.github import GitHub, GitHubError, find_card  # noqa: E402
from pce2e.gitops import Git, Redactor, apply_overlay, auth_env, build_branches, clean_git_env  # noqa: E402
from pce2e.scenario import ScenarioError, load_scenario, load_set, patch_paths, substitute  # noqa: E402

from proofcheck.card import CARD_MARKER, render_card  # noqa: E402
from proofcheck.result import new_result, validate_result  # noqa: E402

TOY = ROOT / "e2e" / "scenarios" / "python-toy"
TOKEN = "ghp_SECRETtoken1234567890"
BOT = {"login": "github-actions[bot]", "type": "Bot"}
HUMAN = {"login": "mallory", "type": "User"}


def make_card(head, verdict="evidence", claims=None, agent=None, f2p=1, warnings=0, flags=()):
    claims = claims if claims is not None else [
        {"type": "fixes_issue", "text": "fixes #3", "source": "", "details": {"number": 3}, "origin": "rules",
         "status": "verified" if verdict == "evidence" else "unverified", "evidence": ""}]
    doc = new_result(
        repo="acme/toy", pr_number=4, base="a" * 40, head=head, merge_base="a" * 40, verdict=verdict, claims=claims,
        tests={"status": "evidence" if f2p else "no_tests", "runner": "pytest", "counts": {"fail_to_pass": f2p}, "tests": []},
        repro=[], benchmark=None, notes=[], config_source="defaults",
        scope={"files": [], "file_count": 2, "total_additions": 3, "total_deletions": 1,
               "flags": [{"kind": k, "message": "m", "paths": [], "heuristic": False} for k in flags]},
        ablation={"status": "ran", "total_hunks": 2, "tested": 2, "required": 1, "not_exercised": 1},
    )
    if agent:
        doc["agent"] = {"status": agent, "candidates": [], "variants": []}
    return render_card(validate_result(doc), trusted_warnings=["> [!WARNING]\n> w"] * warnings)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


SIMPLE_TOML = '''title = "Fix it"
body = "Fixes #{issue}."
[issue]
title = "It is broken"
[expected]
verdict = "evidence"
label = "proofcheck: verified"
'''


def make_set(tmp, scenarios=None, set_toml=None):
    """A small scenario set: base overlay with toy/a.py, scenarios given as {name: (toml, {path: text})}."""
    root = Path(tmp) / "set"
    write(root / "_base" / "files" / "toy" / "a.py", "A = 1\n")
    if set_toml is not None:
        write(root / "set.toml", set_toml)
    else:
        write(root / "set.toml", 'allowed_paths = ["toy/*"]\n')
    for name, (toml, files) in (scenarios or {"one": (SIMPLE_TOML, {"files/toy/a.py": "A = 2\n", "issue.md": "bug"})}).items():
        write(root / name / "scenario.toml", toml)
        for rel, text in files.items():
            write(root / name / rel, text)
    return root


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pce2e-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


# --------------------------------------------------------------------------- scenarios


class ScenarioLoadingTest(TempDirCase):
    def test_python_toy_set(self):
        sset = load_set(TOY)
        names = [s.name for s in sset.scenarios]
        self.assertEqual(names, ["agent-no-test", "fake-fix", "no-behaviour-change", "prompt-injection", "real-fix",
                                 "smuggled-hunk", "workflow-change"])
        self.assertIsNotNone(sset.base_overlay)
        by = {s.name: s for s in sset.scenarios}
        self.assertIsNotNone(by["smuggled-hunk"].patch)
        self.assertIsNotNone(by["real-fix"].overlay)
        self.assertTrue(by["agent-no-test"].wait_agent)
        self.assertTrue(by["real-fix"].has_issue)
        self.assertFalse(by["workflow-change"].has_issue)
        # online-only expectations are dropped locally, [expected_local] overrides
        local = by["agent-no-test"].expectations("local")
        self.assertNotIn("label", local)
        self.assertNotIn("agent_status", local)
        self.assertEqual(local["verdict"], "no_evidence")
        self.assertIn("none", by["agent-no-test"].expectations("online")["agent_status"])
        self.assertNotIn("none", by["agent-no-test"].expectations("online", require_agent=True)["agent_status"])
        self.assertEqual(by["real-fix"].expectations("online", require_agent=True)["agent_status"], ["none"])
        self.assertEqual(sset.select(["real-fix", "fake-fix"])[0].name, "fake-fix")
        with self.assertRaises(ScenarioError):
            sset.select(["nope"])

    def test_every_toy_scenario_only_touches_allowed_paths(self):
        sset = load_set(TOY)
        self.assertEqual(sset.allowed_paths, ("e2e_toy/*", ".github/workflows/proofcheck-e2e-*.yml"))

    def test_minimal_set(self):
        sset = load_set(make_set(self.tmp))
        s = sset.scenarios[0]
        self.assertEqual((s.name, s.title, s.issue_title, s.issue_body), ("one", "Fix it", "It is broken", "bug"))
        self.assertEqual(substitute(s.body, 12), "Fixes #12.")
        self.assertEqual(substitute("{x} {issue} {issue}", None), "{x} ? ?")

    def bad(self, toml, files=None, set_toml=None, msg=""):
        root = make_set(self.tmp / msg.replace(" ", "_"), {"bad": (toml, files if files is not None else {"files/toy/a.py": "x"})},
                        set_toml=set_toml)
        with self.assertRaises(ScenarioError) as cm:
            load_set(root)
        self.assertIn(msg, str(cm.exception))

    def test_validation_errors(self):
        exp = '\n[expected]\nverdict = "evidence"\n'
        self.bad('body = "x"' + exp, msg="title")
        self.bad('title = "t"\nsurprise = 1' + exp, msg="unknown key")
        self.bad('title = "t"\n[expected]\nverdikt = "x"\n', msg="unknown expectation")
        self.bad('title = "t"\n[expected]\nmin_fail_to_pass = -1\n', msg="non-negative")
        self.bad('title = "t"\n[expected]\nmin_fail_to_pass = true\n', msg="non-negative")
        self.bad('title = "t"\n[expected]\nverdict = []\n', msg="non-empty list")
        self.bad('title = "t"\n[expected]\nmin_fail_to_pass = 3\nmax_fail_to_pass = 1\n', msg="min_fail_to_pass > max")
        self.bad('title = "t"\n[expected]\nclaims = [{status = "verified"}]\n', msg="needs at least a `type`")
        self.bad('title = "t"\n[expected]\nclaims = [{type = "x", colour = "red"}]\n', msg="unknown key 'colour'")
        self.bad('title = "t"\n[expected]\n', msg="is empty")
        self.bad('title = "t"\n', msg="[expected] table is required")
        self.bad('title = "Fixes #{issue}"' + exp, msg="has no issue")
        self.bad('title = "t"\n[issue]\ntitle = "i"' + exp, msg="issue.md is missing")
        self.bad('title = "t"' + exp, files={"files/toy/a.py": "x", "issue.md": "b"}, msg="needs an [issue] table")
        self.bad('title = "t"' + exp, files={}, msg="changes nothing")
        self.bad('title = "t"' + exp, files={"files/other/a.py": "x"}, msg="outside the set's allowed_paths")
        self.bad('title = "t"' + exp, files={"files/toy/a.py": "x", "patch.diff": "diff --git a/toy/a b/toy/a\n"},
                 msg="not both")
        self.bad('title = "t"' + exp, files={"patch.diff": "diff --git a/../etc/x b/../etc/x\n"}, msg="unsafe path")
        self.bad('title = "t"' + exp, files={"patch.diff": "just text\n"}, msg="no `diff --git`")
        self.bad('title = "t"' + exp, files={"patch.diff": "diff --git a/toy/l b/toy/l\nnew file mode 120000\n"},
                 msg="symlinks")
        self.bad('title = "t"\ndelete = ["../x"]' + exp, msg="unsafe path")
        self.bad('title = "t"\ndraft = "yes"' + exp, msg="true or false")
        self.bad('title = "t"\n[wait]\nagent = true\nforever = 1' + exp, msg="[wait] takes")
        self.bad('title = "t"' + exp, set_toml='allowed_paths = []\n', msg="must not be empty")
        self.bad('title = "t"' + exp, set_toml='bogus = 1\n', msg="unknown key")
        self.bad('title = = broken', msg="scenario.toml")

    def test_bad_scenario_names_and_symlinks(self):
        root = make_set(self.tmp)
        shutil.copytree(root / "one", root / "Bad_Name")
        with self.assertRaises(ScenarioError):
            load_set(root)
        shutil.rmtree(root / "Bad_Name")
        os.symlink("/etc/passwd", root / "one" / "files" / "toy" / "link.py")
        with self.assertRaises(ScenarioError) as cm:
            load_set(root)
        self.assertIn("regular files", str(cm.exception))

    def test_patch_paths(self):
        text = (TOY / "smuggled-hunk" / "patch.diff").read_text()
        self.assertEqual(patch_paths(text), ["e2e_toy/settings.py", "e2e_toy/tests/test_textops.py", "e2e_toy/textops.py"])
        self.assertEqual(patch_paths("diff --git a/x b/y\nrename from x\nrename to y\n"), ["x", "y"])

    def test_single_scenario_without_set(self):
        s = load_scenario(TOY / "real-fix")
        self.assertEqual(s.expected["verdict"], "evidence")


# --------------------------------------------------------------------------- git


def init_repo(path):
    git = Git(path)
    git.init()
    (path / "README.md").write_text("hi\n")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    git("branch", "-M", "main")
    return git


class GitOpsTest(TempDirCase):
    def test_build_branches_with_overlay_patch_and_delete(self):
        git = init_repo(self.tmp / "repo")
        sset = load_set(TOY)
        by = {s.name: s for s in sset.scenarios}
        base, head = build_branches(git, sset, by["smuggled-hunk"], "main", "b1", "h1")
        self.assertEqual(git("rev-parse", "b1"), base)
        self.assertEqual(git("rev-parse", "h1^"), base)
        self.assertEqual(git("rev-parse", "b1^"), git("rev-parse", "main"))
        self.assertEqual(sorted(git("diff", "--name-only", base, head).splitlines()),
                         ["e2e_toy/settings.py", "e2e_toy/tests/test_textops.py", "e2e_toy/textops.py"])
        self.assertIn("return high", git("show", f"{head}:e2e_toy/textops.py"))
        # overlay scenario on the same repo, starting again from main
        base2, head2 = build_branches(git, sset, by["workflow-change"], "main", "b2", "h2")
        self.assertEqual(git("diff", "--name-only", base2, head2), ".github/workflows/proofcheck-e2e-dummy.yml")
        self.assertNotIn("REPORT_URL = \"https", git("show", f"{head2}:e2e_toy/settings.py"))

    def test_deletions_and_empty_changes(self):
        root = make_set(self.tmp, {
            "rm": ('title = "t"\ndelete = ["toy/a.py"]\n[expected]\nverdict = "evidence"\n', {}),
            "same": ('title = "t"\n[expected]\nverdict = "evidence"\n', {"files/toy/a.py": "A = 1\n"}),
            "gone": ('title = "t"\ndelete = ["toy/zzz.py"]\n[expected]\nverdict = "evidence"\n', {}),
        })
        sset = load_set(root)
        git = init_repo(self.tmp / "repo")
        by = {s.name: s for s in sset.scenarios}
        base, head = build_branches(git, sset, by["rm"], "main", "b", "h")
        self.assertEqual(git("diff", "--name-status", base, head), "D\ttoy/a.py")
        with self.assertRaises(ScenarioError) as cm:
            build_branches(git, sset, by["same"], "main", "b", "h")
        self.assertIn("empty", str(cm.exception))
        with self.assertRaises(ScenarioError):
            build_branches(git, sset, by["gone"], "main", "b", "h")

    def test_patch_that_does_not_apply(self):
        root = make_set(self.tmp, {"p": ('title = "t"\n[expected]\nverdict = "evidence"\n', {"patch.diff": (
            "diff --git a/toy/a.py b/toy/a.py\n--- a/toy/a.py\n+++ b/toy/a.py\n@@ -1 +1 @@\n-B = 7\n+B = 8\n")})})
        sset = load_set(root)
        git = init_repo(self.tmp / "repo")
        from pce2e.gitops import GitError

        with self.assertRaises(GitError):
            build_branches(git, sset, sset.scenarios[0], "main", "b", "h")

    def test_overlay_refuses_symlinked_directories_in_the_repo(self):
        repo = self.tmp / "repo"
        init_repo(repo)
        (self.tmp / "elsewhere").mkdir()
        os.symlink(self.tmp / "elsewhere", repo / "toy")
        overlay = self.tmp / "ov"
        write(overlay / "toy" / "a.py", "x")
        with self.assertRaises(ScenarioError):
            apply_overlay(repo, overlay)
        self.assertFalse((self.tmp / "elsewhere" / "a.py").exists())

    def test_env_and_redaction(self):
        env = clean_git_env({"GIT_DIR": "/x", "GIT_WORK_TREE": "/y", "GIT_CONFIG_COUNT": "1", "HOME": "/h"})
        self.assertNotIn("GIT_DIR", env)
        self.assertNotIn("GIT_WORK_TREE", env)
        self.assertNotIn("GIT_CONFIG_COUNT", env)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        a = auth_env(TOKEN)
        self.assertEqual(a["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        self.assertNotIn(TOKEN, json.dumps(a))  # only the base64 form, which the redactor also knows
        red = Redactor([TOKEN])
        self.assertEqual(red(f"x {TOKEN} {a['GIT_CONFIG_VALUE_0']}"), "x *** AUTHORIZATION: basic ***")
        self.assertEqual(Redactor([""])("abc"), "abc")

    def test_git_errors_are_redacted(self):
        git = Git(self.tmp / "nope", redact=Redactor([TOKEN]))
        from pce2e.gitops import GitError

        with self.assertRaises(GitError) as cm:
            git("fetch", f"https://example.invalid/{TOKEN}")
        self.assertNotIn(TOKEN, str(cm.exception))


# --------------------------------------------------------------------------- expectations


class ExpectTest(unittest.TestCase):
    def data(self, **kw):
        d = {"v": 1, "head": "b" * 40, "verdict": "evidence",
             "claims": [{"type": "fixes_issue", "text": "fixes #3", "status": "verified", "verified_by": "agent"},
                        {"type": "adds_tests", "text": "adds tests", "status": "unverified"}],
             "tests": {"status": "evidence", "fail_to_pass": 2, "pass_to_pass": 1, "fail_on_head": 0},
             "ablation": {"status": "ran", "required": 1, "not_exercised": 2}, "agent": {"status": "evidence"},
             "flags": ["workflow", "unexercised"], "repro": ["behavior_changed"], "warnings": 1}
        d.update(kw)
        return d

    def test_all_met(self):
        exp = {"verdict": ["evidence", "partial"], "tests_status": "evidence", "min_fail_to_pass": 2, "max_fail_to_pass": 2,
               "min_pass_to_pass": 1, "max_fail_on_head": 0, "ablation_status": "ran", "min_ablation_required": 1,
               "min_ablation_not_exercised": 2, "max_ablation_not_exercised": 2, "agent_status": ["verified (agent test)"],
               "flags": ["workflow"], "absent_flags": ["binary"], "repro": ["behavior_changed"], "warnings": True,
               "claim_count": 2, "label": "proofcheck: verified", "card_contains": ["abc"], "card_not_contains": ["<img"],
               "claims": [{"type": "fixes_issue", "status": "verified", "verified_by": "agent", "text_contains": "#3"},
                          {"type": ["adds_tests", "x"], "status": ["unverified", "verified"], "verified_by": ""}]}
        self.assertEqual(expect.evaluate(exp, self.data(), card="xx abc", labels=["bug", "proofcheck: verified"]), [])

    def test_failures_are_reported(self):
        exp = {"verdict": "contradicted", "min_fail_to_pass": 3, "max_pass_to_pass": 0, "max_ablation_required": 0,
               "agent_status": ["no evidence", "none"], "flags": ["binary"], "absent_flags": ["workflow"],
               "repro": ["unchanged"], "warnings": False, "claim_count": 1, "label": "proofcheck: verified",
               "card_contains": ["zzz"], "card_not_contains": ["abc"], "tests_status": "no_tests",
               "claims": [{"type": "fixes_issue", "verified_by": ""}, {"type": "performance"}]}
        fails = expect.evaluate(exp, self.data(), card="abc", labels=["proofcheck: partial"])
        text = "\n".join(fails)
        for needle in ("verdict: expected contradicted, got 'evidence'", "fail_to_pass: expected at least 3, got 2",
                       "pass_to_pass: expected at most 0, got 1", "ablation_required: expected at most 0, got 1",
                       "agent_status: expected one of no_evidence, none, got 'evidence'", "flags: expected 'binary'",
                       "did not expect 'workflow'", "repro: expected a snippet with status 'unchanged'",
                       "warnings: expected no trusted warning", "claim_count: expected 1, got 2",
                       "label: expected proofcheck: verified, got ['proofcheck: partial']", "'zzz' not found",
                       "'abc' found", "tests_status: expected no_tests", "claims of that type: ['fixes_issue=verified (by agent)']",
                       "claims of that type: none"):
            self.assertIn(needle, text)
        self.assertEqual(len(fails), 16)

    def test_missing_sections_count_as_zero_and_none(self):
        d = self.data(tests=None, ablation=None, agent=None, flags=None, repro=None, claims=[])
        self.assertEqual(expect.evaluate({"tests_status": "none", "ablation_status": "none", "agent_status": "absent",
                                          "max_fail_to_pass": 0, "max_ablation_required": 0, "claim_count": 0}, d), [])
        self.assertEqual(expect.evaluate({"min_fail_to_pass": 1}, d), ["fail_to_pass: expected at least 1, got 0"])
        self.assertEqual(expect.evaluate({"verdict": "evidence"}, None), ["no proofcheck data block found on the card"])

    def test_labels(self):
        d = self.data()
        self.assertEqual(expect.evaluate({"label": ""}, d, labels=["bug"]), [])
        self.assertEqual(len(expect.evaluate({"label": ""}, d, labels=["proofcheck: partial"])), 1)
        self.assertEqual(expect.evaluate({"label": ["proofcheck: verified", "proofcheck: no evidence"]}, d,
                                         labels=["proofcheck: no evidence"]), [])
        self.assertIn("got none", expect.evaluate({"label": "proofcheck: verified"}, d, labels=None)[0])

    def test_claims_are_matched_one_to_one(self):
        d = self.data(claims=[{"type": "fixes_issue", "status": "verified"}])
        exp = {"claims": [{"type": "fixes_issue"}, {"type": "fixes_issue"}]}
        self.assertEqual(len(expect.evaluate(exp, d)), 1)


# --------------------------------------------------------------------------- report


class ReportTest(TempDirCase):
    def test_render_and_write(self):
        rep = report.Report(mode="online", scenario_set="python-toy", repo="acme/toy", started="t0", finished="t1")
        ok = report.ScenarioResult(name="real-fix", status="pass", pr_number=4, pr_url="https://github.com/acme/toy/pull/4",
                                   head_sha="b" * 40, base_sha="a" * 40, head_branch="h", base_branch="b",
                                   data={"verdict": "evidence", "tests": {"fail_to_pass": 1, "pass_to_pass": 0},
                                         "ablation": {"required": 1, "not_exercised": 0}, "agent": None},
                                   labels=["proofcheck: verified"], cleanup=["closed PR #4"], log=["[t] line"])
        bad = report.ScenarioResult(name="fake|fix", status="fail", failures=["verdict: expected x, got y"],
                                    diagnostics=[{"name": "proofcheck", "event": "pull_request", "status": "completed",
                                                  "conclusion": "failure", "url": "u"}])
        err = report.ScenarioResult(name="boom", status="error", error="push rejected")
        rep.results += [ok, bad, err]
        self.assertFalse(rep.ok)
        self.assertEqual(rep.summary(), {"pass": 1, "fail": 1, "error": 1})
        md = report.render_markdown(rep)
        self.assertIn("# proofcheck e2e report: `python-toy` (GitHub)", md)
        self.assertIn("**FAIL** (1 error, 1 fail, 1 pass)", md)
        self.assertIn("| `real-fix` | ✅ pass | [#4](https://github.com/acme/toy/pull/4) | evidence | 1 / 0 | 1 / 0 | none |", md)
        self.assertIn("`fake\\|fix`", md)
        self.assertIn("- ❌ verdict: expected x, got y", md)
        self.assertIn("- **Error:** push rejected", md)
        self.assertIn("workflow run: proofcheck (pull_request): completed/failure u", md)
        self.assertIn('"verdict": "evidence"', md)
        self.assertIn("- cleanup: closed PR #4", md)
        md_path, js_path = report.write_report(rep, self.tmp / "out")
        doc = json.loads(js_path.read_text())
        self.assertEqual(doc["summary"], {"pass": 1, "fail": 1, "error": 1})
        self.assertFalse(doc["ok"])
        self.assertEqual(doc["scenarios"][0]["labels"], ["proofcheck: verified"])
        self.assertEqual(md_path.read_text(), md)
        planned = report.Report(mode="dry-run", scenario_set="s", results=[report.ScenarioResult(name="x", status="planned")])
        self.assertTrue(planned.ok)


# --------------------------------------------------------------------------- HTTP layer


class FakeResponse:
    def __init__(self, status, payload=None, headers=None, text=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text if text is not None else ("" if payload is None else json.dumps(payload))

    def json(self):
        if self._payload is None and not self.text:
            raise ValueError("no body")
        return self._payload if self._payload is not None else json.loads(self.text)


class ScriptedSession:
    """Returns the scripted responses in order and records the requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, json=None, params=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}), "json": json, "params": params})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class HTTPLayerTest(unittest.TestCase):
    def gh(self, responses, **kw):
        self.slept = []
        self.session = ScriptedSession(responses)
        return GitHub("acme/toy", TOKEN, session=self.session, sleep=self.slept.append, clock=lambda: 1000.0,
                      redact=Redactor([TOKEN]), **kw)

    def test_headers_and_basic_calls(self):
        gh = self.gh([FakeResponse(200, {"default_branch": "trunk"}), FakeResponse(201, {"number": 9}),
                      FakeResponse(204), FakeResponse(200, {"number": 10, "head": {"sha": "c" * 40}})])
        self.assertEqual(gh.get_repo()["default_branch"], "trunk")
        self.assertEqual(gh.create_issue("t", "b")["number"], 9)
        gh.close_pull(10)
        gh.create_pull("t", "b", "head-br", "base-br", draft=True)
        c = self.session.calls
        self.assertEqual(c[0]["url"], "https://api.github.com/repos/acme/toy")
        self.assertEqual(c[0]["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(c[1]["json"], {"title": "t", "body": "b"})
        self.assertEqual((c[2]["method"], c[2]["json"]), ("PATCH", {"state": "closed"}))
        self.assertEqual(c[3]["json"], {"title": "t", "body": "b", "head": "head-br", "base": "base-br", "draft": True})

    def test_retries_transient_errors_with_backoff(self):
        gh = self.gh([FakeResponse(502, text="bad gateway"), ConnectionError("reset"), FakeResponse(200, {"ok": 1})])
        self.assertEqual(gh.request("GET", "/x")[0], {"ok": 1})
        self.assertEqual(self.slept, [2.0, 4.0])

    def test_rate_limits(self):
        gh = self.gh([FakeResponse(403, {"message": "rate limited"}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1030"}),
                      FakeResponse(429, {}, {"Retry-After": "7"}), FakeResponse(200, [])])
        self.assertEqual(gh.request("GET", "/x")[0], [])
        self.assertEqual(self.slept, [31.0, 7.0])

    def test_errors_are_not_retried_and_redacted(self):
        gh = self.gh([FakeResponse(422, {"message": f"Validation Failed for {TOKEN}", "errors": [{"code": "invalid"}]})])
        with self.assertRaises(GitHubError) as cm:
            gh.create_pull("t", "b", "h", "b")
        self.assertEqual(cm.exception.status, 422)
        self.assertIn("HTTP 422 Validation Failed for ***", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertEqual(self.slept, [])
        gh = self.gh([ConnectionError(f"proxy said {TOKEN}")] * 3, max_retries=2)
        with self.assertRaises(GitHubError) as cm:
            gh.get_repo()
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertEqual(len(self.slept), 2)
        gh = self.gh([FakeResponse(503, text="down")] * 2, max_retries=1)
        with self.assertRaises(GitHubError) as cm:
            gh.get_repo()
        self.assertEqual(cm.exception.status, 503)
        gh = self.gh([FakeResponse(200, text="<html>")])
        with self.assertRaises(GitHubError):
            gh.get_repo()

    def test_pagination(self):
        nxt = 'https://api.github.com/repositories/1/issues/4/comments?per_page=100&page=2'
        gh = self.gh([FakeResponse(200, [{"id": 1}], {"Link": f'<{nxt}>; rel="next", <x>; rel="last"'}),
                      FakeResponse(200, [{"id": 2}])])
        self.assertEqual(gh.issue_comments(4), [{"id": 1}, {"id": 2}])
        self.assertEqual(self.session.calls[0]["params"], {"per_page": 100})
        self.assertEqual(self.session.calls[1]["url"], nxt)
        self.assertIsNone(self.session.calls[1]["params"])
        gh = self.gh([FakeResponse(200, [], {"Link": '<https://evil.example/steal>; rel="next"'})])
        with self.assertRaises(GitHubError):
            gh.issue_comments(4)

    def test_delete_branch_and_runs(self):
        gh = self.gh([FakeResponse(204), FakeResponse(422, {"message": "Reference does not exist"}),
                      FakeResponse(500, {})] + [FakeResponse(500, {})] * 4 +
                     [FakeResponse(200, {"workflow_runs": [{"name": "proofcheck", "event": "pull_request", "status": "queued",
                                                            "conclusion": None, "html_url": "u", "secret": "x"}]})])
        self.assertTrue(gh.delete_branch("proofcheck-e2e/a-1"))
        self.assertEqual(self.session.calls[0]["url"], "https://api.github.com/repos/acme/toy/git/refs/heads/proofcheck-e2e/a-1")
        self.assertFalse(gh.delete_branch("gone"))
        with self.assertRaises(GitHubError):
            gh.delete_branch("x")
        self.assertEqual(gh.runs_for_sha("c" * 40), [{"name": "proofcheck", "event": "pull_request", "status": "queued",
                                                      "conclusion": None, "url": "u"}])

    def test_invalid_repo(self):
        with self.assertRaises(GitHubError):
            GitHub("acme/toy/../x", TOKEN, session=object())

    def test_find_card(self):
        head = "c" * 40
        old = {"body": make_card("d" * 40), "user": BOT}
        planted = {"body": make_card(head), "user": HUMAN}
        legacy = {"body": CARD_MARKER + "\nno data block", "user": BOT}
        good = {"body": make_card(head, verdict="no_evidence"), "user": BOT}
        c, data = find_card([good, old, planted, legacy], head)
        self.assertIs(c, good)
        self.assertEqual(data["verdict"], "no_evidence")
        self.assertEqual(find_card([old, planted, legacy], head), (None, None))
        self.assertIs(find_card([old], None)[0], old)
        self.assertIs(find_card([planted], head, bot_login="MALLORY")[0], planted)
        self.assertEqual(find_card([good], head, bot_login="someone-else"), (None, None))


# --------------------------------------------------------------------------- online orchestration (fake GitHub)


class FakeGitHubServer:
    """Simulates the GitHub endpoints the harness uses, plus proofcheck posting a card.

    ``card_for(pr) -> (body, labels)`` produces the card (None: never). Cards
    appear after ``delay_polls`` comment listings. Branches live in a real
    bare repository that the harness pushes to.
    """

    def __init__(self, bare, card_for, delay_polls=1, fail_pull=False, label_lag=0, refuse_delete=False):
        self.bare = bare
        self.refuse_delete = refuse_delete
        self.label_lag = label_lag
        self.card_for = card_for
        self.delay_polls = delay_polls
        self.fail_pull = fail_pull
        self.n = 0
        self.issues, self.pulls, self.comments, self.labels, self.polls = {}, {}, {}, {}, {}
        self.deleted, self.calls = [], []

    def _sha(self, branch):
        r = subprocess.run(["git", "-C", str(self.bare), "rev-parse", f"refs/heads/{branch}"], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    def request(self, method, url, headers=None, json=None, params=None, timeout=None):
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        path = urlparse(url).path
        self.calls.append((method, path))
        parts = path.strip("/").split("/")[3:]  # after repos/acme/toy
        if method == "GET" and not parts:
            return FakeResponse(200, {"default_branch": "main"})
        if method == "POST" and parts == ["issues"]:
            self.n += 1
            self.issues[self.n] = dict(json, state="open")
            return FakeResponse(201, {"number": self.n, "html_url": f"https://github.com/acme/toy/issues/{self.n}"})
        if method == "POST" and parts == ["pulls"]:
            if self.fail_pull:
                return FakeResponse(422, {"message": f"No commits between (token {TOKEN})"})
            self.n += 1
            sha = self._sha(json["head"])
            assert self._sha(json["base"]) is not None, "base branch must be pushed first"
            self.pulls[self.n] = dict(json, state="open", sha=sha)
            return FakeResponse(201, {"number": self.n, "html_url": f"https://github.com/acme/toy/pull/{self.n}",
                                      "head": {"sha": sha}})
        if method == "PATCH" and parts[0] in ("issues", "pulls"):
            (self.issues if parts[0] == "issues" else self.pulls)[int(parts[1])]["state"] = json["state"]
            return FakeResponse(200, {})
        if parts[:1] == ["issues"] and parts[2:] == ["labels"]:
            n = int(parts[1])
            if method == "POST":
                self.labels.setdefault(n, []).extend(json["labels"])
                return FakeResponse(200, [])
            self.label_lag -= 1
            names = [x for x in self.labels.get(n, []) if self.label_lag < 0 or not x.startswith("proofcheck: ")]
            return FakeResponse(200, [{"name": x} for x in names])
        if method == "GET" and parts[:1] == ["issues"] and parts[2:] == ["comments"]:
            n = int(parts[1])
            self.polls[n] = self.polls.get(n, 0) + 1
            out = [{"id": 1, "body": "first!", "user": HUMAN}]
            if self.polls[n] > self.delay_polls:
                made = self.card_for(self.pulls[n], self.polls[n])
                if made is not None:
                    body, labels = made
                    out.append({"id": 2, "body": body, "user": BOT})
                    self.labels[n] = [x for x in self.labels.get(n, []) if not x.startswith("proofcheck: ")] + list(labels)
            return FakeResponse(200, out)
        if method == "DELETE" and parts[:3] == ["git", "refs", "heads"] and self.refuse_delete:
            return FakeResponse(403, {"message": "Write access to this GitHub API path is not permitted"})
        if method == "DELETE" and parts[:3] == ["git", "refs", "heads"]:
            branch = "/".join(parts[3:])
            if self._sha(branch) is None:
                return FakeResponse(422, {"message": "Reference does not exist"})
            subprocess.run(["git", "-C", str(self.bare), "update-ref", "-d", f"refs/heads/{branch}"], check=True)
            self.deleted.append(branch)
            return FakeResponse(204)
        if method == "GET" and parts == ["actions", "runs"]:
            return FakeResponse(200, {"workflow_runs": [{"name": "proofcheck", "event": "pull_request",
                                                         "status": "in_progress", "conclusion": None, "html_url": "u"}]})
        return FakeResponse(404, {"message": f"not found {method} {path}"})


ONLINE_SET = {
    "good": ('title = "Fix a"\nbody = "Fixes #{issue}. token-free body"\nlabels = ["e2e"]\n[issue]\ntitle = "a is wrong"\n'
             '[expected]\nverdict = "evidence"\nlabel = "proofcheck: verified"\nmin_fail_to_pass = 1\n'
             'agent_status = ["none"]\nclaims = [{type = "fixes_issue", status = "verified"}]\n',
             {"files/toy/a.py": "A = 2\n", "issue.md": "a should be 2"}),
    "wf": ('title = "Touch b"\n[wait]\nagent = true\nagent_grace = 100\n[expected]\nverdict = "no_evidence"\n'
           'label = "proofcheck: no evidence"\nagent_status = ["no evidence", "none"]\nwarnings = true\n',
           {"files/toy/b.py": "B = 1\n"}),
}


class OnlineRunnerTest(TempDirCase):
    def setUp(self):
        super().setUp()
        seed = self.tmp / "seed"
        init_repo(seed)
        self.bare = self.tmp / "remote.git"
        subprocess.run(["git", *GIT_ID, "clone", "-q", "--bare", str(seed), str(self.bare)], check=True)
        self.sset = load_set(make_set(self.tmp, ONLINE_SET))
        self.clock_now = 0.0

    def clock(self):
        return self.clock_now

    def sleep(self, s):
        self.slept.append(s)
        self.clock_now += s

    def run_harness(self, server, keep_open=False, timeout=600, only=None):
        self.slept = []
        out = io.StringIO()
        log = harness.Log(Redactor([TOKEN]), stream=out)
        gh = GitHub("acme/toy", TOKEN, session=server, sleep=self.sleep, clock=self.clock, redact=log.redact)
        opts = harness.Options(repo="acme/toy", timeout=timeout, keep_open=keep_open, poll_initial=10, poll_max=40,
                               remote_url=str(self.bare), label_wait=20)
        rep = harness.OnlineRunner(self.sset, self.sset.select(only), gh, opts, log, self.tmp / "work", token=TOKEN,
                                   clock=self.clock, sleep=self.sleep).run()
        self.output = out.getvalue()
        self.assertNotIn(TOKEN, self.output)
        self.assertNotIn(TOKEN, json.dumps(rep.to_dict()))
        return rep

    @staticmethod
    def cards(pr, polls):
        if pr["title"] == "Fix a":
            return make_card(pr["sha"]), ["proofcheck: verified"]
        # the wf scenario: first card without the agent section, then with it
        return make_card(pr["sha"], verdict="no_evidence", f2p=0, warnings=1,
                         agent="no_evidence" if polls >= 4 else None), ["proofcheck: no evidence"]

    def test_full_run_passes_and_cleans_up(self):
        server = FakeGitHubServer(self.bare, self.cards)
        rep = self.run_harness(server)
        by = {r.name: r for r in rep.results}
        self.assertTrue(rep.ok, [(r.name, r.status, r.failures, r.error) for r in rep.results])
        good = by["good"]
        self.assertEqual(good.status, "pass")
        self.assertEqual(good.data["verdict"], "evidence")
        pr = server.pulls[good.pr_number]
        self.assertEqual(pr["sha"], good.head_sha)
        self.assertTrue(pr["base"].startswith("proofcheck-e2e/good-base-"))
        self.assertTrue(pr["head"].startswith("proofcheck-e2e/good-"))
        self.assertEqual(pr["body"], f"Fixes #{good.issue_number}. token-free body")
        self.assertFalse(pr["draft"])
        self.assertIn("e2e", server.labels[good.pr_number])
        self.assertEqual(server.issues[good.issue_number]["state"], "closed")
        self.assertEqual(pr["state"], "closed")
        self.assertEqual(by["wf"].data["agent"]["status"], "no_evidence")  # waited for the agent section
        self.assertIsNone(by["wf"].issue_number)
        self.assertEqual(len(server.deleted), 4)
        refs = subprocess.run(["git", "-C", str(self.bare), "for-each-ref", "--format=%(refname)"],
                              capture_output=True, text=True).stdout.split()
        self.assertEqual(refs, ["refs/heads/main"])
        self.assertIn("closed PR", "\n".join(good.cleanup))
        self.assertTrue(all(s <= 40 for s in self.slept))  # backoff is capped
        self.assertIn("waiting up to 100s for the agent section", self.output)

    def test_refused_api_branch_deletes_fall_back_to_git(self):
        # Some tokens may not delete refs through the API; the harness retries
        # once with `git push --delete` and only reports what still remains.
        server = FakeGitHubServer(self.bare, self.cards, refuse_delete=True)
        rep = self.run_harness(server)
        self.assertTrue(rep.ok, [(r.name, r.status, r.failures, r.error) for r in rep.results])
        refs = subprocess.run(["git", "-C", str(self.bare), "for-each-ref", "--format=%(refname)"],
                              capture_output=True, text=True).stdout.split()
        self.assertEqual(refs, ["refs/heads/main"])
        self.assertIn("deleted 4 branch(es) with git push --delete", self.output)
        self.assertFalse(any("could not be deleted" in n for n in rep.notes))

    def test_branches_that_cannot_be_deleted_are_reported(self):
        server = FakeGitHubServer(self.bare, self.cards, refuse_delete=True)
        hook = self.bare / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nwhile read old new ref; do [ \"$new\" = 0000000000000000000000000000000000000000 ] && exit 1; done\nexit 0\n")
        hook.chmod(0o755)
        rep = self.run_harness(server)
        note = next(n for n in rep.notes if "could not be deleted" in n)
        self.assertIn("git push origin --delete proofcheck-e2e/", note)
        self.assertEqual(note.count("proofcheck-e2e/"), 4)

    def test_agent_grace_expires(self):
        server = FakeGitHubServer(self.bare, lambda pr, polls: (make_card(pr["sha"], verdict="no_evidence", f2p=0, warnings=1),
                                                                ["proofcheck: no evidence"]))
        rep = self.run_harness(server, only=["wf"])
        r = rep.results[0]
        self.assertEqual(r.status, "pass", r.failures)  # "none" is allowed
        self.assertIn("no agent section within the grace period", self.output)

    def test_card_for_an_older_head_is_ignored_and_timeout_cleans_up(self):
        server = FakeGitHubServer(self.bare, lambda pr, polls: (make_card("d" * 40), []))
        rep = self.run_harness(server, timeout=120, only=["good"])
        r = rep.results[0]
        self.assertEqual(r.status, "timeout")
        self.assertIn("no proof card for head", r.error)
        self.assertEqual(r.diagnostics[0]["status"], "in_progress")
        self.assertFalse(rep.ok)
        self.assertEqual(server.pulls[r.pr_number]["state"], "closed")
        self.assertEqual(len(server.deleted), 2)
        self.assertGreaterEqual(self.clock_now, 120)

    def test_failed_expectations(self):
        server = FakeGitHubServer(self.bare, lambda pr, polls: (make_card(pr["sha"], verdict="partial"), ["proofcheck: partial"]))
        rep = self.run_harness(server, only=["good"])
        r = rep.results[0]
        self.assertEqual(r.status, "fail")
        self.assertIn("verdict: expected evidence, got 'partial'", r.failures)
        self.assertIn("label: expected proofcheck: verified, got ['proofcheck: partial']", r.failures)

    def test_waits_for_the_verdict_label(self):
        server = FakeGitHubServer(self.bare, self.cards, label_lag=2)
        rep = self.run_harness(server, only=["good"])
        self.assertEqual(rep.results[0].status, "pass", rep.results[0].failures)
        self.assertIn(5, self.slept)

    def test_keep_open(self):
        server = FakeGitHubServer(self.bare, self.cards)
        rep = self.run_harness(server, keep_open=True, only=["good"])
        self.assertTrue(rep.ok)
        self.assertEqual(server.deleted, [])
        self.assertEqual(server.pulls[rep.results[0].pr_number]["state"], "open")
        self.assertEqual(rep.results[0].cleanup, ["kept open (--keep-open)"])

    def test_error_while_opening_still_cleans_up(self):
        server = FakeGitHubServer(self.bare, self.cards, fail_pull=True)
        rep = self.run_harness(server)
        for r in rep.results:
            self.assertEqual(r.status, "error")
            self.assertIn("HTTP 422", r.error)
            self.assertIn("***", r.error)
        self.assertEqual(len(server.deleted), 4)  # both branches of both scenarios
        self.assertEqual([i["state"] for i in server.issues.values()], ["closed"])

    def test_interrupt_still_cleans_up(self):
        def boom(pr, polls):
            raise KeyboardInterrupt

        server = FakeGitHubServer(self.bare, boom)
        with self.assertRaises(KeyboardInterrupt):
            self.run_harness(server, only=["good"])
        self.assertEqual(len(server.deleted), 2)
        self.assertEqual([p["state"] for p in server.pulls.values()], ["closed"])


# --------------------------------------------------------------------------- CLI


class CLITest(TempDirCase):
    def test_parse_args(self):
        a = harness.parse_args(["--repo", "o/n", "--scenarios", "s", "--only", "a,b", "--only", "c", "--keep-open"])
        self.assertEqual(a.only, ["a", "b", "c"])
        self.assertTrue(a.keep_open)
        self.assertEqual(a.timeout, 3600)
        for bad in (["--scenarios", "s"], ["--scenarios", "s", "--dry-run", "--local"]):
            with self.assertRaises(SystemExit):
                with unittest.mock.patch("sys.stderr", io.StringIO()):
                    harness.parse_args(bad)

    def test_main_without_token_and_bad_set(self):
        env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
        with unittest.mock.patch.dict(os.environ, env, clear=True), \
                unittest.mock.patch("sys.stderr", io.StringIO()) as err, unittest.mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(harness.main(["--repo", "o/n", "--scenarios", str(TOY), "--report-dir", str(self.tmp)]), 2)
            self.assertIn("GITHUB_TOKEN", err.getvalue())
            self.assertEqual(harness.main(["--scenarios", str(self.tmp / "missing"), "--dry-run"]), 2)
            self.assertEqual(harness.main(["--scenarios", str(TOY), "--dry-run", "--only", "nope"]), 2)
        self.assertEqual(list(self.tmp.iterdir()), [])  # nothing written

    def test_dry_run_prints_plan_and_never_the_token(self):
        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {"GITHUB_TOKEN": TOKEN}), unittest.mock.patch("sys.stdout", out):
            code = harness.main(["--repo", "acme/toy", "--scenarios", str(TOY), "--dry-run", "--only", "real-fix,workflow-change"])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertNotIn(TOKEN, text)
        self.assertIn("would open PR 'Fix word_count with repeated whitespace'", text)
        self.assertIn("change: +1/-1 e2e_toy/textops.py", text)
        self.assertIn("change: +15/-0 .github/workflows/proofcheck-e2e-dummy.yml", text)
        self.assertIn("would close the PR and the issue and delete both branches", text)
        self.assertIn("nothing was pushed or opened", text)


if __name__ == "__main__":
    unittest.main()
