"""``proofcheck init``: set a repository up in one step.

Writes small *caller* workflows that delegate to the reusable workflows in
the proofcheck repository, plus a commented ``proofcheck.toml``:

* ``.github/workflows/proofcheck.yml``         -- evidence (``pull_request``, unprivileged)
* ``.github/workflows/proofcheck-comment.yml`` -- posts the card (``workflow_run``, never runs PR code)
* ``.github/workflows/proofcheck-agent.yml``   -- with ``--agent``: experimental agent evidence
* ``proofcheck.toml``

Existing files are never overwritten unless ``force`` is set; in that case
nothing is written at all, so a half-initialised repository cannot result.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__

TOOL_REPOSITORY = "bhaskargurram-ai/proofcheck"
DEFAULT_REF = f"v{__version__}"
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")

EVIDENCE_PATH = ".github/workflows/proofcheck.yml"
COMMENT_PATH = ".github/workflows/proofcheck-comment.yml"
AGENT_PATH = ".github/workflows/proofcheck-agent.yml"
CONFIG_PATH = "proofcheck.toml"

INSTALL_COMMANDS = {
    "pytest": 'pip install -e ".[dev]" pytest || pip install pytest',
    "unittest": 'pip install -e ".[dev]" || pip install -e . || true',
    "jest": "npm ci",
    "cargo": "",  # cargo builds the crates itself while testing
}

#: Directories never searched for subprojects.
SKIP_DIRS = frozenset({"examples", "example", "fixtures", "node_modules", ".venv", "venv", "target", "vendor",
                       "packaging", "docs", "doc", "build", "dist", "__pycache__"})
MAX_PROJECT_DEPTH = 3
_ROOT_NAMES = {"cargo": "rust", "pytest": "python", "unittest": "python", "jest": "js"}


class InitError(RuntimeError):
    """Raised when ``init`` cannot proceed (existing files, bad ref)."""


@dataclass
class DetectedProject:
    name: str
    path: str  # "." for the repository root
    runner: str
    reason: str


@dataclass
class InitResult:
    runner: str
    reason: str
    written: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    #: Test projects found (more than one: written as [[project]] tables).
    projects: list[DetectedProject] = field(default_factory=list)
    #: (path, reason) of manifests that were looked at but not configured.
    skipped: list[tuple[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------- detection

def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def detect_runner(repo: str | Path) -> tuple[str, str]:
    """Return ``(runner, reason)`` for the repository at ``repo``."""
    root = Path(repo)
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(_read(pkg) or "{}")
        except ValueError:
            data = {}
        if isinstance(data, dict):
            deps: dict = {}
            for key in ("dependencies", "devDependencies"):
                if isinstance(data.get(key), dict):
                    deps.update(data[key])
            scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
            if "jest" in deps or "jest" in str(scripts.get("test", "")) or "jest" in data:
                return "jest", "package.json uses jest"
    pyproject = _read(root / "pyproject.toml")
    if "[tool.pytest" in pyproject or re.search(r"""["']pytest\b""", pyproject):
        return "pytest", "pyproject.toml mentions pytest"
    if (root / "pytest.ini").is_file():
        return "pytest", "pytest.ini found"
    if "[tool:pytest]" in _read(root / "setup.cfg"):
        return "pytest", "setup.cfg has [tool:pytest]"
    if "[pytest]" in _read(root / "tox.ini"):
        return "pytest", "tox.ini has [pytest]"
    if (root / "conftest.py").is_file():
        return "pytest", "conftest.py found"
    for d in ("tests", "test"):
        if (root / d).is_dir() and any((root / d).rglob("test_*.py")):
            return "pytest", f"{d}/ directory found (pytest also runs unittest-style tests)"
    return "unittest", "no pytest or jest configuration found; using the stdlib unittest runner"


_FALLBACK_REASON = "no pytest or jest configuration found"


def _is_maturin(pyproject_text: str) -> bool:
    return bool(re.search(r"""build-backend\s*=\s*["']maturin""", pyproject_text))


def _package_json_test(path: Path) -> tuple[bool, str]:
    """``(has_test_script, runner_or_reason)`` for a package.json."""
    try:
        data = json.loads(_read(path) or "{}")
    except ValueError:
        return False, "package.json is not valid JSON"
    if not isinstance(data, dict):
        return False, "package.json is not an object"
    scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
    test = str(scripts.get("test", ""))
    if not test or "no test specified" in test:
        return False, "package.json has no test script"
    runner, _ = detect_runner(path.parent)
    if runner == "jest":
        return True, "jest"
    shown = test if len(test) <= 60 else test[:59] + "…"
    return True, f"its test script ({shown}) does not use jest, the only JavaScript runner proofcheck supports"


