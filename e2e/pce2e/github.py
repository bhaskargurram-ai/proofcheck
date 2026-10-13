"""A small GitHub REST client for the harness.

It talks to any ``requests.Session``-like object (``session.request(method,
url, headers=..., json=..., params=..., timeout=...)`` returning an object
with ``status_code``, ``headers``, ``json()`` and ``text``), so the unit
tests use a fake session and never touch the network. The token is only
ever placed in the ``Authorization`` header of a request; error messages
are redacted.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable
from urllib.parse import quote

from proofcheck.card import CARD_MARKER
from proofcheck.carddata import parse_card_data

API_URL = "https://api.github.com"
_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_PAGES = 30


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def default_session() -> Any:
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise GitHubError("the e2e harness needs `requests` (pip install requests)") from exc
    return requests.Session()


class GitHub:
    def __init__(self, repo: str, token: str, session: Any = None, api_url: str = API_URL,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 redact: Callable[[object], str] = str, max_retries: int = 4, timeout: float = 30) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
            raise GitHubError(f"invalid repository {repo!r}; expected OWNER/NAME")
        self.repo = repo
        self._token = token
        self.session = session if session is not None else default_session()
        self.api_url = api_url.rstrip("/")
        self.sleep = sleep
        self.clock = clock
        self.redact = redact
        self.max_retries = max_retries
        self.timeout = timeout

    # -- transport -------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "proofcheck-e2e"}

    def _retry_delay(self, resp: Any, attempt: int) -> float | None:
        """Seconds to wait before retrying ``resp``, or None if it is not retryable."""
        status = resp.status_code
        headers = {k.lower(): v for k, v in (resp.headers or {}).items()}
        rate_limited = status == 429 or (status == 403 and (headers.get("x-ratelimit-remaining") == "0"
                                                            or "retry-after" in headers))
        if status not in RETRY_STATUSES and not rate_limited:
            return None
        if "retry-after" in headers:
            try:
                return min(float(headers["retry-after"]), 300.0)
            except ValueError:
                pass
        if rate_limited and headers.get("x-ratelimit-reset", "").isdigit():
            return max(1.0, min(float(headers["x-ratelimit-reset"]) - self.clock() + 1, 900.0))
        return min(2.0 * (2 ** attempt), 60.0)

    def request(self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None,
                ok: tuple[int, ...] = (200, 201, 204), url: str | None = None) -> tuple[Any, Any]:
        """Send a request; returns ``(parsed_json_or_None, response)``. Retries transient failures."""
        full = url or f"{self.api_url}{path}"
        last_err = ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.request(method, full, headers=self._headers(), json=json, params=params,
                                            timeout=self.timeout)
            except Exception as exc:  # network errors: requests.ConnectionError, timeouts, ...
                last_err = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_retries:
                    self.sleep(min(2.0 * (2 ** attempt), 60.0))
                    continue
                raise GitHubError(self.redact(f"{method} {path}: {last_err}")) from None
            if resp.status_code in ok:
                if resp.status_code == 204 or not (resp.text or "").strip():
                    return None, resp
                try:
                    return resp.json(), resp
                except ValueError:
                    raise GitHubError(self.redact(f"{method} {path}: response is not JSON"), resp.status_code) from None
            delay = self._retry_delay(resp, attempt)
            if delay is not None and attempt < self.max_retries:
                self.sleep(delay)
                continue
            msg = ""
            try:
                body = resp.json()
                msg = str(body.get("message", "")) if isinstance(body, dict) else ""
                if isinstance(body, dict) and body.get("errors"):
                    msg += f" {body['errors']}"
            except ValueError:
                msg = (resp.text or "")[:200]
            raise GitHubError(self.redact(f"{method} {path}: HTTP {resp.status_code} {msg[:400]}"), resp.status_code)
        raise GitHubError(self.redact(f"{method} {path}: {last_err or 'retries exhausted'}"))  # pragma: no cover

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        items: list[Any] = []
        url: str | None = None
        p = dict(params or {}, per_page=100)
        for _ in range(MAX_PAGES):
            data, resp = self.request("GET", path, params=None if url else p, url=url)
            if isinstance(data, list):
                items.extend(data)
            elif isinstance(data, dict):  # e.g. {"total_count": n, "workflow_runs": [...]}
                for v in data.values():
                    if isinstance(v, list):
                        items.extend(v)
            m = _NEXT_RE.search((resp.headers or {}).get("Link", "") or (resp.headers or {}).get("link", ""))
            if not m:
                break
            url = m.group(1)
            if not url.startswith(self.api_url + "/"):
                raise GitHubError(f"refusing to follow pagination link to another host: {url[:80]}")
        return items

    # -- endpoints ---------------------------------------------------------------------
    @property
    def _r(self) -> str:
        return f"/repos/{self.repo}"

    def get_repo(self) -> dict[str, Any]:
        return self.request("GET", self._r)[0]

    def create_issue(self, title: str, body: str) -> dict[str, Any]:
        return self.request("POST", f"{self._r}/issues", json={"title": title, "body": body})[0]

    def close_issue(self, number: int) -> None:
        self.request("PATCH", f"{self._r}/issues/{int(number)}", json={"state": "closed", "state_reason": "not_planned"})

    def create_pull(self, title: str, body: str, head: str, base: str, draft: bool = False) -> dict[str, Any]:
        return self.request("POST", f"{self._r}/pulls",
                            json={"title": title, "body": body, "head": head, "base": base, "draft": draft})[0]

    def get_pull(self, number: int) -> dict[str, Any]:
        return self.request("GET", f"{self._r}/pulls/{int(number)}")[0]

    def close_pull(self, number: int) -> None:
        self.request("PATCH", f"{self._r}/pulls/{int(number)}", json={"state": "closed"})

    def add_labels(self, number: int, labels: list[str]) -> None:
        self.request("POST", f"{self._r}/issues/{int(number)}/labels", json={"labels": labels})

    def issue_labels(self, number: int) -> list[str]:
        return [str(x.get("name", "")) for x in self.paginate(f"{self._r}/issues/{int(number)}/labels")]

    def issue_comments(self, number: int) -> list[dict[str, Any]]:
        return self.paginate(f"{self._r}/issues/{int(number)}/comments")

    def delete_branch(self, branch: str) -> bool:
        """Delete ``refs/heads/<branch>``; False when it was already gone."""
        try:
            self.request("DELETE", f"{self._r}/git/refs/heads/{quote(branch, safe='/')}")
        except GitHubError as exc:
            if exc.status in (404, 422):
                return False
            raise
        return True

    def runs_for_sha(self, sha: str) -> list[dict[str, Any]]:
        """Workflow runs for a commit (for diagnostics when no card arrives)."""
        data, _ = self.request("GET", f"{self._r}/actions/runs", params={"head_sha": sha, "per_page": 50})
        runs = (data or {}).get("workflow_runs") or []
        return [{"name": r.get("name"), "event": r.get("event"), "status": r.get("status"),
                 "conclusion": r.get("conclusion"), "url": r.get("html_url")} for r in runs if isinstance(r, dict)]


def find_card(comments: list[dict[str, Any]], head_sha: str | None = None,
              bot_login: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The newest proofcheck card comment by a bot (or ``bot_login``) and its parsed data block.

    With ``head_sha``, only a card whose data block names that head counts.
    """
    for c in reversed(comments):
        body = c.get("body")
        if not isinstance(body, str) or not body.lstrip().startswith(CARD_MARKER):
            continue
        user = c.get("user") or {}
        if bot_login:
            if str(user.get("login", "")).lower() != bot_login.lower():
                continue
        elif user.get("type") != "Bot":
            continue
        data = parse_card_data(body)
        if data is None:
            continue
        if head_sha and data.get("head") != head_sha:
            continue
        return c, data
    return None, None
