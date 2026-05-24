import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import app
from agent.memory import daily_dream_scheduler
from agent.memory.summarizer import MemoryFlushManager


def _scheduler_config(**overrides):
    cfg = {
        "enabled": True,
        "check_time": "00:00",
        "catch_up_on_startup": True,
        "catch_up_days": 1,
        "flush_active_agents": False,
        "include_user_memories": False,
        "state_path": "",
    }
    cfg.update(overrides)
    return cfg


def _write_daily(workspace: Path, day: str, content: str = "- useful memory\n") -> None:
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / f"{day}.md").write_text(
        f"# Daily Memory: {day}\n\n{content}",
        encoding="utf-8",
    )


class DailyMemoryDreamSchedulerTest(unittest.TestCase):
    def test_startup_catches_up_previous_day_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_daily(workspace, "2026-05-23")
            calls = []

            def runner(ws, target, user_id, force):
                calls.append((ws, target, user_id, force))
                return True

            with patch.object(daily_dream_scheduler, "_config", return_value=_scheduler_config()):
                result = daily_dream_scheduler.run_due_memory_dream_jobs(
                    now=datetime(2026, 5, 24, 8, 0, 0),
                    workspace=workspace,
                    runner=runner,
                )
                self.assertEqual(result[0]["decision"], "success")
                self.assertEqual(calls, [(workspace, date(2026, 5, 23), None, False)])

                again = daily_dream_scheduler.run_due_memory_dream_jobs(
                    now=datetime(2026, 5, 24, 8, 1, 0),
                    workspace=workspace,
                    runner=runner,
                )
                self.assertEqual(again[0]["decision"], "skipped")
                self.assertEqual(len(calls), 1)

            state = json.loads(
                (workspace / "memory" / ".deep_dream_scheduler_state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["completed_by_scope"]["shared"], "2026-05-23")

    def test_failed_catchup_does_not_mark_completed_and_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_daily(workspace, "2026-05-23")
            calls = []

            def runner(ws, target, user_id, force):
                calls.append(target)
                return False

            with patch.object(daily_dream_scheduler, "_config", return_value=_scheduler_config()):
                first = daily_dream_scheduler.run_daily_memory_dream_once(
                    now=datetime(2026, 5, 24, 8, 0, 0),
                    workspace=workspace,
                    runner=runner,
                )
                second = daily_dream_scheduler.run_daily_memory_dream_once(
                    now=datetime(2026, 5, 24, 8, 1, 0),
                    workspace=workspace,
                    runner=runner,
                )

            self.assertEqual(first["decision"], "failed")
            self.assertEqual(second["decision"], "failed")
            self.assertEqual(calls, [date(2026, 5, 23), date(2026, 5, 23)])
            state = json.loads(
                (workspace / "memory" / ".deep_dream_scheduler_state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn("shared", state.get("completed_by_scope", {}))

    def test_midnight_job_targets_previous_day_not_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_daily(workspace, "2026-05-23", "- yesterday\n")
            _write_daily(workspace, "2026-05-24", "- today should not be processed\n")
            calls = []

            def runner(ws, target, user_id, force):
                calls.append(target)
                return True

            with patch.object(daily_dream_scheduler, "_config", return_value=_scheduler_config()):
                result = daily_dream_scheduler.run_daily_memory_dream_once(
                    now=datetime(2026, 5, 24, 0, 0, 5),
                    workspace=workspace,
                    runner=runner,
                )

            self.assertEqual(result["target_date"], "2026-05-23")
            self.assertEqual(calls, [date(2026, 5, 23)])


class MemoryFlushManagerDateAnchorTest(unittest.TestCase):
    def test_read_recent_dailies_can_anchor_to_target_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_daily(workspace, "2026-05-23", "- yesterday\n")
            _write_daily(workspace, "2026-05-24", "- today\n")

            manager = MemoryFlushManager(workspace)
            content, has_content = manager._read_recent_dailies(
                lookback_days=1,
                end_date=date(2026, 5, 23),
            )

            self.assertTrue(has_content)
            self.assertIn("yesterday", content)
            self.assertNotIn("today", content)

    def test_write_dream_diary_uses_target_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            manager = MemoryFlushManager(workspace)

            manager._write_dream_diary("dream body", diary_date=date(2026, 5, 23))

            diary = workspace / "memory" / "dreams" / "2026-05-23.md"
            self.assertTrue(diary.is_file())
            self.assertIn("# Dream Diary: 2026-05-23", diary.read_text(encoding="utf-8"))


class AppStartupDailyDreamTest(unittest.TestCase):
    def test_app_run_starts_daily_dream_scheduler_before_channels(self):
        fake_config = {"channel_type": "web", "web_console": False}
        start_mock = Mock()
        started_channels = []

        class FakeChannelManager:
            def start(self, channel_names, first_start=False):
                started_channels.append((channel_names, first_start))

        with patch.object(app, "load_config"), \
            patch.object(app, "sigterm_handler_wrap"), \
            patch.object(app, "conf", return_value=fake_config), \
            patch.object(app, "_sync_builtin_skills"), \
            patch("common.llm_backend_auto_switcher.start_llm_backend_auto_switcher"), \
            patch(
                "agent.memory.daily_dream_scheduler.start_daily_memory_dream_scheduler",
                start_mock,
            ), \
            patch.object(app, "_warmup_mcp_tools"), \
            patch.object(app, "ChannelManager", FakeChannelManager), \
            patch.object(app.time, "sleep", side_effect=KeyboardInterrupt):
            app.run()

        start_mock.assert_called_once()
        self.assertEqual(started_channels, [(["web"], True)])


if __name__ == "__main__":
    unittest.main()
