"""A small tool-use chat client for two wire formats.

* **Anthropic Messages API** (``provider = "anthropic"``): ``tools`` with
  ``input_schema``, ``tool_use`` blocks in assistant turns and
  ``tool_result`` blocks in the following user turn.
* **OpenAI-compatible chat completions** (``provider = "openai"``):
  ``tools`` of ``type: function``, ``tool_calls`` on assistant messages and
  ``role: tool`` result messages. Works with any server that implements this
  format (``PROOFCHECK_LLM_BASE_URL``).

The conversation is kept in a provider-neutral form (:class:`Turn`) and
converted on every request. HTTP goes through :mod:`proofcheck.httpclient`
(stdlib only), so tests can substitute a scripted fake transport.

Configuration comes from the environment: ``PROOFCHECK_LLM_PROVIDER``,
``PROOFCHECK_LLM_API_KEY``, ``PROOFCHECK_LLM_MODEL`` (**required**: there is
no built-in default model id) and optionally ``PROOFCHECK_LLM_BASE_URL``.
The ``[llm]`` table of the config may provide provider/model/base_url as a
fallback; the key is only ever read from the environment.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..httpclient import HTTPResponse, Transport, urllib_transport

PROVIDERS = ("anthropic", "openai")
ANTHROPIC_VERSION = "2023-06-01"
#: HTTP statuses worth retrying (rate limits, overload, transient server errors).
RETRY_STATUSES = (408, 409, 425, 429, 500, 502, 503, 504, 529)
MAX_RETRY_AFTER = 60.0


class LLMToolError(RuntimeError):
    """The LLM endpoint could not be used (configuration, HTTP or response shape)."""


class LLMConfigError(LLMToolError):
    """The LLM is not configured (missing provider/model/key)."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    #: Set when the model sent arguments that are not a JSON object.
    parse_error: str = ""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.requests += other.requests

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    def cost(self, price_in: float | None, price_out: float | None) -> float | None:
        """USD cost, or ``None`` when no prices are configured.

        Cached input tokens are charged at the full input price, so the
        figure is an upper bound when the provider discounts cache reads.
        """
        if price_in is None or price_out is None:
            return None
        tokens_in = self.input_tokens + self.cache_read_tokens + self.cache_write_tokens
        return round(tokens_in * price_in / 1e6 + self.output_tokens * price_out / 1e6, 6)

    def to_dict(self) -> dict[str, int]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens, "cache_write_tokens": self.cache_write_tokens,
                "requests": self.requests}


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    usage: Usage


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class Turn:
    """One provider-neutral conversation entry.

    ``role`` is ``user`` (``text``), ``assistant`` (``text`` + ``tool_calls``)
    or ``tool`` (``results``, answering the previous assistant turn).
    """

    role: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    results: list[ToolResult] = field(default_factory=list)


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


# --------------------------------------------------------------------------- wire formats


def _anthropic_messages(turns: Sequence[Turn], cache: bool) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    for t in turns:
        if t.role == "user":
            block = {"type": "text", "text": t.text or "(empty)"}
            if msgs and msgs[-1]["role"] == "user":
                msgs[-1]["content"].append(block)  # e.g. feedback right after tool results
            else:
                msgs.append({"role": "user", "content": [block]})
        elif t.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if t.text:
                blocks.append({"type": "text", "text": t.text})
            for c in t.tool_calls:
                blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
            msgs.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no output)"}]})
        elif t.role == "tool":
            blocks = [{"type": "tool_result", "tool_use_id": r.call_id, "content": r.content or "(empty)",
                       **({"is_error": True} if r.is_error else {})} for r in t.results]
            msgs.append({"role": "user", "content": blocks})  # tool_result blocks must lead the user turn
        else:  # pragma: no cover - guarded by construction
            raise ValueError(f"unknown turn role {t.role!r}")
    if cache and msgs and msgs[-1]["role"] == "user" and msgs[-1]["content"]:
        # One moving cache breakpoint on the newest block: the whole prefix
        # (system, tools, history) can then be read from the prompt cache.
        msgs[-1]["content"][-1] = {**msgs[-1]["content"][-1], "cache_control": {"type": "ephemeral"}}
    return msgs


def _openai_messages(system: str, turns: Sequence[Turn]) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for t in turns:
        if t.role == "user":
            msgs.append({"role": "user", "content": t.text})
        elif t.role == "assistant":
            m: dict[str, Any] = {"role": "assistant", "content": t.text or None}
            if t.tool_calls:
                m["tool_calls"] = [{"id": c.id, "type": "function",
                                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)}} for c in t.tool_calls]
            msgs.append(m)
        elif t.role == "tool":
            for r in t.results:
                msgs.append({"role": "tool", "tool_call_id": r.call_id,
                             "content": ("ERROR: " if r.is_error else "") + (r.content or "(empty)")})
    return msgs


