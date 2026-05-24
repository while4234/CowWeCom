import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _jwt(claims):
    def encode(node):
        raw = json.dumps(node, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(claims)}."


class TestCodexAuth(unittest.TestCase):
    def test_resolve_access_tokens_extracts_account_id_without_exposing_secret(self):
        from models.codex.codex_auth import CodexAuthCredentialSource

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.json"
            access_token = _jwt({"exp": time.time() + 3600})
            id_token = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acc_test"}})
            path.write_text(
                json.dumps({"tokens": {"access_token": access_token, "id_token": id_token}}),
                encoding="utf-8",
            )

            tokens = CodexAuthCredentialSource(path).resolve_access_tokens()

        self.assertEqual(tokens["access_token"], access_token)
        self.assertEqual(tokens["account_id"], "acc_test")
        self.assertIn("credential", tokens)

    def test_expired_codex_auth_is_rejected(self):
        from models.codex.codex_auth import CodexAuthCredentialSource

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.json"
            path.write_text(
                json.dumps({"tokens": {"access_token": _jwt({"exp": time.time() - 10})}}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "codex_auth_expired"):
                CodexAuthCredentialSource(path).resolve_access_tokens()

    def test_auth_path_reuses_image_generation_codex_auth_config(self):
        from models.codex.codex_auth import resolve_codex_auth_path

        configured = "C:/safe/codex/auth.json"
        with patch.dict(os.environ, {"CODEX_AUTH_FILE": ""}, clear=False):
            path = resolve_codex_auth_path({
                "skill": {
                    "image-generation": {
                        "codex_auth_file": configured,
                    }
                }
            })

        self.assertEqual(str(path).replace("\\", "/"), configured)


class TestCodexSSEParser(unittest.TestCase):
    def test_parser_buffers_utf8_until_complete_sse_event(self):
        from models.codex.codex_direct_client import CodexSSEParser

        payload = 'data: {"type":"response.output_text.delta","delta":"你好"}\n\n'
        raw = payload.encode("utf-8")
        chunks = [raw[:17], raw[17:31], raw[31:]]

        events = list(CodexSSEParser().iter_json_events(chunks, limit=1000, request_id="req"))

        self.assertEqual(events, [{"type": "response.output_text.delta", "delta": "你好"}])

    def test_parser_ignores_done_and_comments(self):
        from models.codex.codex_direct_client import CodexSSEParser

        events = CodexSSEParser().parse_text(
            ": keepalive\n\n"
            'data: {"type":"response.completed","response":{"output":[]}}\n\n'
            "data: [DONE]\n\n"
        )

        self.assertEqual(events, [{"type": "response.completed", "response": {"output": []}}])


class TestCodexDirectBot(unittest.TestCase):
    def test_call_without_tools_uses_text_only_payload(self):
        from models.codex.codex_bot import CodexBot

        class FakeCredential:
            def resolve_access_tokens(self):
                return {"access_token": "test-token", "account_id": "acc_test"}

        class FakeTransport:
            payload = None
            tokens = None
            config = None

            def stream_responses(self, payload, tokens, *, config=None, request_id=""):
                self.payload = payload
                self.tokens = tokens
                self.config = config
                yield {"type": "response.output_text.delta", "delta": "hello"}
                yield {
                    "type": "response.completed",
                    "response": {
                        "model": payload["model"],
                        "output": [],
                        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
                    },
                }

        fake_transport = FakeTransport()
        provider = {
            "model": "gpt-5.5",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "endpoint_path": "/responses",
            "reasoning_effort": "",
            "tools_enabled": True,
        }
        with patch.object(CodexBot, "_provider_config", staticmethod(lambda: provider)):
            with patch.object(CodexBot, "_record_prompt_cache_usage", lambda *_, **__: None):
                bot = CodexBot(credential_source=FakeCredential(), transport=fake_transport)
                result = bot.call_with_tools(
                    [
                        {"role": "user", "content": "answer directly"},
                    ],
                    stream=False,
                    system="You are concise.",
                    channel_type="wecom_bot",
                )

        self.assertEqual(result["choices"][0]["message"]["content"], "hello")
        self.assertEqual(fake_transport.tokens["access_token"], "test-token")
        self.assertEqual(fake_transport.payload["model"], "gpt-5.5")
        self.assertNotIn("tools", fake_transport.payload)
        self.assertNotIn("tool_choice", fake_transport.payload)
        self.assertIn("Use text responses only", fake_transport.payload["instructions"])
        self.assertEqual(fake_transport.payload["prompt_cache_key"], "cowwechat:gpt-5.5:wecom_bot")
        self.assertTrue(all(item.get("type") == "message" for item in fake_transport.payload["input"]))

    def test_call_with_tools_uses_codex_responses_tool_payload(self):
        from models.codex.codex_bot import CodexBot

        class FakeCredential:
            def resolve_access_tokens(self):
                return {"access_token": "test-token", "account_id": "acc_test"}

        class FakeTransport:
            payload = None

            def stream_responses(self, payload, tokens, *, config=None, request_id=""):
                self.payload = payload
                yield {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "read_file",
                        "arguments": "",
                    },
                }
                yield {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "call_id": "call_1",
                    "delta": '{"path"',
                }
                yield {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "call_id": "call_1",
                    "delta": ':"README.md"}',
                }
                yield {
                    "type": "response.completed",
                    "response": {
                        "model": payload["model"],
                        "output": [],
                        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
                    },
                }

        fake_transport = FakeTransport()
        records = []
        provider = {
            "model": "gpt-5.5",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "endpoint_path": "/responses",
            "reasoning_effort": "",
            "tools_enabled": True,
            "tools_enabled": True,
        }
        with patch.object(CodexBot, "_provider_config", staticmethod(lambda: provider)):
            with patch.object(
                CodexBot,
                "_record_prompt_cache_usage",
                lambda self, usage, **kwargs: records.append((usage, kwargs)),
            ):
                bot = CodexBot(credential_source=FakeCredential(), transport=fake_transport)
                result = bot.call_with_tools(
                    [
                        {"role": "user", "content": "use a tool"},
                    ],
                    tools=[{"name": "read_file", "description": "Read", "input_schema": {"type": "object"}}],
                    stream=False,
                    system="You are concise.",
                    channel_type="wecom_bot",
                )

        self.assertIn("tools", fake_transport.payload)
        self.assertEqual(fake_transport.payload["tools"][0]["name"], "read_file")
        self.assertEqual(fake_transport.payload["tool_choice"], "auto")
        self.assertEqual(fake_transport.payload["prompt_cache_key"], "cowwechat:gpt-5.5:wecom_bot")
        self.assertEqual(fake_transport.payload["reasoning"]["effort"], "xhigh")
        self.assertNotIn("Do not call tools", fake_transport.payload.get("instructions", ""))
        self.assertEqual(result["choices"][0]["finish_reason"], "tool_calls")
        tool_call = result["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(tool_call["id"], "call_1")
        self.assertEqual(tool_call["function"]["name"], "read_file")
        self.assertEqual(tool_call["function"]["arguments"], '{"path":"README.md"}')
        self.assertEqual(records[0][0]["total_tokens"], 4)
        self.assertEqual(records[0][1]["wire_api"], "codex")
        self.assertEqual(records[0][1]["request_payload"]["prompt_cache_key"], "cowwechat:gpt-5.5:wecom_bot")

    def test_tools_disabled_returns_error_without_capi_fallback(self):
        from models.codex.codex_bot import CodexBot

        provider = {
            "model": "gpt-5.5",
            "tools_enabled": False,
        }
        with patch.object(CodexBot, "_provider_config", staticmethod(lambda: provider)):
            bot = CodexBot(credential_source=object(), transport=object())
            result = bot.call_with_tools(
                [{"role": "user", "content": "use a tool"}],
                tools=[{"name": "read_file", "description": "Read"}],
                stream=False,
            )

        self.assertTrue(result["error"])
        self.assertIn("codex_tools_disabled", result["message"])

    def test_locked_empty_effort_omits_reasoning_and_unsupported_params(self):
        from models.codex.codex_bot import CodexBot

        class FakeCredential:
            def resolve_access_tokens(self):
                return {"access_token": "test-token", "account_id": "acc_test"}

        class FakeTransport:
            payload = None

            def stream_responses(self, payload, tokens, *, config=None, request_id=""):
                self.payload = payload
                yield {"type": "response.output_text.delta", "delta": '{"effort":"medium"}'}
                yield {
                    "type": "response.completed",
                    "response": {
                        "model": payload["model"],
                        "output": [],
                        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
                    },
                }

        fake_transport = FakeTransport()
        provider = {
            "model": "gpt-5.5",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "endpoint_path": "/responses",
            "reasoning_effort": "xhigh",
            "tools_enabled": True,
        }
        with patch.object(CodexBot, "_provider_config", staticmethod(lambda: provider)):
            with patch.object(CodexBot, "_record_prompt_cache_usage", lambda *_, **__: None):
                bot = CodexBot(credential_source=FakeCredential(), transport=fake_transport)
                bot.call_with_tools(
                    [{"role": "user", "content": "classify this"}],
                    stream=False,
                    max_tokens=80,
                    reasoning_effort_locked=True,
                    thinking={"type": "enabled"},
                )

        self.assertNotIn("reasoning", fake_transport.payload)
        self.assertNotIn("max_output_tokens", fake_transport.payload)
        self.assertNotIn("prompt_cache_retention", fake_transport.payload)


if __name__ == "__main__":
    unittest.main()
