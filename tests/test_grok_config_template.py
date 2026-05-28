# encoding:utf-8

import json
import unittest
from pathlib import Path


class TestGrokConfigTemplate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = json.loads(Path("config-template.json").read_text(encoding="utf-8"))

    def test_template_exposes_grok_bot_type_entry_without_changing_default(self):
        self.assertIn("bot_type", self.template)
        self.assertEqual(self.template["bot_type"], "")
        self.assertEqual(self.template["grok_model"], "grok-4.3")

    def test_bare_manual_code_compatibility_is_disabled_by_default(self):
        self.assertIs(self.template["grok_oauth_accept_bare_code"], False)

    def test_template_exposes_grok_tts_and_voice_mode_defaults(self):
        expected = {
            "text_to_voice": "openai",
            "grok_tts_voice_id": "eve",
            "grok_tts_language": "zh",
            "grok_tts_sample_rate": 24000,
            "grok_tts_bit_rate": 128000,
            "grok_tts_auto_speech_tags": False,
            "grok_voice_reply_enabled": False,
            "grok_voice_mode_enabled": False,
            "grok_voice_conversation_mode_enabled": False,
            "grok_voice_reply_channels": ["wechatcom_app", "wecom_bot"],
            "grok_voice_streaming_enabled": True,
            "grok_voice_require_low_reasoning": True,
            "grok_voice_require_low_reasoning_when_not_conversation_mode": True,
            "grok_voice_force_voice_for_voice_input_in_conversation_mode": True,
            "grok_voice_force_reasoning_effort": "low",
            "grok_voice_low_latency_backend": "",
            "grok_voice_low_latency_model": "",
            "grok_voice_max_output_tokens": 220,
            "grok_voice_short_answer_prompt_enabled": True,
            "grok_voice_max_segment_chars": 180,
            "grok_voice_min_segment_chars": 18,
            "grok_voice_flush_idle_ms": 1500,
            "grok_voice_tts_queue_size": 4,
            "wecom_voice_max_seconds": 55,
            "wecom_voice_max_bytes": 1900000,
            "wecom_voice_normalize_enabled": True,
            "wecom_voice_normalize_target_dbfs": -18.0,
            "wecom_voice_normalize_headroom_db": 1.0,
            "wecom_voice_amr_bitrate": "12.2k",
            "reasoning_effort_policy_low_effort": "low",
        }
        for key, value in expected.items():
            self.assertIn(key, self.template)
            self.assertEqual(self.template[key], value)


if __name__ == "__main__":
    unittest.main()