def detect_root_project(root: Path) -> tuple[str, str, bool]:
    """``(runner, reason, recognised)`` for the repository root."""
    cargo = root / "Cargo.toml"
    pyproject = _read(root / "pyproject.toml")
    if cargo.is_file() and not _is_maturin(pyproject):
        return "cargo", "Cargo.toml found", True
    runner, reason = detect_runner(root)
    recognised = not reason.startswith(_FALLBACK_REASON) or any(
        (root / f).is_file() for f in ("pyproject.toml", "setup.py", "setup.cfg"))
    return runner, reason, recognised


def _has_cargo_ancestor(root: Path, d: Path) -> bool:
    for parent in d.relative_to(root).parents:
        if (root / parent / "Cargo.toml").is_file():
            return True
    return False


def detect_projects(repo: str | Path) -> tuple[list[DetectedProject], list[tuple[str, str]]]:
    """Find test projects: the root, plus subfolders (up to depth 3) with a ``pyproject.toml``,
    ``setup.cfg``, ``package.json`` with a test script, or a ``Cargo.toml`` that is not inside
    another cargo project (workspace members are covered by their workspace).

    Returns ``(projects, skipped)``; the root project is listed first when it is recognised.
    """
    root = Path(repo)
    projects: list[DetectedProject] = []
    skipped: list[tuple[str, str]] = []
    runner, reason, recognised = detect_root_project(root)
    if recognised:
        projects.append(DetectedProject(_ROOT_NAMES.get(runner, "root"), ".", runner, reason))

    def walk(d: Path, depth: int) -> None:
        try:
            children = sorted(c for c in d.iterdir() if c.is_dir() and not c.is_symlink())
        except OSError:
            return
        for c in children:
            if c.name.startswith(".") or c.name in SKIP_DIRS:
                continue
            consider(c)
            if depth < MAX_PROJECT_DEPTH:
                walk(c, depth + 1)

    def consider(d: Path) -> None:
        rel = d.relative_to(root).as_posix()
        if (d / "Cargo.toml").is_file():
            if _has_cargo_ancestor(root, d):
                if (d / "pyproject.toml").is_file():
                    skipped.append((f"{rel}/pyproject.toml", "Python packaging of a Rust crate"))
                return  # a member (or helper) of an enclosing cargo workspace
            projects.append(DetectedProject("", rel, "cargo", "Cargo.toml found"))
            return
        for manifest in ("pyproject.toml", "setup.cfg"):
            if (d / manifest).is_file():
                runner, reason = detect_runner(d)
                if reason.startswith(_FALLBACK_REASON) and not (d / "tests").is_dir() and not (d / "test").is_dir():
                    skipped.append((f"{rel}/{manifest}", "no tests found"))
                    return
                projects.append(DetectedProject("", rel, runner, f"{manifest}: {reason}"))
                return
        if (d / "package.json").is_file():
            has_test, what = _package_json_test(d / "package.json")
            if has_test and what == "jest":
                projects.append(DetectedProject("", rel, "jest", "package.json uses jest"))
            elif has_test:
                skipped.append((f"{rel}/package.json", what))

    walk(root, 1)
    _name_projects(projects)
    return projects, skipped


def _name_projects(projects: list[DetectedProject]) -> None:
    used = {p.name for p in projects if p.name}
    for p in projects:
        if p.name:
            continue
        candidates = [re.sub(r"[^A-Za-z0-9_.-]", "-", p.path.rsplit("/", 1)[-1]).strip("-.") or "project",
                      re.sub(r"[^A-Za-z0-9_.-]", "-", p.path).strip("-.") or "project"]
        name = next((c[:40] for c in candidates if c[:40] not in used and c[:40] != "root"), None)
        k = 2
        while name is None:
            cand = f"{candidates[0][:36]}-{k}"
            if cand not in used:
                name = cand
            k += 1
        if not re.match(r"^[A-Za-z0-9]", name):
            name = "p" + name[:39]
        p.name = name
        used.add(name)


