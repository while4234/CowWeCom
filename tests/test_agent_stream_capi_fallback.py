import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.protocol.agent_stream import AgentStreamExecutor
from agent.protocol.models import LLMModel
from common.llm_backend_router import BACKEND_CAPI, BACKEND_CAPI_MONTHLY, BACKEND_CODEX, get_current_backend, save_state
from config import conf


def weekly_payload(used_percent=5, resets_at=4102444800):
    return {
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "limitName": "Codex",
                "primary": {
                    "windowDurationMins": 60,
                    "usedPercent": 0,
                    "resetsAt": resets_at,
                },
                "secondary": {
                    "windowDurationMins": 10080,
                    "usedPercent": used_percent,
                    "resetsAt": resets_at,
                },
            }
        }
    }


class FakeAgent:
    memory_manager = None
    skill_manager = None
    max_context_tokens = None

    def _estimate_message_tokens(self, msg):
        return len(str(msg))

    def _get_model_context_window(self):
        return 100000


class CapiFailThenCodexModel(LLMModel):
    def __init__(self):
        super().__init__(model="routed-model")
        self.requests = []

    def call_stream(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            yield {
                "error": {
                    "message": "{'raw': 'Internal Server Error'}",
                    "code": "",
                    "type": "",
                },
                "status_code": 500,
            }
            return

        yield {"choices": [{"delta": {"content": "completed on codex"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}


class MonthlyQuotaThenSuccessModel(LLMModel):
    def __init__(self, response_text="continued"):
        super().__init__(model="routed-model")
        self.requests = []
        self.backends = []
        self.response_text = response_text

    def call_stream(self, request):
        self.requests.append(request)
        self.backends.append(get_current_backend())
        if len(self.requests) == 1:
            yield {
                "error": {
                    "message": "insufficient_quota: monthly quota exhausted",
                    "code": "insufficient_quota",
                    "type": "billing_error",
                },
                "status_code": 402,
            }
            return

        yield {"choices": [{"delta": {"content": self.response_text}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}


class MonthlyQuotaThenCapiQuotaThenCodexModel(MonthlyQuotaThenSuccessModel):
    def call_stream(self, request):
        self.requests.append(request)
        self.backends.append(get_current_backend())
        if len(self.requests) == 1:
            yield {
                "error": {
                    "message": "insufficient_quota: monthly quota exhausted",
                    "code": "insufficient_quota",
                    "type": "billing_error",
                },
                "status_code": 402,
            }
            return
        if len(self.requests) == 2:
            yield {
                "error": {
                    "message": "insufficient_quota: quota card exhausted",
                    "code": "insufficient_quota",
                    "type": "billing_error",
                },
                "status_code": 402,
            }
            return

        yield {"choices": [{"delta": {"content": self.response_text}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}


class NetworkErrorThenCodexModel(LLMModel):
    def __init__(self, error_text="ConnectError: getaddrinfo failed"):
        super().__init__(model="routed-model")
        self.requests = []
        self.backends = []
        self.error_text = error_text

    def call_stream(self, request):
        self.requests.append(request)
        self.backends.append(get_current_backend())
        if get_current_backend() != BACKEND_CODEX:
            raise TimeoutError(self.error_text)

        yield {"choices": [{"delta": {"content": "continued on codex"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}


class PartialTextNetworkErrorThenCodexModel(NetworkErrorThenCodexModel):
    def call_stream(self, request):
        self.requests.append(request)
        self.backends.append(get_current_backend())
        if get_current_backend() != BACKEND_CODEX:
            yield {"choices": [{"delta": {"content": "partial text before disconnect"}}]}
            yield {
                "error": {
                    "message": "RemoteDisconnected: remote end closed connection without response",
                    "code": "",
                    "type": "",
                },
                "status_code": 0,
            }
            return

        yield {"choices": [{"delta": {"content": "continued on codex"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}


def current_task_messages():
    return [
        {"role": "user", "content": [{"type": "text", "text": "current travel request"}]},
        {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "call_1",
                "name": "bash",
                "input": {"command": "python skills\\flyai\\scripts\\flyai_wrapper.py search-hotel"},
            }],
        },
        {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": "{\"ok\": true, \"result\": \"hotel data\"}",
            }],
        },
    ]


class TestAgentStreamCapiFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_backend_config = conf().get("llm_backend")
        conf()["llm_backend"] = {
            "current_backend": BACKEND_CAPI,
            "state_path": str(Path(self.tmp.name) / "state.json"),
            "providers": {
                "capi": {"api_key": "TEST-CAPI-KEY", "model": "gpt-4.1-mini"},
                "codex": {"model": "gpt-5.5", "tools_enabled": True},
            },
        }
        save_state({"current_backend": BACKEND_CAPI})

    def tearDown(self):
        if self.previous_backend_config is None:
            conf().pop("llm_backend", None)
        else:
            conf()["llm_backend"] = self.previous_backend_config
        self.tmp.cleanup()

    def test_capi_stream_error_replays_same_request_on_codex(self):
        model = CapiFailThenCodexModel()
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "current user request"}]},
            {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "read",
                    "input": {"path": "notes.md"},
                }],
            },
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": "notes content",
                }],
            },
        ]
        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=model,
            system_prompt="system prompt",
            tools=[],
            messages=messages,
        )

        response, tool_calls = executor._call_llm_stream(max_retries=0)

        self.assertEqual(response, "completed on codex")
        self.assertEqual(tool_calls, [])
        self.assertEqual(get_current_backend(), BACKEND_CODEX)
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(model.requests[1].messages, model.requests[0].messages)
        self.assertEqual(model.requests[1].system, "system prompt")

    def test_monthly_quota_exhaustion_prefers_codex_when_under_fair_share_without_restarting(self):
        conf()["llm_backend"] = {
            "current_backend": BACKEND_CAPI_MONTHLY,
            "state_path": str(Path(self.tmp.name) / "state.json"),
            "providers": {
                "capi": {"api_key": "TEST-CAPI-KEY", "model": "gpt-5.5"},
                "capi_monthly": {"api_key": "TEST-MONTHLY-KEY", "model": "gpt-5.5"},
                "codex": {"model": "gpt-5.5", "tools_enabled": True},
            },
        }
        save_state({"current_backend": BACKEND_CAPI_MONTHLY})
        model = MonthlyQuotaThenSuccessModel("continued on codex")
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "current travel request"}]},
            {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "bash",
                    "input": {"command": "python skills\\flyai\\scripts\\flyai_wrapper.py search-hotel"},
                }],
            },
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": "{\"ok\": true, \"result\": \"hotel data\"}",
                }],
            },
        ]
        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=model,
            system_prompt="system prompt",
            tools=[],
            messages=messages,
        )

        with patch(
            "agent.protocol.agent_stream.query_codex_quota_json",
            return_value=weekly_payload(used_percent=5),
        ):
            response, tool_calls = executor._call_llm_stream(max_retries=3)

        self.assertEqual(response, "continued on codex")
        self.assertEqual(tool_calls, [])
        self.assertEqual(model.backends, [BACKEND_CAPI_MONTHLY, BACKEND_CODEX])
        self.assertEqual(get_current_backend(), BACKEND_CODEX)
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(model.requests[1].messages, model.requests[0].messages)
        self.assertEqual(model.requests[1].system, "system prompt")

    def test_capi_network_error_retries_then_replays_same_request_on_codex(self):
        model = NetworkErrorThenCodexModel()
        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=model,
            system_prompt="system prompt",
            tools=[],
            messages=current_task_messages(),
        )

        with patch.object(executor, "_sleep_with_cancel", return_value=None):
            response, tool_calls = executor._call_llm_stream(max_retries=2)

        self.assertEqual(response, "continued on codex")
        self.assertEqual(tool_calls, [])
        self.assertEqual(model.backends, [BACKEND_CAPI, BACKEND_CAPI, BACKEND_CAPI, BACKEND_CODEX])
        self.assertEqual(get_current_backend(), BACKEND_CODEX)
        self.assertEqual(model.requests[-1].messages, model.requests[0].messages)
        self.assertEqual(model.requests[-1].system, "system prompt")

    def test_monthly_network_error_retries_then_replays_same_request_on_codex(self):
        conf()["llm_backend"] = {
            "current_backend": BACKEND_CAPI_MONTHLY,
            "state_path": str(Path(self.tmp.name) / "state.json"),
            "providers": {
                "capi": {"api_key": "TEST-CAPI-KEY", "model": "gpt-5.5"},
                "capi_monthly": {"api_key": "TEST-MONTHLY-KEY", "model": "gpt-5.5"},
                "codex": {"model": "gpt-5.5", "tools_enabled": True},
            },
        }
        save_state({"current_backend": BACKEND_CAPI_MONTHLY})
        model = NetworkErrorThenCodexModel("ReadTimeout: request timed out")
        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=model,
            system_prompt="system prompt",
            tools=[],
            messages=current_task_messages(),
        )

        with patch.object(executor, "_sleep_with_cancel", return_value=None):
            response, tool_calls = executor._call_llm_stream(max_retries=1)

        self.assertEqual(response, "continued on codex")
        self.assertEqual(tool_calls, [])
        self.assertEqual(model.backends, [BACKEND_CAPI_MONTHLY, BACKEND_CAPI_MONTHLY, BACKEND_CODEX])
        self.assertEqual(get_current_backend(), BACKEND_CODEX)
        self.assertEqual(model.requests[-1].messages, model.requests[0].messages)
        self.assertEqual(model.requests[-1].system, "system prompt")

    def test_capi_network_error_after_partial_text_still_replays_on_codex(self):
        model = PartialTextNetworkErrorThenCodexModel()
        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=model,
            system_prompt="system prompt",
            tools=[],
            messages=current_task_messages(),
        )

        response, tool_calls = executor._call_llm_stream(max_retries=0)

        self.assertEqual(response, "continued on codex")
        self.assertEqual(tool_calls, [])
        self.assertEqual(model.backends, [BACKEND_CAPI, BACKEND_CODEX])
        self.assertEqual(get_current_backend(), BACKEND_CODEX)
        self.assertEqual(model.requests[-1].messages, model.requests[0].messages)
        self.assertEqual(model.requests[-1].system, "system prompt")

    def test_monthly_quota_exhaustion_uses_quota_card_when_codex_over_average_then_codex_if_card_empty(self):
        conf()["llm_backend"] = {
            "current_backend": BACKEND_CAPI_MONTHLY,
            "state_path": str(Path(self.tmp.name) / "state.json"),
            "providers": {
                "capi": {"api_key": "TEST-CAPI-KEY", "model": "gpt-5.5"},
                "capi_monthly": {"api_key": "TEST-MONTHLY-KEY", "model": "gpt-5.5"},
                "codex": {"model": "gpt-5.5", "tools_enabled": True},
            },
        }
        save_state({"current_backend": BACKEND_CAPI_MONTHLY})
        model = MonthlyQuotaThenCapiQuotaThenCodexModel("continued on codex")
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "current travel request"}]},
            {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "bash",
                    "input": {"command": "python skills\\flyai\\scripts\\flyai_wrapper.py search-hotel"},
                }],
            },
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": "{\"ok\": true, \"result\": \"hotel data\"}",
                }],
            },
        ]
        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=model,
            system_prompt="system prompt",
            tools=[],
            messages=messages,
        )

        with patch(
            "agent.protocol.agent_stream.query_codex_quota_json",
            return_value=weekly_payload(used_percent=30),
        ):
            response, tool_calls = executor._call_llm_stream(max_retries=3)

        self.assertEqual(response, "continued on codex")
        self.assertEqual(tool_calls, [])
        self.assertEqual(model.backends, [BACKEND_CAPI_MONTHLY, BACKEND_CAPI, BACKEND_CODEX])
        self.assertEqual(get_current_backend(), BACKEND_CODEX)
        self.assertEqual(len(model.requests), 3)
        self.assertEqual(model.requests[1].messages, model.requests[0].messages)
        self.assertEqual(model.requests[2].messages, model.requests[0].messages)
        self.assertEqual(model.requests[2].system, "system prompt")


if __name__ == "__main__":
    unittest.main()
