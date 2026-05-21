import os
import tempfile
import unittest
from unittest.mock import patch

from bridge.reply import ReplyType
from voice.openai.openai_voice import OpenaiVoice


class _FakeConfig(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class _FakeResponse:
    def __init__(self, status_code=200, body=None, content=b""):
        self.status_code = status_code
        self._body = body or {}
        self.content = content
        self.text = str(self._body)

    def json(self):
        return self._body


class TestOpenAIVoice(unittest.TestCase):
    def test_voice_to_text_uses_configured_base_model_and_timeout(self):
        cfg = _FakeConfig({
            "open_ai_api_base": "https://example.test/openai",
            "open_ai_api_key": "test-key",
            "voice_to_text_model": "gpt-4o-mini-transcribe",
            "request_timeout": 12,
        })

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"audio")
            tmp_path = tmp.name

        try:
            with patch("voice.openai.openai_voice.conf", return_value=cfg):
                with patch("voice.openai.openai_voice.requests.post") as post:
                    post.return_value = _FakeResponse(body={"text": "hello"})
                    reply = OpenaiVoice().voiceToText(tmp_path)

            self.assertEqual(reply.type, ReplyType.TEXT)
            self.assertEqual(reply.content, "hello")
            _, kwargs = post.call_args
            self.assertEqual(
                post.call_args.args[0],
                "https://example.test/openai/audio/transcriptions",
            )
            self.assertEqual(kwargs["data"]["model"], "gpt-4o-mini-transcribe")
            self.assertEqual(kwargs["timeout"], 12)
        finally:
            os.remove(tmp_path)

    def test_text_to_voice_uses_configured_base_model_and_timeout(self):
        cfg = _FakeConfig({
            "open_ai_api_base": "https://example.test/openai",
            "open_ai_api_key": "test-key",
            "text_to_voice_model": "gpt-4o-mini-tts",
            "tts_voice_id": "alloy",
            "request_timeout": 12,
        })

        with patch("voice.openai.openai_voice.conf", return_value=cfg):
            with patch("voice.openai.openai_voice.requests.post") as post:
                post.return_value = _FakeResponse(content=b"mp3")
                reply = OpenaiVoice().textToVoice("hello")

        try:
            self.assertEqual(reply.type, ReplyType.VOICE)
            self.assertTrue(os.path.exists(reply.content))
            _, kwargs = post.call_args
            self.assertEqual(
                post.call_args.args[0],
                "https://example.test/openai/audio/speech",
            )
            self.assertEqual(kwargs["json"]["model"], "gpt-4o-mini-tts")
            self.assertEqual(kwargs["timeout"], 12)
        finally:
            if reply.type == ReplyType.VOICE and os.path.exists(reply.content):
                os.remove(reply.content)


if __name__ == "__main__":
    unittest.main()