def install_command(projects: list[DetectedProject]) -> str:
    """One shell command installing every project's test dependencies (runs PR code in CI)."""
    parts = []
    for p in projects:
        if p.runner == "cargo":
            continue
        if p.path == ".":
            parts.append(INSTALL_COMMANDS[p.runner])
        elif p.runner == "pytest":
            parts.append(f'pip install -e "./{p.path}[dev]" pytest || pip install pytest')
        elif p.runner == "unittest":
            parts.append(f'pip install -e "./{p.path}[dev]" || pip install -e "./{p.path}" || true')
        elif p.runner == "jest":
            parts.append(f'npm ci --prefix "{p.path}"')
    return "; ".join(parts)


# --------------------------------------------------------------------------- templates

def _yaml_str(s: str) -> str:
    return f"'{s}'" if "'" not in s else json.dumps(s)


def _extra_inputs(runners: list[str]) -> str:
    out = ""
    if "jest" in runners:
        out += '      node-version: "20"\n'
    if "cargo" in runners:
        out += "      timeout-minutes: 90   # Rust: compiling the workspace for base, head and ablation takes a while\n"
    return out


def evidence_caller(ref: str, runner: str = "pytest", install: str | None = None, runners: list[str] | None = None) -> str:
    prefix = f"{TOOL_REPOSITORY}/.github/workflows"
    node = _extra_inputs(runners or [runner])
    install = INSTALL_COMMANDS.get(runner, INSTALL_COMMANDS["pytest"]) if install is None else install
    return f"""# proofcheck, part 1 of 2: gather evidence (UNPRIVILEGED).
#
# Runs on `pull_request`, so for pull requests from forks GitHub gives this
# workflow a read-only token and no secrets. Untrusted PR code (tests, issue
# reproducers) only ever runs here. The proof card goes to the job summary and
# into the `proofcheck-result` artifact; proofcheck-comment.yml posts it.
#
# Generated by `proofcheck init`. Pin the reusable workflow to a release tag
# or, stricter, to a full commit SHA, and keep `proofcheck-ref` the same.
name: proofcheck

on:
  pull_request:
    types: [opened, synchronize, reopened, edited]

permissions:
  contents: read
  issues: read   # read linked issues on private repositories (read-only)

concurrency:
  group: proofcheck-${{{{ github.event.pull_request.number }}}}
  cancel-in-progress: true

jobs:
  evidence:
    uses: {prefix}/evidence.yml@{ref}
    with:
      proofcheck-ref: {ref}
      python-version: "3.12"
{node}      # Installs the project under test; this runs PR code (unprivileged job).
      install-command: {_yaml_str(install)}
      fail-on: never   # or "contradicted" / "no-evidence" to fail the check
"""


def comment_caller(ref: str) -> str:
    prefix = f"{TOOL_REPOSITORY}/.github/workflows"
    return f"""# proofcheck, part 2 of 2: post the proof card (PRIVILEGED, never runs PR code).
#
# Triggered when the `proofcheck` workflow completes. `workflow_run` runs with
# a token that can comment, so the reusable workflow never checks out,
# installs or executes anything from the pull request: it downloads the result
# artifact, validates it, re-renders the card, upserts one comment and sets a
# `proofcheck: <verdict>` label. Do not use `pull_request_target` instead.
#
# Generated by `proofcheck init`.
name: proofcheck comment

on:
  workflow_run:
    workflows: ["proofcheck"]   # must match the `name:` of proofcheck.yml
    types: [completed]

permissions: {{}}

jobs:
  comment:
    if: github.event.workflow_run.event == 'pull_request' && github.event.workflow_run.conclusion != 'cancelled'
    permissions:
      contents: read
      actions: read          # download the artifact of the triggering run
      pull-requests: write   # upsert the comment and the verdict label
    uses: {prefix}/comment.yml@{ref}
    with:
      proofcheck-ref: {ref}
      labels: true
"""


