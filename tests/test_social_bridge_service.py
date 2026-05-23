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
                "nickname": "Rondle",
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
                "nickname": "Alice",
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
        self.assertEqual(result["users"][0]["nickname"], "Alice")
        self.assertEqual(result["users"][0]["known_names"], ["Alice"])
        self.assertEqual(result["users"][0]["display_label"], "Alice / alice_wx")
        self.assertEqual(result["users"][0]["relationship_to_viewer"], "")
        self.assertNotIn("display_name", result["users"][0])
        self.assertEqual(result["users"][0]["channel_type"], "weixin_user")

    def test_list_users_uses_registered_nickname_when_wechat_id_is_unavailable(self):
        self.store.register_user(
            "weixin:c",
            "memory_c",
            "raw-user@im.wechat",
            {
                "channel_type": "weixin_user",
                "nickname": "小栀",
                "receiver": "raw-c",
                "context_token": "ctx-c",
                "can_active_send": True,
            },
        )
        service = SocialBridgeService(self.store, FakeRouter())

        result = service.list_users("weixin:a")

        public_id = service._public_user_id("weixin:c")
        user = next(item for item in result["users"] if item["bridge_user_id"] == public_id)
        self.assertEqual(user["wechat_id"], "")
        self.assertEqual(user["known_names"], ["小栀"])
        self.assertEqual(user["display_label"], "小栀")

    def test_list_users_does_not_leak_agent_name_from_memory(self):
        self.store.register_user(
            "weixin:c",
            "memory_c",
            "Haodong",
            {
                "channel_type": "weixin_user",
                "wechat_id": "yuyu_wx",
                "nickname": "Yuyu",
                "receiver": "raw-c",
                "context_token": "ctx-c",
                "can_active_send": True,
            },
        )
        memory_dir = self.workspace / "memory" / "users" / "memory_c"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "USER.md").write_text("call me Haodong\n", encoding="utf-8")
        service = SocialBridgeService(self.store, FakeRouter())

        result = service.list_users("weixin:a")

        public_id = service._public_user_id("weixin:c")
        user = next(item for item in result["users"] if item["bridge_user_id"] == public_id)
        self.assertEqual(user["wechat_id"], "yuyu_wx")
        self.assertEqual(user["known_names"], ["Yuyu"])
        self.assertEqual(user["display_label"], "Yuyu / yuyu_wx")
        self.assertNotIn("Haodong", user["known_names"])

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

    def test_list_users_reads_user_declared_name_without_agent_name(self):
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
        (memory_dir / "MEMORY.md").write_text(
            "- 用户称呼：玉玉。\n- 助手在与用户对话中的名字：昊东。\n",
            encoding="utf-8",
        )
        service = SocialBridgeService(self.store, FakeRouter())

        result = service.list_users("weixin:a")

        public_id = service._public_user_id("weixin:c")
        user = next(item for item in result["users"] if item["bridge_user_id"] == public_id)
        self.assertEqual(user["known_names"], ["玉玉"])
        self.assertEqual(user["display_label"], "玉玉")
        self.assertNotIn("昊东", user["known_names"])

    def test_list_users_uses_manual_channel_identity_for_connected_instance(self):
        self.store.register_user(
            "weixin_user:raw-user@im.wechat",
            "memory_c",
            "raw-user@im.wechat",
            {
                "channel_type": "weixin_user",
                "raw_user_id": "raw-user@im.wechat",
                "receiver": "raw-user@im.wechat",
                "context_token": "ctx-c",
                "can_active_send": True,
            },
        )
        self.config_patch.stop()
        with patch(
            "agent.social_bridge.service.conf",
            return_value={
                "social_bridge_enabled": True,
                "social_bridge_auto_send": True,
                "social_bridge_max_users": 100,
                "weixin_instances": {
                    "weixin_user": {
                        "user_id": "raw-user@im.wechat",
                        "wechat_id": "Rikoo032700",
                    },
                },
            },
        ):
            service = SocialBridgeService(self.store, FakeRouter())
            result = service.list_users("weixin:a")
        self.config_patch.start()

        public_id = service._public_user_id("weixin_user:raw-user@im.wechat")
        user = next(item for item in result["users"] if item["bridge_user_id"] == public_id)
        self.assertEqual(user["wechat_id"], "Rikoo032700")
        self.assertEqual(user["display_label"], "Rikoo032700")

    def test_send_message_uses_model_rewrite_and_marks_sent(self):
        router = FakeRouter(delivered=True)
        model = FakeModel("他让我带句话：他是真的想认真道歉，也希望你们能好好聊聊。")
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
        self.assertIn("朋友", model.requests[0].system)
        self.assertIn("避免使用“授权我转述”", model.requests[0].system)
        self.assertEqual(self.store.list_pending_for_actor("weixin:b"), [])

    def test_send_message_falls_back_without_model_with_natural_boundary(self):
        router = FakeRouter(delivered=True)
        service = SocialBridgeService(self.store, router)

        result = service.send_message("weixin:a", "weixin:b", "请告诉 B 我会按时到。")

        sent_text = router.sent[0][1]
        self.assertTrue(result["delivered"])
        self.assertIn("我帮对方带句话", sent_text)
        self.assertNotIn("授权我转述", sent_text)
        self.assertNotIn("隐私记忆", sent_text)
        self.assertIn("请告诉 B 我会按时到。", sent_text)

    def test_send_message_supports_non_apology_messages(self):
        router = FakeRouter(delivered=True)
        model = FakeModel("他想问你今晚想不想吃火锅；如果你累了，也可以改天。")
        service = SocialBridgeService(self.store, router)

        result = service.send_message(
            "weixin:a",
            "weixin:b",
            "问问她今晚想不想吃火锅，如果累了就改天。",
            model=model,
        )

        self.assertTrue(result["delivered"])
        self.assertEqual(router.sent[0][1], model.text)
        self.assertIn("问问她今晚想不想吃火锅", model.requests[0].messages[0]["content"])

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

    def test_send_message_marks_target_stale_when_weixin_rejects(self):
        class RejectingRouter:
            def send_text(self, target, text):
                return {"delivered": False, "reason": "weixin_send_rejected", "ret": -2}

        service = SocialBridgeService(self.store, RejectingRouter())

        result = service.send_message("weixin:a", "weixin:b", "hello")
        target = self.store.get_user("weixin:b")
        pending = service.pending_messages("weixin:b")

        self.assertFalse(result["delivered"])
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["result"]["ret"], -2)
        self.assertFalse(target.metadata["can_active_send"])
        self.assertEqual(target.metadata["context_token"], "")
        self.assertEqual(target.metadata["active_send_stale_ret"], -2)
        self.assertEqual(pending["messages"][0]["result"]["ret"], -2)

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

    def test_pending_message_retry_after_context_refresh_uses_updated_target_metadata(self):
        class TokenAwareRouter:
            def __init__(self):
                self.tokens = []

            def send_text(self, target, text):
                token = (target.metadata or {}).get("context_token", "")
                self.tokens.append(token)
                if token == "new-token":
                    return {"delivered": True, "reason": "sent", "token": token}
                return {"delivered": False, "reason": "weixin_send_rejected", "ret": -2, "token": token}

        self.store.register_user(
            "weixin:b",
            "memory_b",
            "Alice",
            {
                "channel_type": "weixin_user",
                "wechat_id": "alice_wx",
                "nickname": "Alice",
                "receiver": "raw-b",
                "context_token": "old-token",
                "can_active_send": True,
            },
        )
        router = TokenAwareRouter()
        service = SocialBridgeService(self.store, router)

        queued = service.send_message("weixin:a", "weixin:b", "hello")
        service.register_user(
            "weixin:b",
            "memory_b",
            "Alice",
            channel_type="weixin_user",
            raw_user_id="raw-b",
            receiver="raw-b",
            context_token="new-token",
            can_active_send=True,
            metadata={"wechat_id": "alice_wx", "nickname": "Alice"},
        )
        result = service.pending_messages("weixin:b", retry_message_id=queued["message_id"])

        self.assertEqual(router.tokens, ["old-token", "new-token"])
        self.assertTrue(result["retry"]["delivered"])
        self.assertEqual(result["retry"]["status"], "sent")
        self.assertEqual(result["messages"], [])

    def test_retry_pending_for_target_only_retries_inbound_messages(self):
        router = FakeRouter(delivered=False)
        service = SocialBridgeService(self.store, router)
        service.send_message("weixin:a", "weixin:b", "for b")
        service.send_message("weixin:b", "weixin:a", "for a")

        router.delivered = True
        result = service.retry_pending_for_target("weixin:b")

        self.assertEqual(len(result["retried"]), 1)
        self.assertEqual(result["retried"][0]["status"], "sent")

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

    def test_router_sends_to_running_wecom_bot_channel(self):
        from agent.social_bridge.service import ActiveMessageRouter

        calls = []
        self.store.register_user(
            "wecom_bot:user-a",
            "wecom_user_a",
            "WeCom User",
            {
                "channel_type": "wecom_bot",
                "receiver": "user-a",
                "can_active_send": True,
            },
        )

        class FakeChannel:
            def active_send_text_result(self, receiver, text, is_group=False):
                calls.append((receiver, text, is_group))
                return {"ok": True, "reason": "sent"}

        fake_main = SimpleNamespace(
            get_channel_manager=lambda: SimpleNamespace(get_channel=lambda channel_type: FakeChannel())
        )
        target = self.store.get_user("wecom_bot:user-a")

        with patch.dict(sys.modules, {"app": SimpleNamespace(get_channel_manager=lambda: None), "__main__": fake_main}):
            result = ActiveMessageRouter().send_text(target, "hello wecom")

        self.assertTrue(result["delivered"])
        self.assertEqual(result["channel_type"], "wecom_bot")
        self.assertEqual(calls, [("user-a", "hello wecom", False)])

    def test_router_requires_running_wecom_bot_channel(self):
        from agent.social_bridge.service import ActiveMessageRouter

        self.store.register_user(
            "wecom_bot:user-a",
            "wecom_user_a",
            "WeCom User",
            {
                "channel_type": "wecom_bot",
                "receiver": "user-a",
                "can_active_send": True,
            },
        )
        target = self.store.get_user("wecom_bot:user-a")

        with patch.object(ActiveMessageRouter, "_get_channel_manager", return_value=None), patch.object(
            ActiveMessageRouter,
            "_create_standalone_channel",
        ) as create_channel:
            result = ActiveMessageRouter().send_text(target, "hello wecom")

        create_channel.assert_not_called()
        self.assertFalse(result["delivered"])
        self.assertEqual(result["reason"], "channel_not_running")

    def test_router_does_not_treat_weixin_payload_dict_as_success(self):
        from agent.social_bridge.service import ActiveMessageRouter

        class FakeChannel:
            def active_send_text(self, receiver, text, context_token=""):
                return {"ret": -2}

        fake_main = SimpleNamespace(
            get_channel_manager=lambda: SimpleNamespace(get_channel=lambda channel_type: FakeChannel())
        )
        target = self.store.get_user("weixin:b")

        with patch.dict(sys.modules, {"app": SimpleNamespace(get_channel_manager=lambda: None), "__main__": fake_main}):
            result = ActiveMessageRouter().send_text(target, "hello")

        self.assertFalse(result["delivered"])
        self.assertEqual(result["reason"], "send_rejected")
        self.assertEqual(result["ret"], -2)

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

    def test_send_message_resolves_wife_and_spouse_aliases(self):
        router = FakeRouter(delivered=True)
        service = SocialBridgeService(self.store, router)
        service.set_relationship("weixin:a", "weixin:b", "老婆 / 配偶")

        by_wife = service.send_message("weixin:a", "老婆", "跟我老婆说一下我爱她。")
        by_spouse = service.send_message("weixin:a", "配偶", "跟我的配偶说一下我晚点回。")

        self.assertTrue(by_wife["delivered"])
        self.assertTrue(by_spouse["delivered"])

    def test_send_message_uses_model_fallback_for_unmatched_target_phrase(self):
        router = FakeRouter(delivered=True)
        service = SocialBridgeService(self.store, router)
        target_ref = service.list_users("weixin:a")["users"][0]["bridge_user_id"]
        model = FakeModel(f'{{"bridge_user_id":"{target_ref}"}}')

        result = service.send_message("weixin:a", "我的爱人", "跟她说我爱她。", model=model)

        self.assertTrue(result["delivered"])
        self.assertEqual(result["target_bridge_user_id"], target_ref)
        self.assertGreaterEqual(len(model.requests), 1)
        self.assertIn("我的爱人", model.requests[0].messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
