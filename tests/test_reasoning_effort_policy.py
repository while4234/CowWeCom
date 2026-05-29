import json
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.protocol.agent_stream import AgentStreamExecutor
from agent.protocol.models import LLMRequest
from bridge.agent_bridge import AgentBridge, AgentLLMModel
from bridge.context import Context
from bridge.reply import ReplyType
from common import reasoning_effort_policy
from common.llm_backend_router import BACKEND_CAPI, BACKEND_GROK
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
    is_group = False

    def __init__(self, response='{"effort":"medium","reason":"simple"}'):
        self.response = response
        self.calls = []

    def call(self, request):
        self.calls.append(request)
        return {
            "choices": [{"message": {"content": self.response}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        }


class FailingPolicyModel(FakePolicyModel):
    def call(self, request):
        self.calls.append(request)
        return {"error": True, "message": "optimizer failed because test"}


class TestReasoningEffortPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_conf = dict(conf())
        conf().clear()
        conf().update({
            "agent_workspace": self.tmp.name,
            "reasoning_effort_policy_enabled": True,
            "reasoning_effort_policy_admin_only": False,
            "reasoning_effort_policy_default_effort": "medium",
            "reasoning_effort_policy_quality_effort": "xhigh",
            "reasoning_effort_policy_audit_enabled": True,
            "reasoning_effort_policy_runtime_auto_optimize_enabled": False,
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

    def test_policy_disabled_for_grok_backend(self):
        model = FakePolicyModel()
        model._active_backend = lambda: BACKEND_GROK

        self.assertIsNone(resolve_reasoning_effort_for_task("write a python script", model))

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

    def test_chinese_simple_explain_uses_medium(self):
        effort, rule = classify_local_task("简单解释一下 token 是什么")

        self.assertEqual(effort, "medium")
        self.assertEqual(rule, "simple_explain")

    def test_chinese_summary_uses_medium(self):
        effort, rule = classify_local_task("总结一下这段话的重点")

        self.assertEqual(effort, "medium")
        self.assertEqual(rule, "short_summary")

    def test_simple_chinese_non_development_tasks_use_medium(self):
        cases = {
            "帮我看看这句话有没有语病：我明天去上海。": "sentence_check",
            "帮我写一条请假短信，说我今天身体不舒服。": "short_writing",
            "给我起三个中文标题，主题是周末整理房间。": "short_writing",
            "这句话是什么意思？": "simple_explain",
            "把这句话翻译成英文：我明天到。": "short_translation",
            "润色这句话：今天会议我会晚点到。": "short_rewrite",
            "帮我想想怎么回复这条消息": "daily_expression_advice",
            "给这个小项目取名": "daily_expression_advice",
            "要上班了 都周一了": "casual_daily_chat",
        }

        for text, expected_rule in cases.items():
            with self.subTest(text=text):
                effort, rule = classify_local_task(text)
                self.assertEqual(effort, "medium")
                self.assertEqual(rule, expected_rule)

    def test_simple_chinese_sentence_check_resolves_medium_without_model_call(self):
        model = FakePolicyModel()
        decision = resolve_reasoning_effort_for_task("帮我看看这句话有没有问题", model)

        self.assertEqual(decision.selected_effort, "medium")
        self.assertEqual(decision.decision_source, "local")
        self.assertEqual(decision.reason, "sentence_check")
        self.assertEqual(model.calls, [])

    def test_chinese_coding_and_debugging_stay_xhigh(self):
        self.assertEqual(classify_local_task("帮我开发一个新功能")[0], "xhigh")
        self.assertEqual(classify_local_task("帮我修复这个报错")[0], "xhigh")

    def test_high_risk_and_quality_first_stay_xhigh(self):
        self.assertEqual(classify_local_task("详细分析这个架构方案")[0], "xhigh")
        self.assertEqual(classify_local_task("删除这个账号的权限")[0], "xhigh")

    def test_quality_first_rules_win_over_simple_medium_rules(self):
        cases = [
            "请翻译这段 Python 代码",
            "简单解释这个 bug 报错",
            "检查这个仓库的文件并提交 commit",
            "请评估这个法律和财务风险",
            "多步骤调用工具检查后台任务",
            "请做详细分析并给出实现方案",
        ]

        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(classify_local_task(text)[0], "xhigh")

    def test_non_admin_applies_policy_when_admin_only_disabled(self):
        model = FakePolicyModel()
        model.actor_role = "user"
        model.is_admin = False

        decision = resolve_reasoning_effort_for_task("你好", model)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.selected_effort, "medium")

    def test_non_admin_does_not_apply_policy_when_admin_only_enabled(self):
        conf()["reasoning_effort_policy_admin_only"] = True
        model = FakePolicyModel()
        model.actor_role = "user"
        model.is_admin = False

        self.assertIsNone(resolve_reasoning_effort_for_task("你好", model))

    def test_audit_log_is_sanitized(self):
        model = FakePolicyModel()
        resolve_reasoning_effort_for_task("帮我看看这句话有没有问题 private marker", model)

        path = reasoning_effort_policy.audit_log_path("private")
        with open(path, "r", encoding="utf-8") as f:
            record = json.loads(f.readline())
        serialized = json.dumps(record, ensure_ascii=False)

        self.assertEqual(record["event_type"], "decision")
        self.assertEqual(record["chat_scope"], "private")
        self.assertEqual(record["local_rule"], "sentence_check")
        self.assertIn("message_features", record)
        self.assertNotIn("classifier_model", record)
        self.assertIn("session_hash", record)
        self.assertIn("user_hash", record)
        self.assertNotIn("session-secret", serialized)
        self.assertNotIn("user-secret", serialized)
        self.assertNotIn("private marker", serialized)
        self.assertNotIn("messages", serialized)
        self.assertNotIn("api_key", serialized)

    def test_group_and_private_decisions_use_separate_logs(self):
        private_model = FakePolicyModel()
        group_model = FakePolicyModel()
        group_model.is_group = True

        resolve_reasoning_effort_for_task("你好", private_model)
        resolve_reasoning_effort_for_task("你好", group_model)

        with open(reasoning_effort_policy.audit_log_path("private"), "r", encoding="utf-8") as f:
            private_record = json.loads(f.readline())
        with open(reasoning_effort_policy.audit_log_path("group"), "r", encoding="utf-8") as f:
            group_record = json.loads(f.readline())

        self.assertEqual(private_record["chat_scope"], "private")
        self.assertEqual(group_record["chat_scope"], "group")
        self.assertNotEqual(
            reasoning_effort_policy.audit_log_path("private"),
            reasoning_effort_policy.audit_log_path("group"),
        )

    def test_task_outcome_records_turns_and_max_turn_exhaustion(self):
        model = FakePolicyModel()
        decision = resolve_reasoning_effort_for_task("write a python script", model)

        reasoning_effort_policy.record_policy_task_outcome(
            decision,
            status="max_turns_exhausted",
            turn_count=3,
            max_turns=3,
            model_adapter=model,
            runtime_stats={
                "tool_attempt_count": 4,
                "tool_attempt_success_count": 2,
                "tool_attempt_error_count": 2,
                "tool_skip_count": 1,
                "tool_failure_class": "repeat_error",
            },
            failure_reason="max_turns_exhausted",
            final_response="partial result",
        )

        with open(reasoning_effort_policy.audit_log_path("private"), "r", encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        outcome = rows[-1]

        self.assertEqual(outcome["event_type"], "task_outcome")
        self.assertEqual(outcome["task_id"], decision.task_id)
        self.assertEqual(outcome["task_status"], "max_turns_exhausted")
        self.assertEqual(outcome["turn_count"], 3)
        self.assertEqual(outcome["max_turns"], 3)
        self.assertTrue(outcome["max_turns_exhausted"])
        self.assertEqual(outcome["tool_attempt_count"], 4)
        self.assertEqual(outcome["tool_failure_class"], "repeat_error")

    def test_auto_optimizer_triggers_after_threshold_without_blocking(self):
        conf()["reasoning_effort_policy_runtime_auto_optimize_enabled"] = True
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

    def test_legacy_auto_optimizer_key_alone_does_not_trigger_runtime_optimizer(self):
        conf()["reasoning_effort_policy_runtime_auto_optimize_enabled"] = False
        conf()["reasoning_effort_policy_auto_optimize_enabled"] = True
        conf()["reasoning_effort_policy_auto_optimize_every_tasks"] = 1

        model = FakePolicyModel()
        with patch.object(reasoning_effort_policy, "run_policy_optimizer_once") as optimizer:
            resolve_reasoning_effort_for_task("please look at whether this sentence has issues", model)

        optimizer.assert_not_called()
        self.assertFalse(os.path.exists(reasoning_effort_policy.learning_buffer_path()))

    def test_optimizer_reads_group_and_private_logs_together(self):
        private_model = FakePolicyModel()
        group_model = FakePolicyModel()
        group_model.is_group = True
        resolve_reasoning_effort_for_task("你好", private_model)
        resolve_reasoning_effort_for_task("你好", group_model)

        report = reasoning_effort_policy.run_policy_optimizer_once(
            model_adapter=FakePolicyModel(response='{"ok":true}'),
            reason="manual",
        )

        self.assertEqual(report["status"], "success")
        self.assertEqual(report["analyzed_records"], 2)

    def test_optimizer_attempt_log_records_success_model_and_effort(self):
        model = FakePolicyModel()
        resolve_reasoning_effort_for_task("你好", model)

        reasoning_effort_policy.run_policy_optimizer_once(
            model_adapter=FakePolicyModel(response='{"ok":true}'),
            reason="manual",
        )

        with open(reasoning_effort_policy.optimizer_attempt_path(), "r", encoding="utf-8") as f:
            attempt = json.loads(f.readline())

        self.assertEqual(attempt["status"], "success")
        self.assertEqual(attempt["optimizer_model"], "gpt-5.5")
        self.assertEqual(attempt["optimizer_reasoning_effort"], "xhigh")
        self.assertNotIn("failure_reason", attempt)

    def test_optimizer_attempt_log_records_failure_reason(self):
        model = FakePolicyModel()
        resolve_reasoning_effort_for_task("你好", model)

        reasoning_effort_policy.run_policy_optimizer_once(
            model_adapter=FailingPolicyModel(),
            reason="manual",
        )

        with open(reasoning_effort_policy.optimizer_attempt_path(), "r", encoding="utf-8") as f:
            attempt = json.loads(f.readline())

        self.assertEqual(attempt["status"], "failed")
        self.assertEqual(attempt["optimizer_model"], "gpt-5.5")
        self.assertIn("optimizer failed", attempt["failure_reason"])

    def test_learned_medium_rule_applies_after_builtin_quality_rules(self):
        os.makedirs(os.path.dirname(reasoning_effort_policy.learned_rules_path()), exist_ok=True)
        with open(reasoning_effort_policy.learned_rules_path(), "w", encoding="utf-8") as f:
            json.dump({
                "version": 1,
                "rules": [
                    {
                        "id": "learned_medium_lunchbox",
                        "enabled": True,
                        "effort": "medium",
                        "name": "lunchbox",
                        "keywords": ["lunchbox"],
                        "max_chars": 120,
                    },
                    {
                        "id": "learned_medium_python",
                        "enabled": True,
                        "effort": "medium",
                        "name": "python_chat",
                        "keywords": ["python"],
                        "max_chars": 120,
                    },
                ],
            }, f)

        self.assertEqual(classify_local_task("lunchbox ideas"), ("medium", "learned_medium_lunchbox"))
        self.assertEqual(classify_local_task("write a python script")[0], "xhigh")

    def test_optimizer_applies_supported_rule_and_deletes_raw_learning_buffer(self):
        conf()["reasoning_effort_policy_runtime_auto_optimize_enabled"] = True
        conf()["reasoning_effort_policy_auto_optimize_enabled"] = True
        conf()["reasoning_effort_policy_auto_apply_min_support"] = 2
        response = json.dumps({
            "summary": "safe local medium rule",
            "rules": [
                {
                    "effort": "medium",
                    "name": "lunchbox_chat",
                    "keywords": ["lunchbox"],
                    "max_chars": 120,
                    "confidence": 0.91,
                    "reason": "short repeated non-code chat",
                }
            ],
        })
        model = FakePolicyModel(response=response)

        first = resolve_reasoning_effort_for_task("lunchbox idea please", model)
        second = resolve_reasoning_effort_for_task("quick lunchbox suggestion", model)
        reasoning_effort_policy.record_policy_task_outcome(first, status="success", turn_count=1, max_turns=3)
        reasoning_effort_policy.record_policy_task_outcome(second, status="success", turn_count=1, max_turns=3)

        self.assertTrue(os.path.exists(reasoning_effort_policy.learning_buffer_path()))

        report = reasoning_effort_policy.run_policy_optimizer_once(
            model_adapter=model,
            reason="manual",
        )

        self.assertEqual(report["status"], "success")
        self.assertEqual(report["candidate_rule_count"], 1)
        self.assertEqual(report["applied_rule_count"], 1)
        self.assertEqual(report["raw_learning_samples_consumed"], 2)
        self.assertFalse(os.path.exists(reasoning_effort_policy.learning_buffer_path()))
        self.assertEqual(classify_local_task("lunchbox recommendation"), ("medium", "learned_medium_lunchbox_chat"))
        self.assertEqual(model.calls[-1].model, "gpt-5.5")
        self.assertEqual(model.calls[-1].reasoning_effort, "xhigh")
        self.assertTrue(model.calls[-1].reasoning_effort_locked)

        with open(reasoning_effort_policy.optimizer_report_path(), "r", encoding="utf-8") as f:
            serialized_report = f.read()
        self.assertNotIn("lunchbox idea please", serialized_report)
        self.assertNotIn("quick lunchbox suggestion", serialized_report)

    def test_optimizer_rejects_medium_rule_without_success_outcomes(self):
        conf()["reasoning_effort_policy_runtime_auto_optimize_enabled"] = True
        conf()["reasoning_effort_policy_auto_optimize_enabled"] = True
        conf()["reasoning_effort_policy_auto_apply_min_support"] = 2
        model = FakePolicyModel(response=json.dumps({
            "rules": [{
                "effort": "medium",
                "name": "bookchat",
                "keywords": ["bookchat"],
                "confidence": 0.9,
            }]
        }))

        resolve_reasoning_effort_for_task("bookchat maybe", model)
        resolve_reasoning_effort_for_task("quick bookchat thought", model)
        report = reasoning_effort_policy.run_policy_optimizer_once(model_adapter=model, reason="manual")

        self.assertEqual(report["status"], "success")
        self.assertEqual(report["applied_rule_count"], 0)
        self.assertEqual(report["rejected_rule_count"], 1)
        self.assertEqual(report["rejected_rules"][0]["reason"], "medium_support_missing_success_evidence")
        self.assertEqual(resolve_reasoning_effort_for_task("bookchat later", model).selected_effort, "xhigh")

    def test_optimizer_rejects_insufficient_support_without_persisting_raw_text(self):
        conf()["reasoning_effort_policy_runtime_auto_optimize_enabled"] = True
        conf()["reasoning_effort_policy_auto_optimize_enabled"] = True
        conf()["reasoning_effort_policy_auto_apply_min_support"] = 2
        model = FakePolicyModel(response=json.dumps({
            "rules": [{
                "effort": "medium",
                "name": "solo_chat",
                "keywords": ["soloquery"],
                "confidence": 0.9,
            }]
        }))

        resolve_reasoning_effort_for_task("soloquery maybe", model)
        report = reasoning_effort_policy.run_policy_optimizer_once(model_adapter=model, reason="manual")

        self.assertEqual(report["status"], "success")
        self.assertEqual(report["applied_rule_count"], 0)
        self.assertEqual(report["rejected_rule_count"], 1)
        self.assertFalse(os.path.exists(reasoning_effort_policy.learning_buffer_path()))
        self.assertEqual(resolve_reasoning_effort_for_task("soloquery later", model).selected_effort, "xhigh")
        with open(reasoning_effort_policy.optimizer_report_path(), "r", encoding="utf-8") as f:
            self.assertNotIn("soloquery maybe", f.read())


class FakeStreamingModel:
    model = "gpt-5.5"
    channel_type = "wecom_bot"
    session_id = "session"
    user_id = "user"
    actor_role = "admin"
    is_admin = True

    def __init__(self):
        self.requests = []
        self.calls = 0

    def call_stream(self, request):
        self.requests.append(request)
        self.calls += 1
        if self.calls == 1:
            yield {
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_for_summary",
                            "type": "function",
                            "function": {
                                "name": "missing_tool",
                                "arguments": "{}",
                            },
                        }]
                    }
                }]
            }
            yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
            return

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
        self.assertEqual(metadata["reasoning_effort_chat_scope"], "private")

    def test_stream_request_gets_sticky_medium_effort(self):
        model = FakeStreamingModel()
        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=model,
            system_prompt="system",
            tools=[],
            max_turns=1,
        )

        self.assertEqual(executor.run_stream("帮我总结一下这句话：今天很好"), "done")

        self.assertEqual(len(model.requests), 2)
        self.assertTrue(all(request.reasoning_effort == "medium" for request in model.requests))
        self.assertTrue(all(request.reasoning_effort_locked for request in model.requests))
        metadata = model.requests[-1].cache_shape_metadata
        self.assertEqual(metadata["reasoning_effort_selected"], "medium")
        self.assertEqual(metadata["reasoning_effort_decision_source"], "local")


