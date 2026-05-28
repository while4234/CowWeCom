# encoding:utf-8

import unittest
from unittest.mock import patch

from models.openai_compatible_bot import OpenAICompatibleBot


class _FakeResponsesClient:
    def __init__(self):
        self.calls = []

    def responses(self, **kwargs):
        self.calls.append(kwargs)
        yield {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "model": kwargs.get("model"),
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            },
        }


class _FakeBot(OpenAICompatibleBot):
    def __init__(self, client):
        self.client = client

    def get_api_config(self):
        return {
            "api_key": "token",
            "api_base": "https://api.x.ai/v1",
            "model": "grok-4.3",
            "wire_api": "responses",
            "provider": "xai-oauth",
        }

    def _get_http_client(self):
        return self.client


class TestGrokResponsesAdapter(unittest.TestCase):
    def test_xai_responses_adds_conv_header_and_prompt_cache_key(self):
        client = _FakeResponsesClient()
        bot = _FakeBot(client)

        result = bot._handle_sync_response(
            {
                "model": "grok-4.3",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
                "stream": False,
                "_cache_metadata": {"session_id": "session-123", "channel_type": "web"},
            },
            "token",
            "https://api.x.ai/v1",
            bot.get_api_config(),
        )

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        call = client.calls[0]
        self.assertEqual(call["extra_headers"], {"x-grok-conv-id": "session-123"})
        self.assertEqual(call["prompt_cache_key"], "session-123")
        self.assertEqual(call["reasoning"], {"effort": "high"})

    def test_unsupported_grok_model_drops_reasoning_and_service_tier(self):
        bot = _FakeBot(_FakeResponsesClient())
        payload, headers = bot._prepare_responses_request(
            {
                "model": "grok-2",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
                "reasoning": {"effort": "high"},
                "service_tier": "auto",
            },
            api_config={"provider": "xai-oauth"},
            api_base="https://api.x.ai/v1",
            cache_metadata={"session_id": "session-1"},
        )

        self.assertEqual(headers, {"x-grok-conv-id": "session-1"})
        self.assertNotIn("reasoning", payload)
        self.assertNotIn("service_tier", payload)
        self.assertEqual(payload["prompt_cache_key"], "session-1")

    def test_xai_tool_schema_sanitizer_removes_slash_enum(self):
        bot = _FakeBot(_FakeResponsesClient())
        payload, _ = bot._prepare_responses_request(
            {
                "model": "grok-4.3",
                "input": [],
                "tools": [
                    {
                        "type": "function",
                        "name": "choose",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "mode": {"type": "string", "enum": ["read/write", "read"]},
                            },
                            "examples": [{"mode": "read"}],
                        },
                    }
                ],
            },
            api_config={"provider": "xai-oauth"},
            api_base="https://api.x.ai/v1",
            cache_metadata={},
        )

        props = payload["tools"][0]["parameters"]["properties"]
        self.assertNotIn("enum", props["mode"])
        self.assertNotIn("examples", payload["tools"][0]["parameters"])

    def test_non_xai_requests_keep_openai_prompt_cache_behavior(self):
        client = _FakeResponsesClient()
        bot = _FakeBot(client)

        with patch("config.conf") as conf_func:
            fake_conf = conf_func.return_value
            fake_conf.get.side_effect = lambda key, default=None: {
                "enable_prompt_cache_key": True,
                "prompt_cache_key_prefix": "cowwechat",
                "prompt_cache_key_granularity": "session",
                "prompt_cache_retention": "",
            }.get(key, default)
            payload, headers = bot._prepare_responses_request(
                {"model": "gpt-5.5", "input": []},
                api_config={"provider": "openai"},
                api_base="https://api.openai.com/v1",
                cache_metadata={"session_id": "raw-session", "channel_type": "web"},
            )

        self.assertEqual(headers, {})
        self.assertNotEqual(payload["prompt_cache_key"], "raw-session")
        self.assertTrue(payload["prompt_cache_key"].startswith("cowwechat:gpt-5.5:web"))


if __name__ == "__main__":
    unittest.main()
