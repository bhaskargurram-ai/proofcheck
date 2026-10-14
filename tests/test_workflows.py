"""Security invariants of the GitHub workflows (skipped when PyYAML is not installed).

These encode docs/SECURITY_MODEL.md as checks, so a later edit cannot quietly
break the trust-zone split:

* no workflow is triggered by ``pull_request_target``;
* no ``run:`` script contains a ``${{ }}`` expression (values reach scripts
  through ``env:`` only, preventing script injection);
* every checkout sets ``persist-credentials: false``;
* comment.yml never checks out the pull request and never installs a project;
* agent.yml: ``propose`` (holds the key) never installs or runs the PR, which
  it checks out into ``pr/`` as data; ``verify`` (runs PR code) references no
  secrets, has a read-only token and uses no caches; ``gate`` checks nothing out;
* Rust: the toolchain is set up (PR-controlled rust-toolchain.toml) only after
  the token step, and ``Swatinem/rust-cache`` only appears in the unprivileged
  ``pull_request`` evidence job, never in a ``workflow_run`` workflow.
"""

import json
import re
import unittest

import _helpers

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is optional for the test suite
    yaml = None

WF_DIR = _helpers.ROOT / ".github" / "workflows"
EXAMPLES = _helpers.ROOT / "examples" / "workflows"


