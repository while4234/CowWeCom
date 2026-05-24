import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from agent.tools.scheduler.integration import (
    LLM_BACKEND_AUTO_SWITCH_ACTION,
    LLM_BACKEND_AUTO_SWITCH_TASK_ID,
    _current_agent_bridge,
    _execute_agent_task,
    _execute_llm_backend_auto_switch,
    _execute_send_message,
    _execute_tool_call,
    ensure_llm_backend_auto_switch_task,
)
from agent.tools.scheduler.scheduler_tool import SchedulerTool
from agent.tools.scheduler.task_store import TaskStore
from bridge.context import Context, ContextType
from config import conf


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


class FakeBridgeSingleton:
    def __init__(self, agent_bridge):
        self._agent_bridge = agent_bridge

    def get_agent_bridge(self):
        return self._agent_bridge


class FakeChannel:
    def __init__(self):
        self.sent = []

    def send(self, reply, context):
        self.sent.append((reply, context))


class FakeWeixinChannel:
    def __init__(self, result):
        self.result = result
        self.active_sends = []

    def active_send_text_result(self, receiver, text):
        self.active_sends.append((receiver, text))
        return self.result


class FakeWecomChannel:
    def __init__(self, result):
        self.result = result
        self.active_sends = []

    def active_send_text_result(self, receiver, text, is_group=False, **kwargs):
        self.active_sends.append((receiver, text, is_group, kwargs))
        return self.result


class FakeTaskStore:
    def __init__(self):
        self.tasks = []

    def list_tasks(self, enabled_only=False):
        return list(self.tasks)

    def add_task(self, task):
        self.tasks.append(task)


