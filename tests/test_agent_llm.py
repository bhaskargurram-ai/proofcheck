"""The tool-use LLM client: wire formats, retries, accounting and configuration."""

import json
import unittest

import _helpers  # noqa: F401  (sets up sys.path)
from _agent_fakes import ScriptedTransport

from proofcheck.agent.llm_tools import (
    LLMConfigError,
    LLMToolError,
    ToolCall,
    ToolLLM,
    ToolResult,
    ToolSpec,
    Turn,
    Usage,
    client_from_env,
    parse_anthropic,
    parse_openai,
)
from proofcheck.httpclient import HTTPResponse

SPEC = ToolSpec("read_file", "Read a file.", {"type": "object", "properties": {"path": {"type": "string"}}})
TURNS = [
    Turn("user", text="hello"),
    Turn("assistant", text="looking", tool_calls=[ToolCall("t1", "read_file", {"path": "a.py"})]),
    Turn("tool", results=[ToolResult("t1", "read_file", "contents", False)]),
    Turn("user", text="feedback"),
]


def llm(provider, transport=None, **kw):
    return ToolLLM(provider, "model-x", "key-abc", transport=transport or ScriptedTransport(provider, []), sleep=lambda s: None, **kw)


class WireFormatTest(unittest.TestCase):
    def test_anthropic_request(self):
        url, headers, payload = llm("anthropic").build_request("SYS", TURNS, [SPEC], force_tool="submit_test")
        self.assertTrue(url.endswith("/v1/messages"))
        self.assertEqual(headers["x-api-key"], "key-abc")
        self.assertIn("anthropic-version", headers)
        self.assertEqual(payload["model"], "model-x")
        self.assertEqual(payload["tools"][0]["input_schema"]["type"], "object")
        self.assertEqual(payload["tool_choice"], {"type": "tool", "name": "submit_test"})
        self.assertEqual(payload["system"][0]["text"], "SYS")
        self.assertNotIn("temperature", payload)
        msgs = payload["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual(msgs[1]["content"][1], {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.py"}})
        # tool_result first, then the follow-up text, in one user turn; newest block carries the cache breakpoint
        self.assertEqual(msgs[2]["content"][0]["type"], "tool_result")
        self.assertEqual(msgs[2]["content"][0]["tool_use_id"], "t1")
        self.assertEqual(msgs[2]["content"][1]["text"], "feedback")
        self.assertIn("cache_control", msgs[2]["content"][1])

    def test_openai_request(self):
        url, headers, payload = llm("openai", temperature=0.2).build_request("SYS", TURNS, [SPEC])
        self.assertTrue(url.endswith("/chat/completions"))
        self.assertEqual(headers["Authorization"], "Bearer key-abc")
        self.assertEqual(payload["tools"][0], {"type": "function", "function": {"name": "read_file", "description": "Read a file.",
                                                                                "parameters": SPEC.input_schema}})
        self.assertEqual(payload["temperature"], 0.2)
        self.assertIn("max_completion_tokens", payload)
        msgs = payload["messages"]
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "tool", "user"])
        self.assertEqual(json.loads(msgs[2]["tool_calls"][0]["function"]["arguments"]), {"path": "a.py"})
        self.assertEqual(msgs[3]["tool_call_id"], "t1")
        # a compatible server (custom base URL) gets max_tokens
        _, _, payload = ToolLLM("openai", "m", "k", base_url="https://llm.example/v1").build_request("S", TURNS, [SPEC], "submit_test")
        self.assertIn("max_tokens", payload)
        self.assertEqual(payload["tool_choice"], {"type": "function", "function": {"name": "submit_test"}})

    def test_parse_replies(self):
        r = parse_anthropic({"content": [{"type": "text", "text": "hi"}, {"type": "tool_use", "id": "a", "name": "grep",
                                                                         "input": {"pattern": "x"}}],
                             "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5,
                                                                  "cache_read_input_tokens": 100, "cache_creation_input_tokens": 7}})
        self.assertEqual((r.text, r.tool_calls[0].name, r.tool_calls[0].arguments), ("hi", "grep", {"pattern": "x"}))
        self.assertEqual(r.usage.to_dict(), {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 100,
                                             "cache_write_tokens": 7, "requests": 1})
        r = parse_openai({"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "grep", "arguments": "{not json"}}]}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 9, "prompt_tokens_details": {"cached_tokens": 20}}})
        self.assertTrue(r.tool_calls[0].parse_error)
        self.assertEqual((r.usage.input_tokens, r.usage.cache_read_tokens, r.usage.output_tokens), (30, 20, 9))
        for bad in ({}, {"content": "x"}, None):
            with self.assertRaises(LLMToolError):
                parse_anthropic(bad)
        with self.assertRaises(LLMToolError):
            parse_openai({"choices": []})


class RetryTest(unittest.TestCase):
    def test_retries_rate_limits_and_server_errors(self):
        sleeps = []
        t = ScriptedTransport("openai", [HTTPResponse(429, b"{}", {"retry-after": "3"}), HTTPResponse(503, b""), "done"])
        client = ToolLLM("openai", "m", "k", transport=t, sleep=sleeps.append)
        reply = client.chat("s", [Turn("user", text="u")], [SPEC])
        self.assertEqual(reply.text, "done")
        self.assertEqual(len(t.requests), 3)
        self.assertEqual(sleeps[0], 3.0)
        self.assertEqual(len(sleeps), 2)

    def test_transport_exceptions_are_retried(self):
        calls = []

        def flaky(method, url, headers, body, timeout):
            calls.append(timeout)
            if len(calls) < 2:
                raise TimeoutError("timed out")
            return HTTPResponse(200, json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode())

        client = ToolLLM("anthropic", "m", "k", timeout=7, transport=flaky, sleep=lambda s: None)
        self.assertEqual(client.chat("s", [Turn("user", text="u")], [SPEC]).text, "ok")
        self.assertEqual(calls, [7, 7])

    def test_non_retryable_and_exhausted(self):
        t = ScriptedTransport("anthropic", [HTTPResponse(400, json.dumps({"error": {"message": "bad tool schema"}}).encode())])
        with self.assertRaisesRegex(LLMToolError, "HTTP 400: bad tool schema"):
            ToolLLM("anthropic", "m", "k", transport=t, sleep=lambda s: None).chat("s", [Turn("user", text="u")], [SPEC])
        t = ScriptedTransport("anthropic", [HTTPResponse(500, b"")] * 3)
        with self.assertRaisesRegex(LLMToolError, "after 3 attempt"):
            ToolLLM("anthropic", "m", "k", max_retries=2, transport=t, sleep=lambda s: None).chat("s", [Turn("user", text="u")], [SPEC])

    def test_api_key_never_in_errors(self):
        t = ScriptedTransport("openai", [HTTPResponse(401, json.dumps({"error": {"message": "invalid key"}}).encode())])
        with self.assertRaises(LLMToolError) as ctx:
            ToolLLM("openai", "m", "sk-very-secret-value", transport=t).chat("s", [Turn("user", text="u")], [SPEC])
        self.assertNotIn("sk-very-secret-value", str(ctx.exception))


class ConfigFromEnvTest(unittest.TestCase):
    BASE = {"PROOFCHECK_LLM_PROVIDER": "anthropic", "PROOFCHECK_LLM_API_KEY": "k", "PROOFCHECK_LLM_MODEL": "m"}

    def test_complete(self):
        c = client_from_env(self.BASE)
        self.assertEqual((c.provider, c.model, c.api_key), ("anthropic", "m", "k"))

    def test_model_is_required_with_a_clear_message(self):
        env = dict(self.BASE)
        del env["PROOFCHECK_LLM_MODEL"]
        with self.assertRaisesRegex(LLMConfigError, "PROOFCHECK_LLM_MODEL is not set; proofcheck has no default model"):
            client_from_env(env)
        with self.assertRaises(LLMConfigError):
            ToolLLM("openai", "")

    def test_provider_and_key(self):
        with self.assertRaisesRegex(LLMConfigError, "PROOFCHECK_LLM_PROVIDER"):
            client_from_env({"PROOFCHECK_LLM_MODEL": "m", "PROOFCHECK_LLM_API_KEY": "k"})
        with self.assertRaisesRegex(LLMConfigError, "must be one of"):
            client_from_env(dict(self.BASE, PROOFCHECK_LLM_PROVIDER="gemini"))
        with self.assertRaisesRegex(LLMConfigError, "API_KEY"):
            client_from_env({"PROOFCHECK_LLM_PROVIDER": "openai", "PROOFCHECK_LLM_MODEL": "m"})
        # a local OpenAI-compatible server needs no key
        c = client_from_env({"PROOFCHECK_LLM_PROVIDER": "openai", "PROOFCHECK_LLM_MODEL": "m",
                             "PROOFCHECK_LLM_BASE_URL": "http://localhost:11434/v1"})
        self.assertEqual(c.base_url, "http://localhost:11434/v1")

    def test_config_values_are_fallbacks(self):
        c = client_from_env({"PROOFCHECK_LLM_API_KEY": "k"}, provider="openai", model="from-config")
        self.assertEqual(c.model, "from-config")
        c = client_from_env(dict(self.BASE, PROOFCHECK_LLM_MODEL="from-env"), model="from-config")
        self.assertEqual(c.model, "from-env")


class UsageTest(unittest.TestCase):
    def test_cost_only_with_prices(self):
        u = Usage(1_000_000, 100_000, 0, 0, 3)
        self.assertIsNone(u.cost(None, None))
        self.assertIsNone(u.cost(3.0, None))
        self.assertAlmostEqual(u.cost(3.0, 15.0), 4.5)
        total = Usage()
        total.add(u)
        total.add(Usage(1, 1, 1, 1, 1))
        self.assertEqual(total.requests, 4)
        self.assertEqual(total.total_tokens, 1_100_004)


if __name__ == "__main__":
    unittest.main()
