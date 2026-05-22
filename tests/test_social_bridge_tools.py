import sys
import types
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from agent.tools.social_bridge.social_bridge import (
    BridgeListUsersTool,
    BridgePendingMessagesTool,
    BridgeSendMessageTool,
    BridgeSetRelationshipTool,
)


class FakeSocialBridgeService:
    def __init__(self):
        self.calls = []

    def list_users(self, **kwargs):
        self.calls.append(("list_users", kwargs))
        return [{"id": "user-b"}]

    def set_relationship(self, **kwargs):
        self.calls.append(("set_relationship", kwargs))
        return {"ok": True, "relationship": kwargs["relationship"]}

    def send_message(self, **kwargs):
        self.calls.append(("send_message", kwargs))
        return {"queued": True}

    def pending_messages(self, **kwargs):
        self.calls.append(("pending_messages", kwargs))
        return [{"from": "user-b", "text": "hello"}]


class TestSocialBridgeTools(unittest.TestCase):
    def setUp(self):
        self.service = FakeSocialBridgeService()
        module = types.ModuleType("agent.social_bridge")
        module.get_social_bridge_service = lambda: self.service
        sys.modules["agent.social_bridge"] = module
        sys.modules.pop("agent.social_bridge.service", None)

    def tearDown(self):
        sys.modules.pop("agent.social_bridge", None)
        sys.modules.pop("agent.social_bridge.service", None)

    def test_list_users_uses_actor_profile(self):
        tool = BridgeListUsersTool()
        tool.context = SimpleNamespace(
            _actor_profile=SimpleNamespace(actor_id="weixin:user-a")
        )

        result = tool.execute({"include_relationships": True})

        self.assertEqual(result.status, "success")
        self.assertEqual(result.result, [{"id": "user-b"}])
        self.assertEqual(
            self.service.calls,
            [
                (
                    "list_users",
                    {
                        "actor_id": "weixin:user-a",
                        "include_relationships": True,
                    },
                )
            ],
        )

    def test_set_relationship_falls_back_to_current_user_id(self):
        tool = BridgeSetRelationshipTool()
        tool.context = SimpleNamespace(_current_user_id="user-a")

        result = tool.execute({
            "target_user_id": "user-b",
            "relationship": "friend",
            "notes": "met today",
        })

        self.assertEqual(result.status, "success")
        self.assertEqual(
            self.service.calls[0],
            (
                "set_relationship",
                {
                    "actor_id": "user-a",
                    "target_user_id": "user-b",
                    "relationship": "friend",
                    "notes": "met today",
                },
            ),
        )

    def test_send_message_requires_explicit_authorization(self):
        tool = BridgeSendMessageTool()
        tool.context = SimpleNamespace(_current_user_id="user-a")

        result = tool.execute({
            "target_user_id": "user-b",
            "message": "hello",
        })

        self.assertEqual(result.status, "error")
        self.assertIn("explicit authorization", result.result)
        self.assertEqual(self.service.calls, [])

    def test_send_message_calls_service_when_authorized(self):
        tool = BridgeSendMessageTool()
        tool.context = SimpleNamespace(_current_user_id="user-a")

        result = tool.execute({
            "target_user_id": "user-b",
            "message": "hello",
            "authorized": True,
        })

        self.assertEqual(result.status, "success")
        self.assertEqual(
            self.service.calls[0],
            (
                "send_message",
                {
                    "actor_id": "user-a",
                    "target_user_id": "user-b",
                    "message": "hello",
                },
            ),
        )

    def test_pending_messages_validates_actor_and_limit(self):
        tool = BridgePendingMessagesTool()
        no_actor = tool.execute({"limit": 1})

        tool.context = SimpleNamespace(_current_user_id="user-a")
        bad_limit = tool.execute({"limit": 0})
        ok = tool.execute({"limit": 1, "mark_seen": True})

        self.assertEqual(no_actor.status, "error")
        self.assertIn("current actor", no_actor.result)
        self.assertEqual(bad_limit.status, "error")
        self.assertEqual(ok.status, "success")
        self.assertEqual(
            self.service.calls[0],
            (
                "pending_messages",
                    {
                        "actor_id": "user-a",
                        "limit": 1,
                        "mark_seen": True,
                        "retry_message_id": "",
                    },
                ),
        )

    def test_pending_messages_can_request_retry(self):
        tool = BridgePendingMessagesTool()
        tool.context = SimpleNamespace(_current_user_id="user-a")

        result = tool.execute({"limit": 3, "retry_message_id": "bridge_msg_1"})

        self.assertEqual(result.status, "success")
        self.assertEqual(
            self.service.calls[0],
            (
                "pending_messages",
                {
                    "actor_id": "user-a",
                    "limit": 3,
                    "mark_seen": False,
                    "retry_message_id": "bridge_msg_1",
                },
            ),
        )

    def test_import_error_is_reported_cleanly(self):
        tool = BridgeListUsersTool()
        tool.context = SimpleNamespace(_current_user_id="user-a")

        original_import = __import__

        def blocked_import(name, *args, **kwargs):
            if name.startswith("agent.social_bridge"):
                raise ImportError("blocked by test")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=blocked_import):
            result = tool.execute({})

        self.assertEqual(result.status, "error")
        self.assertIn("Social bridge service is not available", result.result)


if __name__ == "__main__":
    unittest.main()
