"""GitHub REST API helpers: fetch PR/issue text and upsert the proof card comment.

Only two kinds of calls are made:

* reads (PR title/body, issue bodies) -- need ``contents: read`` /
  ``issues: read`` or nothing at all for public repositories;
* the comment upsert -- needs ``pull-requests: write`` (or ``issues: write``)
  and runs in the privileged ``workflow_run`` workflow, which never executes
  PR code.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .card import CARD_MARKER
from .httpclient import HTTPResponse, Transport, urllib_transport

API_URL = "https://api.github.com"
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
#: Hard cap on pages walked when searching for an existing comment.
MAX_COMMENT_PAGES = 20


class GitHubError(RuntimeError):
    """Raised for unexpected GitHub API responses."""


def validate_slug(slug: str) -> str:
    """Validate an ``owner/repo`` string."""
    if not slug or not _SLUG_RE.match(slug) or ".." in slug:
        raise GitHubError(f"invalid repository slug: {slug!r}")
    return slug


def validate_number(value: Any) -> int:
    """Validate a PR/issue number coming from an untrusted source (artifact, event, CLI)."""
    if isinstance(value, bool):
        raise GitHubError("PR number must be an integer")
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and re.fullmatch(r"\s*[0-9]{1,10}\s*", value):
        n = int(value)
    else:
        raise GitHubError(f"PR number must be a positive integer, got {value!r}")
    if n <= 0:
        raise GitHubError("PR number must be a positive integer")
    return n


@dataclass
class GitHubClient:
    """Tiny GitHub REST client."""

    token: str | None
    repo: str
    api_url: str = API_URL
    transport: Transport = urllib_transport
    timeout: float = 30

    def __post_init__(self) -> None:
        validate_slug(self.repo)
        self.api_url = self.api_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "proofcheck",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def request(self, method: str, path: str, payload: Any = None) -> HTTPResponse:
        body = None
        headers = self._headers()
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return self.transport(method, f"{self.api_url}{path}", headers, body, self.timeout)

    def _json(self, method: str, path: str, payload: Any = None, ok: tuple[int, ...] = (200,)) -> Any:
        resp = self.request(method, path, payload)
        if resp.status not in ok:
            raise GitHubError(f"{method} {path} -> HTTP {resp.status}: {resp.body[:300].decode('utf-8', 'replace')}")
        return resp.json()

    # -- reads -----------------------------------------------------------------------
    def get_pull(self, number: int) -> dict[str, Any]:
        return self._json("GET", f"/repos/{self.repo}/pulls/{validate_number(number)}")

    def get_issue(self, number: int) -> dict[str, Any] | None:
        """Return an issue, or ``None`` if it does not exist / is not accessible."""
        path = f"/repos/{self.repo}/issues/{validate_number(number)}"
        resp = self.request("GET", path)
        if resp.status in (401, 403, 404) and self.token:
            # A token scoped to `contents: read` may not be allowed to read
            # issues; public issues are still readable anonymously.
            resp = self.transport("GET", f"{self.api_url}{path}", {k: v for k, v in self._headers().items() if k != "Authorization"},
                                  None, self.timeout)
        if resp.status != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None

    def list_comments(self, number: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        n = validate_number(number)
        for page in range(1, MAX_COMMENT_PAGES + 1):
            batch = self._json("GET", f"/repos/{self.repo}/issues/{n}/comments?per_page=100&page={page}")
            if not isinstance(batch, list):
                raise GitHubError("unexpected comments payload")
            out.extend(batch)
            if len(batch) < 100:
                break
        return out

    def list_pull_files(self, number: int, max_pages: int = 30) -> list[str]:
        """Return the paths changed by a PR, as reported by GitHub (trusted data)."""
        n = validate_number(number)
        out: list[str] = []
        for page in range(1, max_pages + 1):
            batch = self._json("GET", f"/repos/{self.repo}/pulls/{n}/files?per_page=100&page={page}")
            if not isinstance(batch, list):
                raise GitHubError("unexpected files payload")
            for f in batch:
                if isinstance(f, dict):
                    for key in ("filename", "previous_filename"):
                        if isinstance(f.get(key), str):
                            out.append(f[key])
            if len(batch) < 100:
                break
        return out

    # -- writes ----------------------------------------------------------------------
    def upsert_card_comment(self, number: int, body: str, bot_login: str | None = None) -> tuple[str, int]:
        """Create the proof card comment or update the existing one.

        An existing comment is only reused if it contains the hidden marker
        **and** was written by a bot (or by ``bot_login`` when given), so a
        human cannot plant the marker to hijack the card.

        Returns ``("created"|"updated", comment_id)``.
        """
        n = validate_number(number)
        if CARD_MARKER not in body:
            raise GitHubError("refusing to post a card without the proofcheck marker")
        existing = find_card_comment(self.list_comments(n), bot_login)
        if existing is not None:
            cid = int(existing["id"])
            self._json("PATCH", f"/repos/{self.repo}/issues/comments/{cid}", {"body": body})
            return "updated", cid
        data = self._json("POST", f"/repos/{self.repo}/issues/{n}/comments", {"body": body}, ok=(200, 201))
        return "created", int(data["id"])

    # -- labels ----------------------------------------------------------------------
    def list_issue_labels(self, number: int) -> list[str]:
        """Names of the labels currently on an issue / PR."""
        data = self._json("GET", f"/repos/{self.repo}/issues/{validate_number(number)}/labels?per_page=100")
        if not isinstance(data, list):
            raise GitHubError("unexpected labels payload")
        return [str(x["name"]) for x in data if isinstance(x, dict) and isinstance(x.get("name"), str)]

    def ensure_label(self, name: str, color: str, description: str = "") -> bool:
        """Create the repository label ``name`` if it does not exist. Returns True if created."""
        resp = self.request("GET", f"/repos/{self.repo}/labels/{_q(name)}")
        if resp.status == 200:
            return False
        if resp.status != 404:
            raise GitHubError(f"GET label {name!r} -> HTTP {resp.status}")
        resp = self.request("POST", f"/repos/{self.repo}/labels", {"name": name, "color": color, "description": description})
        if resp.status == 201:
            return True
        if resp.status == 422:  # created concurrently by another run
            return False
        raise GitHubError(f"POST label {name!r} -> HTTP {resp.status}: {resp.body[:200].decode('utf-8', 'replace')}")

    def add_label(self, number: int, name: str) -> None:
        self._json("POST", f"/repos/{self.repo}/issues/{validate_number(number)}/labels", {"labels": [name]})

    def remove_label(self, number: int, name: str) -> None:
        resp = self.request("DELETE", f"/repos/{self.repo}/issues/{validate_number(number)}/labels/{_q(name)}")
        if resp.status not in (200, 204, 404):
            raise GitHubError(f"DELETE label {name!r} -> HTTP {resp.status}")

    def sync_verdict_label(self, number: int, verdict: str) -> tuple[list[str], list[str]]:
        """Make the PR carry exactly one ``proofcheck: <verdict>`` label.

        Idempotent: labels are only added or removed when needed, the label is
        created with its colour if the repository lacks it, and only the
        verdict labels below are ever removed (``proofcheck:agent`` and other
        labels are left alone). Returns ``(added, removed)``.
        """
        n = validate_number(number)
        wanted = VERDICT_LABELS.get(verdict)
        current = self.list_issue_labels(n)
        removed = [lbl for lbl in current if lbl in VERDICT_LABEL_NAMES and lbl != wanted]
        for lbl in removed:
            self.remove_label(n, lbl)
        added: list[str] = []
        if wanted and wanted not in current:
            color, desc = LABEL_STYLES[wanted]
            self.ensure_label(wanted, color, desc)
            self.add_label(n, wanted)
            added.append(wanted)
        return added, removed


#: Verdict -> label. ``error`` maps to no label (stale verdict labels are removed).
VERDICT_LABELS = {
    "evidence": "proofcheck: verified",
    "partial": "proofcheck: partial",
    "contradicted": "proofcheck: contradicted",
    "no_evidence": "proofcheck: no evidence",
}
VERDICT_LABEL_NAMES = frozenset(VERDICT_LABELS.values())
LABEL_STYLES = {
    "proofcheck: verified": ("1a7f37", "proofcheck: the PR's claims are backed by executed evidence"),
    "proofcheck: partial": ("d4a72c", "proofcheck: some claims are backed by evidence, others are not"),
    "proofcheck: contradicted": ("cf222e", "proofcheck: executed evidence contradicts the PR"),
    "proofcheck: no evidence": ("8c959f", "proofcheck: no evidence found for the PR's claims (not the same as wrong)"),
}


def _q(name: str) -> str:
    from urllib.parse import quote

    return quote(name, safe="")


#: Files that, when changed by a PR, let the PR influence how its own proof card is produced.
HARNESS_PATTERNS = (".github/workflows/*", ".github/actions/*", "action.yml", "action.yaml", "proofcheck.toml",
                    "pyproject.toml", "conftest.py", "*/conftest.py", "pytest.ini", "tox.ini", "setup.cfg",
                    "package.json", "jest.config.*")


def harness_changes(paths: list[str]) -> list[str]:
    """Return the changed paths that can alter the CI job or test harness itself."""
    import fnmatch

    hits = []
    for p in paths:
        name = p.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatchcase(p, pat) or fnmatch.fnmatchcase(name, pat) for pat in HARNESS_PATTERNS):
            hits.append(p)
    return sorted(set(hits))


def find_card_comment(comments: list[dict[str, Any]], bot_login: str | None = None) -> dict[str, Any] | None:
    """Return the most recent comment that is a proofcheck card written by the bot."""
    for c in reversed(comments):
        body = c.get("body") or ""
        if not isinstance(body, str) or not body.lstrip().startswith(CARD_MARKER):
            continue
        user = c.get("user") or {}
        login = str(user.get("login", ""))
        if bot_login:
            if login.lower() != bot_login.lower():
                continue
        elif user.get("type") != "Bot":
            continue
        return c
    return None


def pr_number_from_event(event_path: str | None = None) -> int | None:
    """Read the PR number from ``$GITHUB_EVENT_PATH`` (pull_request events)."""
    path = event_path or os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            event = json.load(fh)
    except (OSError, ValueError):
        return None
    for candidate in (event.get("pull_request", {}).get("number"), event.get("number"), event.get("issue", {}).get("number")):
        if candidate is not None:
            try:
                return validate_number(candidate)
            except GitHubError:
                return None
    return None


def env_client(env: Mapping[str, str] | None = None, transport: Transport | None = None) -> GitHubClient:
    """Build a client from ``GITHUB_TOKEN``, ``GITHUB_REPOSITORY`` and ``GITHUB_API_URL``."""
    e = os.environ if env is None else env
    repo = e.get("GITHUB_REPOSITORY", "")
    return GitHubClient(
        token=e.get("GITHUB_TOKEN") or None,
        repo=repo,
        api_url=e.get("GITHUB_API_URL", API_URL),
        transport=transport or urllib_transport,
    )
