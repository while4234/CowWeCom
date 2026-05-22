import tempfile
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.memory.config import MemoryConfig, set_global_memory_config
from agent.social_bridge.service import SocialBridgeService
from agent.social_bridge.store import BridgeStore


class FakeRouter:
    def __init__(self, delivered=True):
        self.delivered = delivered
        self.sent = []

    def send_text(self, target, text):
        self.sent.append((target, text))
        if self.delivered:
            return {"delivered": True, "reason": "sent"}
        return {"delivered": False, "reason": "unreachable"}


class FakeModel:
    def __init__(self, text):
        self.text = text
        self.requests = []

    def call(self, request):
        self.requests.append(request)
        return {"choices": [{"message": {"content": self.text}}]}


class TestSocialBridgeService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.workspace = Path(self.tmp.name)
        set_global_memory_config(MemoryConfig(workspace_root=str(self.workspace)))
        self.store = BridgeStore()
        self.store.register_user(
            "weixin:a",
            "memory_a",
            "Rondle",
            {
                "channel_type": "weixin",
                "wechat_id": "rondle_wx",
                "receiver": "raw-a",
                "context_token": "ctx-a",
                "can_active_send": True,
            },
        )
        self.store.register_user(
            "weixin:b",
            "memory_b",
            "Alice",
            {
                "channel_type": "weixin_user",
                "wechat_id": "alice_wx",
                "receiver": "raw-b",
                "context_token": "ctx-b",
                "can_active_send": True,
            },
        )
        self.config_patch = patch(
            "agent.social_bridge.service.conf",
            return_value={
                "social_bridge_enabled": True,
                "social_bridge_auto_send": True,
                "social_bridge_max_users": 100,
            },
        )
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        set_global_memory_config(MemoryConfig())
        self.tmp.cleanup()

    def test_list_users_excludes_actor_and_masks_display_name(self):
        service = SocialBridgeService(self.store, FakeRouter())

        result = service.list_users("weixin:a")

        self.assertTrue(result["enabled"])
        self.assertEqual(len(result["users"]), 1)
        self.assertEqual(result["users"][0]["bridge_user_id"], service._public_user_id("weixin:b"))
        self.assertNotIn("actor_user_id", result["users"][0])
        self.assertEqual(result["users"][0]["wechat_id"], "alice_wx")
        self.assertEqual(result["users"][0]["known_names"], ["Alice"])
        self.assertEqual(result["users"][0]["display_label"], "alice_wx (Alice)")
        self.assertEqual(result["users"][0]["relationship_to_viewer"], "")
        self.assertNotIn("display_name", result["users"][0])
        self.assertEqual(result["users"][0]["channel_type"], "weixin_user")

    def test_list_users_uses_profile_name_when_wechat_id_is_unavailable(self):
        self.store.register_user(
            "weixin:c",
            "memory_c",
            "raw-user@im.wechat",
            {
                "channel_type": "weixin_user",
                "receiver": "raw-c",
                "context_token": "ctx-c",
                "can_active_send": True,
            },
        )
        memory_dir = self.workspace / "memory" / "users" / "memory_c"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "USER.md").write_text("用户希望被称为「小栀」。\n", encoding="utf-8")
        (memory_dir / "MEMORY.md").write_text("用户称呼：小栀。\n", encoding="utf-8")
        service = SocialBridgeService(self.store, FakeRouter())

        result = service.list_users("weixin:a")

        public_id = service._public_user_id("weixin:c")
        user = next(item for item in result["users"] if item["bridge_user_id"] == public_id)
        self.assertEqual(user["wechat_id"], "")
        self.assertEqual(user["known_names"], ["小栀"])
        self.assertEqual(user["display_label"], "小栀")

    def test_list_users_ignores_identity_template_placeholders(self):
        self.store.register_user(
            "weixin:c",
            "memory_c",
            "raw-user@im.wechat",
            {
                "channel_type": "weixin_user",
                "receiver": "raw-c",
                "context_token": "ctx-c",
                "can_active_send": True,
            },
        )
        memory_dir = self.workspace / "memory" / "users" / "memory_c"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "USER.md").write_text("- **称呼**: *(用户希望被如何称呼)*\n", encoding="utf-8")
        service = SocialBridgeService(self.store, FakeRouter())

        result = service.list_users("weixin:a")

        public_id = service._public_user_id("weixin:c")
        user = next(item for item in result["users"] if item["bridge_user_id"] == public_id)
        self.assertEqual(user["known_names"], [])
        self.assertEqual(user["display_label"], "未知微信用户")

    def test_send_message_uses_model_rewrite_and_marks_sent(self):
        router = FakeRouter(delivered=True)
        model = FakeModel("这是 A 授权我转述给你的：他想认真道歉，也希望你们能好好聊聊。")
        service = SocialBridgeService(self.store, router)
        service.set_relationship("weixin:a", "weixin:b", "夫妻", "最近因为晚归吵过架")
        actor_memory = self.workspace / "memory" / "users" / "memory_a" / "MEMORY.md"
        target_memory = self.workspace / "memory" / "users" / "memory_b" / "MEMORY.md"
        actor_memory.parent.mkdir(parents=True, exist_ok=True)
        target_memory.parent.mkdir(parents=True, exist_ok=True)
        actor_memory.write_text("A 最近反复提到想认真道歉。", encoding="utf-8")
        target_memory.write_text("B 更希望被温和地理解。", encoding="utf-8")

        result = service.send_message(
            "weixin:a",
            "weixin:b",
            "我很抱歉，想和好。",
            model=model,
        )

        self.assertTrue(result["delivered"])
        self.assertEqual(result["status"], "sent")
        self.assertEqual(router.sent[0][1], model.text)
        self.assertIn("夫妻", model.requests[0].messages[0]["content"])
        self.assertIn("A 最近反复提到想认真道歉", model.requests[0].messages[0]["content"])
        self.assertIn("B 更希望被温和地理解", model.requests[0].messages[0]["content"])
        self.assertIn("最近因为晚归吵过架", model.requests[0].messages[0]["content"])
        self.assertEqual(self.store.list_pending_for_actor("weixin:b"), [])

    def test_send_message_falls_back_without_model_and_keeps_privacy_boundary(self):
        router = FakeRouter(delivered=True)
        service = SocialBridgeService(self.store, router)

        result = service.send_message("weixin:a", "weixin:b", "请告诉 B 我会按时到。")

        sent_text = router.sent[0][1]
        self.assertTrue(result["delivered"])
        self.assertIn("明确授权我转述", sent_text)
        self.assertIn("请告诉 B 我会按时到。", sent_text)

    def test_send_message_becomes_pending_when_target_unreachable(self):
        router = FakeRouter(delivered=False)
        service = SocialBridgeService(self.store, router)

        result = service.send_message("weixin:a", "weixin:b", "稍后见。")
        pending = service.pending_messages("weixin:b")

        self.assertFalse(result["delivered"])
        self.assertEqual(result["status"], "pending")
        self.assertEqual(len(pending["messages"]), 1)
        self.assertEqual(pending["messages"][0]["from"]["bridge_user_id"], service._public_user_id("weixin:a"))
        self.assertEqual(len(service.pending_messages("weixin:a")["messages"]), 1)

    def test_pending_message_retry_marks_sent_after_router_recovers(self):
        router = FakeRouter(delivered=False)
        service = SocialBridgeService(self.store, router)
        queued = service.send_message("weixin:a", "weixin:b", "稍后见。")

        router.delivered = True
        result = service.pending_messages(
            "weixin:b",
            retry_message_id=queued["message_id"],
        )

        self.assertTrue(result["retry"]["delivered"])
        self.assertEqual(result["retry"]["status"], "sent")
        self.assertEqual(result["messages"], [])
        self.assertEqual(router.sent[-1][1], router.sent[0][1])

    def test_pending_message_retry_allows_sender(self):
        router = FakeRouter(delivered=False)
        service = SocialBridgeService(self.store, router)
        queued = service.send_message("weixin:a", "weixin:b", "稍后见。")

        router.delivered = True
        result = service.pending_messages(
            "weixin:a",
            retry_message_id=queued["message_id"],
        )

        self.assertTrue(result["retry"]["delivered"])
        self.assertEqual(result["retry"]["status"], "sent")
        self.assertEqual(result["messages"], [])

    def test_router_uses_running_weixin_channel_active_send(self):
        from agent.social_bridge.service import ActiveMessageRouter

        calls = []

        class FakeChannel:
            def active_send_text(self, receiver, text, context_token=""):
                calls.append((receiver, text, context_token))
                return True

        class FakeManager:
            def get_channel(self, channel_type):
                self.channel_type = channel_type
                return FakeChannel()

        target = self.store.get_user("weixin:b")
        with patch.dict(
            "sys.modules",
            {"app": SimpleNamespace(get_channel_manager=lambda: FakeManager())},
        ):
            result = ActiveMessageRouter().send_text(target, "hello")

        self.assertTrue(result["delivered"])
        self.assertEqual(calls, [("raw-b", "hello", "ctx-b")])

    def test_router_finds_channel_manager_from_main_module(self):
        from agent.social_bridge.service import ActiveMessageRouter

        calls = []

        class FakeChannel:
            def active_send_text(self, receiver, text, context_token=""):
                calls.append((receiver, text, context_token))
                return True

        fake_main = SimpleNamespace(
            get_channel_manager=lambda: SimpleNamespace(get_channel=lambda channel_type: FakeChannel())
        )
        target = self.store.get_user("weixin:b")

        with patch.dict(sys.modules, {"app": SimpleNamespace(get_channel_manager=lambda: None), "__main__": fake_main}):
            result = ActiveMessageRouter().send_text(target, "hello from main")

        self.assertTrue(result["delivered"])
        self.assertEqual(calls, [("raw-b", "hello from main", "ctx-b")])

    def test_router_falls_back_to_standalone_channel(self):
        from agent.social_bridge.service import ActiveMessageRouter

        calls = []

        class FakeChannel:
            def active_send_text(self, receiver, text, context_token=""):
                calls.append((receiver, text, context_token))
                return True

        target = self.store.get_user("weixin:b")

        with patch.object(ActiveMessageRouter, "_get_channel_manager", return_value=None), patch.object(
            ActiveMessageRouter,
            "_create_standalone_channel",
            return_value=FakeChannel(),
        ):
            result = ActiveMessageRouter().send_text(target, "hello standalone")

        self.assertTrue(result["delivered"])
        self.assertEqual(calls, [("raw-b", "hello standalone", "ctx-b")])

    def test_send_message_accepts_public_bridge_user_id(self):
        router = FakeRouter(delivered=True)
        service = SocialBridgeService(self.store, router)
        target_ref = service.list_users("weixin:a")["users"][0]["bridge_user_id"]

        result = service.send_message("weixin:a", target_ref, "请转告 B 我会准时到。")

        self.assertTrue(result["delivered"])
        self.assertEqual(result["target_bridge_user_id"], target_ref)

    def test_send_message_accepts_wechat_id_or_declared_name(self):
        router = FakeRouter(delivered=True)
        service = SocialBridgeService(self.store, router)

        by_wechat = service.send_message("weixin:a", "alice_wx", "按微信 ID 转述。")
        by_name = service.send_message("weixin:a", "Alice", "按名字转述。")

        self.assertTrue(by_wechat["delivered"])
        self.assertTrue(by_name["delivered"])

    def test_user_directory_and_send_resolve_relationship_alias(self):
        router = FakeRouter(delivered=True)
        service = SocialBridgeService(self.store, router)
        service.set_relationship("weixin:b", "weixin:a", "老公")

        listed = service.list_users("weixin:b", include_relationships=True)["users"][0]
        result = service.send_message("weixin:b", "老公", "跟我老公说一下我晚点回。")

        self.assertEqual(listed["relationship_to_viewer"], "老公")
        self.assertEqual(listed["relationship"], "老公")
        self.assertTrue(result["delivered"])


if __name__ == "__main__":
    unittest.main()