def _parse_arguments(raw: Any) -> tuple[dict[str, Any], str]:
    if isinstance(raw, dict):
        return raw, ""
    if raw is None or raw == "":
        return {}, ""
    if isinstance(raw, str):
        try:
            val = json.loads(raw)
        except ValueError as exc:
            return {}, f"arguments are not valid JSON: {exc}"
        if isinstance(val, dict):
            return val, ""
    return {}, "arguments must be a JSON object"


def _int0(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def parse_anthropic(data: Any) -> Reply:
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        raise LLMToolError("unexpected Anthropic response shape")
    text, calls = [], []
    for block in data["content"]:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            args, err = _parse_arguments(block.get("input"))
            calls.append(ToolCall(str(block.get("id", "")), str(block.get("name", "")), args, err))
    u = data.get("usage") or {}
    usage = Usage(_int0(u.get("input_tokens")), _int0(u.get("output_tokens")),
                  _int0(u.get("cache_read_input_tokens")), _int0(u.get("cache_creation_input_tokens")), 1)
    return Reply("".join(text), calls, str(data.get("stop_reason") or ""), usage)


def parse_openai(data: Any) -> Reply:
    try:
        choice = data["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMToolError("unexpected OpenAI-compatible response shape") from exc
    if not isinstance(msg, dict):
        raise LLMToolError("unexpected OpenAI-compatible response shape")
    calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        args, err = _parse_arguments(fn.get("arguments"))
        calls.append(ToolCall(str(tc.get("id") or f"call_{i}"), str(fn.get("name", "")), args, err))
    u = data.get("usage") or {}
    details = u.get("prompt_tokens_details") or {}
    cached = _int0(details.get("cached_tokens")) if isinstance(details, dict) else 0
    prompt = _int0(u.get("prompt_tokens"))
    usage = Usage(max(prompt - cached, 0), _int0(u.get("completion_tokens")), cached, 0, 1)
    return Reply(str(msg.get("content") or ""), calls, str(choice.get("finish_reason") or ""), usage)


# --------------------------------------------------------------------------- client


@dataclass
class ToolLLM:
    """A chat client that supports tool calls for one provider."""

    provider: str
    model: str
    api_key: str = ""
    base_url: str = ""
    timeout: float = 120
    max_retries: int = 4
    max_output_tokens: int = 4096
    temperature: float | None = None
    transport: Transport = urllib_transport
    sleep: Callable[[float], None] = time.sleep
    prompt_cache: bool = True
    reasoning_effort: str = ""  # OpenAI-compatible only; e.g. "none" enables tool calls on GPT-6 Luna in Chat Completions

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS:
            raise LLMConfigError(f"unknown LLM provider {self.provider!r}; use one of {', '.join(PROVIDERS)}")
        if not self.model:
            raise LLMConfigError("PROOFCHECK_LLM_MODEL is not set; proofcheck has no default model, set it to the model id to use")

    # -- request building -------------------------------------------------------------
    def build_request(self, system: str, turns: Sequence[Turn], tools: Sequence[ToolSpec],
                      force_tool: str | None = None) -> tuple[str, dict[str, str], dict[str, Any]]:
        headers = {"Content-Type": "application/json", "User-Agent": "proofcheck-agent"}
        if self.provider == "anthropic":
            url = (self.base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
            if self.api_key:
                headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = ANTHROPIC_VERSION
            sys_block: dict[str, Any] = {"type": "text", "text": system}
            if self.prompt_cache:
                sys_block["cache_control"] = {"type": "ephemeral"}
            payload: dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_output_tokens,
                "system": [sys_block],
                "messages": _anthropic_messages(turns, self.prompt_cache),
                "tools": [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools],
            }
            if force_tool:
                payload["tool_choice"] = {"type": "tool", "name": force_tool}
        else:
            default = not self.base_url
            url = (self.base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            payload = {
                "model": self.model,
                "messages": _openai_messages(system, turns),
                "tools": [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                             "parameters": t.input_schema}} for t in tools],
            }
            # api.openai.com wants max_completion_tokens; many compatible servers only know max_tokens.
            payload["max_completion_tokens" if default else "max_tokens"] = self.max_output_tokens
            if force_tool:
                payload["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
            if self.reasoning_effort:
                payload["reasoning_effort"] = self.reasoning_effort
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        return url, headers, payload

    # -- sending -------------------------------------------------------------------------
    def _retry_delay(self, attempt: int, resp: HTTPResponse | None) -> float:
        if resp is not None:
            ra = resp.headers.get("retry-after", "")
            try:
                return min(max(float(ra), 0.0), MAX_RETRY_AFTER)
            except ValueError:
                pass
        return min(MAX_RETRY_AFTER, (2 ** attempt) + random.uniform(0, 1))

    def chat(self, system: str, turns: Sequence[Turn], tools: Sequence[ToolSpec], force_tool: str | None = None) -> Reply:
        """Send one request (with retries) and parse the reply."""
        url, headers, payload = self.build_request(system, turns, tools, force_tool)
        body = json.dumps(payload).encode("utf-8")
        last_error = ""
        for attempt in range(self.max_retries + 1):
            resp: HTTPResponse | None = None
            try:
                resp = self.transport("POST", url, headers, body, self.timeout)
            except (OSError, TimeoutError, ValueError) as exc:  # URLError is an OSError
                if isinstance(exc, ValueError) and "refusing" in str(exc):
                    raise LLMToolError(str(exc)) from exc
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status == 200:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        raise LLMToolError("LLM endpoint returned invalid JSON") from exc
                    return parse_anthropic(data) if self.provider == "anthropic" else parse_openai(data)
                last_error = f"HTTP {resp.status}"
                if resp.status not in RETRY_STATUSES:
                    raise LLMToolError(f"LLM endpoint returned {last_error}{_error_hint(resp)}")
            if attempt < self.max_retries:
                self.sleep(self._retry_delay(attempt, resp))
        raise LLMToolError(f"LLM request failed after {self.max_retries + 1} attempt(s): {last_error}")


def _error_hint(resp: HTTPResponse) -> str:
    """A short, key-free description of an API error body."""
    try:
        data = resp.json()
    except ValueError:
        return ""
    msg = ""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message") or err.get("type") or "")
        elif isinstance(err, str):
            msg = err
    msg = " ".join(msg.split())[:200]
    return f": {msg}" if msg else ""


def _is_local(url: str) -> bool:
    return url.startswith(("http://localhost", "http://127.0.0.1", "https://localhost", "https://127.0.0.1"))


#: Values accepted for PROOFCHECK_LLM_REASONING_EFFORT (sent as ``reasoning_effort`` to OpenAI-compatible APIs).
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def client_from_env(env: Mapping[str, str] | None = None, *, provider: str = "", model: str = "", base_url: str = "",
                    timeout: float = 120, max_retries: int = 4, max_output_tokens: int = 4096,
                    temperature: float | None = None, transport: Transport | None = None) -> ToolLLM:
    """Build a :class:`ToolLLM` from ``PROOFCHECK_LLM_*`` (config values are fallbacks).

    Raises :class:`LLMConfigError` with an actionable message when something is missing.
    """
    e = os.environ if env is None else env
    provider = (e.get("PROOFCHECK_LLM_PROVIDER") or provider or "").strip().lower()
    model = (e.get("PROOFCHECK_LLM_MODEL") or model or "").strip()
    base_url = (e.get("PROOFCHECK_LLM_BASE_URL") or base_url or "").strip()
    key = e.get("PROOFCHECK_LLM_API_KEY", "")
    effort = (e.get("PROOFCHECK_LLM_REASONING_EFFORT") or "").strip().lower()
    if effort and effort not in REASONING_EFFORTS:
        raise LLMConfigError(f"PROOFCHECK_LLM_REASONING_EFFORT must be one of {', '.join(REASONING_EFFORTS)}, not {effort!r}")
    if not provider:
        raise LLMConfigError("PROOFCHECK_LLM_PROVIDER is not set (use 'anthropic' or 'openai')")
    if provider not in PROVIDERS:
        raise LLMConfigError(f"PROOFCHECK_LLM_PROVIDER must be one of {', '.join(PROVIDERS)}, not {provider!r}")
    if not model:
        raise LLMConfigError("PROOFCHECK_LLM_MODEL is not set; proofcheck has no default model, set it to the model id to use")
    if not key and not _is_local(base_url):
        raise LLMConfigError("PROOFCHECK_LLM_API_KEY is not set")
    kwargs: dict[str, Any] = {}
    if transport is not None:
        kwargs["transport"] = transport
    if effort and provider == "openai":
        kwargs["reasoning_effort"] = effort
    return ToolLLM(provider, model, key, base_url, timeout, max_retries, max_output_tokens, temperature, **kwargs)
