import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app


class AppSchedulerStartupTest(unittest.TestCase):
    def test_start_scheduler_service_initializes_agent_bridge_once(self):
        bridge = SimpleNamespace(
            get_agent_bridge=lambda: SimpleNamespace(scheduler_initialized=False)
        )

        with patch("bridge.bridge.Bridge", return_value=bridge), patch(
            "agent.tools.scheduler.integration.init_scheduler", return_value=True
        ) as init_scheduler:
            app._start_scheduler_service()

        self.assertEqual(init_scheduler.call_count, 1)
        agent_bridge = init_scheduler.call_args.args[0]
        self.assertTrue(agent_bridge.scheduler_initialized)

    def test_start_scheduler_service_skips_when_already_initialized(self):
        bridge = SimpleNamespace(
            get_agent_bridge=lambda: SimpleNamespace(scheduler_initialized=True)
        )

        with patch("bridge.bridge.Bridge", return_value=bridge), patch(
            "agent.tools.scheduler.integration.init_scheduler"
        ) as init_scheduler:
            app._start_scheduler_service()

        init_scheduler.assert_not_called()

    def test_start_image_generation_recovery_runs_manager_recovery(self):
        manager = SimpleNamespace(recover_unfinished_jobs=Mock(return_value=[]))
        bridge = SimpleNamespace(get_agent_bridge=lambda: object())

        def immediate_thread(target, **kwargs):
            return SimpleNamespace(start=target)

        with patch("app.threading.Thread", side_effect=immediate_thread), patch(
            "bridge.bridge.Bridge", return_value=bridge
        ), patch(
            "agent.tools.image_generation.job_manager.get_image_generation_job_manager",
            return_value=manager,
        ):
            app._start_image_generation_recovery(delay_seconds=0)

        manager.recover_unfinished_jobs.assert_called_once_with(notify=True)


if __name__ == "__main__":
    unittest.main()
