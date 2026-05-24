import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bridge.agent_bridge import AgentBridge
from bridge.context import Context
from bridge.reply import ReplyType


class FakeTool:
    def __init__(self, name):
        self.name = name


class FakePlainTextAgent:
    def __init__(self):
        self.model = SimpleNamespace()
        self.tools = [FakeTool("memory_search"), FakeTool("bash")]
        self.messages = []
        self.messages_lock = threading.Lock()
        self._last_run_new_messages = []
        self.tools_seen_during_run = None

    def run_stream(self, **_kwargs):
        self.tools_seen_during_run = list(self.tools)
        return "ok"


def make_context():
    return Context(kwargs={
        "session_id": "raw-session",
        "channel_type": "wecom_bot",
    })


class TestAgentPlainTextMode(unittest.TestCase):
    def test_classifier_disables_tools_for_self_contained_copywriting(self):
        text = "我堂哥今天孩子出生了是个男孩帮我想个微信文案我要转1200红包"

        self.assertTrue(AgentBridge._should_disable_tools_for_plain_text(text, make_context()))

    def test_classifier_keeps_tools_for_fresh_search_or_project_state(self):
        self.assertFalse(
            AgentBridge._should_disable_tools_for_plain_text("查下今天小红书的热点", make_context())
        )
        self.assertFalse(
            AgentBridge._should_disable_tools_for_plain_text("重启了吗", make_context())
        )
        self.assertFalse(
            AgentBridge._should_disable_tools_for_plain_text("帮我优化一下这个项目的代码", make_context())
        )

    def test_agent_reply_temporarily_removes_tools_for_plain_text_request(self):
        fake_agent = FakePlainTextAgent()
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

        with (
            patch("bridge.agent_bridge.resolve_agent_user_profile", return_value=profile),
            patch("bridge.agent_bridge.apply_profile_to_context"),
        ):
            reply = bridge.agent_reply(
                "我堂哥今天孩子出生了是个男孩帮我想个微信文案我要转1200红包",
                make_context(),
            )

        self.assertEqual(reply.type, ReplyType.TEXT)
        self.assertEqual(fake_agent.tools_seen_during_run, [])
        self.assertEqual([tool.name for tool in fake_agent.tools], ["memory_search", "bash"])
        self.assertFalse(hasattr(fake_agent, "_skip_knowledge_auto_retrieval_once"))

    def test_agent_reply_keeps_tools_for_search_request(self):
        fake_agent = FakePlainTextAgent()
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

        with (
            patch("bridge.agent_bridge.resolve_agent_user_profile", return_value=profile),
            patch("bridge.agent_bridge.apply_profile_to_context"),
        ):
            reply = bridge.agent_reply("查下今天小红书的热点", make_context())

        self.assertEqual(reply.type, ReplyType.TEXT)
        self.assertEqual(
            [tool.name for tool in fake_agent.tools_seen_during_run],
            ["memory_search", "bash"],
        )


if __name__ == "__main__":
    unittest.main()
