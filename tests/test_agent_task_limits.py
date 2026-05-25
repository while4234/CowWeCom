import re
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.protocol.agent import Agent
from agent.chat.service import ChatService
from bridge.agent_bridge import AgentBridge
from bridge.context import Context
from bridge.reply import ReplyType
from common.agent_task_limits import (
    is_complex_planning_task,
    is_development_task,
    resolve_agent_max_steps,
    resolve_agent_task_budget,
)
from common.travel_planning_gate import build_travel_planning_clarification
from config import conf


class TestAgentTaskLimits(unittest.TestCase):
    def test_development_tasks_use_development_step_budget(self):
        settings = {
            "agent_max_steps": 20,
            "agent_development_max_steps": 40,
            "agent_complex_planning_max_steps": 40,
        }

        self.assertEqual(resolve_agent_max_steps("帮我开发代码并补测试", settings), 40)
        self.assertEqual(resolve_agent_max_steps("讲一个简短故事", settings), 20)

    def test_complex_travel_planning_uses_planning_step_budget(self):
        settings = {
            "agent_max_steps": 20,
            "agent_development_max_steps": 40,
            "agent_complex_planning_max_steps": 40,
        }
        prompt = (
            "6月2日从北京出发，先坐高铁去上海，再从上海飞东京玩5天，"
            "请帮我做完整旅行方案，包含高铁余票、机票、东京天气、签证、每日行程、预算和风险。"
        )

        budget = resolve_agent_task_budget(prompt, settings)

        self.assertTrue(is_complex_planning_task(prompt))
        self.assertEqual(budget.max_steps, 40)
        self.assertEqual(budget.kind, "complex_planning")

    def test_plain_language_round_trip_planning_uses_planning_step_budget(self):
        settings = {
            "agent_max_steps": 20,
            "agent_development_max_steps": 40,
            "agent_complex_planning_max_steps": 40,
        }
        prompt = "6月10日从广州去首尔，6月15日回来，两个人，预算1万5，帮我规划一下。"

        budget = resolve_agent_task_budget(prompt, settings)

        self.assertTrue(is_complex_planning_task(prompt))
        self.assertEqual(budget.max_steps, 40)
        self.assertEqual(budget.kind, "complex_planning")

    def test_natural_vague_international_trip_uses_planning_step_budget(self):
        settings = {
            "agent_max_steps": 20,
            "agent_development_max_steps": 40,
            "agent_complex_planning_max_steps": 40,
        }
        prompt = "我想6月下旬从深圳去釜山玩几天，别太赶，帮我规划一下。"

        budget = resolve_agent_task_budget(prompt, settings)

        self.assertTrue(is_complex_planning_task(prompt))
        self.assertEqual(budget.max_steps, 40)
        self.assertEqual(budget.kind, "complex_planning")

    def test_natural_complete_international_trip_uses_planning_step_budget(self):
        settings = {
            "agent_max_steps": 20,
            "agent_development_max_steps": 40,
            "agent_complex_planning_max_steps": 40,
        }
        prompt = "6月18日从深圳去釜山，6月23日回来，2个成年人，中国护照，预算1.6万人民币，想住交通方便的地方，帮我继续做完整方案。"

        budget = resolve_agent_task_budget(prompt, settings)

        self.assertTrue(is_complex_planning_task(prompt))
        self.assertEqual(budget.max_steps, 40)
        self.assertEqual(budget.kind, "complex_planning")

    def test_travel_planning_gate_asks_key_questions_before_tools(self):
        prompt = "我想6月下旬从深圳去釜山玩几天，别太赶，帮我规划一下。"

        clarification = build_travel_planning_clarification(prompt)

        self.assertIsNotNone(clarification)
        assert clarification is not None
        self.assertIn("规划前确认", clarification.message)
        self.assertIn("具体出发和返回日期", clarification.message)
        self.assertIn("一共几位出行", clarification.message)
        self.assertTrue("预算" in clarification.message or "护照" in clarification.message)
        numbered_lines = [
            line for line in clarification.message.splitlines() if re.match(r"^\d+\. ", line)
        ]
        self.assertLessEqual(len(numbered_lines), 3)

    def test_travel_planning_gate_skips_complete_or_explicit_rough_plan(self):
        complete_prompt = "6月18日从深圳去釜山，6月23日回来，2个成年人，中国护照，预算1.6万人民币，想住交通方便的地方，帮我继续做完整方案。"
        rough_prompt = "我想6月下旬从深圳去釜山玩几天，先按假设给我一个粗略方案。"

        self.assertIsNone(build_travel_planning_clarification(complete_prompt))
        self.assertIsNone(build_travel_planning_clarification(rough_prompt))

    def test_travel_planning_gate_skips_commute_route(self):
        self.assertIsNone(build_travel_planning_clarification("明天从家去公司，帮我规划一下路线。"))

    def test_simple_weather_query_stays_on_base_budget(self):
        settings = {
            "agent_max_steps": 20,
            "agent_development_max_steps": 40,
            "agent_complex_planning_max_steps": 40,
        }

        self.assertFalse(is_complex_planning_task("帮我查一下成都天气"))
        self.assertEqual(resolve_agent_task_budget("帮我查一下成都天气", settings).max_steps, 20)

    def test_commute_route_query_stays_on_base_budget(self):
        settings = {
            "agent_max_steps": 20,
            "agent_development_max_steps": 40,
            "agent_complex_planning_max_steps": 40,
        }
        prompt = "明天从家去公司，帮我规划一下路线"

        self.assertFalse(is_complex_planning_task(prompt))
        self.assertEqual(resolve_agent_task_budget(prompt, settings).max_steps, 20)

    def test_development_budget_never_lowers_base_budget(self):
        settings = {
            "agent_max_steps": 60,
            "agent_development_max_steps": 40,
        }

        self.assertEqual(resolve_agent_max_steps("fix this Python bug", settings), 60)

    def test_development_task_detection_covers_code_and_repo_work(self):
        self.assertTrue(is_development_task("重构这个接口并运行 pytest"))
        self.assertTrue(is_development_task("commit and push the repo changes"))
        self.assertFalse(is_development_task("帮我总结今天的天气"))


