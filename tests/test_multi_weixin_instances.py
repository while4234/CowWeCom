import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from channel import channel_factory
from channel.weixin.weixin_api import WeixinApi
from channel.weixin.weixin_channel import WeixinChannel
from channel.weixin.weixin_identity import extract_real_wechat_id, extract_wechat_nickname
from channel.web.web_channel import ChannelsHandler
from channel.web.web_channel import WeixinQrHandler
from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from config import conf


class TestMultiWeixinInstances(unittest.TestCase):
    def setUp(self):
        self._config_backup = dict(conf())

    def tearDown(self):
        conf().clear()
        conf().update(self._config_backup)

    def test_factory_creates_distinct_named_weixin_instances(self):
        admin = channel_factory.create_channel("weixin")
        normal = channel_factory.create_channel("weixin_user")

        self.assertIsInstance(admin, WeixinChannel)
        self.assertIsInstance(normal, WeixinChannel)
        self.assertIsNot(admin, normal)
        self.assertEqual(admin.instance_id, "weixin")
        self.assertEqual(normal.instance_id, "weixin_user")
        self.assertEqual(admin.channel_type, "weixin")
        self.assertEqual(normal.channel_type, "weixin_user")

    def test_named_instance_uses_own_credentials_path(self):
        conf()["weixin_credentials_path"] = "~/.weixin_cow_credentials.json"
        conf()["weixin_instances"] = {
            "weixin_user": {
                "credentials_path": "~/.weixin_cow_credentials_user.json",
                "base_url": "https://example.invalid",
            }
        }

        normal = WeixinChannel("weixin_user")

        self.assertEqual(
            normal._get_instance_value("credentials_path", ""),
            "~/.weixin_cow_credentials_user.json",
        )
        self.assertEqual(normal._get_instance_value("base_url", ""), "https://example.invalid")

    def test_qr_handler_accepts_only_weixin_instance_names(self):
        self.assertEqual(WeixinQrHandler._normalize_instance("weixin"), "weixin")
        self.assertEqual(WeixinQrHandler._normalize_instance("wx"), "weixin")
        self.assertEqual(WeixinQrHandler._normalize_instance("weixin_user"), "weixin_user")

        with self.assertRaises(ValueError):
            WeixinQrHandler._normalize_instance("feishu")

    def test_send_reinitializes_api_from_instance_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            cred_path = Path(tmp) / "weixin_user_credentials.json"
            cred_path.write_text(
                json.dumps({"token": "saved-token", "base_url": "https://saved.example"}),
                encoding="utf-8",
            )
            conf()["weixin_instances"] = {
                "weixin_user": {
                    "credentials_path": str(cred_path),
                    "cdn_base_url": "https://cdn.example",
                }
            }
            sent = []

            class FakeWeixinApi:
                def __init__(self, base_url, token, cdn_base_url):
                    self.base_url = base_url
                    self.token = token
                    self.cdn_base_url = cdn_base_url

                def send_text(self, receiver, text, context_token):
                    sent.append((receiver, text, context_token, self.base_url, self.token, self.cdn_base_url))

            channel = WeixinChannel("weixin_user")
            context = Context(ContextType.TEXT, "hello")
            context["receiver"] = "receiver-id"
            context["msg"] = SimpleNamespace(context_token="ctx-token")

            with patch("channel.weixin.weixin_channel.WeixinApi", FakeWeixinApi):
                channel.send(Reply(ReplyType.TEXT, "hello from background"), context)

            self.assertEqual(
                sent,
                [(
                    "receiver-id",
                    "hello from background",
                    "ctx-token",
                    "https://saved.example",
                    "saved-token",
                    "https://cdn.example",
                )],
            )

    def test_active_send_text_uses_cached_context_token(self):
        sent = []

        class FakeWeixinApi:
            def send_text(self, receiver, text, context_token):
                sent.append((receiver, text, context_token))

        channel = WeixinChannel("weixin_user")
        channel.channel_type = "weixin_user"
        channel.api = FakeWeixinApi()
        channel._context_tokens["receiver-id"] = "ctx-token"

        ok = channel.active_send_text("receiver-id", "hello from bridge")

        self.assertTrue(ok)
        self.assertEqual(sent, [("receiver-id", "hello from bridge", "ctx-token")])

    def test_active_send_text_returns_false_without_context_token(self):
        channel = WeixinChannel("weixin_user")
        channel.channel_type = "weixin_user"

        self.assertFalse(channel.active_send_text("receiver-id", "hello from bridge"))

    def test_active_send_text_returns_false_when_api_send_fails(self):
        class FailingWeixinApi:
            def send_text(self, receiver, text, context_token):
                raise RuntimeError("network down")

        channel = WeixinChannel("weixin_user")
        channel.channel_type = "weixin_user"
        channel.api = FailingWeixinApi()
        channel._context_tokens["receiver-id"] = "ctx-token"

        self.assertFalse(channel.active_send_text("receiver-id", "hello from bridge"))

    def test_active_send_text_returns_false_when_api_rejects_send(self):
        class RejectingWeixinApi:
            def send_text(self, receiver, text, context_token):
                return {"ret": 40001, "errmsg": "invalid context token"}

        channel = WeixinChannel("weixin_user")
        channel.channel_type = "weixin_user"
        channel.api = RejectingWeixinApi()
        channel._context_tokens["receiver-id"] = "ctx-token"

        self.assertFalse(channel.active_send_text("receiver-id", "hello from bridge"))

    def test_weixin_api_send_timeout_is_not_success(self):
        api = WeixinApi()
        with patch("channel.weixin.weixin_api.requests.post", side_effect=requests.exceptions.Timeout()):
            result = api.send_text("receiver-id", "hello", "ctx-token")

        self.assertNotEqual(result.get("ret"), 0)
        self.assertEqual(result.get("error"), "timeout")

    def test_weixin_api_long_poll_timeout_is_successful_empty_poll(self):
        api = WeixinApi()
        with patch("channel.weixin.weixin_api.requests.post", side_effect=requests.exceptions.Timeout()):
            result = api.get_updates(timeout=1)

        self.assertEqual(result, {"ret": 0, "msgs": []})

    def test_social_bridge_registration_uses_resolved_wechat_id(self):
        registered = []

        class FakeBridgeStore:
            def register_user(self, **kwargs):
                registered.append(kwargs)

        context = Context(ContextType.TEXT, "hello")
        context["channel_type"] = "weixin_user"
        context["session_id"] = "raw-user@im.wechat"
        context["receiver"] = "raw-user@im.wechat"
        context["msg"] = SimpleNamespace(from_user_id="raw-user@im.wechat")
        channel = WeixinChannel("weixin_user")

        with patch("agent.social_bridge.get_bridge_store", return_value=FakeBridgeStore()):
            channel._remember_social_bridge_user(
                context,
                "raw-user@im.wechat",
                "ctx-token",
                "alice_wx",
            )

        self.assertEqual(registered[0]["actor_user_id"], "weixin_user:raw-user@im.wechat")
        self.assertEqual(registered[0]["display_name"], "alice_wx")
        self.assertEqual(registered[0]["metadata"]["wechat_id"], "alice_wx")
        self.assertEqual(registered[0]["metadata"]["context_token"], "ctx-token")

    def test_social_bridge_registration_uses_resolved_nickname(self):
        registered = []

        class FakeBridgeStore:
            def register_user(self, **kwargs):
                registered.append(kwargs)

        context = Context(ContextType.TEXT, "hello")
        context["channel_type"] = "weixin_user"
        context["session_id"] = "raw-user@im.wechat"
        context["receiver"] = "raw-user@im.wechat"
        context["msg"] = SimpleNamespace(from_user_id="raw-user@im.wechat")
        channel = WeixinChannel("weixin_user")

        with patch("agent.social_bridge.get_bridge_store", return_value=FakeBridgeStore()):
            channel._remember_social_bridge_user(
                context,
                "raw-user@im.wechat",
                "ctx-token",
                "alice_wx",
                "Alice",
            )

        self.assertEqual(registered[0]["display_name"], "Alice")
        self.assertEqual(registered[0]["metadata"]["wechat_id"], "alice_wx")
        self.assertEqual(registered[0]["metadata"]["nickname"], "Alice")

    def test_channel_info_includes_named_weixin_identity_and_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            default_cred = Path(tmp) / "admin_credentials.json"
            user_cred = Path(tmp) / "user_credentials.json"
            default_cred.write_text(
                json.dumps({
                    "token": "admin-token",
                    "base_url": "https://saved.example",
                    "user_id": "raw-admin@im.wechat",
                    "wechat_id": "admin_wxid",
                }),
                encoding="utf-8",
            )
            user_cred.write_text(
                json.dumps({
                    "token": "user-token",
                    "base_url": "https://saved.example",
                    "user_id": "raw-user@im.wechat",
                    "wechat_id": "member_wxid",
                }),
                encoding="utf-8",
            )
            conf()["channel_type"] = "weixin,weixin_user"
            conf()["weixin_credentials_path"] = str(default_cred)
            conf()["weixin_instances"] = {
                "weixin_user": {
                    "credentials_path": str(user_cred),
                    "role": "user",
                }
            }
            conf()["agent_admin_users"] = ["weixin:raw-admin@im.wechat"]

            active = ChannelsHandler._active_channel_set()
            admin_info = ChannelsHandler._build_channel_info("weixin", conf(), active)
            user_info = ChannelsHandler._build_channel_info("weixin_user", conf(), active)

            self.assertEqual(admin_info["wechat_id"], "admin_wxid")
            self.assertEqual(admin_info["role"], "admin")
            self.assertEqual(user_info["wechat_id"], "member_wxid")
            self.assertEqual(user_info["role"], "user")
            self.assertTrue(user_info["active"])

    def test_channel_info_does_not_display_ilink_raw_id_as_wechat_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            cred_path = Path(tmp) / "weixin_credentials.json"
            cred_path.write_text(
                json.dumps({
                    "token": "saved-token",
                    "base_url": "https://saved.example",
                    "user_id": "opaque@im.wechat",
                }),
                encoding="utf-8",
            )
            conf()["weixin_credentials_path"] = str(cred_path)
            conf().pop("agent_user_profiles", None)
            conf().pop("llm_usage_user_labels", None)

            info = ChannelsHandler._build_channel_info("weixin", conf(), {"weixin"})

            self.assertEqual(info["raw_user_id"], "opaque@im.wechat")
            self.assertEqual(info["wechat_id"], "")
            self.assertEqual(info["display_wechat_id"], "")

    def test_channel_info_backfills_wechat_id_from_running_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            cred_path = Path(tmp) / "weixin_credentials.json"
            cred_path.write_text(
                json.dumps({
                    "token": "saved-token",
                    "base_url": "https://saved.example",
                    "user_id": "opaque@im.wechat",
                }),
                encoding="utf-8",
            )
            conf()["weixin_credentials_path"] = str(cred_path)

            class FakeRunningChannel:
                login_status = "logged_in"

                def _resolve_login_wechat_id_from_credentials(self):
                    saved = json.loads(cred_path.read_text(encoding="utf-8"))
                    saved["wechat_id"] = "resolved_wxid"
                    cred_path.write_text(json.dumps(saved), encoding="utf-8")
                    return "resolved_wxid"

            with patch.object(ChannelsHandler, "_get_running_weixin_channel", return_value=FakeRunningChannel()):
                info = ChannelsHandler._build_channel_info("weixin", conf(), {"weixin"})

            self.assertEqual(info["raw_user_id"], "opaque@im.wechat")
            self.assertEqual(info["wechat_id"], "resolved_wxid")
            self.assertEqual(info["display_wechat_id"], "resolved_wxid")
            self.assertEqual(json.loads(cred_path.read_text(encoding="utf-8"))["wechat_id"], "resolved_wxid")

    def test_save_weixin_identity_persists_credentials_and_label_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            cred_path = Path(tmp) / "weixin_credentials.json"
            cred_path.write_text(
                json.dumps({
                    "token": "saved-token",
                    "base_url": "https://saved.example",
                    "user_id": "opaque@im.wechat",
                }),
                encoding="utf-8",
            )
            conf()["weixin_credentials_path"] = str(cred_path)

            with patch("channel.weixin.weixin_identity._save_config_patch"), \
                    patch.object(ChannelsHandler, "_save_config_patch"):
                result = json.loads(ChannelsHandler()._handle_save_weixin_identity(
                    "weixin",
                    {"wechat_id": "y553344388"},
                ))

            saved = json.loads(cred_path.read_text(encoding="utf-8"))
            labels = conf()["llm_usage_user_labels"]
            profiles = conf()["agent_user_profiles"]

            self.assertEqual(result["status"], "success")
            self.assertEqual(saved["wechat_id"], "y553344388")
            self.assertEqual(conf()["weixin_channel"]["wechat_id"], "y553344388")
            self.assertEqual(conf()["weixin_channel"]["user_id"], "opaque@im.wechat")
            self.assertEqual(labels["weixin:opaque@im.wechat"], "y553344388")
            self.assertEqual(labels["opaque@im.wechat"], "y553344388")
            self.assertEqual(profiles["weixin:opaque@im.wechat"]["wechat_id"], "y553344388")

    def test_extract_real_wechat_id_ignores_ilink_raw_id(self):
        self.assertEqual(
            extract_real_wechat_id({
                "ilink_user_id": "opaque@im.wechat",
                "user_info": {"wechat_id": "y553344388"},
            }),
            "y553344388",
        )
        self.assertEqual(extract_real_wechat_id({"ilink_user_id": "opaque@im.wechat"}), "")

    def test_extract_wechat_nickname_from_nested_payload(self):
        self.assertEqual(
            extract_wechat_nickname({
                "user_info": {
                    "nickname": "Alice",
                    "ilink_user_id": "opaque@im.wechat",
                }
            }),
            "Alice",
        )
        self.assertEqual(extract_wechat_nickname({"nickname": "opaque@im.wechat"}), "")

    def test_weixin_channel_resolves_real_id_from_get_config(self):
        class FakeApi:
            def get_config(self, user_id, context_token):
                return {"user_info": {"wechat_id": "y553344388"}}

        channel = WeixinChannel("weixin")
        channel.channel_type = "weixin"
        channel.api = FakeApi()

        with patch("channel.weixin.weixin_channel.remember_wechat_identity") as remember:
            resolved = channel._resolve_wechat_id(
                "opaque@im.wechat",
                "ctx-token",
                {"from_user_id": "opaque@im.wechat"},
            )

        self.assertEqual(resolved, "y553344388")
        remember.assert_called_once_with(
            channel_type="weixin",
            raw_user_id="opaque@im.wechat",
            wechat_id="y553344388",
        )

    def test_weixin_channel_resolves_profile_from_get_config(self):
        class FakeApi:
            def get_config(self, user_id, context_token):
                return {"user_info": {"wechat_id": "y553344388", "nickname": "Alice"}}

        channel = WeixinChannel("weixin")
        channel.channel_type = "weixin"
        channel.api = FakeApi()

        with patch("channel.weixin.weixin_channel.remember_wechat_identity"):
            resolved = channel._resolve_wechat_profile(
                "opaque@im.wechat",
                "ctx-token",
                {"from_user_id": "opaque@im.wechat"},
            )

        self.assertEqual(resolved, {"wechat_id": "y553344388", "nickname": "Alice"})
        self.assertEqual(channel._user_identity_cache["opaque@im.wechat"], "y553344388")
        self.assertEqual(channel._user_nickname_cache["opaque@im.wechat"], "Alice")

    def test_weixin_channel_loads_manually_saved_logged_in_wechat_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            cred_path = Path(tmp) / "weixin_credentials.json"
            cred_path.write_text(
                json.dumps({
                    "token": "saved-token",
                    "base_url": "https://saved.example",
                    "bot_id": "bot-id",
                    "user_id": "opaque@im.wechat",
                    "wechat_id": "y553344388",
                }),
                encoding="utf-8",
            )

            channel = WeixinChannel("weixin")
            channel.channel_type = "weixin"
            channel._credentials_path = str(cred_path)

            with patch("channel.weixin.weixin_channel.remember_wechat_identity") as remember:
                resolved = channel._resolve_login_wechat_id_from_credentials()

            saved = json.loads(cred_path.read_text(encoding="utf-8"))
            self.assertEqual(resolved, "y553344388")
            self.assertEqual(saved["wechat_id"], "y553344388")
            self.assertEqual(channel._user_identity_cache["opaque@im.wechat"], "y553344388")
            remember.assert_called_once_with(
                channel_type="weixin",
                raw_user_id="opaque@im.wechat",
                wechat_id="y553344388",
            )


if __name__ == "__main__":
    unittest.main()
