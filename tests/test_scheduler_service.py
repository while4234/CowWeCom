import os
import tempfile
import unittest
from datetime import datetime

from agent.tools.scheduler.scheduler_service import SchedulerService
from agent.tools.scheduler.task_store import TaskStore


def make_task(task_id="task1", schedule=None, next_run_at="2026-05-24T08:05:00"):
    return {
        "id": task_id,
        "name": "A2E check",
        "enabled": True,
        "created_at": "2026-05-23T23:00:00",
        "updated_at": "2026-05-23T23:00:00",
        "owner_actor_id": "wecom_bot:admin",
        "owner_role": "admin",
        "owner_memory_user_id": "admin-memory",
        "owner_conversation_id": "wecom_bot:admin",
        "schedule": schedule or {"type": "cron", "expression": "5 8 * * *"},
        "action": {
            "type": "agent_task",
            "task_description": "run A2E",
            "receiver": "admin",
            "receiver_name": "Admin",
            "is_group": False,
            "channel_type": "wecom_bot",
            "notify_session_id": "admin",
        },
        "next_run_at": next_run_at,
    }


class SchedulerServiceTest(unittest.TestCase):
    def test_recurring_overdue_task_notifies_and_reschedules(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(os.path.join(tmp, "tasks.json"))
            store.add_task(make_task())
            calls = []

            service = SchedulerService(store, calls.append)
            service._check_and_execute_tasks(datetime(2026, 5, 24, 10, 47, 33))

            task = store.get_task("task1")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["action"]["type"], "send_message")
            self.assertIn("定时任务未执行", calls[0]["action"]["content"])
            self.assertEqual(task["next_run_at"], "2026-05-25T08:05:00")
            self.assertEqual(task["last_missed_run_at"], "2026-05-24T08:05:00")
            self.assertIn("missed scheduled run", task["last_error"])
            self.assertNotIn("last_run_at", task)

    def test_one_time_overdue_task_notifies_and_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(os.path.join(tmp, "tasks.json"))
            store.add_task(make_task(
                schedule={"type": "once", "run_at": "2026-05-24T08:05:00"},
                next_run_at="2026-05-24T08:05:00",
            ))
            calls = []

            service = SchedulerService(store, calls.append)
            service._check_and_execute_tasks(datetime(2026, 5, 24, 10, 47, 33))

            task = store.get_task("task1")
            self.assertEqual(len(calls), 1)
            self.assertFalse(task["enabled"])
            self.assertEqual(task["last_missed_run_at"], "2026-05-24T08:05:00")
            self.assertIn("你可以回复让我现在补跑", calls[0]["action"]["content"])

    def test_failed_due_task_notifies_and_advances_recurring_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(os.path.join(tmp, "tasks.json"))
            store.add_task(make_task())
            calls = []

            def execute(task):
                calls.append(task)
                return task["action"]["type"] == "send_message"

            service = SchedulerService(store, execute)
            service._check_and_execute_tasks(datetime(2026, 5, 24, 8, 5, 1))

            task = store.get_task("task1")
            self.assertEqual([call["action"]["type"] for call in calls], ["agent_task", "send_message"])
            self.assertIn("定时任务执行失败", calls[1]["action"]["content"])
            self.assertEqual(task["next_run_at"], "2026-05-25T08:05:00")
            self.assertIn("scheduled task execution returned failure", task["last_error"])
            self.assertNotIn("last_run_at", task)

    def test_invalid_next_run_notifies_and_disables_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(os.path.join(tmp, "tasks.json"))
            store.add_task(make_task(next_run_at="not-a-date"))
            calls = []

            service = SchedulerService(store, calls.append)
            service._check_and_execute_tasks(datetime(2026, 5, 24, 8, 5, 1))

            task = store.get_task("task1")
            self.assertEqual(len(calls), 1)
            self.assertFalse(task["enabled"])
            self.assertIn("定时任务已暂停", calls[0]["action"]["content"])
            self.assertIn("invalid next_run_at", task["last_error"])


if __name__ == "__main__":
    unittest.main()