class TestAgentRunStreamMaxSteps(unittest.TestCase):
    def test_run_stream_accepts_per_run_max_steps_override(self):
        created = []

        class FakeExecutor:
            def __init__(self, **kwargs):
                self.messages = []
                self.max_turns = kwargs["max_turns"]
                created.append(self)

            def run_stream(self, user_message):
                return "done"

        agent = Agent(
            system_prompt="system",
            model=object(),
            tools=[],
            max_steps=20,
            enable_skills=False,
        )
        agent.get_full_system_prompt = lambda skill_filter=None: "system"

        with patch("agent.protocol.agent.AgentStreamExecutor", FakeExecutor):
            self.assertEqual(agent.run_stream("开发代码", max_steps=40), "done")

        self.assertEqual(created[-1].max_turns, 40)


class TestAgentBridgeTaskLimits(unittest.TestCase):
    def setUp(self):
        self.old_conf = dict(conf())
        conf().clear()
        conf().update({
            "agent_max_steps": 20,
            "agent_development_max_steps": 40,
            "agent_complex_planning_max_steps": 40,
            "agent_max_context_turns": 20,
        })

    def tearDown(self):
        conf().clear()
        conf().update(self.old_conf)

    def _make_bridge_and_agent(self):
        captured = {}

        def run_stream(**kwargs):
            captured.update(kwargs)
            return "ok"

        fake_agent = SimpleNamespace(
            model=SimpleNamespace(),
            tools=[],
            messages=[{"role": "assistant", "content": "ok"}],
            messages_lock=threading.Lock(),
            _last_run_new_messages=[],
            run_stream=run_stream,
        )
        profile = SimpleNamespace(
            actor_id="wecom:user",
            display_name="User",
            memory_user_id="wecom:user",
            role="admin",
            is_admin=True,
            conversation_id="wecom:user",
        )
        bridge = AgentBridge.__new__(AgentBridge)
        bridge.get_agent = lambda session_id=None, profile=None: fake_agent
        bridge._try_onboarding_welcome = lambda query, profile=None, **kwargs: None
        bridge._persist_messages = lambda *args, **kwargs: None
        bridge._schedule_mcp_hot_reload = lambda agent: None
        return bridge, profile, captured

    def _make_context(self):
        return Context(kwargs={
            "session_id": "raw-session",
            "channel_type": "wecom_bot",
            "isgroup": False,
        })

    def test_agent_bridge_uses_development_budget_for_code_tasks(self):
        bridge, profile, captured = self._make_bridge_and_agent()

        with (
            patch("bridge.agent_bridge.resolve_agent_user_profile", return_value=profile),
            patch("bridge.agent_bridge.apply_profile_to_context"),
        ):
            reply = bridge.agent_reply("帮我开发代码并补测试", self._make_context())

        self.assertEqual(reply.type, ReplyType.TEXT)
        self.assertEqual(captured["max_steps"], 40)

    def test_agent_bridge_keeps_ordinary_tasks_on_base_budget(self):
        bridge, profile, captured = self._make_bridge_and_agent()

        with (
            patch("bridge.agent_bridge.resolve_agent_user_profile", return_value=profile),
            patch("bridge.agent_bridge.apply_profile_to_context"),
        ):
            reply = bridge.agent_reply("帮我总结一句话", self._make_context())

        self.assertEqual(reply.type, ReplyType.TEXT)
        self.assertEqual(captured["max_steps"], 20)

    def test_agent_bridge_uses_planning_budget_for_complex_travel(self):
        bridge, profile, captured = self._make_bridge_and_agent()
        prompt = "6月10日从广州去首尔，6月15日回来，两个人，中国护照，预算1万5，帮我规划一下。"

        with (
            patch("bridge.agent_bridge.resolve_agent_user_profile", return_value=profile),
            patch("bridge.agent_bridge.apply_profile_to_context"),
        ):
            reply = bridge.agent_reply(prompt, self._make_context())

        self.assertEqual(reply.type, ReplyType.TEXT)
        self.assertEqual(captured["max_steps"], 40)

    def test_agent_bridge_travel_gate_returns_before_agent_loop(self):
        bridge, profile, captured = self._make_bridge_and_agent()

        with (
            patch("bridge.agent_bridge.resolve_agent_user_profile", return_value=profile),
            patch("bridge.agent_bridge.apply_profile_to_context"),
        ):
            reply = bridge.agent_reply(
                "我想6月下旬从深圳去釜山玩几天，别太赶，帮我规划一下。",
                self._make_context(),
            )

        self.assertEqual(reply.type, ReplyType.TEXT)
        self.assertIn("规划前确认", reply.content)
        self.assertEqual(captured, {})


