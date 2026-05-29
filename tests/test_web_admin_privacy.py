import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.memory.service import MemoryService
from agent.tools.scheduler.task_store import TaskStore
from agent.user_profiles import resolve_single_admin_profile
from channel.web import web_channel
from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.image_recognition import ImageRecognitionManager, reset_image_recognition_manager


def _singleton_class(factory):
    for cell in factory.__closure__ or []:
        value = cell.cell_contents
        if isinstance(value, type):
            return value
    raise AssertionError("singleton class not found")


def _conf_get(workspace):
    def getter(key, default=None):
        return {
            "agent_workspace": workspace,
            "agent_admin_users": ["weixin:admin"],
            "agent_user_profiles": {
                "weixin:admin": {
                    "role": "admin",
                    "memory_user_id": "admin-memory",
                },
                "weixin:normal": {
                    "role": "user",
                    "memory_user_id": "normal-memory",
                },
            },
            "llm_usage_user_labels": {},
            "agent_default_role": "user",
            "agent_user_workspace_root": "",
        }.get(key, default)

    return getter


class WebAdminPrivacyTest(unittest.TestCase):
    def test_single_admin_profile_resolves_configured_memory_user_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("config.conf") as mock_conf:
                mock_conf.return_value.get.side_effect = _conf_get(tmp)

                profile = resolve_single_admin_profile()

            self.assertIsNotNone(profile)
            self.assertEqual(profile.actor_id, "weixin:admin")
            self.assertEqual(profile.memory_user_id, "admin-memory")
            self.assertTrue(profile.is_admin)

    def test_memory_service_can_scope_dreams_to_admin_user_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            shared_dreams = workspace / "memory" / "dreams"
            admin_dreams = workspace / "memory" / "users" / "admin-memory" / "dreams"
            user_dreams = workspace / "memory" / "users" / "normal-memory" / "dreams"
            for folder in (shared_dreams, admin_dreams, user_dreams):
                folder.mkdir(parents=True)
            (shared_dreams / "2026-05-20.md").write_text("shared dream", encoding="utf-8")
            (admin_dreams / "2026-05-21.md").write_text("admin dream", encoding="utf-8")
            (user_dreams / "2026-05-22.md").write_text("user dream", encoding="utf-8")

            service = MemoryService(tmp, user_id="admin-memory", include_shared_memory=False)
            result = service.list_files(category="dream")
            content = service.get_content("2026-05-21.md", category="dream")

            self.assertEqual([item["filename"] for item in result["list"]], ["2026-05-21.md"])
            self.assertIn("admin dream", content["content"])
            with self.assertRaises(FileNotFoundError):
                service.get_content("2026-05-22.md", category="dream")

    def test_memory_service_scopes_memory_files_to_admin_user_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "MEMORY.md").write_text("shared root", encoding="utf-8")
            admin_dir = workspace / "memory" / "users" / "admin-memory"
            user_dir = workspace / "memory" / "users" / "normal-memory"
            admin_dir.mkdir(parents=True)
            user_dir.mkdir(parents=True)
            (admin_dir / "MEMORY.md").write_text("admin memory", encoding="utf-8")
            (admin_dir / "2026-05-21.md").write_text("admin daily", encoding="utf-8")
            (user_dir / "MEMORY.md").write_text("user memory", encoding="utf-8")

            service = MemoryService(tmp, user_id="admin-memory", include_shared_memory=False)
            result = service.list_files(category="memory")
            main = service.get_content("MEMORY.md", category="memory")

            self.assertEqual(
                [item["filename"] for item in result["list"]],
                ["MEMORY.md", "2026-05-21.md"],
            )
            self.assertIn("admin memory", main["content"])
            self.assertNotIn("shared root", main["content"])

    def test_task_store_can_scope_tasks_to_admin_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(os.path.join(tmp, "tasks.json"))
            base = {
                "enabled": True,
                "created_at": "2026-05-24T08:00:00",
                "updated_at": "2026-05-24T08:00:00",
                "schedule": {"type": "cron", "expression": "0 9 * * *"},
                "action": {"type": "send_message"},
                "next_run_at": "2026-05-25T09:00:00",
            }
            store.add_task({**base, "id": "admin-task", "name": "admin", "owner_actor_id": "weixin:admin"})
            store.add_task({**base, "id": "user-task", "name": "user", "owner_actor_id": "weixin:normal"})

            scoped = store.list_tasks_for_owner("weixin:admin")

            self.assertEqual([task["id"] for task in scoped], ["admin-task"])

    def test_web_file_path_fallback_denies_memory_and_allows_output_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            output_file = workspace / "users" / "admin-memory" / "files" / "report.txt"
            memory_file = workspace / "memory" / "users" / "normal-memory" / "MEMORY.md"
            output_file.parent.mkdir(parents=True)
            memory_file.parent.mkdir(parents=True)
            output_file.write_text("report", encoding="utf-8")
            memory_file.write_text("private", encoding="utf-8")

            with patch("channel.web.web_channel.conf") as mock_conf:
                mock_conf.return_value.get.side_effect = _conf_get(tmp)

                self.assertTrue(web_channel._is_allowed_web_file_path(str(output_file)))
                self.assertFalse(web_channel._is_allowed_web_file_path(str(memory_file)))

    def test_web_file_tokens_allow_only_issued_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_file = Path(tmp) / "memory" / "users" / "normal-memory" / "MEMORY.md"
            private_file.parent.mkdir(parents=True)
            private_file.write_text("private", encoding="utf-8")

            url = web_channel._register_web_file(str(private_file))
            token = url.split("token=", 1)[1]

            self.assertEqual(web_channel._resolve_web_file_token(token), str(private_file.resolve()))
            self.assertEqual(web_channel._resolve_web_file_token("missing"), "")

    def test_web_channel_polling_media_reply_registers_download_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "users" / "admin-memory" / "files" / "grok-video-generation" / "job" / "result.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"\x00\x00\x00\x18ftypmp4")

            channel = object.__new__(_singleton_class(web_channel.WebChannel))
            channel.session_queues = {"session": web_channel.Queue()}
            channel.request_to_session = {"request": "session"}
            channel.sse_queues = {}
            context = Context(ContextType.TEXT, "")
            context["request_id"] = "request"

            channel.send(Reply(ReplyType.VIDEO, str(media)), context)

            item = channel.session_queues["session"].get(block=False)
            self.assertEqual(item["type"], "video")
            self.assertEqual(item["file_name"], "result.mp4")
            self.assertTrue(item["content"].startswith("/api/file?token="))
            token = item["content"].split("token=", 1)[1]
            self.assertEqual(web_channel._resolve_web_file_token(token), str(media.resolve()))

    def test_web_uploaded_images_are_cached_for_later_video_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            image = Path(tmp) / "upload.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            manager = ImageRecognitionManager(workspace_root=str(workspace), max_workers=1)
            reset_image_recognition_manager(manager)

            with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
                count = web_channel._register_web_uploaded_images(
                    "web-session",
                    [{"file_type": "image", "file_path": str(image)}],
                )

            refs = manager.recent_image_refs_for_session("web-session", limit=1)
            self.assertEqual(count, 1)
            self.assertEqual(len(refs), 1)
            self.assertTrue(Path(refs[0]).exists())
            reset_image_recognition_manager(None)

    def test_web_admin_context_applies_single_admin_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = Context(ContextType.TEXT, "hello")
            with patch("config.conf") as mock_conf:
                mock_conf.return_value.get.side_effect = _conf_get(tmp)

                web_channel._apply_web_admin_context(context)

            self.assertEqual(context["actor_id"], "weixin:admin")
            self.assertEqual(context["actor_role"], "admin")
            self.assertEqual(context["memory_user_id"], "admin-memory")

    def test_commands_handler_uses_cow_cli_suggestions(self):
        with patch("channel.web.web_channel._require_auth", return_value=None), patch(
            "channel.web.web_channel.web.header",
            return_value=None,
        ), patch(
            "channel.web.web_channel._cow_cli_slash_command_suggestions",
            return_value=[{"cmd": "/grok-direct image -- <prompt>", "desc": "Grok 直出生图"}],
        ):
            body = web_channel.CommandsHandler().GET()

        self.assertIn("/grok-direct image", body)


if __name__ == "__main__":
    unittest.main()
