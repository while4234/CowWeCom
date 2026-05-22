import os
import tempfile
import unittest
from unittest.mock import patch

from bridge.agent_bridge import AgentBridge
from bridge.reply import ReplyType


class TestAgentOnboardingWelcome(unittest.TestCase):
    def test_returns_welcome_for_first_greeting_when_bootstrap_exists(self):
        with tempfile.TemporaryDirectory() as workspace:
            bootstrap_path = os.path.join(workspace, "BOOTSTRAP.md")
            with open(bootstrap_path, "w", encoding="utf-8") as f:
                f.write("pending")

            with patch("bridge.agent_bridge.conf") as mock_conf:
                mock_conf.return_value.get.side_effect = lambda key, default=None: {
                    "agent_workspace": workspace,
                }.get(key, default)

                reply = AgentBridge._try_onboarding_welcome("你好呀")

        self.assertIsNotNone(reply)
        self.assertEqual(reply.type, ReplyType.TEXT)
        self.assertIn("这是我第一次以全新的视角和你聊天", reply.content)
        self.assertIn("你希望给我起个什么名字", reply.content)

    def test_does_not_intercept_task_like_greeting(self):
        with tempfile.TemporaryDirectory() as workspace:
            bootstrap_path = os.path.join(workspace, "BOOTSTRAP.md")
            with open(bootstrap_path, "w", encoding="utf-8") as f:
                f.write("pending")

            with patch("bridge.agent_bridge.conf") as mock_conf:
                mock_conf.return_value.get.side_effect = lambda key, default=None: {
                    "agent_workspace": workspace,
                }.get(key, default)

                reply = AgentBridge._try_onboarding_welcome("你好，帮我查一下日志")

        self.assertIsNone(reply)

    def test_does_not_intercept_when_onboarding_is_complete(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch("bridge.agent_bridge.conf") as mock_conf:
                mock_conf.return_value.get.side_effect = lambda key, default=None: {
                    "agent_workspace": workspace,
                }.get(key, default)

                reply = AgentBridge._try_onboarding_welcome("你好")

        self.assertIsNone(reply)

    def test_blank_workspace_config_falls_back_to_default_workspace(self):
        with tempfile.TemporaryDirectory() as workspace:
            bootstrap_path = os.path.join(workspace, "BOOTSTRAP.md")
            with open(bootstrap_path, "w", encoding="utf-8") as f:
                f.write("pending")

            with patch("bridge.agent_bridge.conf") as mock_conf, \
                    patch("bridge.agent_bridge.expand_path", return_value=workspace) as mock_expand:
                mock_conf.return_value.get.side_effect = lambda key, default=None: {
                    "agent_workspace": "",
                }.get(key, default)

                reply = AgentBridge._try_onboarding_welcome("你好呀！")

        self.assertIsNotNone(reply)
        mock_expand.assert_called_once_with("~/cow")


if __name__ == "__main__":
    unittest.main()
