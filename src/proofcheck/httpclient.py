"""A minimal JSON-over-HTTPS transport built on :mod:`urllib` (no third-party deps).

The transport is a plain callable so tests can substitute a fake::

    transport(method, url, headers, body_bytes_or_None, timeout) -> HTTPResponse
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

#: Upper bound on response bodies we read (protects against huge/hostile responses).
MAX_RESPONSE_BYTES = 10_000_000


@dataclass
class HTTPResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None


Transport = Callable[[str, str, Mapping[str, str], Optional[bytes], float], HTTPResponse]


def urllib_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float) -> HTTPResponse:
    """Default transport. Only ``https://`` URLs (and ``http://localhost`` for testing) are allowed."""
    if not (url.startswith("https://") or url.startswith("http://localhost") or url.startswith("http://127.0.0.1")):
        raise ValueError(f"refusing non-HTTPS URL: {url}")
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - scheme checked above
            data = resp.read(MAX_RESPONSE_BYTES + 1)
            return HTTPResponse(resp.status, data[:MAX_RESPONSE_BYTES], {k.lower(): v for k, v in resp.headers.items()})
    except urllib.error.HTTPError as exc:
        data = exc.read(MAX_RESPONSE_BYTES) if exc.fp else b""
        return HTTPResponse(exc.code, data, {k.lower(): v for k, v in (exc.headers or {}).items()})