def agent_caller(ref: str, runner: str = "pytest", install: str | None = None, runners: list[str] | None = None) -> str:
    prefix = f"{TOOL_REPOSITORY}/.github/workflows"
    node = '      node-version: "20"\n' if "jest" in (runners or [runner]) else ""
    install = INSTALL_COMMANDS.get(runner, INSTALL_COMMANDS["pytest"]) if install is None else install
    return f"""# proofcheck agent evidence (EXPERIMENTAL, opt-in).
#
# When the evidence run finds no proof for a "fixes #N" claim, an LLM agent
# tries to write a reproduction test, and proofcheck runs it on base and head.
# The work is split over separate jobs (separate VMs):
#   gate    - decides whether to run; reads the API only, runs no PR code
#   propose - holds the LLM key; reads the PR as data, never installs or runs it
#   verify  - runs the candidate tests on base and head; no secrets, read-only token
#   comment - re-posts the card with the agent section; never runs PR code
#
# Setup: repository secret PROOFCHECK_LLM_API_KEY, repository variables
# PROOFCHECK_LLM_PROVIDER (anthropic | openai) and PROOFCHECK_LLM_MODEL
# (optional: PROOFCHECK_LLM_BASE_URL). By default the agent only runs for pull
# requests by owners, members and collaborators, or labelled `proofcheck:agent`.
#
# Generated by `proofcheck init --agent`.
name: proofcheck agent

on:
  workflow_run:
    workflows: ["proofcheck"]   # must match the `name:` of proofcheck.yml
    types: [completed]

permissions: {{}}

jobs:
  agent:
    if: github.event.workflow_run.event == 'pull_request' && github.event.workflow_run.conclusion != 'cancelled'
    permissions:
      contents: read
      actions: read
      pull-requests: read
      issues: read
    uses: {prefix}/agent.yml@{ref}
    with:
      proofcheck-ref: {ref}
      llm-provider: ${{{{ vars.PROOFCHECK_LLM_PROVIDER }}}}
      llm-model: ${{{{ vars.PROOFCHECK_LLM_MODEL }}}}
      llm-base-url: ${{{{ vars.PROOFCHECK_LLM_BASE_URL }}}}
      python-version: "3.12"
{node}      install-command: {_yaml_str(install)}
      run-for: trusted   # or "everyone" (spends API credits on any fork PR)
    secrets:
      llm-api-key: ${{{{ secrets.PROOFCHECK_LLM_API_KEY }}}}

  comment:
    needs: agent
    if: needs.agent.outputs.verified == 'true'
    permissions:
      contents: read
      actions: read
      pull-requests: write
    uses: {prefix}/comment.yml@{ref}
    with:
      proofcheck-ref: {ref}
      # Trusted values: the PR number comes from the gate job (API data, no PR
      # code ran there) and the artifact from this run's verify job.
      pr-number: ${{{{ needs.agent.outputs.pr-number }}}}
      artifact-run-id: ${{{{ github.run_id }}}}
      labels: true
"""


def _toml_str(s: str) -> str:
    return json.dumps(s)


def projects_toml(projects: list[DetectedProject]) -> str:
    out = ["", "# Test projects (detected by `proofcheck init`). Each changed file belongs to the deepest",
           "# project containing it; tests run inside the project's directory. Per-project keys:",
           "# runner, test_command, setup_command, test_paths, timeout, test_env, cargo_target_dir."]
    for p in projects:
        out += ["", "[[project]]", f"name = {_toml_str(p.name)}", f"path = {_toml_str(p.path)}", f"runner = {_toml_str(p.runner)}"]
        if p.runner == "cargo":
            out.append("# timeout = 1800                   # seconds per run (cargo default: 30 minutes)")
    return "\n".join(out) + "\n"


def config_template(runner: str, agent: bool = False, projects: list[DetectedProject] | None = None) -> str:
    text = f"""# proofcheck configuration (https://github.com/{TOOL_REPOSITORY}).
#
# Read from the pull request's BASE commit, so a PR cannot weaken its own
# check. Every key is optional; the commented values are the defaults.

runner = "{runner}"                 # pytest | unittest | jest | cargo
# test_command = ""                 # custom command; placeholders {{junitxml}} and {{tests}}
# setup_command = ""                # run once in each worktree before the tests
# timeout = 600                     # seconds per test run
# test_env = {{ PYTHONHASHSEED = "0" }}
# benchmark_command = ""            # e.g. "python bench.py": lets "2x faster" claims be checked
# benchmark_runs = 5
# large_diff_threshold = 1000
# max_issues = 5

# [repro]                           # replay ```python and >>> reproducers from linked issues
# enabled = true
# timeout = 60

[ablation]                          # diff evidence map: revert each changed hunk, re-run the evidence
enabled = true
max_hunks = 12
time_budget_s = 300
# exclude = ["*.md", "*.rst", "*.txt", "docs/*", "LICENSE*", "CHANGELOG*", ".github/*"]

# [llm]                             # optional LLM claim extraction (off by default)
# enabled = false
# provider = "anthropic"            # or "openai" for any OpenAI-compatible endpoint
# model = ""                        # the key comes from PROOFCHECK_LLM_API_KEY, never from this file

# [agent]                           # experimental agent evidence (proofcheck-agent.yml)
# enabled = false
# variants = 3
# max_rounds = 2
# max_tool_calls = 25
# when = "missing-evidence"         # or "always"
# test_dir = "tests/proofcheck_agent"
"""
    if agent:
        # proofcheck-agent.yml only proposes tests when [agent] is enabled in the
        # base commit's config, so `init --agent` must switch it on.
        text = text.replace("# [agent]  ", "[agent]    ", 1).replace("# enabled = false\n# variants", "enabled = true\n# variants", 1)
    if runner == "cargo" or any(p.runner == "cargo" for p in projects or []):
        # Every ablated hunk rebuilds the changed crate: give the map more time.
        text = text.replace("time_budget_s = 300\n", "time_budget_s = 1200                # Rust: each hunk rebuilds the crate\n", 1)
    if projects and len(projects) > 1:
        if any(p.path == "." for p in projects):
            # The root project is one of the [[project]] tables; a top-level runner would clash with it.
            text = text.replace(f'runner = "{runner}"                 #', f'# runner = "{runner}"               #', 1)
        text += projects_toml(projects)
    return text


