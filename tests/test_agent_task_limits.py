import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.protocol.agent import Agent
from bridge.agent_bridge import AgentBridge
from bridge.context import Context
from bridge.reply import ReplyType
from common.agent_task_limits import (
    is_complex_planning_task,
    is_development_task,
    resolve_agent_max_steps,
    resolve_agent_task_budget,
)
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

    def test_simple_weather_query_stays_on_base_budget(self):
        settings = {
            "agent_max_steps": 20,
            "agent_development_max_steps": 40,
            "agent_complex_planning_max_steps": 40,
        }

        self.assertFalse(is_complex_planning_task("帮我查一下成都天气"))
        self.assertEqual(resolve_agent_task_budget("帮我查一下成都天气", settings).max_steps, 20)

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
        prompt = "上海飞东京5天，帮我做完整旅行方案，包含机票、酒店、天气、签证、预算和风险"

        with (
            patch("bridge.agent_bridge.resolve_agent_user_profile", return_value=profile),
            patch("bridge.agent_bridge.apply_profile_to_context"),
        ):
            reply = bridge.agent_reply(prompt, self._make_context())

        self.assertEqual(reply.type, ReplyType.TEXT)
        self.assertEqual(captured["max_steps"], 40)


if __name__ == "__main__":
    unittest.main()
