import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.social_bridge.service import ActiveMessageRouter
from agent.social_bridge.store import BridgeUser
from agent.user_profiles import resolve_agent_user_profile, safe_actor_slug
from bridge.agent_bridge import AgentBridge
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from channel.file_cache import get_file_cache
from channel.image_recognition import ImageRecognitionManager, reset_image_recognition_manager
from channel.web.web_channel import ChannelsHandler
from channel.wecom_bot.wecom_bot_channel import MARKDOWN_TEXT_CHUNK_LIMIT, WecomBotChannel
from common.agent_task_runtime import SessionRuntime
from config import conf

PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


class FakeBridgeStore:
    def __init__(self):
        self.registered = []

    def register_user(self, **kwargs):
        self.registered.append(kwargs)


class FakeBridgeService:
    def __init__(self):
        self.retry_calls = []

    def retry_pending_for_target(self, actor_id, limit=5):
        self.retry_calls.append((actor_id, limit))
        return {"retried": []}


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(json.loads(payload))


class TestWecomBotSocialBridge(unittest.TestCase):
    def setUp(self):
        self._config_backup = dict(conf())
        self.channel = WecomBotChannel()
        self.channel.channel_type = "wecom_bot"
        self.channel.received_msgs = {}
        self.channel._stream_states = {}
        self.channel._connected = False
        self.channel._ws = None
        self._save_config_patch_patcher = patch(
            "channel.wecom_bot.wecom_bot_channel._save_config_patch",
            lambda patch_data: None,
        )
        self._save_config_patch_patcher.start()

    def tearDown(self):
        self._save_config_patch_patcher.stop()
        self.channel.received_msgs = {}
        self.channel._stream_states = {}
        self.channel._connected = False
        self.channel._ws = None
        conf().clear()
        conf().update(self._config_backup)
        reset_image_recognition_manager(None)

    def _dispatch_text_message(
        self,
        *,
        msgid,
        chattype,
        sender,
        content="hello",
        chatid=None,
        chatname=None,
        sender_name=None,
    ):
        produced = []
        self.channel.produce = produced.append
        from_info = {"userid": sender}
        if sender_name is not None:
            from_info["name"] = sender_name
        body = {
            "msgid": msgid,
            "chattype": chattype,
            "msgtype": "text",
            "from": from_info,
            "aibotid": "bot-1",
            "text": {"content": content},
        }
        if chatid is not None:
            body["chatid"] = chatid
        if chatname is not None:
            body["chatname"] = chatname

        with patch("agent.social_bridge.get_bridge_store", return_value=FakeBridgeStore()), patch(
            "agent.social_bridge.get_social_bridge_service",
            return_value=FakeBridgeService(),
        ):
            self.channel._handle_msg_callback(
                {
                    "cmd": "aibot_msg_callback",
                    "headers": {"req_id": f"req-{msgid}"},
                    "body": body,
                }
            )
        return produced

    def _dispatch_group_text(self, *, msgid, chatid, sender, chatname=None, sender_name=None, content="hello"):
        produced = self._dispatch_text_message(
            msgid=msgid,
            chattype="group",
            sender=sender,
            sender_name=sender_name,
            content=content,
            chatid=chatid,
            chatname=chatname,
        )
        self.assertEqual(len(produced), 1)
        return produced[0]

    def test_inbound_single_chat_registers_reachable_bridge_user(self):
        store = FakeBridgeStore()
        service = FakeBridgeService()
        produced = []
        self.channel.produce = produced.append

        with patch("agent.social_bridge.get_bridge_store", return_value=store), patch(
            "agent.social_bridge.get_social_bridge_service",
            return_value=service,
        ):
            self.channel._handle_msg_callback(
                {
                    "cmd": "aibot_msg_callback",
                    "headers": {"req_id": "req-1"},
                    "body": {
                        "msgid": "msg-1",
                        "chattype": "single",
                        "msgtype": "text",
                        "from": {"userid": "wecom-user-1"},
                        "aibotid": "bot-1",
                        "text": {"content": "hello bridge"},
                    },
                }
            )

        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0]["channel_type"], "wecom_bot")
        self.assertEqual(produced[0]["session_id"], "wecom-user-1")
        self.assertEqual(produced[0]["receiver"], "wecom-user-1")
        self.assertEqual(produced[0]["actor_id"], "wecom_bot:wecom-user-1")

        self.assertEqual(len(store.registered), 1)
        registered = store.registered[0]
        self.assertEqual(registered["actor_user_id"], "wecom_bot:wecom-user-1")
        self.assertEqual(registered["display_name"], "wecom-user-1")
        self.assertEqual(registered["metadata"]["channel_type"], "wecom_bot")
        self.assertEqual(registered["metadata"]["platform"], "wecom_bot")
        self.assertEqual(registered["metadata"]["raw_user_id"], "wecom-user-1")
        self.assertEqual(registered["metadata"]["receiver"], "wecom-user-1")
        self.assertTrue(registered["metadata"]["can_active_send"])
        self.assertFalse(registered["metadata"]["is_group"])
        self.assertEqual(conf()["agent_user_profiles"]["wecom_bot:wecom-user-1"]["role"], "user")
        self.assertEqual(
            conf()["agent_user_profiles"]["wecom_bot:wecom-user-1"]["memory_user_id"],
            produced[0]["memory_user_id"],
        )
        self.assertEqual(service.retry_calls, [("wecom_bot:wecom-user-1", 5)])

    def test_private_chat_profile_keeps_sender_actor_identity(self):
        produced = self._dispatch_text_message(
            msgid="msg-private-identity",
            chattype="single",
            sender="wecom-user-private",
        )

        self.assertEqual(len(produced), 1)
        context = produced[0]
        profile = resolve_agent_user_profile(context)

        self.assertEqual(context["session_id"], "wecom-user-private")
        self.assertEqual(context["receiver"], "wecom-user-private")
        self.assertEqual(profile.actor_id, "wecom_bot:wecom-user-private")
        self.assertEqual(profile.conversation_id, "wecom_bot:wecom-user-private")
        self.assertEqual(context["actor_id"], "wecom_bot:wecom-user-private")

    def test_private_image_queues_background_recognition_context(self):
        with tempfile.TemporaryDirectory() as workspace:
            conf()["agent_workspace"] = workspace
            conf()["single_chat_image_recognition"] = True
            conf()["image_recognition_followup_wait_seconds"] = 0
            produced = []
            self.channel.produce = produced.append
            sent = []
            self.channel._send_plain_text = lambda context, text, *args, **kwargs: sent.append(text)
            manager = ImageRecognitionManager(workspace_root=workspace, max_workers=1)
            reset_image_recognition_manager(manager)
            get_file_cache().clear("wecom-user-image")

            with patch("channel.wecom_bot.wecom_bot_message._decrypt_media", return_value=PNG_BYTES), \
                    patch.object(ImageRecognitionManager, "_recognize_image", return_value="A small test image."), \
                    patch.object(ImageRecognitionManager, "_synthesize_casual_reply", return_value=""):
                self.channel._handle_msg_callback(
                    {
                        "cmd": "aibot_msg_callback",
                        "headers": {"req_id": "req-image-private"},
                        "body": {
                            "msgid": "msg-image-private",
                            "chattype": "single",
                            "msgtype": "image",
                            "from": {"userid": "wecom-user-image"},
                            "aibotid": "bot-1",
                            "image": {"url": "https://example.test/image", "aeskey": "unused"},
                        },
                    }
                )
                deadline = time.time() + 1
                while not sent and time.time() < deadline:
                    time.sleep(0.01)

            self.assertEqual(produced, [])
            self.assertEqual(get_file_cache().get("wecom-user-image"), [])
            followup_context = manager.build_followup_context("wecom-user-image", wait_seconds=2)
            record = manager.latest_for_session("wecom-user-image")
            self.assertIsNotNone(record)
            self.assertEqual(record.status, "done")
            self.assertTrue(Path(record.image_path).exists())
            self.assertIn("A small test image.", followup_context)
            self.assertIn("[image:", followup_context)
            self.assertTrue(any("A small test image." in text for text in sent))

    def test_group_image_stays_cached_for_explicit_followup(self):
        with tempfile.TemporaryDirectory() as workspace:
            conf()["agent_workspace"] = workspace
            conf()["single_chat_image_recognition"] = True
            produced = []
            self.channel.produce = produced.append
            manager = ImageRecognitionManager(workspace_root=workspace, max_workers=1)
            reset_image_recognition_manager(manager)
            get_file_cache().clear("group-image-chat")

            with patch("channel.wecom_bot.wecom_bot_message._decrypt_media", return_value=PNG_BYTES), \
                    patch.object(ImageRecognitionManager, "_recognize_image", return_value="Group image summary."):
                self.channel._handle_msg_callback(
                    {
                        "cmd": "aibot_msg_callback",
                        "headers": {"req_id": "req-image-group"},
                        "body": {
                            "msgid": "msg-image-group",
                            "chattype": "group",
                            "chatid": "group-image-chat",
                            "msgtype": "image",
                            "from": {"userid": "sender-a", "name": "Alice"},
                            "aibotid": "bot-1",
                            "image": {"url": "https://example.test/image", "aeskey": "unused"},
                        },
                    }
                )

            self.assertEqual(produced, [])
            self.assertEqual(get_file_cache().get("group-image-chat"), [])
            followup_context = manager.build_followup_context("group-image-chat", wait_seconds=2)
            record = manager.latest_for_session("group-image-chat")
            self.assertIsNotNone(record)
            self.assertEqual(record.status, "done")
            self.assertTrue(Path(record.image_path).exists())
            self.assertIn("Group image summary.", followup_context)
            get_file_cache().clear("group-image-chat")

    def test_group_messages_share_group_level_session_conversation_and_memory(self):
        conf()["agent_user_profiles"] = {
            "wecom_bot:sender-a": {
                "role": "admin",
                "memory_user_id": "private-sender-a-memory",
            }
        }
        first_context = self._dispatch_group_text(
            msgid="msg-group-alpha-a",
            chatid="group-alpha",
            chatname="Nico 之家",
            sender="sender-a",
            sender_name="Alice",
            content="请记住我在群里的偏好",
        )
        second_context = self._dispatch_group_text(
            msgid="msg-group-alpha-b",
            chatid="group-alpha",
            sender="sender-b",
        )

        first_profile = resolve_agent_user_profile(first_context)
        second_profile = resolve_agent_user_profile(second_context)

        self.assertEqual(first_context["session_id"], "group-alpha")
        self.assertEqual(second_context["session_id"], "group-alpha")
        self.assertEqual(first_context["receiver"], "group-alpha")
        self.assertEqual(second_context["receiver"], "group-alpha")
        self.assertEqual(first_profile.conversation_id, second_profile.conversation_id)
        self.assertEqual(first_profile.memory_user_id, second_profile.memory_user_id)
        self.assertEqual(first_profile.actor_id, second_profile.actor_id)
        self.assertEqual(first_context["actor_id"], "wecom_bot:group:group-alpha")
        self.assertEqual(first_context["conversation_id"], "wecom_bot:group:group-alpha")
        self.assertEqual(first_context["actor_role"], "user")
        self.assertEqual(
            first_context["memory_user_id"],
            safe_actor_slug("wecom_bot:group:group-alpha"),
        )
        self.assertEqual(first_context["group_sender_id"], "sender-a")
        self.assertEqual(first_context["group_sender_label"], "Alice")
        self.assertEqual(first_context["group_chat_name"], "Nico 之家")
        self.assertEqual(first_context["_visible_task_summary"], "请记住我在群里的偏好")
        self.assertIn("- 群名称: Nico 之家", first_context.content)
        self.assertIn("- 群会话ID: group-alpha", first_context.content)
        self.assertNotIn("- 群ID:", first_context.content)
        self.assertIn("[群成员: Alice]", first_context.content)
        self.assertNotEqual(first_profile.memory_user_id, "private-sender-a-memory")
        self.assertEqual(first_profile.role, "user")

    def test_group_sender_label_uses_fixed_alias_config(self):
        conf()["wecom_bot_group_member_aliases"] = {
            "group-alpha": {
                "LiuHao": "刘昊",
            }
        }

        context = self._dispatch_group_text(
            msgid="msg-group-alias",
            chatid="group-alpha",
            chatname="Nico 之家",
            sender="LiuHao",
            sender_name="LiuHao",
            content="1分钟后提醒我喝水",
        )

        self.assertEqual(context["group_sender_label"], "刘昊")
        self.assertIn("[群成员: 刘昊]", context.content)
        self.assertIn({"user_id": "LiuHao", "name": "刘昊"}, context["group_known_members"])

    def test_different_group_messages_use_distinct_group_level_keys(self):
        alpha_context = self._dispatch_group_text(
            msgid="msg-group-alpha-distinct",
            chatid="group-alpha",
            sender="sender-a",
        )
        beta_context = self._dispatch_group_text(
            msgid="msg-group-beta-distinct",
            chatid="group-beta",
            sender="sender-a",
        )

        alpha_profile = resolve_agent_user_profile(alpha_context)
        beta_profile = resolve_agent_user_profile(beta_context)

        self.assertEqual(alpha_context["session_id"], "group-alpha")
        self.assertEqual(beta_context["session_id"], "group-beta")
        self.assertNotEqual(alpha_profile.conversation_id, beta_profile.conversation_id)
        self.assertNotEqual(alpha_profile.memory_user_id, beta_profile.memory_user_id)

    def test_group_message_without_chatid_does_not_produce_context(self):
        produced = self._dispatch_text_message(
            msgid="msg-group-missing-chatid",
            chattype="group",
            sender="sender-a",
        )

        self.assertEqual(produced, [])

    def test_group_onboarding_pending_state_is_per_member_under_group_memory(self):
        with tempfile.TemporaryDirectory() as workspace:
            conf()["agent_workspace"] = workspace
            first_context = self._dispatch_group_text(
                msgid="msg-group-onboarding-a",
                chatid="group-onboarding",
                sender="sender-a",
                sender_name="Alice",
                content="please summarize the project status",
            )
            second_context = self._dispatch_group_text(
                msgid="msg-group-onboarding-b",
                chatid="group-onboarding",
                sender="sender-b",
            )
            first_profile = resolve_agent_user_profile(first_context)
            second_profile = resolve_agent_user_profile(second_context)
            group_memory_dir = Path(workspace) / "memory" / "users" / first_profile.memory_user_id

            first_reply = AgentBridge._try_onboarding_welcome(
                "please summarize the project status",
                profile=first_profile,
                context=first_context,
            )
            second_reply = AgentBridge._try_onboarding_welcome(
                "hello",
                profile=second_profile,
                context=second_context,
            )
            first_user_file = Path(AgentBridge._group_member_user_file(first_profile, first_context))
            second_user_file = Path(AgentBridge._group_member_user_file(second_profile, second_context))

            self.assertEqual(first_profile.memory_user_id, second_profile.memory_user_id)
            self.assertIsNone(first_reply)
            self.assertEqual(second_reply.type, ReplyType.TEXT)
            self.assertNotEqual(first_user_file, second_user_file)
            self.assertTrue(first_user_file.exists())
            self.assertIn("Alice", first_user_file.read_text(encoding="utf-8"))
            self.assertEqual(os.path.commonpath([group_memory_dir, first_user_file]), str(group_memory_dir))
            self.assertEqual(os.path.commonpath([group_memory_dir, second_user_file]), str(group_memory_dir))

    def test_initialized_group_member_no_longer_gets_welcome_while_peer_still_does(self):
        with tempfile.TemporaryDirectory() as workspace:
            conf()["agent_workspace"] = workspace
            initialized_context = self._dispatch_group_text(
                msgid="msg-group-initialized-member",
                chatid="group-member-onboarding",
                sender="initialized-member",
            )
            pending_context = self._dispatch_group_text(
                msgid="msg-group-pending-member",
                chatid="group-member-onboarding",
                sender="pending-member",
            )
            initialized_profile = resolve_agent_user_profile(initialized_context)
            pending_profile = resolve_agent_user_profile(pending_context)
            initialized_user_file = Path(
                AgentBridge._group_member_user_file(initialized_profile, initialized_context)
            )
            initialized_user_file.parent.mkdir(parents=True, exist_ok=True)
            initialized_user_file.write_text(
                "# USER.md\n\n- Name: Initialized Member\n- Communication style: concise\n",
                encoding="utf-8",
            )

            initialized_reply = AgentBridge._try_onboarding_welcome(
                "hello",
                profile=initialized_profile,
                context=initialized_context,
            )
            pending_reply = AgentBridge._try_onboarding_welcome(
                "hello",
                profile=pending_profile,
                context=pending_context,
            )

            self.assertEqual(initialized_profile.memory_user_id, pending_profile.memory_user_id)
            self.assertIsNone(initialized_reply)
            self.assertIsNotNone(pending_reply)
            self.assertEqual(pending_reply.type, ReplyType.TEXT)

    def test_inbound_single_chat_preserves_configured_admin_role(self):
        conf()["agent_admin_users"] = ["wecom_bot:wecom-user-1"]
        store = FakeBridgeStore()
        service = FakeBridgeService()
        self.channel.produce = lambda context: None

        with patch("agent.social_bridge.get_bridge_store", return_value=store), patch(
            "agent.social_bridge.get_social_bridge_service",
            return_value=service,
        ):
            self.channel._handle_msg_callback(
                {
                    "cmd": "aibot_msg_callback",
                    "headers": {"req_id": "req-1"},
                    "body": {
                        "msgid": "msg-admin",
                        "chattype": "single",
                        "msgtype": "text",
                        "from": {"userid": "wecom-user-1"},
                        "aibotid": "bot-1",
                        "text": {"content": "hello admin"},
                    },
                }
            )

        self.assertEqual(conf()["agent_user_profiles"]["wecom_bot:wecom-user-1"]["role"], "admin")

    def test_enter_chat_event_registers_user_as_normal_by_default(self):
        store = FakeBridgeStore()
        service = FakeBridgeService()

        with patch("agent.social_bridge.get_bridge_store", return_value=store), patch(
            "agent.social_bridge.get_social_bridge_service",
            return_value=service,
        ):
            self.channel._handle_event_callback(
                {
                    "body": {
                        "event": {"eventtype": "enter_chat"},
                        "from": {"userid": "fresh-user", "name": "Fresh User"},
                    }
                }
            )

        self.assertEqual(len(store.registered), 1)
        registered = store.registered[0]
        self.assertEqual(registered["actor_user_id"], "wecom_bot:fresh-user")
        self.assertEqual(registered["display_name"], "Fresh User")
        self.assertEqual(conf()["agent_user_profiles"]["wecom_bot:fresh-user"]["role"], "user")

    def test_channels_api_summarizes_wecom_connected_users_with_roles(self):
        class FakeStore:
            def list_visible_users(self, exclude_actor_id, limit=100):
                return self.list_users(limit=limit)

            def list_users(self, limit=100):
                return [
                    BridgeUser(
                        actor_user_id="wecom_bot:admin-user",
                        memory_user_id="admin-memory",
                        display_name="Admin User",
                        metadata={
                            "channel_type": "wecom_bot",
                            "raw_user_id": "admin-user",
                            "receiver": "admin-user",
                            "can_active_send": True,
                        },
                    ),
                    BridgeUser(
                        actor_user_id="weixin:old-user",
                        memory_user_id="old-memory",
                        display_name="Old User",
                        metadata={"channel_type": "weixin"},
                    ),
                ]

        conf()["agent_admin_users"] = ["wecom_bot:admin-user"]

        fake_service = SimpleNamespace(sync_configured_users=lambda: {"synced": [], "count": 0})
        with patch("agent.social_bridge.get_bridge_store", return_value=FakeStore()), patch(
            "agent.social_bridge.get_social_bridge_service",
            return_value=fake_service,
        ):
            users = ChannelsHandler._bridge_channel_users("wecom_bot")

        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["actor_id"], "wecom_bot:admin-user")
        self.assertEqual(users[0]["role"], "admin")
        self.assertTrue(users[0]["can_active_send"])

    def test_active_send_text_result_sends_markdown_and_reports_delivery(self):
        ws = FakeWebSocket()
        self.channel._ws = ws
        self.channel._connected = True

        result = self.channel.active_send_text_result("wecom-user-1", "hello from bridge")

        self.assertTrue(result["ok"])
        self.assertTrue(result["delivered"])
        self.assertEqual(result["reason"], "sent")
        self.assertEqual(result["receiver"], "wecom-user-1")
        self.assertEqual(len(ws.sent), 1)
        payload = ws.sent[0]
        self.assertEqual(payload["cmd"], "aibot_send_msg")
        self.assertEqual(payload["body"]["chatid"], "wecom-user-1")
        self.assertEqual(payload["body"]["chat_type"], 1)
        self.assertEqual(payload["body"]["msgtype"], "markdown")
        self.assertEqual(payload["body"]["markdown"]["content"], "hello from bridge")

    def test_active_group_send_mentions_target_without_repeating_label(self):
        ws = FakeWebSocket()
        self.channel._ws = ws
        self.channel._connected = True

        result = self.channel.active_send_text_result(
            "group-alpha",
            "Riko, 该喝水啦 💧",
            is_group=True,
            mention_user_ids=["riko-user"],
            mention_display_names=["Riko"],
        )

        self.assertTrue(result["ok"])
        payload = ws.sent[0]
        self.assertEqual(payload["body"]["chatid"], "group-alpha")
        self.assertEqual(payload["body"]["chat_type"], 2)
        self.assertEqual(payload["body"]["markdown"]["content"], "@Riko\n该喝水啦 💧")
        self.assertNotIn("riko-user, 该喝水", payload["body"]["markdown"]["content"])

    def test_active_group_send_uses_fixed_alias_for_mention_name(self):
        ws = FakeWebSocket()
        self.channel._ws = ws
        self.channel._connected = True
        conf()["wecom_bot_group_member_aliases"] = {
            "group-alpha": {
                "LiuHao": "刘昊",
            }
        }

        result = self.channel.active_send_text_result(
            "group-alpha",
            "LiuHao, drink water",
            is_group=True,
            mention_user_ids=["LiuHao"],
            mention_display_names=["LiuHao"],
        )

        self.assertTrue(result["ok"])
        payload = ws.sent[0]
        self.assertEqual(payload["body"]["markdown"]["content"], "@刘昊\ndrink water")
        self.assertNotIn("LiuHao, drink water", payload["body"]["markdown"]["content"])

    def test_group_reply_mentions_sender(self):
        ws = FakeWebSocket()
        self.channel._ws = ws
        self.channel._connected = True
        context = {
            "receiver": "group-alpha",
            "isgroup": True,
            "group_sender_id": "sender-a",
            "group_sender_label": "Alice",
            "msg": SimpleNamespace(actual_user_id="sender-a", actual_user_nickname="Alice", req_id=None),
        }

        delivered = self.channel.send(Reply(ReplyType.TEXT, "好的"), context)

        self.assertTrue(delivered)
        payload = ws.sent[0]
        self.assertEqual(payload["body"]["chat_type"], 2)
        self.assertEqual(payload["body"]["markdown"]["content"], "@Alice\n好的")

    def test_stream_push_failure_does_not_advance_visible_cursor(self):
        ws = FakeWebSocket()
        self.channel._ws = ws
        self.channel._connected = False
        on_event = self.channel._make_stream_callback("req-stream")

        delivered = on_event({"type": "message_update", "data": {"delta": "partial"}})

        self.assertFalse(delivered)
        self.assertEqual(ws.sent, [])
        self.assertEqual(self.channel._stream_states["req-stream"]["last_push_len"], 0)

    def test_silence_notice_does_not_finish_active_stream(self):
        ws = FakeWebSocket()
        self.channel._ws = ws
        self.channel._connected = True
        self.channel._make_stream_callback("req-stream")
        runtime = SessionRuntime()
        runtime.start_task("long task")
        context = {
            "receiver": "wecom-user-1",
            "isgroup": False,
            "msg": SimpleNamespace(req_id="req-stream"),
            "_session_runtime": runtime,
        }

        sent = self.channel._send_silence_notice(context, "still working")

        self.assertTrue(sent)
        self.assertIn("req-stream", self.channel._stream_states)
        self.assertEqual(len(ws.sent), 1)
        payload = ws.sent[0]
        self.assertEqual(payload["cmd"], "aibot_send_msg")
        self.assertEqual(payload["body"]["msgtype"], "markdown")
        self.assertEqual(payload["body"]["markdown"]["content"], "still working")
        self.assertEqual(runtime.last_visible_output_source, "silence_notice")

    def test_final_stream_reply_is_merged_with_committed_content(self):
        ws = FakeWebSocket()
        self.channel._ws = ws
        self.channel._connected = True
        self.channel._stream_states["req-final"] = {
            "stream_id": "stream-1",
            "committed": "thinking\n\n---\n\n",
            "current": "",
            "last_push_time": 0,
            "last_push_len": 0,
            "mention_user_ids": [],
            "mention_display_names": [],
        }

        sent = self.channel._send_text("final answer", "wecom-user-1", False, req_id="req-final")

        self.assertTrue(sent)
        payload = ws.sent[0]
        content = payload["body"]["stream"]["content"]
        self.assertTrue(payload["body"]["stream"]["finish"])
        self.assertIn("thinking", content)
        self.assertTrue(content.endswith("final answer"))

    def test_long_final_stream_reply_sends_active_followup_chunks(self):
        ws = FakeWebSocket()
        self.channel._ws = ws
        self.channel._connected = True
        self.channel._stream_states["req-long"] = {
            "stream_id": "stream-long",
            "committed": "",
            "current": "",
            "last_push_time": 0,
            "last_push_len": 0,
            "mention_user_ids": [],
            "mention_display_names": [],
        }
        long_text = "A" * (MARKDOWN_TEXT_CHUNK_LIMIT + 25)

        with patch("channel.wecom_bot.wecom_bot_channel.time.sleep", lambda _: None):
            sent = self.channel._send_text(long_text, "wecom-user-1", False, req_id="req-long")

        self.assertTrue(sent)
        self.assertEqual(len(ws.sent), 2)
        first, second = ws.sent
        self.assertEqual(first["cmd"], "aibot_respond_msg")
        self.assertTrue(first["body"]["stream"]["finish"])
        self.assertLessEqual(len(first["body"]["stream"]["content"]), MARKDOWN_TEXT_CHUNK_LIMIT)
        self.assertEqual(second["cmd"], "aibot_send_msg")
        self.assertEqual(second["body"]["msgtype"], "markdown")
        self.assertTrue(second["body"]["markdown"]["content"].startswith("(2/2)\n\n"))

    def test_long_active_text_splits_into_markdown_chunks(self):
        ws = FakeWebSocket()
        self.channel._ws = ws
        self.channel._connected = True
        long_text = "B" * (MARKDOWN_TEXT_CHUNK_LIMIT + 25)

        with patch("channel.wecom_bot.wecom_bot_channel.time.sleep", lambda _: None):
            sent = self.channel._send_text(long_text, "wecom-user-1", False)

        self.assertTrue(sent)
        self.assertEqual(len(ws.sent), 2)
        self.assertEqual(ws.sent[0]["cmd"], "aibot_send_msg")
        self.assertFalse(ws.sent[0]["body"]["markdown"]["content"].startswith("(1/2)"))
        self.assertTrue(ws.sent[1]["body"]["markdown"]["content"].startswith("(2/2)\n\n"))

    def test_active_send_text_result_reports_websocket_send_failure(self):
        class BrokenWebSocket:
            def send(self, payload):
                raise RuntimeError("socket closed")

        self.channel._ws = BrokenWebSocket()
        self.channel._connected = True

        result = self.channel.active_send_text_result("wecom-user-1", "hello from bridge")

        self.assertFalse(result["ok"])
        self.assertFalse(result["delivered"])
        self.assertEqual(result["reason"], "send_error")

    def test_send_text_returns_false_when_channel_not_ready(self):
        ws = FakeWebSocket()
        self.channel._ws = ws
        self.channel._connected = False

        delivered = self.channel.send(
            Reply(ReplyType.TEXT, "hello"),
            {"receiver": "wecom-user-1", "isgroup": False, "msg": None},
        )

        self.assertFalse(delivered)
        self.assertEqual(ws.sent, [])

    def test_active_message_router_sends_to_running_wecom_bot_channel(self):
        calls = []

        class FakeWecomBotChannel:
            def active_send_text_result(self, receiver, text, is_group=False):
                calls.append((receiver, text, is_group))
                return {"ok": True, "reason": "sent"}

        target = BridgeUser(
            actor_user_id="wecom_bot:wecom-user-1",
            memory_user_id="memory_wecom_user_1",
            display_name="wecom-user-1",
            metadata={
                "channel_type": "wecom_bot",
                "receiver": "wecom-user-1",
                "can_active_send": True,
                "is_group": False,
            },
        )
        manager = SimpleNamespace(get_channel=lambda channel_type: FakeWecomBotChannel())

        with patch.object(ActiveMessageRouter, "_get_channel_manager", return_value=manager):
            result = ActiveMessageRouter().send_text(target, "hello through router")

        self.assertTrue(result["delivered"])
        self.assertEqual(result["reason"], "sent")
        self.assertEqual(result["channel_type"], "wecom_bot")
        self.assertEqual(result["receiver"], "wecom-user-1")
        self.assertEqual(calls, [("wecom-user-1", "hello through router", False)])


if __name__ == "__main__":
    unittest.main()
