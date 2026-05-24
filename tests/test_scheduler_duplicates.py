import os
import tempfile
import unittest
from types import SimpleNamespace

from agent.protocol.agent_stream import AgentStreamExecutor
from agent.protocol.models import LLMModel
from agent.tools.base_tool import BaseTool, ToolResult
from agent.tools.scheduler.scheduler_tool import SchedulerTool
from agent.tools.scheduler.task_store import TaskStore
from bridge.context import Context, ContextType


class CountingTool(BaseTool):
    name = "dummy"
    description = "dummy"
    params = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
    }

    def __init__(self):
        super().__init__()
        self.calls = 0

    def execute(self, params):
        self.calls += 1
        return ToolResult.success({"calls": self.calls, "params": params})


class DuplicateToolCallModel(LLMModel):
    def __init__(self):
        super().__init__(model="test-model")
        self.calls = 0

    def call_stream(self, request):
        self.calls += 1
        if self.calls == 1:
            yield {
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_a",
                            "type": "function",
                            "function": {
                                "name": "dummy",
                                "arguments": '{"x":1}',
                            },
                        }]
                    }
                }]
            }
            yield {
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 1,
                            "id": "call_b",
                            "type": "function",
                            "function": {
                                "name": "dummy",
                                "arguments": '{"x":1}',
                            },
                        }]
                    }
                }]
            }
            yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
            return

        yield {"choices": [{"delta": {"content": "done"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}


class FakeAgent:
    memory_manager = None
    skill_manager = None
    max_context_tokens = None

    def _estimate_message_tokens(self, msg):
        return len(str(msg))

    def _get_model_context_window(self):
        return 100000


class FakeSchedulerService:
    def __init__(self, result=True):
        self.result = result
        self.run_calls = []

    def run_task_now(self, task_id):
        self.run_calls.append(task_id)
        return self.result


def make_context(
    actor_id="weixin:user-a",
    session_id="session-a",
    actor_role="user",
    memory_user_id="user-a-memory",
    conversation_id=None,
):
    msg = SimpleNamespace(
        sender_staff_id=None,
        other_user_nickname=None,
        from_user_nickname="User A",
    )
    context = Context(ContextType.TEXT, "remind me", {"msg": msg})
    context["receiver"] = "receiver-a"
    context["isgroup"] = False
    context["session_id"] = session_id
    context["actor_id"] = actor_id
    context["actor_role"] = actor_role
    context["memory_user_id"] = memory_user_id
    context["conversation_id"] = conversation_id or actor_id
    return context


class TestSchedulerDuplicates(unittest.TestCase):
    def test_scheduler_create_reuses_recent_identical_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            tool = SchedulerTool({"channel_type": "weixin"})
            tool.task_store = TaskStore(os.path.join(tmp, "tasks.json"))
            tool.current_context = make_context()

            params = {
                "action": "create",
                "name": "drink water",
                "message": "drink water",
                "schedule_type": "once",
                "schedule_value": "+1m",
            }

            first = tool.execute(params)
            second = tool.execute(params)
            tasks = tool.task_store.list_tasks()

            self.assertEqual(first.status, "success")
            self.assertEqual(second.status, "success")
            self.assertEqual(len(tasks), 1)
            self.assertIn(tasks[0]["id"], second.result)
            self.assertIn("未重复创建", second.result)

    def test_scheduler_duplicate_check_is_scoped_to_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(os.path.join(tmp, "tasks.json"))
            params = {
                "action": "create",
                "name": "drink water",
                "message": "drink water",
                "schedule_type": "once",
                "schedule_value": "+1m",
            }

            first_tool = SchedulerTool({"channel_type": "weixin"})
            first_tool.task_store = store
            first_tool.current_context = make_context(actor_id="weixin:user-a")

            second_tool = SchedulerTool({"channel_type": "weixin"})
            second_tool.task_store = store
            second_tool.current_context = make_context(actor_id="weixin:user-b", session_id="session-b")

            first_tool.execute(params)
            second_tool.execute(params)

            self.assertEqual(len(store.list_tasks()), 2)

    def test_scheduler_list_is_scoped_to_current_owner_for_all_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(os.path.join(tmp, "tasks.json"))

            admin_tool = SchedulerTool({"channel_type": "weixin"})
            admin_tool.task_store = store
            admin_tool.current_context = make_context(
                actor_id="weixin:admin",
                actor_role="admin",
                session_id="admin-session",
            )
            admin_tool.execute({
                "action": "create",
                "name": "admin daily",
                "message": "admin report",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
            })

            user_tool = SchedulerTool({"channel_type": "weixin_user"})
            user_tool.task_store = store
            user_tool.current_context = make_context(
                actor_id="weixin_user:normal",
                actor_role="user",
                session_id="normal-session",
            )
            user_tool.execute({
                "action": "create",
                "name": "user gold",
                "message": "gold reminder",
                "schedule_type": "once",
                "schedule_value": "+1m",
            })

            admin_list = admin_tool.execute({"action": "list"}).result
            user_list = user_tool.execute({"action": "list"}).result

            self.assertIn("admin daily", admin_list)
            self.assertNotIn("user gold", admin_list)
            self.assertIn("user gold", user_list)
            self.assertNotIn("admin daily", user_list)

    def test_scheduler_admin_cannot_manage_another_owners_task_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(os.path.join(tmp, "tasks.json"))
            user_tool = SchedulerTool({"channel_type": "weixin_user"})
            user_tool.task_store = store
            user_tool.current_context = make_context(
                actor_id="weixin_user:normal",
                session_id="normal-session",
            )
            user_tool.execute({
                "action": "create",
                "name": "user gold",
                "message": "gold reminder",
                "schedule_type": "once",
                "schedule_value": "+1m",
            })
            user_task = store.list_tasks()[0]

            admin_tool = SchedulerTool({"channel_type": "weixin"})
            admin_tool.task_store = store
            admin_tool.current_context = make_context(
                actor_id="weixin:admin",
                actor_role="admin",
                session_id="admin-session",
            )

            get_result = admin_tool.execute({"action": "get", "task_id": user_task["id"]}).result
            delete_result = admin_tool.execute({"action": "delete", "task_id": user_task["id"]}).result

            self.assertNotIn("user gold", get_result)
            self.assertNotIn("user gold", delete_result)
            self.assertIsNotNone(store.get_task(user_task["id"]))

    def test_scheduler_run_now_requires_owner_and_uses_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(os.path.join(tmp, "tasks.json"))
            service = FakeSchedulerService(result=True)
            user_tool = SchedulerTool({"channel_type": "weixin_user"})
            user_tool.task_store = store
            user_tool.scheduler_service = service
            user_tool.current_context = make_context(
                actor_id="weixin_user:normal",
                session_id="normal-session",
            )
            user_tool.execute({
                "action": "create",
                "name": "user gold",
                "message": "gold reminder",
                "schedule_type": "once",
                "schedule_value": "+1m",
            })
            task = store.list_tasks()[0]

            owner_result = user_tool.execute({"action": "run_now", "task_id": task["id"]}).result

            admin_tool = SchedulerTool({"channel_type": "weixin"})
            admin_tool.task_store = store
            admin_tool.scheduler_service = service
            admin_tool.current_context = make_context(
                actor_id="weixin:admin",
                actor_role="admin",
                session_id="admin-session",
            )
            admin_result = admin_tool.execute({"action": "run_now", "task_id": task["id"]}).result

            self.assertEqual(service.run_calls, [task["id"]])
            self.assertIn("已立即执行任务", owner_result)
            self.assertIn("无权限", admin_result)

    def test_scheduler_skip_pending_clears_only_owned_task_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(os.path.join(tmp, "tasks.json"))
            store.add_task({
                "id": "missed1",
                "name": "missed task",
                "enabled": True,
                "created_at": "2026-05-22T08:00:00",
                "updated_at": "2026-05-22T08:00:00",
                "owner_actor_id": "weixin_user:normal",
                "schedule": {"type": "cron", "expression": "0 9 * * *"},
                "action": {
                    "type": "send_message",
                    "content": "legacy",
                    "receiver": "normal",
                    "notify_session_id": "normal",
                    "channel_type": "weixin_user",
                },
                "next_run_at": "2026-05-25T09:00:00",
                "last_error": "missed scheduled run",
                "last_error_at": "2026-05-24T10:47:33",
                "last_missed_run_at": "2026-05-24T09:00:00",
            })
            owner_tool = SchedulerTool({"channel_type": "weixin_user"})
            owner_tool.task_store = store
            owner_tool.current_context = make_context(actor_id="weixin_user:normal")

            result = owner_tool.execute({"action": "skip_pending", "task_id": "missed1"}).result
            task = store.get_task("missed1")

            self.assertIn("已跳过任务", result)
            self.assertIsNone(task["last_error"])
            self.assertIsNone(task["last_error_at"])
            self.assertIsNone(task["last_missed_run_at"])

    def test_scheduler_create_stores_owner_profile_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            tool = SchedulerTool({"channel_type": "weixin"})
            tool.task_store = TaskStore(os.path.join(tmp, "tasks.json"))
            tool.current_context = make_context(
                actor_id="weixin:user-a",
                actor_role="user",
                memory_user_id="memory-a",
                conversation_id="conversation-a",
            )

            tool.execute({
                "action": "create",
                "name": "snapshot",
                "message": "snapshot",
                "schedule_type": "once",
                "schedule_value": "+1m",
            })

            task = tool.task_store.list_tasks()[0]
            self.assertEqual(task["owner_actor_id"], "weixin:user-a")
            self.assertEqual(task["owner_role"], "user")
            self.assertEqual(task["owner_memory_user_id"], "memory-a")
            self.assertEqual(task["owner_conversation_id"], "conversation-a")

    def test_scheduler_legacy_owner_fallback_uses_notify_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(os.path.join(tmp, "tasks.json"))
            store.add_task({
                "id": "legacy1",
                "name": "legacy task",
                "enabled": True,
                "created_at": "2026-05-22T08:00:00",
                "updated_at": "2026-05-22T08:00:00",
                "schedule": {"type": "cron", "expression": "0 9 * * *"},
                "action": {
                    "type": "send_message",
                    "content": "legacy",
                    "receiver": "legacy-user",
                    "notify_session_id": "legacy-user",
                    "channel_type": "weixin",
                },
                "next_run_at": "2026-05-23T09:00:00",
            })
            owner_tool = SchedulerTool({"channel_type": "weixin"})
            owner_tool.task_store = store
            owner_tool.current_context = make_context(actor_id="weixin:legacy-user")
            other_tool = SchedulerTool({"channel_type": "weixin"})
            other_tool.task_store = store
            other_tool.current_context = make_context(actor_id="weixin:other")

            self.assertIn("legacy task", owner_tool.execute({"action": "list"}).result)
            self.assertNotIn("legacy task", other_tool.execute({"action": "list"}).result)

    def test_scheduler_legacy_task_without_owner_is_not_assigned_from_receiver(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(os.path.join(tmp, "tasks.json"))
            store.add_task({
                "id": "legacy1",
                "name": "legacy task",
                "enabled": True,
                "created_at": "2026-05-22T08:00:00",
                "updated_at": "2026-05-22T08:00:00",
                "schedule": {"type": "cron", "expression": "0 9 * * *"},
                "action": {
                    "type": "send_message",
                    "content": "legacy",
                    "receiver": "legacy-user",
                    "channel_type": "weixin",
                },
                "next_run_at": "2026-05-23T09:00:00",
            })
            tool = SchedulerTool({"channel_type": "weixin"})
            tool.task_store = store
            tool.current_context = make_context(actor_id="weixin:legacy-user")

            self.assertNotIn("legacy task", tool.execute({"action": "list"}).result)

    def test_executor_skips_duplicate_same_turn_tool_call(self):
        tool = CountingTool()
        events = []
        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=DuplicateToolCallModel(),
            system_prompt="system",
            tools=[tool],
            on_event=events.append,
            messages=[],
        )

        response = executor.run_stream("run dummy")

        self.assertEqual(response, "done")
        self.assertEqual(tool.calls, 1)

        tool_result_messages = [
            msg for msg in executor.messages
            if msg.get("role") == "user"
            and isinstance(msg.get("content"), list)
            and any(block.get("type") == "tool_result" for block in msg["content"])
        ]
        self.assertEqual(len(tool_result_messages), 1)
        self.assertEqual(len(tool_result_messages[0]["content"]), 2)
        self.assertEqual(
            {block["tool_use_id"] for block in tool_result_messages[0]["content"]},
            {"call_a", "call_b"},
        )


if __name__ == "__main__":
    unittest.main()
