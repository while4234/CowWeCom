import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.tools.scheduler.integration import _execute_agent_task, _execute_tool_call


class FakeAgentBridge:
    def __init__(self):
        self.context = None

    def agent_reply(self, query, context=None, on_event=None, clear_history=False):
        self.query = query
        self.context = context
        self.clear_history = clear_history
        return SimpleNamespace(content="done")

    def remember_scheduled_output(self, *args, **kwargs):
        self.remembered = (args, kwargs)


class FakeChannel:
    def __init__(self):
        self.sent = []

    def send(self, reply, context):
        self.sent.append((reply, context))


class TestSchedulerExecutionIdentity(unittest.TestCase):
    def test_agent_task_preserves_owner_identity_and_scheduler_session(self):
        bridge = FakeAgentBridge()
        channel = FakeChannel()
        task = {
            "id": "task1",
            "name": "owner task",
            "owner_actor_id": "weixin_user:normal",
            "owner_role": "user",
            "owner_memory_user_id": "normal-memory",
            "action": {
                "type": "agent_task",
                "task_description": "run owner task",
                "receiver": "normal",
                "receiver_name": "Normal User",
                "is_group": False,
                "channel_type": "weixin_user",
                "notify_session_id": "normal",
            },
        }

        with patch("channel.channel_factory.create_channel", return_value=channel):
            _execute_agent_task(task, bridge)

        context = bridge.context
        self.assertIsNotNone(context)
        self.assertEqual(context.get("actor_id"), "weixin_user:normal")
        self.assertEqual(context.get("actor_role"), "user")
        self.assertEqual(context.get("memory_user_id"), "normal-memory")
        self.assertEqual(context.get("channel_type"), "weixin_user")
        self.assertEqual(context.get("session_id"), "scheduler_normal_task1")
        self.assertEqual(context.get("conversation_id"), "scheduler_normal_task1")
        self.assertTrue(context.get("is_scheduled_task"))
        self.assertEqual(len(channel.sent), 1)

    def test_agent_task_without_owner_does_not_execute(self):
        bridge = FakeAgentBridge()
        task = {
            "id": "task1",
            "name": "ownerless task",
            "action": {
                "type": "agent_task",
                "task_description": "run ownerless task",
                "receiver": "normal",
                "is_group": False,
                "channel_type": "weixin_user",
            },
        }

        _execute_agent_task(task, bridge)

        self.assertIsNone(bridge.context)

    def test_tool_call_without_owner_does_not_execute(self):
        bridge = FakeAgentBridge()
        task = {
            "id": "task1",
            "name": "ownerless tool",
            "action": {
                "type": "tool_call",
                "tool_name": "read",
                "tool_params": {"path": "anything.txt"},
                "receiver": "normal",
                "is_group": False,
                "channel_type": "weixin_user",
            },
        }

        with patch("agent.tools.tool_manager.ToolManager.create_tool") as create_tool:
            _execute_tool_call(task, bridge)

        create_tool.assert_not_called()


if __name__ == "__main__":
    unittest.main()
