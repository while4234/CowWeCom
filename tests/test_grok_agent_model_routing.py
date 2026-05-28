# encoding:utf-8

import unittest
from unittest.mock import MagicMock, patch

from agent.protocol import LLMRequest
from bridge.agent_bridge import AgentLLMModel
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


class _FakeOpenAICompatibleBot(OpenAICompatibleBot):
    def __init__(self, client):
        self.client = client

    def get_api_config(self):
        return {
            "api_key": "token",
            "api_base": "https://provider.example/v1",
            "model": "deepseek-v4-flash",
            "wire_api": "responses",
            "provider": "openai",
        }

    def _get_http_client(self):
        return self.client


class TestGrokAgentModelRouting(unittest.TestCase):
    def _fake_conf(self, bot_type):
        values = {
            "agent": True,
            "bot_type": bot_type,
            "model": "deepseek-v4-flash",
            "grok_model": "grok-4.3",
            "grok_api_base": "https://api.x.ai/v1",
            "grok_wire_api": "responses",
            "temperature": 0.7,
            "top_p": 1.0,
            "frequency_penalty": 0.0,
            "presence_penalty": 0.0,
            "request_timeout": 60,
            "use_linkai": False,
            "linkai_api_key": "",
            "enable_thinking": False,
            "model_reasoning_effort": "",
            "reasoning_effort": "high",
            "disable_response_storage": False,
        }
        fake_conf = MagicMock()
        fake_conf.get.side_effect = lambda key, default=None: values.get(key, default)
        return fake_conf

    def _request(self):
        return LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            model="deepseek-v4-flash",
        )

    def _run_agent_call(self, bot_type):
        client = _FakeResponsesClient()
        fake_conf = self._fake_conf(bot_type)

        with patch("config.conf", return_value=fake_conf), \
                patch("common.llm_backend_router.is_codex_active", return_value=False), \
                patch("bridge.agent_bridge.conf", return_value=fake_conf), \
                patch("models.grok.grok_bot.conf", return_value=fake_conf), \
                patch(
                    "models.grok.grok_bot.resolve_xai_http_credentials",
                    return_value={
                        "api_key": "oauth-token",
                        "base_url": "https://api.x.ai/v1",
                        "provider": "xai-oauth",
                        "auth_mode": "oauth_pkce",
                    },
                ), \
                patch.object(OpenAICompatibleBot, "_get_http_client", return_value=client):
            model = AgentLLMModel(bridge=MagicMock())
            result = model.call(self._request())

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        return client.calls[0]

    def test_agent_grok_uses_grok_model_in_responses_payload(self):
        call = self._run_agent_call("grok")

        self.assertEqual(call["model"], "grok-4.3")

    def test_agent_xai_alias_uses_grok_model_in_responses_payload(self):
        call = self._run_agent_call("xai")

        self.assertEqual(call["model"], "grok-4.3")

    def test_grok_bot_direct_call_ignores_non_grok_model_kwarg(self):
        from models.grok.grok_bot import GrokBot

        client = _FakeResponsesClient()
        fake_conf = self._fake_conf("grok")

        with patch("config.conf", return_value=fake_conf), \
                patch("models.grok.grok_bot.conf", return_value=fake_conf), \
                patch(
                    "models.grok.grok_bot.resolve_xai_http_credentials",
                    return_value={
                        "api_key": "oauth-token",
                        "base_url": "https://api.x.ai/v1",
                        "provider": "xai-oauth",
                        "auth_mode": "oauth_pkce",
                    },
                ), \
                patch.object(OpenAICompatibleBot, "_get_http_client", return_value=client):
            bot = GrokBot()
            result = bot.call_with_tools(
                messages=[{"role": "user", "content": "hi"}],
                stream=False,
                model="deepseek-v4-flash",
            )

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(client.calls[0]["model"], "grok-4.3")

    def test_non_grok_agent_preserves_request_model(self):
        client = _FakeResponsesClient()
        bot = _FakeOpenAICompatibleBot(client)
        fake_conf = self._fake_conf("deepseek")

        with patch("config.conf", return_value=fake_conf), \
                patch("common.llm_backend_router.is_codex_active", return_value=False), \
                patch("bridge.agent_bridge.conf", return_value=fake_conf), \
                patch("models.bot_factory.create_bot", return_value=bot):
            model = AgentLLMModel(bridge=MagicMock())
            result = model.call(self._request())

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(client.calls[0]["model"], "deepseek-v4-flash")


if __name__ == "__main__":
    unittest.main()
