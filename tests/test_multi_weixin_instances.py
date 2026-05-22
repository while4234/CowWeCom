import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from channel import channel_factory
from channel.weixin.weixin_channel import WeixinChannel
from channel.weixin.weixin_identity import extract_real_wechat_id
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

    def test_extract_real_wechat_id_ignores_ilink_raw_id(self):
        self.assertEqual(
            extract_real_wechat_id({
                "ilink_user_id": "opaque@im.wechat",
                "user_info": {"wechat_id": "y553344388"},
            }),
            "y553344388",
        )
        self.assertEqual(extract_real_wechat_id({"ilink_user_id": "opaque@im.wechat"}), "")

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

    def test_weixin_channel_backfills_logged_in_wechat_id_from_credentials(self):
        class FakeApi:
            def get_config(self, user_id, context_token):
                return {"profile": {"wechat_id": "y553344388"}}

        with tempfile.TemporaryDirectory() as tmp:
            cred_path = Path(tmp) / "weixin_credentials.json"
            cred_path.write_text(
                json.dumps({
                    "token": "saved-token",
                    "base_url": "https://saved.example",
                    "bot_id": "bot-id",
                    "user_id": "opaque@im.wechat",
                }),
                encoding="utf-8",
            )

            channel = WeixinChannel("weixin")
            channel.channel_type = "weixin"
            channel._credentials_path = str(cred_path)
            channel.api = FakeApi()

            with patch("channel.weixin.weixin_channel.remember_wechat_identity") as remember:
                resolved = channel._resolve_login_wechat_id_from_credentials()

            saved = json.loads(cred_path.read_text(encoding="utf-8"))
            self.assertEqual(resolved, "y553344388")
            self.assertEqual(saved["wechat_id"], "y553344388")
            remember.assert_called_once_with(
                channel_type="weixin",
                raw_user_id="opaque@im.wechat",
                wechat_id="y553344388",
            )


if __name__ == "__main__":
    unittest.main()