# --------------------------------------------------------------------------- init

def init_repo(repo: str | Path = ".", agent: bool = False, force: bool = False, ref: str | None = None,
              runner: str | None = None) -> InitResult:
    """Write the caller workflows and config into ``repo``."""
    root = Path(repo)
    if not root.is_dir():
        raise InitError(f"not a directory: {root}")
    ref = ref or DEFAULT_REF
    if not _REF_RE.match(ref) or ".." in ref:
        raise InitError(f"invalid --ref {ref!r}: use a tag, branch or commit SHA")
    projects: list[DetectedProject] = []
    skipped: list[tuple[str, str]] = []
    if runner:
        detected, reason = runner, "given with --runner"
    else:
        projects, skipped = detect_projects(root)
        root_runner, reason, _ = detect_root_project(root)
        root_proj = next((p for p in projects if p.path == "."), None)
        detected = root_proj.runner if root_proj else root_runner
        reason = root_proj.reason if root_proj else reason
    multi = len(projects) > 1
    runners = sorted({p.runner for p in projects}) if multi else [detected]
    install = install_command(projects) if multi else None
    files = {
        EVIDENCE_PATH: evidence_caller(ref, detected, install, runners),
        COMMENT_PATH: comment_caller(ref),
        CONFIG_PATH: config_template(detected, agent=agent, projects=projects if multi else None),
    }
    if agent:
        files[AGENT_PATH] = agent_caller(ref, detected, install, runners)
    existing = [p for p in files if (root / p).exists()]
    if existing and not force:
        raise InitError("refusing to overwrite existing file(s): " + ", ".join(existing) + " (use --force)")
    for rel, text in files.items():
        target = root / rel
        if target.is_symlink():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    res = InitResult(runner=detected, reason=reason, written=list(files), projects=projects if multi else [], skipped=skipped)
    res.next_steps.append("Review and commit the files: git add " + " ".join(files) + " && git commit -m 'Add proofcheck'")
    res.next_steps.append("Merge them to the default branch: workflow_run workflows (the comment and agent "
                          "workflows) only run from the default branch.")
    if "jest" in runners:
        res.next_steps.append("Jest projects need the JUnit reporter: npm install --save-dev jest-junit")
    if "cargo" in runners:
        res.next_steps.append("Rust: the workflow sets up the toolchain from rust-toolchain.toml and caches the target "
                              "directory; integration tests (crates/*/tests/*.rs) and changed #[cfg(test)] modules are checked.")
    if agent:
        res.next_steps.append("Add the LLM key as a repository secret: gh secret set PROOFCHECK_LLM_API_KEY")
        res.next_steps.append("Set repository variables: gh variable set PROOFCHECK_LLM_PROVIDER --body anthropic "
                              "(or openai) and gh variable set PROOFCHECK_LLM_MODEL --body <model id> "
                              "(optional: PROOFCHECK_LLM_BASE_URL)")
        res.next_steps.append("The agent runs for PRs by owners/members/collaborators, or when a maintainer adds the "
                              "`proofcheck:agent` label; set run-for: everyone to change that.")
    res.next_steps.append("Open a pull request: the card appears in the job summary and as a PR comment.")
    return res