class TestAgentBridgeReasoningEffortMetadata(unittest.TestCase):
    def test_agent_bridge_passes_group_scope_to_model_adapter(self):
        fake_agent = SimpleNamespace(
            model=SimpleNamespace(),
            tools=[],
            messages=[{"role": "assistant", "content": "ok"}],
            messages_lock=threading.Lock(),
            _last_run_new_messages=[],
            run_stream=lambda **kwargs: "ok",
        )
        profile = SimpleNamespace(
            actor_id="wecom:user",
            display_name="User",
            memory_user_id="wecom:user",
            role="user",
            is_admin=False,
            conversation_id="wecom:user",
        )
        bridge = AgentBridge.__new__(AgentBridge)
        bridge.get_agent = lambda session_id=None, profile=None: fake_agent
        bridge._try_onboarding_welcome = lambda query, profile=None, **kwargs: None
        bridge._persist_messages = lambda *args, **kwargs: None
        bridge._schedule_mcp_hot_reload = lambda agent: None
        context = Context(kwargs={
            "session_id": "raw-session",
            "channel_type": "wecom_bot",
            "isgroup": True,
        })

        with (
            patch("bridge.agent_bridge.resolve_agent_user_profile", return_value=profile),
            patch("bridge.agent_bridge.apply_profile_to_context"),
        ):
            reply = bridge.agent_reply("hello", context)

        self.assertEqual(reply.type, ReplyType.TEXT)
        self.assertTrue(fake_agent.model.is_group)
        self.assertEqual(fake_agent.model.channel_type, "wecom_bot")
        self.assertEqual(fake_agent.model.session_id, "wecom:user")


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
        self.tmp = tempfile.TemporaryDirectory()
        self.old_conf = dict(conf())
        conf().clear()
        conf().update({
            "model": "gpt-5.5",
            "enable_thinking": False,
            "llm_backend": {"current_backend": BACKEND_CAPI, "state_path": os.path.join(self.tmp.name, "state.json")},
        })

    def tearDown(self):
        conf().clear()
        conf().update(self.old_conf)
        self.tmp.cleanup()

    @staticmethod
    def _attach_fake_bot(adapter, fake_bot, model=None):
        adapter._bot = fake_bot
        adapter._bot_model = model or adapter.model
        adapter._bot_type = adapter._resolve_bot_type(adapter.model)
        adapter._bot_backend = adapter._active_backend()

    def test_call_forwards_request_model_effort_and_timeout(self):
        adapter = AgentLLMModel(None)
        fake_bot = FakeBot()
        self._attach_fake_bot(adapter, fake_bot, model="gpt-5-mini")

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
        self._attach_fake_bot(adapter, fake_bot, model="gpt-5-mini")

        request = LLMRequest(messages=[{"role": "user", "content": "x"}], model="gpt-5-mini")
        request.reasoning_effort_locked = True
        adapter.call(request)

        self.assertEqual(fake_bot.kwargs["model"], "gpt-5-mini")
        self.assertNotIn("reasoning_effort", fake_bot.kwargs)

    def test_locked_medium_effort_overrides_global_xhigh(self):
        conf()["enable_thinking"] = True
        conf()["model_reasoning_effort"] = "xhigh"
        adapter = AgentLLMModel(None)
        fake_bot = FakeBot()
        self._attach_fake_bot(adapter, fake_bot, model="gpt-5-mini")

        request = LLMRequest(
            messages=[{"role": "user", "content": "x"}],
            model="gpt-5-mini",
            reasoning_effort="medium",
        )
        request.reasoning_effort_locked = True
        adapter.call(request)

        self.assertEqual(fake_bot.kwargs["model"], "gpt-5-mini")
        self.assertEqual(fake_bot.kwargs["reasoning_effort"], "medium")

    def test_stream_model_call_counter_refreshes_every_configured_user_visible_call(self):
        from common.llm_backend_router import load_state

        conf()["llm_backend"]["quota_refresh"] = {"enabled": True, "model_call_interval": 2}
        adapter = AgentLLMModel(None)
        fake_bot = FakeBot()
        self._attach_fake_bot(adapter, fake_bot)
        adapter.channel_type = "wecom_bot"
        request = LLMRequest(messages=[{"role": "user", "content": "x"}], model="gpt-5-mini")

        with patch("common.llm_backend_quota_refresh.schedule_backend_quota_refresh") as schedule:
            adapter.call(request)
            self.assertEqual(load_state()["quota_refresh"]["user_visible_model_call_count"], 1)
            schedule.assert_not_called()

            adapter.call(request)

        self.assertEqual(load_state()["quota_refresh"]["user_visible_model_call_count"], 2)
        schedule.assert_called_once()
        self.assertEqual(schedule.call_args.args[0], BACKEND_CAPI)

    def test_silent_model_call_does_not_count_for_quota_refresh(self):
        from common.llm_backend_router import load_state

        conf()["llm_backend"]["quota_refresh"] = {"enabled": True, "model_call_interval": 1}
        adapter = AgentLLMModel(None)
        fake_bot = FakeBot()
        self._attach_fake_bot(adapter, fake_bot)
        request = LLMRequest(
            messages=[{"role": "user", "content": "x"}],
            quota_refresh_silent=True,
        )

        with patch("common.llm_backend_quota_refresh.schedule_backend_quota_refresh") as schedule:
            adapter.call(request)

        self.assertEqual(load_state().get("quota_refresh", {}), {})
        schedule.assert_not_called()

    def test_grok_route_does_not_forward_thinking_or_reasoning_effort(self):
        conf()["enable_thinking"] = True
        conf()["model_reasoning_effort"] = "xhigh"
        adapter = AgentLLMModel(None)
        fake_bot = FakeBot()
        request = LLMRequest(
            messages=[{"role": "user", "content": "x"}],
            reasoning_effort="medium",
        )
        request.reasoning_effort_locked = True

        with patch("bridge.agent_bridge.get_current_backend_for_profile", return_value=BACKEND_GROK), \
             patch.object(adapter, "_create_bot_for_route", return_value=fake_bot):
            adapter.call(request)

        self.assertNotIn("thinking", fake_bot.kwargs)
        self.assertNotIn("reasoning_effort", fake_bot.kwargs)
        self.assertNotIn("reasoning_effort_locked", fake_bot.kwargs)


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
        self.assertEqual(
            bot._resolve_reasoning_effort({
                "reasoning_effort_locked": True,
                "reasoning_effort": "medium",
            }),
            "medium",
        )


if __name__ == "__main__":
    unittest.main()