def load(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 parses the key `on` as True
    return data


def steps(job):
    return job.get("steps") or []


def checkouts(job):
    return [s for s in steps(job) if str(s.get("uses", "")).startswith("actions/checkout@")]


@unittest.skipIf(yaml is None, "PyYAML not installed")
class WorkflowSecurityTest(unittest.TestCase):
    def all_workflows(self):
        files = sorted(WF_DIR.glob("*.yml")) + sorted(EXAMPLES.glob("*.yml"))
        self.assertGreater(len(files), 8)
        return [(f, load(f)) for f in files]

    def test_no_pull_request_target_and_no_expressions_in_scripts(self):
        for path, wf in self.all_workflows():
            on = wf["on"]
            triggers = on if isinstance(on, (dict, list)) else [on]
            self.assertNotIn("pull_request_target", triggers, path.name)
            for name, job in (wf.get("jobs") or {}).items():
                for s in steps(job):
                    if "run" in s:
                        self.assertNotIn("${{", s["run"], f"{path.name}:{name}:{s.get('name')}")

    def test_every_checkout_drops_credentials(self):
        for path, wf in self.all_workflows():
            for name, job in (wf.get("jobs") or {}).items():
                for s in checkouts(job):
                    self.assertIs((s.get("with") or {}).get("persist-credentials"), False, f"{path.name}:{name}")

    def test_comment_workflow_never_touches_pr_code(self):
        wf = load(WF_DIR / "comment.yml")
        self.assertIn("workflow_call", wf["on"])
        job = wf["jobs"]["comment"]
        self.assertEqual(job["permissions"], {"contents": "read", "actions": "read", "pull-requests": "write"})
        for s in checkouts(job):
            self.assertEqual(s["with"]["repository"], "${{ inputs.proofcheck-repository }}")  # the tool, never the PR
            self.assertNotIn("head", json.dumps(s))
        text = json.dumps(job)
        for forbidden in ("install-command", "INSTALL_COMMAND", "pull_request.head", "pip install -e", "secrets."):
            self.assertNotIn(forbidden, text)

    def test_agent_zones(self):
        wf = load(WF_DIR / "agent.yml")
        jobs = wf["jobs"]
        self.assertEqual(set(jobs), {"gate", "propose", "verify"})
        self.assertEqual(checkouts(jobs["gate"]), [])

        propose = jobs["propose"]
        ptext = json.dumps(propose)
        for forbidden in ("install-command", "INSTALL_COMMAND", "eval ", "pip install -e", "pip install ."):
            self.assertNotIn(forbidden, ptext)
        pr_checkouts = [s for s in checkouts(propose) if "repository" not in s.get("with", {})]
        self.assertEqual(len(pr_checkouts), 1)
        self.assertEqual(pr_checkouts[0]["with"]["path"], "pr")
        self.assertNotIn("write", json.dumps(propose["permissions"]))
        key_steps = [s for s in steps(propose) if "secrets." in json.dumps(s)]
        self.assertEqual([s.get("id") for s in key_steps], ["propose"])

        verify = jobs["verify"]
        vtext = json.dumps(verify)
        self.assertNotIn("secrets", vtext)
        self.assertNotIn("actions/cache", vtext)
        self.assertNotIn('"cache"', vtext)
        self.assertEqual(verify["permissions"], {"contents": "read"})
        self.assertEqual(set(verify["needs"]), {"gate", "propose"})
        # the secret is declared optional and only the gate (as a boolean) and propose read it
        self.assertEqual([j for j in jobs if "secrets." in json.dumps(jobs[j])], ["gate", "propose"])
        gate_secret = re.findall(r"\$\{\{[^}]*secrets\.[^}]*\}\}", json.dumps(jobs["gate"]))
        self.assertEqual(gate_secret, ["${{ secrets.llm-api-key != '' }}"])

    def test_evidence_workflow_is_unprivileged(self):
        wf = load(WF_DIR / "evidence.yml")
        job = wf["jobs"]["evidence"]
        self.assertEqual(job["if"], "github.event_name == 'pull_request'")
        self.assertEqual(job["permissions"], {"contents": "read", "issues": "read"})
        self.assertNotIn("secrets.", json.dumps(job))
        token_steps = [s.get("name") for s in steps(job) if "github.token" in json.dumps(s)]
        self.assertEqual(token_steps, ["Collect PR context (before any PR code runs)"])
        names = [s.get("name") or s.get("uses") for s in steps(job)]
        self.assertLess(names.index("Collect PR context (before any PR code runs)"),
                        names.index("Install the project under test (runs PR code)"))
        self.assertLess(names.index("Install proofcheck (isolated virtualenv)"),
                        names.index("Install the project under test (runs PR code)"))

    def test_callers(self):
        for d in (WF_DIR, EXAMPLES):
            ev = load(d / "proofcheck.yml")
            self.assertEqual(list(ev["on"]), ["pull_request"])
            self.assertEqual(ev["name"], "proofcheck")
            for name in ("proofcheck-comment.yml", "proofcheck-agent.yml"):
                wf = load(d / name)
                self.assertEqual(wf["on"]["workflow_run"]["workflows"], ["proofcheck"])
                self.assertEqual(wf["permissions"], {})
                for job in wf["jobs"].values():
                    self.assertNotIn("steps", job)  # only calls reusable workflows
            agent = load(d / "proofcheck-agent.yml")
            self.assertEqual(agent["jobs"]["comment"]["with"]["pr-number"], "${{ needs.agent.outputs.pr-number }}")
            self.assertEqual(agent["jobs"]["agent"]["secrets"], {"llm-api-key": "${{ secrets.PROOFCHECK_LLM_API_KEY }}"})

    def test_release_and_pages(self):
        rel = load(WF_DIR / "release.yml")
        self.assertEqual(rel["on"]["push"], {"tags": ["v*"]})
        self.assertEqual(set(rel["on"]["workflow_dispatch"]["inputs"]), {"version", "pypi"})
        # "Run workflow" creates the tag itself (only that job may write contents besides the release)
        self.assertEqual(rel["jobs"]["tag"]["if"], "github.event_name == 'workflow_dispatch'")
        self.assertEqual(rel["jobs"]["tag"]["permissions"], {"contents": "write"})
        for s in rel["jobs"]["build"]["steps"]:  # untrusted input reaches scripts via env only
            self.assertNotIn("${{", s.get("run", ""))
        pub = rel["jobs"]["publish"]
        self.assertEqual(pub["steps"][-1]["with"]["password"], "${{ secrets.PYPI_API_TOKEN }}")
        self.assertEqual(rel["jobs"]["publish"]["permissions"], {"id-token": "write"})
        self.assertEqual(rel["jobs"]["publish"]["environment"]["name"], "pypi")
        pages = load(WF_DIR / "pages.yml")
        self.assertEqual(pages["jobs"]["deploy"]["if"], "github.event.repository.private == false")
        self.assertIn("workflow_dispatch", pages["on"])

    def test_rust_cache_only_in_the_unprivileged_evidence_job(self):
        for path, wf in self.all_workflows():
            for name, job in (wf.get("jobs") or {}).items():
                for s in steps(job):
                    uses = str(s.get("uses", ""))
                    if "rust-cache" in uses or uses.startswith("actions/cache"):
                        self.assertEqual((path.name, name), ("evidence.yml", "evidence"), uses)
                        self.assertEqual(wf["jobs"][name]["if"], "github.event_name == 'pull_request'")
            on = wf["on"]
            if isinstance(on, dict) and "workflow_run" in on:
                self.assertNotIn("rust-cache", json.dumps(wf))
        for name in ("comment.yml", "agent.yml"):
            self.assertNotIn("rust-cache", (WF_DIR / name).read_text())

    def test_rust_setup_runs_after_the_token_step(self):
        job = load(WF_DIR / "evidence.yml")["jobs"]["evidence"]
        names = [s.get("name") or s.get("uses") for s in steps(job)]
        rust = next(n for n in names if n.startswith("Set up Rust"))
        cache = next(n for n in names if n.startswith("Cache Rust"))
        ctx = names.index("Collect PR context (before any PR code runs)")
        self.assertLess(ctx, names.index(rust))
        self.assertLess(names.index(rust), names.index(cache))
        self.assertLess(names.index(cache), names.index("Run proofcheck (runs PR code; no token)"))
        for s in steps(job):
            if s.get("name") in (rust, cache):
                self.assertEqual(s["if"], "hashFiles('**/Cargo.toml') != ''")
                self.assertNotIn("token", json.dumps(s))
        wf = load(WF_DIR / "evidence.yml")
        self.assertGreaterEqual(wf["on"]["workflow_call"]["inputs"]["timeout-minutes"]["default"], 60)
        install = next(s for s in steps(job) if s.get("name") == "Install the project under test (runs PR code)")
        self.assertIn('eval "$INSTALL_COMMAND"', install["run"])  # an empty install-command is a no-op

        jobs = load(WF_DIR / "agent.yml")["jobs"]
        vnames = [s.get("name") or s.get("uses") for s in steps(jobs["verify"])]
        vrust = next(n for n in vnames if n.startswith("Set up Rust"))
        self.assertLess(vnames.index(vrust), vnames.index("Run the candidate tests on base and head (runs PR code; no LLM)"))
        for job_name in ("gate", "propose"):  # never build or run the PR there
            self.assertNotIn("rustup", json.dumps(jobs[job_name]))
            self.assertNotIn("cargo", json.dumps(jobs[job_name]))
        # Rust candidates compile without a cache: the verify job's timeout is an input (default >= 60 minutes).
        agent_wf = load(WF_DIR / "agent.yml")
        self.assertGreaterEqual(agent_wf["on"]["workflow_call"]["inputs"]["timeout-minutes"]["default"], 60)
        self.assertEqual(jobs["verify"]["timeout-minutes"], "${{ inputs.timeout-minutes }}")

    def test_generated_callers_only_pass_declared_inputs(self):
        from proofcheck.init_cmd import agent_caller, evidence_caller

        declared = {name: set(load(WF_DIR / name)["on"]["workflow_call"]["inputs"]) for name in ("evidence.yml", "agent.yml")}
        for runners in (["pytest"], ["unittest"], ["cargo"], ["cargo", "pytest", "jest"]):
            for name, text in (("evidence.yml", evidence_caller("v1", runners[0], runners=runners)),
                               ("agent.yml", agent_caller("v1", runners[0], runners=runners))):
                wf = yaml.safe_load(text)
                job = next(j for j in wf["jobs"].values() if str(j.get("uses", "")).split("@")[0].endswith(name))
                with self.subTest(workflow=name, runners=runners):
                    self.assertLessEqual(set(job["with"]), declared[name])

    def test_action_yml_inputs_reach_scripts_via_env_only(self):
        action = load(_helpers.ROOT / "action.yml")
        self.assertEqual(action["branding"], {"icon": "check-circle", "color": "green"})
        for s in action["runs"]["steps"]:
            self.assertNotIn("${{", s.get("run", ""), s.get("name"))
        commands = {"context", "run", "comment", "summary", "agent-propose", "agent-verify"}
        conditions = {re.search(r"inputs.command == '([a-z-]+)'", s.get("if", "")).group(1)
                      for s in action["runs"]["steps"] if s.get("if")}
        self.assertEqual(conditions, commands)


if __name__ == "__main__":
    unittest.main()
