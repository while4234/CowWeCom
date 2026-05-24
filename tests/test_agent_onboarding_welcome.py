import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bridge.agent_initializer import AgentInitializer
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

    def test_does_not_intercept_when_bootstrap_is_stale(self):
        with tempfile.TemporaryDirectory() as workspace:
            bootstrap_path = os.path.join(workspace, "BOOTSTRAP.md")
            with open(bootstrap_path, "w", encoding="utf-8") as f:
                f.write("pending")
            with open(os.path.join(workspace, "USER.md"), "w", encoding="utf-8") as f:
                f.write("# USER.md\n\n- 昵称: Rondle\n")

            with patch("bridge.agent_bridge.conf") as mock_conf:
                mock_conf.return_value.get.side_effect = lambda key, default=None: {
                    "agent_workspace": workspace,
                }.get(key, default)

                reply = AgentBridge._try_onboarding_welcome("你好")

        self.assertIsNone(reply)
        self.assertFalse(os.path.exists(bootstrap_path))

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

    def test_returns_welcome_for_new_profile_without_global_bootstrap(self):
        with tempfile.TemporaryDirectory() as workspace:
            profile = SimpleNamespace(memory_user_id="wecom_new_user", shared_workspace=workspace)

            reply = AgentBridge._try_onboarding_welcome("你好", profile=profile)

            user_file = os.path.join(workspace, "memory", "users", "wecom_new_user", "USER.md")
            self.assertIsNotNone(reply)
            self.assertEqual(reply.type, ReplyType.TEXT)
            self.assertTrue(os.path.exists(user_file))
            with open(user_file, "r", encoding="utf-8") as f:
                self.assertIn("用户基本信息", f.read())

    def test_does_not_intercept_initialized_profile(self):
        with tempfile.TemporaryDirectory() as workspace:
            user_dir = os.path.join(workspace, "memory", "users", "wecom_ready_user")
            os.makedirs(user_dir, exist_ok=True)
            with open(os.path.join(user_dir, "USER.md"), "w", encoding="utf-8") as f:
                f.write("# USER.md\n\n- 称呼: Hao\n- 交流风格: 简洁高效\n")
            profile = SimpleNamespace(memory_user_id="wecom_ready_user", shared_workspace=workspace)

            reply = AgentBridge._try_onboarding_welcome("你好", profile=profile)

            self.assertIsNone(reply)

    def test_does_not_intercept_task_like_greeting_for_new_profile(self):
        with tempfile.TemporaryDirectory() as workspace:
            profile = SimpleNamespace(memory_user_id="wecom_task_user", shared_workspace=workspace)

            reply = AgentBridge._try_onboarding_welcome("你好，帮我查一下日志", profile=profile)

            self.assertIsNone(reply)
            user_file = os.path.join(workspace, "memory", "users", "wecom_task_user", "USER.md")
            self.assertFalse(os.path.exists(user_file))

    def test_existing_placeholder_profile_does_not_repeat_welcome(self):
        with tempfile.TemporaryDirectory() as workspace:
            user_dir = os.path.join(workspace, "memory", "users", "wecom_placeholder_user")
            os.makedirs(user_dir, exist_ok=True)
            with open(os.path.join(user_dir, "USER.md"), "w", encoding="utf-8") as f:
                f.write(AgentBridge._USER_ONBOARDING_TEMPLATE)
            profile = SimpleNamespace(memory_user_id="wecom_placeholder_user", shared_workspace=workspace)

            reply = AgentBridge._try_onboarding_welcome("你好", profile=profile)

            self.assertIsNone(reply)

    def test_existing_conversation_history_does_not_repeat_welcome(self):
        with tempfile.TemporaryDirectory() as workspace:
            profile = SimpleNamespace(
                memory_user_id="wecom_history_user",
                shared_workspace=workspace,
                conversation_id="wecom_bot:history-user",
            )
            fake_store = SimpleNamespace(has_messages=lambda session_id: session_id == "wecom_bot:history-user")

            with patch("agent.memory.get_conversation_store", return_value=fake_store):
                reply = AgentBridge._try_onboarding_welcome("你好", profile=profile)

            user_file = os.path.join(workspace, "memory", "users", "wecom_history_user", "USER.md")
            self.assertIsNone(reply)
            self.assertTrue(os.path.exists(user_file))

    def test_normal_user_prompt_loads_private_user_profile(self):
        with tempfile.TemporaryDirectory() as workspace:
            profile = SimpleNamespace(
                actor_id="wecom_bot:user",
                memory_user_id="wecom_private_user",
                shared_workspace=workspace,
                tool_workspace=os.path.join(workspace, "users", "wecom_private_user", "files"),
                is_admin=False,
            )
            user_dir = os.path.join(workspace, "memory", "users", "wecom_private_user")
            os.makedirs(user_dir, exist_ok=True)
            with open(os.path.join(user_dir, "USER.md"), "w", encoding="utf-8") as f:
                f.write("# USER.md\n\n- 称呼: Rondle\n- 助手称呼: Nico\n")
            initializer = AgentInitializer(
                bridge=None,
                agent_bridge=SimpleNamespace(
                    _ensure_profile_user_file=AgentBridge._ensure_profile_user_file,
                ),
            )

            context_files = initializer._load_context_files(workspace, profile)

            by_path = {context_file.path: context_file.content for context_file in context_files}
            self.assertIn("memory/users/wecom_private_user/USER.md", by_path)
            self.assertIn("Rondle", by_path["memory/users/wecom_private_user/USER.md"])
            self.assertIn("用户静态身份文件", by_path["SESSION.md"])
            self.assertIn("唯一的当前称呼", by_path["SESSION.md"])


if __name__ == "__main__":
    unittest.main()
