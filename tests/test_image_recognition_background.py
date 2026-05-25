import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.tools.vision.vision import Vision
from channel.image_recognition import ImageRecognitionManager, reset_image_recognition_manager
from channel.weixin.weixin_channel import WeixinChannel
from config import conf


PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


class TestImageRecognitionManager(unittest.TestCase):
    def setUp(self):
        self._config_backup = dict(conf())

    def tearDown(self):
        conf().clear()
        conf().update(self._config_backup)
        reset_image_recognition_manager(None)

    def test_register_copies_image_deduplicates_pending_and_builds_followup_context(self):
        with tempfile.TemporaryDirectory() as workspace:
            source = Path(workspace) / "source.png"
            source.write_bytes(PNG_BYTES)
            manager = ImageRecognitionManager(workspace_root=workspace, max_workers=1)
            started = threading.Event()
            release = threading.Event()
            calls = []

            def fake_recognize(image_path, prompt, max_tokens=700):
                started.set()
                release.wait(2)
                calls.append(image_path)
                return "A concise image summary."

            with patch.object(ImageRecognitionManager, "_recognize_image", side_effect=fake_recognize):
                first = manager.register_image(
                    session_id="session-a",
                    channel_type="weixin",
                    image_path=str(source),
                )
                self.assertTrue(first.started_new_job)
                self.assertTrue(started.wait(1))

                second = manager.register_image(
                    session_id="session-a",
                    channel_type="weixin",
                    image_path=str(source),
                )
                self.assertFalse(second.started_new_job)
                self.assertEqual(first.record_id, second.record_id)

                release.set()
                followup_context = manager.build_followup_context("session-a", wait_seconds=2)

            record = manager.latest_for_session("session-a")
            self.assertEqual(len(calls), 1)
            self.assertEqual(record.status, "done")
            self.assertTrue(Path(record.image_path).exists())
            self.assertNotEqual(Path(record.image_path), source)
            self.assertIn("A concise image summary.", followup_context)
            self.assertIn("[image:", followup_context)

    def test_result_and_image_copy_ttls_are_enforced(self):
        with tempfile.TemporaryDirectory() as workspace:
            source = Path(workspace) / "source.png"
            source.write_bytes(PNG_BYTES)
            manager = ImageRecognitionManager(workspace_root=workspace, max_workers=1)
            manager.result_ttl_seconds = 1
            manager.image_ttl_seconds = 1

            with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
                manager.register_image(
                    session_id="session-b",
                    channel_type="wecom_bot",
                    image_path=str(source),
                )
                manager.build_followup_context("session-b", wait_seconds=2)

            record = manager.latest_for_session("session-b")
            copied = Path(record.image_path)
            old = time.time() - 10
            os.utime(str(copied), (old, old))
            with manager._lock:
                manager._records[record.record_id].completed_at = old
                manager.cleanup_locked(time.time())

            self.assertIsNone(manager.latest_for_session("session-b"))
            self.assertFalse(copied.exists())


class TestVisionReasoningEffort(unittest.TestCase):
    def test_call_via_bot_passes_low_reasoning_effort_and_max_tokens(self):
        calls = []

        class FakeBot:
            supports_vision = True

            def call_vision(self, **kwargs):
                calls.append(kwargs)
                return {
                    "model": kwargs["model"],
                    "content": "ok",
                    "usage": {},
                }

        tool = Vision(config={"reasoning_effort": "low", "max_tokens": 321})
        tool.model = SimpleNamespace(bot=FakeBot())

        result = tool._call_via_bot(
            "gpt-test",
            "What is in this image?",
            {"image_url": {"url": "data:image/png;base64,AAAA"}},
            reasoning_effort="low",
            max_tokens=321,
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(calls[0]["reasoning_effort"], "low")
        self.assertTrue(calls[0]["reasoning_effort_locked"])
        self.assertEqual(calls[0]["max_tokens"], 321)


class TestWeixinImageRecognition(unittest.TestCase):
    def setUp(self):
        self._config_backup = dict(conf())

    def tearDown(self):
        conf().clear()
        conf().update(self._config_backup)
        reset_image_recognition_manager(None)

    def test_weixin_private_image_registers_background_job_without_foreground_queue(self):
        with tempfile.TemporaryDirectory() as workspace:
            conf()["agent_workspace"] = workspace
            conf()["single_chat_image_recognition"] = True
            conf()["image_recognition_followup_wait_seconds"] = 0
            manager = ImageRecognitionManager(workspace_root=workspace, max_workers=1)
            reset_image_recognition_manager(manager)

            channel = WeixinChannel()
            channel.channel_type = "weixin"
            channel.api = SimpleNamespace(cdn_base_url="https://cdn.example.test")
            channel._resolve_wechat_profile = lambda raw_user_id, context_token, raw_msg: {
                "wechat_id": "",
                "nickname": "",
            }
            channel._remember_social_bridge_user = lambda *args, **kwargs: None
            produced = []
            sent = []
            channel.produce = produced.append
            channel._send_plain_text = lambda context, text, *args, **kwargs: sent.append(text)

            def fake_download(cdn_base_url, encrypt_param, aes_key, save_path):
                Path(save_path).write_bytes(PNG_BYTES)

            raw_msg = {
                "message_type": 1,
                "message_id": "wx-image-1",
                "from_user_id": "wx-user-a",
                "to_user_id": "bot",
                "context_token": "ctx-token",
                "item_list": [
                    {
                        "type": 2,
                        "image_item": {
                            "aeskey": "unused",
                            "media": {"encrypt_query_param": "encrypted"},
                        },
                    }
                ],
            }

            with patch("channel.weixin.weixin_message.download_media_from_cdn", side_effect=fake_download), \
                    patch.object(ImageRecognitionManager, "_recognize_image", return_value="Weixin image summary."):
                channel._process_message(raw_msg)

            self.assertEqual(produced, [])
            followup_context = manager.build_followup_context("wx-user-a", wait_seconds=2)
            self.assertIn("Weixin image summary.", followup_context)
            deadline = time.time() + 1
            while not sent and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(any("Weixin image summary." in text for text in sent))


if __name__ == "__main__":
    unittest.main()
