# encoding:utf-8

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.protocol import LLMRequest
from bridge.agent_bridge import AgentLLMModel
from common import const
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
    @staticmethod
    def _admin_profile():
        return SimpleNamespace(
            actor_id="admin",
            raw_user_id="admin",
            memory_user_id="admin",
            display_name="Admin",
            role="admin",
            is_admin=True,
            conversation_id="admin",
        )

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

    def _run_grok_agent_call(self, request_backend=""):
        client = _FakeResponsesClient()
        fake_conf = self._fake_conf("")
        request = self._request()
        if request_backend:
            request.backend = request_backend

        with patch("config.conf", return_value=fake_conf), \
                patch("common.llm_backend_router.is_codex_active", return_value=False), \
                patch("bridge.agent_bridge.get_current_backend_for_profile", return_value="grok"), \
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
                patch("models.grok.grok_bot.GrokBot._get_http_client", return_value=client), \
                patch.object(OpenAICompatibleBot, "_get_http_client", return_value=client):
            model = AgentLLMModel(bridge=MagicMock())
            model.set_actor_profile(self._admin_profile())
            result = model.call(request)

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        return client.calls[0]

    def test_agent_grok_uses_grok_model_in_responses_payload(self):
        call = self._run_grok_agent_call()

        self.assertEqual(call["model"], "grok-4.3")

    def test_agent_xai_alias_uses_grok_model_in_responses_payload(self):
        call = self._run_grok_agent_call("xai")

        self.assertEqual(call["model"], "grok-4.3")

    def test_global_grok_bot_type_without_backend_uses_openai_route(self):
        fake_conf = self._fake_conf("grok")

        with patch("config.conf", return_value=fake_conf), \
                patch("bridge.agent_bridge.get_current_backend_for_profile", return_value="capi"), \
                patch("bridge.agent_bridge.conf", return_value=fake_conf):
            model = AgentLLMModel(bridge=MagicMock())
            bot_type, routed_model, route_backend = model._resolve_request_route(self._request())

        self.assertEqual(bot_type, const.DEEPSEEK)
        self.assertEqual(routed_model, "deepseek-v4-flash")
        self.assertEqual(route_backend, "capi")

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
                patch("models.grok.grok_bot.GrokBot._get_http_client", return_value=client), \
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
                patch.object(AgentLLMModel, "_create_bot_for_route", return_value=bot):
            model = AgentLLMModel(bridge=MagicMock())
            result = model.call(self._request())

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(client.calls[0]["model"], "deepseek-v4-flash")

    def test_request_backend_override_resolves_codex_route(self):
        model = AgentLLMModel(bridge=MagicMock())
        request = LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            model="codex-fast",
            backend="codex",
        )

        bot_type, routed_model, route_backend = model._resolve_request_route(request)

        self.assertEqual(bot_type, const.CODEX)
        self.assertEqual(routed_model, "codex-fast")
        self.assertEqual(route_backend, "codex")

    def test_profile_backend_override_resolves_grok_route(self):
        model = AgentLLMModel(bridge=MagicMock())
        model.set_actor_profile(self._admin_profile())
        request = LLMRequest(messages=[{"role": "user", "content": "hi"}])

        with patch("bridge.agent_bridge.get_current_backend_for_profile", return_value="grok"):
            bot_type, routed_model, route_backend = model._resolve_request_route(request)

        self.assertEqual(bot_type, const.GROK)
        self.assertEqual(routed_model, "grok-4.3")
        self.assertEqual(route_backend, "grok")

    def test_normal_user_request_for_grok_falls_back_to_shared_gpt_backend(self):
        model = AgentLLMModel(bridge=MagicMock())
        model.set_actor_profile(SimpleNamespace(
            actor_id="user",
            raw_user_id="user",
            memory_user_id="user",
            display_name="Normal User",
            role="user",
            is_admin=False,
            conversation_id="user",
        ))
        request = LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            backend="grok",
        )

        with patch("bridge.agent_bridge.get_current_backend", return_value="capi"), \
                patch("bridge.agent_bridge.get_effective_openai_api_config", return_value={"model": "gpt-4.1-mini"}):
            bot_type, routed_model, route_backend = model._resolve_request_route(request)

        self.assertEqual(bot_type, const.OPENAI)
        self.assertEqual(routed_model, "gpt-4.1-mini")
        self.assertEqual(route_backend, "capi")

    def test_unknown_request_backend_is_rejected(self):
        model = AgentLLMModel(bridge=MagicMock())
        request = LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            backend="missing_provider",
        )

        with self.assertRaisesRegex(ValueError, "unsupported request backend"):
            model._resolve_request_route(request)

    def test_user_visible_call_counter_uses_request_backend_override(self):
        model = AgentLLMModel(bridge=MagicMock())
        model.channel_type = "wechatcom_app"
        model.session_id = "s1"
        request = LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            backend="codex",
            cache_shape_metadata={"request_kind": "normal"},
        )

        with patch("common.llm_backend_quota_refresh.note_user_visible_model_call") as note:
            model._note_user_visible_model_call(request)

        note.assert_called_once_with(backend="codex", request_kind="normal")

    def test_user_visible_call_counter_uses_profile_backend_when_request_has_none(self):
        model = AgentLLMModel(bridge=MagicMock())
        model.set_actor_profile(self._admin_profile())
        model.channel_type = "wechatcom_app"
        model.session_id = "s1"
        request = LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            cache_shape_metadata={"request_kind": "normal"},
        )

        with patch("bridge.agent_bridge.get_current_backend_for_profile", return_value="grok"), \
                patch("common.llm_backend_quota_refresh.note_user_visible_model_call") as note:
            model._note_user_visible_model_call(request)

        note.assert_called_once_with(backend="grok", request_kind="normal")


if __name__ == "__main__":
    unittest.main()
