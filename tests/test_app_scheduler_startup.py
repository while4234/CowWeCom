import unittest
from types import SimpleNamespace
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
