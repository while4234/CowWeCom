import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.access_control import GuardedTool, ToolAccessPolicy, get_resource_leases
from agent.tools.base_tool import BaseTool, ToolResult
from agent.tools.memory.memory_get import MemoryGetTool
from agent.tools.memory.memory_search import MemorySearchTool
from agent.user_profiles import AgentUserProfile, resolve_agent_user_profile
from bridge.context import Context, ContextType
from agent.memory.storage import SearchResult


class FakeMemoryConfig:
    def __init__(self, workspace):
        self.workspace = Path(workspace)

    def get_workspace(self):
        return self.workspace


class FakeMemoryManager:
    def __init__(self, workspace=None, results=None):
        self.config = FakeMemoryConfig(workspace or tempfile.mkdtemp())
        self.results = results or []

    async def search(self, **kwargs):
        return self.results


class DummyTool(BaseTool):
    name = "dummy"
    description = "dummy"
    params = {"type": "object", "properties": {}}

    def __init__(self, name="dummy", cwd=None):
        super().__init__()
        self.name = name
        self.cwd = cwd or os.getcwd()
        self.called = False

    def execute(self, params):
        self.called = True
        self.last_params = params
        return ToolResult.success("ok")


class MutatingBashTool(DummyTool):
    name = "bash"

    def __init__(self, path_to_mutate, cwd=None):
        super().__init__(name="bash", cwd=cwd)
        self.path_to_mutate = path_to_mutate

    def execute(self, params):
        self.called = True
        with open(self.path_to_mutate, "w", encoding="utf-8") as f:
            f.write("tampered")
        return ToolResult.success("mutated")


def make_profile(role="user", root=None, conversation_id="conv-a", memory_user_id="user_a"):
    root = os.path.abspath(root or tempfile.mkdtemp())
    tool_workspace = os.path.join(root, "users", memory_user_id, "files")
    private_memory = os.path.join(root, "memory", "users", memory_user_id)
    knowledge = os.path.join(root, "knowledge")
    if role == "admin":
        return AgentUserProfile(
            actor_id="weixin:admin",
            raw_user_id="admin",
            display_name="",
            channel_type="weixin",
            role="admin",
            conversation_id=conversation_id,
            memory_user_id=memory_user_id,
            shared_workspace=root,
            tool_workspace=root,
            can_use_bash=True,
            can_use_env_config=True,
        )
    return AgentUserProfile(
        actor_id=f"weixin:{memory_user_id}",
        raw_user_id=memory_user_id,
        display_name="",
        channel_type="weixin",
        role="user",
        conversation_id=conversation_id,
        memory_user_id=memory_user_id,
        shared_workspace=root,
        tool_workspace=tool_workspace,
        readable_roots=(tool_workspace, private_memory, knowledge, os.path.join(root, "skills")),
        writable_roots=(tool_workspace, private_memory),
        denied_roots=(os.path.abspath(os.getcwd()),),
        denied_files=(os.path.join(root, "config.json"),),
        can_use_bash=True,
    )


