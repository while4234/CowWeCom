import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "skills" / "work-progress-reporter" / "scripts" / "work_progress.py"


def load_module():
    spec = importlib.util.spec_from_file_location("work_progress_reporter", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

class WorkProgressReporterTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, *args):
        parser = self.module.build_parser()
        parsed = parser.parse_args([
            *args,
            "--workspace",
            self.workspace,
            "--memory-user-id",
            "user_a",
            "--actor-id",
            "wecom_bot:user-a",
        ])
        return parsed.func(parsed)

    def test_rejects_empty_or_path_like_memory_user_id(self):
        with self.assertRaises(self.module.WorkProgressError):
            self.module.resolve_scope(self.workspace, "")
        with self.assertRaises(self.module.WorkProgressError):
            self.module.resolve_scope(self.workspace, "../user_b")
        with self.assertRaises(self.module.WorkProgressError):
            self.module.resolve_scope(self.workspace, "user/b")

    def test_privacy_notice_does_not_create_user_state(self):
        parser = self.module.build_parser()
        parsed = parser.parse_args(["privacy-notice"])

        result = parsed.func(parsed)

        self.assertTrue(result["private_only"])
        self.assertEqual(result["message"], self.module.PRIVACY_NOTICE)
        self.assertFalse((Path(self.workspace) / "memory").exists())

    def test_init_user_creates_current_week_state(self):
        result = self.command("init-user", "--date", "2026-05-26")

        self.assertTrue(result["ok"])
        self.assertFalse(result["has_current_week_plan"])
        status = self.command("get-status", "--date", "2026-05-26")
        self.assertEqual(status["week_key"], "2026-W22")

    def test_user_state_is_partitioned_by_memory_user_id(self):
        tasks_a = json.dumps([{"title": "A任务", "current_percent": 10, "target_percent": 80}], ensure_ascii=False)
        tasks_b = json.dumps([{"title": "B任务", "current_percent": 40, "target_percent": 60}], ensure_ascii=False)

        self.command("set-week-plan", "--tasks-json", tasks_a, "--date", "2026-05-26")

        parser = self.module.build_parser()
        parsed = parser.parse_args([
            "set-week-plan",
            "--workspace",
            self.workspace,
            "--memory-user-id",
            "user_b",
            "--actor-id",
            "wecom_bot:user-b",
            "--tasks-json",
            tasks_b,
            "--date",
            "2026-05-26",
        ])
        parsed.func(parsed)

        status_a = self.command("get-status", "--date", "2026-05-26")
        self.assertEqual(status_a["tasks"][0]["title"], "A任务")

        state_a = Path(self.workspace) / "memory" / "users" / "user_a" / "work-progress" / "state.json"
        state_b = Path(self.workspace) / "memory" / "users" / "user_b" / "work-progress" / "state.json"
        self.assertTrue(state_a.exists())
        self.assertTrue(state_b.exists())
        self.assertNotEqual(state_a.read_text(encoding="utf-8"), state_b.read_text(encoding="utf-8"))

    def test_first_week_plan_records_tasks_weekend_and_risk(self):
        tasks = json.dumps([
            {"title": "需求开发", "current_percent": 20, "target_percent": 80},
            {"title": "联调验证", "current_percent": 10, "target_percent": 50},
        ], ensure_ascii=False)

        result = self.command(
            "set-week-plan",
            "--tasks-json",
            tasks,
            "--weekend-days",
            "周六,周日",
            "--date",
            "2026-05-26",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["week_key"], "2026-W22")
        self.assertEqual(result["weekend_overtime_days"], ["saturday", "sunday"])
        self.assertEqual(len(result["tasks"]), 2)
        self.assertIn("低于本周目标", result["risk_hints"][0])

    def test_daily_checkin_records_progress_learnings_blockers_and_new_tasks(self):
        tasks = json.dumps([{"title": "需求开发", "current_percent": 20, "target_percent": 80}], ensure_ascii=False)
        self.command("set-week-plan", "--tasks-json", tasks, "--date", "2026-05-26")

        updates = json.dumps([{"title": "需求开发", "current_percent": 55}], ensure_ascii=False)
        new_tasks = json.dumps([{"title": "线上问题排查", "current_percent": 0, "target_percent": 100}], ensure_ascii=False)
        result = self.command(
            "record-checkin",
            "--progress-text",
            "完成接口开发",
            "--learnings",
            "熟悉了调度隔离逻辑",
            "--blockers",
            "等待联调环境",
            "--task-updates-json",
            updates,
            "--new-tasks-json",
            new_tasks,
            "--date",
            "2026-05-27",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["tasks"][0]["current_percent"], 55)
        self.assertTrue(any(task["title"] == "线上问题排查" for task in result["tasks"]))
        self.assertEqual(result["checkin"]["learnings"], "熟悉了调度隔离逻辑")
        self.assertEqual(result["checkin"]["blockers"], "等待联调环境")

    def test_friday_report_contains_required_sections(self):
        tasks = json.dumps([{"title": "需求开发", "current_percent": 20, "target_percent": 80}], ensure_ascii=False)
        self.command("set-week-plan", "--tasks-json", tasks, "--date", "2026-05-26")
        self.command(
            "record-checkin",
            "--progress-text",
            "完成主要功能",
            "--learnings",
            "沉淀了周报模板",
            "--new-tasks-json",
            json.dumps([{"title": "临时支持", "current_percent": 100, "target_percent": 100}], ensure_ascii=False),
            "--date",
            "2026-05-29",
        )

        result = self.command("generate-report", "--date", "2026-05-29")
        report = result["report_markdown"]

        self.assertIn("## 本周工作内容", report)
        self.assertIn("## 进度情况", report)
        self.assertIn("## 新增/临时任务", report)
        self.assertIn("## 风险与未达项", report)
        self.assertIn("## 本周收获", report)
        self.assertIn("## 下周计划与目标进度", report)
        self.assertIn("需求开发", report)
        self.assertIn("临时支持", report)
        self.assertIn("沉淀了周报模板", report)

    def test_weekend_checkin_can_be_recorded_into_next_week(self):
        tasks = json.dumps([{"title": "周末验证", "current_percent": 0, "target_percent": 50}], ensure_ascii=False)
        self.command("set-week-plan", "--tasks-json", tasks, "--weekend-days", "周六", "--date", "2026-05-29")

        result = self.command(
            "record-checkin",
            "--progress-text",
            "周六完成冒烟验证",
            "--next-week",
            "--date",
            "2026-05-30",
        )

        self.assertEqual(result["week_key"], "2026-W23")
        status = self.command("get-status", "--date", "2026-06-01")
        self.assertEqual(status["week_key"], "2026-W23")
        self.assertEqual(status["checkin_count"], 1)

    def test_schedule_plan_outputs_fixed_and_conditional_actions(self):
        tasks = json.dumps([{"title": "需求开发", "current_percent": 20, "target_percent": 80}], ensure_ascii=False)
        self.command("set-week-plan", "--tasks-json", tasks, "--weekend-days", "周六", "--date", "2026-05-26")

        result = self.command("schedule-plan", "--date", "2026-05-26")
        creates = [item for item in result["scheduler_actions"] if item["op"] == "create"]

        self.assertTrue(any(item["schedule_value"] == "0 10 * * 2-5" for item in creates))
        self.assertTrue(any(item["schedule_value"] == "0 16 * * 5" for item in creates))
        self.assertTrue(any(item.get("task_key") == "weekend_overtime_saturday" for item in creates))
        self.assertFalse(any(item.get("task_key") == "monday_plan_fallback" for item in creates))

    def test_scheduler_task_ids_can_be_saved_and_removed(self):
        self.command(
            "save-scheduler-task",
            "--task-key",
            "weekend_overtime_saturday",
            "--task-id",
            "abc123",
            "--name",
            "周末加班进度提醒",
            "--run-at",
            "2026-05-30T17:00:00",
            "--date",
            "2026-05-26",
        )

        scope = self.module.resolve_scope(self.workspace, "user_a")
        state = self.module.load_state(scope)
        self.assertEqual(state["scheduler"]["weekend_overtime_saturday"]["task_id"], "abc123")
        self.assertEqual(state["scheduler"]["weekend_overtime_saturday"]["run_at"], "2026-05-30T17:00:00")

        result = self.command(
            "remove-scheduler-task",
            "--task-key",
            "weekend_overtime_saturday",
            "--date",
            "2026-05-26",
        )

        self.assertTrue(result["ok"])
        self.assertNotIn("weekend_overtime_saturday", result["scheduler"])


if __name__ == "__main__":
    unittest.main()
