import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.social_bridge.service import ActiveMessageRouter
from agent.social_bridge.store import BridgeUser
from channel.wecom_bot.wecom_bot_channel import WecomBotChannel
from config import conf


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

    def tearDown(self):
        self.channel.received_msgs = {}
        self.channel._stream_states = {}
        self.channel._connected = False
        self.channel._ws = None
        conf().clear()
        conf().update(self._config_backup)

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
        self.assertEqual(service.retry_calls, [("wecom_bot:wecom-user-1", 5)])

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