class TestMultiUserIsolation(unittest.TestCase):
    def test_profile_uses_channel_and_sender_as_actor(self):
        msg = SimpleNamespace(from_user_id="wx-user-a", actual_user_id=None)
        context = Context(ContextType.TEXT, "hi", {
            "channel_type": "weixin",
            "session_id": "legacy-session",
            "msg": msg,
        })

        profile = resolve_agent_user_profile(context)

        self.assertEqual(profile.actor_id, "weixin:wx-user-a")
        self.assertEqual(profile.conversation_id, "weixin:wx-user-a")
        self.assertEqual(profile.role, "user")
        self.assertNotIn(":", profile.memory_user_id)

    @patch("config.conf")
    def test_profile_uses_configured_usage_label_for_display_name(self, mock_conf):
        mock_conf.return_value.get.side_effect = lambda key, default=None: {
            "agent_workspace": tempfile.mkdtemp(),
            "agent_user_profiles": {},
            "llm_usage_user_labels": {"weixin:wx-user-a": "wechat-display-name"},
            "agent_admin_users": [],
            "agent_default_role": "user",
        }.get(key, default)
        msg = SimpleNamespace(from_user_id="wx-user-a", actual_user_id=None)
        context = Context(ContextType.TEXT, "hi", {
            "channel_type": "weixin",
            "msg": msg,
        })

        profile = resolve_agent_user_profile(context)

        self.assertEqual(profile.display_name, "wechat-display-name")

    def test_profile_can_use_explicit_owner_with_separate_conversation(self):
        context = Context(ContextType.TEXT, "scheduled")
        context["channel_type"] = "weixin_user"
        context["session_id"] = "scheduler_normal_task1"
        context["conversation_id"] = "scheduler_normal_task1"
        context["actor_id"] = "weixin_user:normal"
        context["actor_role"] = "user"
        context["memory_user_id"] = "normal-memory"

        profile = resolve_agent_user_profile(context)

        self.assertEqual(profile.actor_id, "weixin_user:normal")
        self.assertEqual(profile.raw_user_id, "normal")
        self.assertEqual(profile.channel_type, "weixin_user")
        self.assertEqual(profile.role, "user")
        self.assertEqual(profile.memory_user_id, "normal-memory")
        self.assertEqual(profile.conversation_id, "scheduler_normal_task1")

    @patch("config.conf")
    def test_normal_user_defaults_can_read_common_attachment_roots_and_write_knowledge(self, mock_conf):
        with tempfile.TemporaryDirectory() as tmp:
            mock_conf.return_value.get.side_effect = lambda key, default=None: {
                "agent_workspace": tmp,
                "agent_user_profiles": {},
                "llm_usage_user_labels": {},
                "agent_admin_users": [],
                "agent_default_role": "user",
            }.get(key, default)
            context = Context(ContextType.TEXT, "hi", {
                "channel_type": "weixin",
                "actor_id": "weixin:trusted-user",
            })

            profile = resolve_agent_user_profile(context)

            common_read_roots = (
                os.path.join(tmp, "tmp"),
                os.path.join(tmp, "downloads"),
                os.path.join(tmp, "attachments"),
                tempfile.gettempdir(),
            )
            for root in common_read_roots:
                self.assertIn(os.path.abspath(root), profile.readable_roots)
            self.assertIn(os.path.abspath(os.path.join(tmp, "knowledge")), profile.writable_roots)
            self.assertTrue(profile.can_use_bash)
            self.assertFalse(profile.can_delete_files)

    def test_memory_search_filters_shared_chat_memory_for_normal_user(self):
        results = [
            SearchResult("MEMORY.md", 1, 2, 0.9, "admin memory", "memory", None),
            SearchResult("memory/users/user_a/MEMORY.md", 1, 2, 0.8, "own", "memory", "user_a"),
            SearchResult("memory/users/user_b/MEMORY.md", 1, 2, 0.7, "other", "memory", "user_b"),
            SearchResult("knowledge/index.md", 1, 2, 0.6, "knowledge", "knowledge", None),
        ]
        tool = MemorySearchTool(
            FakeMemoryManager(results=results),
            user_id="user_a",
            include_shared_memory=False,
        )

        result = tool.execute({"query": "anything"})

        self.assertEqual(result.status, "success")
        self.assertIn("memory/users/user_a/MEMORY.md", result.result)
        self.assertIn("knowledge/index.md", result.result)
        self.assertNotIn("admin memory", result.result)
        self.assertNotIn("memory/users/user_b/MEMORY.md", result.result)

    def test_memory_search_with_shared_scope_excludes_other_private_users(self):
        results = [
            SearchResult("MEMORY.md", 1, 2, 0.9, "root", "memory", None),
            SearchResult("memory/users/admin/MEMORY.md", 1, 2, 0.8, "own", "memory", "admin"),
            SearchResult("memory/users/user_b/MEMORY.md", 1, 2, 0.7, "other", "memory", "user_b"),
            SearchResult("knowledge/index.md", 1, 2, 0.6, "knowledge", "knowledge", None),
        ]
        tool = MemorySearchTool(
            FakeMemoryManager(results=results),
            user_id="admin",
            include_shared_memory=True,
        )

        result = tool.execute({"query": "anything"})

        self.assertEqual(result.status, "success")
        self.assertIn("MEMORY.md", result.result)
        self.assertIn("memory/users/admin/MEMORY.md", result.result)
        self.assertIn("knowledge/index.md", result.result)
        self.assertNotIn("memory/users/user_b/MEMORY.md", result.result)

    def test_memory_get_allows_only_own_memory_and_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            own = root / "memory" / "users" / "user_a"
            other = root / "memory" / "users" / "user_b"
            knowledge = root / "knowledge"
            own.mkdir(parents=True)
            other.mkdir(parents=True)
            knowledge.mkdir()
            (own / "MEMORY.md").write_text("own memory", encoding="utf-8")
            (other / "MEMORY.md").write_text("other memory", encoding="utf-8")
            (knowledge / "index.md").write_text("shared knowledge", encoding="utf-8")
            (root / "MEMORY.md").write_text("root memory", encoding="utf-8")

            tool = MemoryGetTool(
                FakeMemoryManager(workspace=root),
                user_id="user_a",
                allow_shared_memory=False,
            )

            own_result = tool.execute({"path": "MEMORY.md"})
            knowledge_result = tool.execute({"path": "knowledge/index.md"})
            other_result = tool.execute({"path": "memory/users/user_b/MEMORY.md"})

            self.assertEqual(own_result.status, "success")
            self.assertIn("own memory", own_result.result)
            self.assertEqual(knowledge_result.status, "success")
            self.assertIn("shared knowledge", knowledge_result.result)
            self.assertEqual(other_result.status, "error")

    def test_memory_get_with_shared_scope_blocks_other_private_users(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            own = root / "memory" / "users" / "admin"
            other = root / "memory" / "users" / "user_b"
            own.mkdir(parents=True)
            other.mkdir(parents=True)
            (root / "MEMORY.md").write_text("root memory", encoding="utf-8")
            (own / "MEMORY.md").write_text("own memory", encoding="utf-8")
            (other / "MEMORY.md").write_text("other memory", encoding="utf-8")

            tool = MemoryGetTool(
                FakeMemoryManager(workspace=root),
                user_id="admin",
                allow_shared_memory=True,
            )

            root_result = tool.execute({"path": "MEMORY.md"})
            own_result = tool.execute({"path": "memory/users/admin/MEMORY.md"})
            other_result = tool.execute({"path": "memory/users/user_b/MEMORY.md"})

            self.assertEqual(root_result.status, "success")
            self.assertIn("root memory", root_result.result)
            self.assertEqual(own_result.status, "success")
            self.assertIn("own memory", own_result.result)
            self.assertEqual(other_result.status, "error")

    def test_guarded_tool_denies_project_path_for_normal_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(root=tmp)
            tool = DummyTool(name="read", cwd=profile.tool_workspace)
            guarded = GuardedTool(tool, ToolAccessPolicy(profile))

            denied = guarded.execute({"path": os.path.abspath("config.py")})
            allowed = guarded.execute({"path": "note.txt"})

            self.assertEqual(denied.status, "error")
            self.assertIn("权限拒绝", denied.result)
            self.assertEqual(allowed.status, "success")

    def test_guarded_tool_maps_memory_path_to_private_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(root=tmp)
            tool = DummyTool(name="write", cwd=profile.tool_workspace)
            guarded = GuardedTool(tool, ToolAccessPolicy(profile))

            result = guarded.execute({"path": "MEMORY.md", "content": "remember"})

            expected = os.path.join(
                profile.shared_workspace,
                "memory",
                "users",
                profile.memory_user_id,
                "MEMORY.md",
            )
            self.assertEqual(result.status, "success")
            self.assertEqual(tool.last_params["path"], expected)

    def test_normal_user_can_read_shared_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(root=tmp)
            tool = DummyTool(name="read", cwd=profile.tool_workspace)
            guarded = GuardedTool(tool, ToolAccessPolicy(profile))

            result = guarded.execute({"path": os.path.join(tmp, "skills", "image-generation", "SKILL.md")})

            self.assertEqual(result.status, "success")

    def test_normal_user_can_read_workspace_tmp_download_and_write_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(root=tmp)
            profile = AgentUserProfile(
                **{
                    **profile.__dict__,
                    "readable_roots": (
                        *profile.readable_roots,
                        os.path.join(tmp, "tmp"),
                        os.path.join(tmp, "downloads"),
                    ),
                    "writable_roots": (
                        *profile.writable_roots,
                        os.path.join(tmp, "knowledge"),
                    ),
                }
            )
            read_tool = DummyTool(name="read", cwd=profile.tool_workspace)
            write_tool = DummyTool(name="write", cwd=profile.tool_workspace)

            tmp_result = GuardedTool(read_tool, ToolAccessPolicy(profile)).execute(
                {"path": os.path.join(tmp, "tmp", "wx_qr.png")}
            )
            download_result = GuardedTool(read_tool, ToolAccessPolicy(profile)).execute(
                {"path": os.path.join(tmp, "downloads", "invoice.pdf")}
            )
            knowledge_result = GuardedTool(write_tool, ToolAccessPolicy(profile)).execute(
                {"path": os.path.join(tmp, "knowledge", "invoice-notes.md"), "content": "notes"}
            )

            self.assertEqual(tmp_result.status, "success")
            self.assertEqual(download_result.status, "success")
            self.assertEqual(knowledge_result.status, "success")

    def test_normal_user_bash_can_run_skill_but_not_sensitive_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(root=tmp)
            tool = DummyTool(name="bash", cwd=profile.tool_workspace)
            guarded = GuardedTool(tool, ToolAccessPolicy(profile))

            skill_script = os.path.join(tmp, "skills", "image-generation", "scripts", "generate.py")
            allowed = guarded.execute({"command": f'python "{skill_script}" "{{}}"', "timeout": 600})
            denied = guarded.execute({"command": f'type "{os.path.join(tmp, "config.json")}"'})

            self.assertEqual(allowed.status, "success")
            self.assertEqual(denied.status, "error")
            self.assertIn("权限", denied.result)

    def test_normal_user_bash_cannot_delete_files_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(root=tmp)
            tool = DummyTool(name="bash", cwd=profile.tool_workspace)
            guarded = GuardedTool(tool, ToolAccessPolicy(profile))

            denied = guarded.execute({"command": "del downloaded.pdf"})

            self.assertEqual(denied.status, "error")
            self.assertIn("删除文件", denied.result)

    def test_normal_user_bash_cannot_modify_project_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(root=tmp)
            tool = DummyTool(name="bash", cwd=profile.tool_workspace)
            guarded = GuardedTool(tool, ToolAccessPolicy(profile))

            denied = guarded.execute({"command": f'echo x > "{os.path.abspath("agent/access_control.py")}"'})

            self.assertEqual(denied.status, "error")
            self.assertIn("修改项目代码", denied.result)

    def test_normal_user_bash_restores_security_policy_files_if_tampered(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            policy_file = project / "agent" / "access_control.py"
            policy_file.parent.mkdir(parents=True)
            policy_file.write_text("original", encoding="utf-8")

            profile = make_profile(root=tmp)
            tool = MutatingBashTool(str(policy_file), cwd=profile.tool_workspace)
            guarded = GuardedTool(tool, ToolAccessPolicy(profile))

            with patch("agent.access_control._security_project_root", return_value=str(project)):
                result = guarded.execute({"command": "echo normal skill helper"})

            self.assertEqual(result.status, "error")
            self.assertIn("security policy files", result.result)
            self.assertEqual(policy_file.read_text(encoding="utf-8"), "original")

    def test_browser_lease_is_reentrant_and_blocks_other_users(self):
        profile_a = make_profile(conversation_id="conv-a", memory_user_id="user_a")
        profile_b = make_profile(conversation_id="conv-b", memory_user_id="user_b")
        leases = get_resource_leases()
        leases.release_owner("conv-a")
        leases.release_owner("conv-b")

        browser_a = GuardedTool(DummyTool(name="browser"), ToolAccessPolicy(profile_a))
        browser_b = GuardedTool(DummyTool(name="browser"), ToolAccessPolicy(profile_b))

        try:
            first = browser_a.execute({"url": "https://example.com"})
            reentrant = browser_a.execute({"url": "https://example.com/2"})
            blocked = browser_b.execute({"url": "https://example.org"})

            self.assertEqual(first.status, "success")
            self.assertEqual(reentrant.status, "success")
            self.assertEqual(blocked.status, "error")
            self.assertIn("浏览器工具正在被另一个用户使用", blocked.result)
        finally:
            leases.release_owner("conv-a")
            leases.release_owner("conv-b")


if __name__ == "__main__":
    unittest.main()