class TestChatServiceTaskLimits(unittest.TestCase):
    def setUp(self):
        self.old_conf = dict(conf())
        conf().clear()
        conf().update({
            "agent_max_steps": 20,
            "agent_development_max_steps": 40,
            "agent_complex_planning_max_steps": 40,
            "agent_max_context_turns": 20,
            "conversation_persistence": False,
        })

    def tearDown(self):
        conf().clear()
        conf().update(self.old_conf)

    def test_chat_service_uses_planning_budget_for_streaming_entrypoint(self):
        captured = {}

        class FakeExecutor:
            def __init__(self, **kwargs):
                captured["max_turns"] = kwargs["max_turns"]
                self.messages = kwargs["messages"]
                self.files_to_send = []

            def run_stream(self, user_message):
                self.messages.append({"role": "user", "content": user_message})
                self.messages.append({"role": "assistant", "content": "ok"})
                return "ok"

        fake_agent = SimpleNamespace(
            model=SimpleNamespace(),
            tools=[],
            messages=[],
            messages_lock=threading.Lock(),
            get_full_system_prompt=lambda: "system",
            _execute_post_process_tools=lambda: None,
        )
        service = ChatService(SimpleNamespace(get_agent=lambda session_id=None: fake_agent))
        prompt = "6月10日从广州去首尔，6月15日回来，两个人，中国护照，预算1万5，帮我规划一下。"

        with (
            patch("agent.protocol.agent_stream.AgentStreamExecutor", FakeExecutor),
            patch.object(ChatService, "_schedule_post_task_self_evolution"),
            patch.object(ChatService, "_collect_tool_error_lesson_snapshot", return_value=None),
            patch.object(ChatService, "_count_tool_error_lesson_changes", return_value=0),
            patch("agent.chat.service.maybe_check_capi_monthly_after_task", return_value={}),
        ):
            service.run(prompt, session_id="chat-session", send_chunk_fn=lambda _chunk: None)

        self.assertEqual(captured["max_turns"], 40)

    def test_chat_service_travel_gate_streams_without_executor(self):
        fake_agent = SimpleNamespace(
            model=SimpleNamespace(),
            tools=[],
            messages=[],
            messages_lock=threading.Lock(),
            get_full_system_prompt=lambda: "system",
            _execute_post_process_tools=lambda: None,
        )
        service = ChatService(SimpleNamespace(get_agent=lambda session_id=None: fake_agent))
        chunks = []

        with (
            patch("agent.protocol.agent_stream.AgentStreamExecutor", side_effect=AssertionError("executor should not run")),
            patch("agent.chat.service.maybe_check_capi_monthly_after_task", return_value={}),
        ):
            response = service.run(
                "我想6月下旬从深圳去釜山玩几天，别太赶，帮我规划一下。",
                session_id="chat-session",
                send_chunk_fn=chunks.append,
            )

        self.assertIn("规划前确认", response)
        self.assertTrue(any("规划前确认" in chunk.get("delta", "") for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
