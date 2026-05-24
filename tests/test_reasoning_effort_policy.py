import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent.protocol.agent_stream import AgentStreamExecutor
from agent.protocol.models import LLMRequest
from bridge.agent_bridge import AgentLLMModel
from common import reasoning_effort_policy
from common.llm_backend_router import BACKEND_CAPI
from common.reasoning_effort_policy import (
    classify_local_task,
    resolve_reasoning_effort_for_task,
)
from config import conf
from models.openai_compatible_bot import OpenAICompatibleBot


class FakePolicyModel:
    channel_type = "wecom_bot"
    session_id = "session-secret"
    user_id = "user-secret"
    actor_role = "admin"
    is_admin = True

    def __init__(self, response='{"effort":"medium","reason":"simple"}'):
        self.response = response
        self.calls = []

    def call(self, request):
        self.calls.append(request)
        return {
            "choices": [{"message": {"content": self.response}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        }


class TestReasoningEffortPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_conf = dict(conf())
        conf().clear()
        conf().update({
            "agent_workspace": self.tmp.name,
            "reasoning_effort_policy_enabled": True,
            "reasoning_effort_policy_admin_only": True,
            "reasoning_effort_policy_default_effort": "medium",
            "reasoning_effort_policy_quality_effort": "xhigh",
            "reasoning_effort_policy_audit_enabled": True,
            "reasoning_effort_policy_auto_optimize_enabled": False,
            "llm_backend": {"current_backend": BACKEND_CAPI, "state_path": os.path.join(self.tmp.name, "state.json")},
            "model": "gpt-5.5",
        })

    def tearDown(self):
        conf().clear()
        conf().update(self.old_conf)
        self.tmp.cleanup()

    def test_local_quality_task_uses_xhigh(self):
        model = FakePolicyModel()
        decision = resolve_reasoning_effort_for_task("帮我修复这个 Python 报错并补测试", model)

        self.assertEqual(decision.selected_effort, "xhigh")
        self.assertEqual(decision.decision_source, "local")
        self.assertEqual(model.calls, [])

    def test_local_simple_im_uses_medium(self):
        effort, rule = classify_local_task("你好")

        self.assertEqual(effort, "medium")
        self.assertEqual(rule, "greeting")

    def test_uncertain_task_defaults_to_local_quality_effort(self):
        model = FakePolicyModel(response='{"effort":"medium","reason":"short"}')
        decision = resolve_reasoning_effort_for_task("please look at whether this sentence has issues", model)

        self.assertEqual(decision.selected_effort, "xhigh")
        self.assertEqual(decision.decision_source, "local")
        self.assertEqual(decision.reason, "uncertain_default_quality")
        self.assertEqual(model.calls, [])

    def test_non_admin_does_not_apply_policy(self):
        model = FakePolicyModel()
        model.actor_role = "user"
        model.is_admin = False

        self.assertIsNone(resolve_reasoning_effort_for_task("你好", model))

    def test_audit_log_is_sanitized(self):
        model = FakePolicyModel()
        resolve_reasoning_effort_for_task("帮我看看这句话有没有问题 private marker", model)

        path = reasoning_effort_policy.audit_log_path()
        with open(path, "r", encoding="utf-8") as f:
            record = json.loads(f.readline())
        serialized = json.dumps(record, ensure_ascii=False)

        self.assertEqual(record["local_rule"], "uncertain_default_quality")
        self.assertNotIn("classifier_model", record)
        self.assertIn("session_hash", record)
        self.assertIn("user_hash", record)
        self.assertNotIn("session-secret", serialized)
        self.assertNotIn("user-secret", serialized)
        self.assertNotIn("private marker", serialized)
        self.assertNotIn("messages", serialized)
        self.assertNotIn("api_key", serialized)

    def test_auto_optimizer_triggers_after_threshold_without_blocking(self):
        conf()["reasoning_effort_policy_auto_optimize_enabled"] = True
        conf()["reasoning_effort_policy_auto_optimize_every_tasks"] = 2
        event = threading.Event()
        calls = []

        def fake_optimizer(**kwargs):
            calls.append(kwargs)
            event.set()
            return {"status": "success"}

        model = FakePolicyModel()
        with patch.object(reasoning_effort_policy, "run_policy_optimizer_once", side_effect=fake_optimizer):
            resolve_reasoning_effort_for_task("你好", model)
            resolve_reasoning_effort_for_task("谢谢", model)

        self.assertTrue(event.wait(1))
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(calls[0]["record_count"], 2)


class FakeStreamingModel:
    model = "gpt-5.5"
    channel_type = "wecom_bot"
    session_id = "session"
    user_id = "user"
    actor_role = "admin"
    is_admin = True

    def __init__(self):
        self.requests = []

    def call_stream(self, request):
        self.requests.append(request)
        yield {"choices": [{"delta": {"content": "done"}, "finish_reason": None}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}


class FakeAgent:
    memory_manager = None
    max_context_tokens = 50000

    def _get_model_context_window(self):
        return 100000

    def _estimate_message_tokens(self, message):
        return len(str(message))


class TestAgentStreamReasoningEffort(unittest.TestCase):
    def setUp(self):
        self.old_conf = dict(conf())
        conf().clear()
        conf().update({
            "reasoning_effort_policy_enabled": True,
            "reasoning_effort_policy_admin_only": True,
            "reasoning_effort_policy_audit_enabled": False,
            "reasoning_effort_policy_default_effort": "medium",
            "reasoning_effort_policy_quality_effort": "xhigh",
            "enable_thinking": True,
            "llm_backend": {"current_backend": BACKEND_CAPI},
            "model": "gpt-5.5",
        })

    def tearDown(self):
        conf().clear()
        conf().update(self.old_conf)

    def test_stream_request_gets_sticky_task_effort(self):
        model = FakeStreamingModel()
        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=model,
            system_prompt="system",
            tools=[],
            max_turns=1,
        )

        self.assertEqual(executor.run_stream("帮我写代码并补测试"), "done")

        self.assertEqual(len(model.requests), 2)
        self.assertTrue(all(request.reasoning_effort == "xhigh" for request in model.requests))
        self.assertTrue(all(request.reasoning_effort_locked for request in model.requests))
        metadata = model.requests[-1].cache_shape_metadata
        self.assertEqual(metadata["reasoning_effort_selected"], "xhigh")
        self.assertEqual(metadata["reasoning_effort_decision_source"], "local")


class FakeBot:
    def __init__(self):
        self.kwargs = None

    def call_with_tools(self, **kwargs):
        self.kwargs = kwargs
        if kwargs.get("stream"):
            return iter([])
        return {"choices": [{"message": {"content": "ok"}}]}


class TestAgentLLMModelReasoningEffort(unittest.TestCase):
    def setUp(self):
        self.old_conf = dict(conf())
        conf().clear()
        conf().update({
            "model": "gpt-5.5",
            "enable_thinking": False,
            "llm_backend": {"current_backend": BACKEND_CAPI},
        })

    def tearDown(self):
        conf().clear()
        conf().update(self.old_conf)

    def test_call_forwards_request_model_effort_and_timeout(self):
        adapter = AgentLLMModel(None)
        fake_bot = FakeBot()
        adapter._bot = fake_bot
        adapter._bot_model = adapter.model
        adapter._bot_type = adapter._resolve_bot_type(adapter.model)

        adapter.call(LLMRequest(
            messages=[{"role": "user", "content": "x"}],
            model="gpt-5-mini",
            reasoning_effort="low",
            request_timeout=0.7,
        ))

        self.assertEqual(fake_bot.kwargs["model"], "gpt-5-mini")
        self.assertEqual(fake_bot.kwargs["reasoning_effort"], "low")
        self.assertEqual(fake_bot.kwargs["request_timeout"], 0.7)

    def test_locked_empty_effort_does_not_fall_back_to_global_xhigh(self):
        conf()["enable_thinking"] = True
        conf()["model_reasoning_effort"] = "xhigh"
        adapter = AgentLLMModel(None)
        fake_bot = FakeBot()
        adapter._bot = fake_bot
        adapter._bot_model = adapter.model
        adapter._bot_type = adapter._resolve_bot_type(adapter.model)

        request = LLMRequest(messages=[{"role": "user", "content": "x"}], model="gpt-5-mini")
        request.reasoning_effort_locked = True
        adapter.call(request)

        self.assertEqual(fake_bot.kwargs["model"], "gpt-5-mini")
        self.assertNotIn("reasoning_effort", fake_bot.kwargs)


class FakeOpenAICompatibleBot(OpenAICompatibleBot):
    def get_api_config(self):
        return {
            "api_key": "test",
            "api_base": "https://example.test",
            "model": "gpt-5.5",
            "default_temperature": 0,
            "default_top_p": 1,
            "default_frequency_penalty": 0,
            "default_presence_penalty": 0,
        }


class TestOpenAICompatibleReasoningEffort(unittest.TestCase):
    def setUp(self):
        self.old_conf = dict(conf())
        conf().clear()
        conf().update({
            "enable_thinking": True,
            "model_reasoning_effort": "xhigh",
            "reasoning_effort": "xhigh",
        })

    def tearDown(self):
        conf().clear()
        conf().update(self.old_conf)

    def test_locked_empty_effort_does_not_fall_back_to_config(self):
        bot = FakeOpenAICompatibleBot()

        self.assertIsNone(bot._resolve_reasoning_effort({"reasoning_effort_locked": True}))
        self.assertEqual(
            bot._resolve_reasoning_effort({
                "reasoning_effort_locked": True,
                "reasoning_effort": "low",
            }),
            "low",
        )


if __name__ == "__main__":
    unittest.main()