class TestSchedulerExecutionIdentity(unittest.TestCase):
    def test_current_agent_bridge_prefers_latest_bridge_singleton(self):
        captured = FakeAgentBridge()
        latest = FakeAgentBridge()

        with patch("bridge.bridge.Bridge", return_value=FakeBridgeSingleton(latest)):
            self.assertIs(_current_agent_bridge(captured), latest)

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
            result = _execute_agent_task(task, bridge)

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
        self.assertTrue(result)

    def test_agent_task_weixin_uses_running_channel_active_send(self):
        bridge = FakeAgentBridge()
        channel = FakeWeixinChannel({"ok": True, "reason": "sent"})
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

        with patch("agent.tools.scheduler.integration._get_running_channel", return_value=channel), patch(
            "channel.channel_factory.create_channel"
        ) as create_channel:
            result = _execute_agent_task(task, bridge)

        create_channel.assert_not_called()
        self.assertEqual(channel.active_sends, [("normal", "done")])
        self.assertTrue(hasattr(bridge, "remembered"))
        self.assertTrue(result)

    def test_agent_task_weixin_send_failure_does_not_remember_delivery(self):
        bridge = FakeAgentBridge()
        channel = FakeWeixinChannel({"ok": False, "reason": "missing_context_token"})
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

        with patch("agent.tools.scheduler.integration._get_running_channel", return_value=channel):
            result = _execute_agent_task(task, bridge)

        self.assertEqual(channel.active_sends, [("normal", "done")])
        self.assertFalse(hasattr(bridge, "remembered"))
        self.assertFalse(result)

    def test_agent_task_wecom_uses_active_send_result(self):
        bridge = FakeAgentBridge()
        channel = FakeWecomChannel({"ok": True, "reason": "sent"})
        task = {
            "id": "task1",
            "name": "owner task",
            "owner_actor_id": "wecom_bot:normal",
            "owner_role": "user",
            "owner_memory_user_id": "normal-memory",
            "action": {
                "type": "agent_task",
                "task_description": "run owner task",
                "receiver": "normal",
                "receiver_name": "Normal User",
                "is_group": False,
                "channel_type": "wecom_bot",
                "notify_session_id": "normal",
            },
        }

        with patch("agent.tools.scheduler.integration._get_running_channel", return_value=channel), patch(
            "channel.channel_factory.create_channel"
        ) as create_channel:
            result = _execute_agent_task(task, bridge)

        create_channel.assert_not_called()
        self.assertTrue(result)
        self.assertEqual(
            channel.active_sends,
            [("normal", "done", False, {"mention_user_ids": None, "mention_display_names": None})],
        )
        self.assertTrue(hasattr(bridge, "remembered"))

    def test_send_message_wecom_group_passes_mention_metadata(self):
        bridge = FakeAgentBridge()
        channel = FakeWecomChannel({"ok": True, "reason": "sent"})
        task = {
            "id": "task1",
            "name": "owner task",
            "owner_actor_id": "wecom_bot:group:group-alpha",
            "owner_role": "user",
            "owner_memory_user_id": "group-memory",
            "action": {
                "type": "send_message",
                "content": "Riko, 该喝水啦",
                "receiver": "group-alpha",
                "receiver_name": "Nico 之家",
                "is_group": True,
                "channel_type": "wecom_bot",
                "notify_session_id": "group-alpha",
                "mention_user_ids": ["riko-user"],
                "mention_display_names": ["Riko"],
            },
        }

        with patch("agent.tools.scheduler.integration._get_running_channel", return_value=channel):
            result = _execute_send_message(task, bridge)

        self.assertTrue(result)
        self.assertEqual(
            channel.active_sends,
            [
                (
                    "group-alpha",
                    "Riko, 该喝水啦",
                    True,
                    {"mention_user_ids": ["riko-user"], "mention_display_names": ["Riko"]},
                )
            ],
        )

    def test_scheduler_create_resolves_wecom_group_mention_target(self):
        store = FakeTaskStore()
        tool = SchedulerTool({"channel_type": "wecom_bot"})
        tool.task_store = store
        context = Context(ContextType.TEXT, "一分钟后提醒 Riko 喝水")
        context["isgroup"] = True
        context["channel_type"] = "wecom_bot"
        context["receiver"] = "group-alpha"
        context["session_id"] = "group-alpha"
        context["actor_id"] = "wecom_bot:group:group-alpha"
        context["actor_role"] = "user"
        context["memory_user_id"] = "group-memory"
        context["conversation_id"] = "wecom_bot:group:group-alpha"
        context["group_sender_id"] = "sender-a"
        context["group_sender_label"] = "Alice"
        context["group_known_members"] = [
            {"user_id": "riko-user", "name": "Riko"},
            {"user_id": "sender-a", "name": "Alice"},
        ]
        context["msg"] = None
        tool.current_context = context

        result = tool.execute(
            {
                "action": "create",
                "name": "喝水提醒",
                "message": "Riko, 该喝水啦",
                "schedule_type": "once",
                "schedule_value": "+1m",
            }
        )

        self.assertEqual(result.status, "success")
        action = store.tasks[0]["action"]
        self.assertEqual(action["mention_user_ids"], ["riko-user"])
        self.assertEqual(action["mention_display_names"], ["Riko"])
        self.assertNotIn("riko-user", action["content"])

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

        result = _execute_agent_task(task, bridge)

        self.assertIsNone(bridge.context)
        self.assertFalse(result)

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

    def test_registers_llm_backend_auto_switch_as_hidden_system_task_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_llm_backend = conf().get("llm_backend")
            conf()["llm_backend"] = {
                "current_backend": "capi",
                "state_path": os.path.join(tmp, "state.json"),
                "auto_switch": {"enabled": True, "check_time": "00:00"},
            }
            try:
                store = TaskStore(os.path.join(tmp, "tasks.json"))
                now = datetime(2026, 5, 24, 23, 0, 0)

                first = ensure_llm_backend_auto_switch_task(store, now=now)
                second = ensure_llm_backend_auto_switch_task(store, now=now)
                tasks = store.list_tasks()

                self.assertEqual(len(tasks), 1)
                self.assertEqual(first["id"], LLM_BACKEND_AUTO_SWITCH_TASK_ID)
                self.assertEqual(second["id"], LLM_BACKEND_AUTO_SWITCH_TASK_ID)
                self.assertTrue(tasks[0]["system"])
                self.assertTrue(tasks[0]["hidden"])
                self.assertTrue(tasks[0]["enabled"])
                self.assertEqual(tasks[0]["schedule"], {"type": "cron", "expression": "0 0 * * *"})
                self.assertEqual(tasks[0]["action"]["type"], LLM_BACKEND_AUTO_SWITCH_ACTION)
                self.assertNotEqual(tasks[0]["action"]["type"], "agent_task")
                self.assertNotIn("receiver", tasks[0]["action"])
                self.assertNotIn("notify_session_id", tasks[0]["action"])
                self.assertIsNone(tasks[0].get("owner_actor_id"))
            finally:
                if old_llm_backend is None:
                    conf().pop("llm_backend", None)
                else:
                    conf()["llm_backend"] = old_llm_backend

    def test_llm_backend_auto_switch_action_runs_router_without_agent_reply(self):
        bridge = FakeAgentBridge()
        task = {
            "id": LLM_BACKEND_AUTO_SWITCH_TASK_ID,
            "system": True,
            "action": {"type": LLM_BACKEND_AUTO_SWITCH_ACTION},
        }

        with patch("common.llm_backend_auto_switcher.run_once", return_value={"auto": {"last_decision": "kept"}}) as run_once, patch(
            "agent.tools.scheduler.integration._send_scheduler_reply"
        ) as send_reply:
            result = _execute_llm_backend_auto_switch(task)

        self.assertTrue(result)
        run_once.assert_called_once()
        send_reply.assert_not_called()
        self.assertFalse(hasattr(bridge, "query"))


if __name__ == "__main__":
    unittest.main()
