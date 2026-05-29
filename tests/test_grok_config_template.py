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
        self.assertIs(self.template["grok_import_hermes_auth"], True)
        self.assertIs(self.template["grok_import_hermes_auth_overwrite"], False)

    def test_template_exposes_grok_tts_and_voice_mode_defaults(self):
        expected = {
            "text_to_voice": "openai",
            "grok_image_model": "grok-imagine-image",
            "grok_image_resolution": "1k",
            "grok_image_aspect_ratio": "square",
            "grok_image_timeout_seconds": 120,
            "grok_image_download_timeout_seconds": 60,
            "image_recognition_image_create_auto_ref_window_seconds": 180,
            "video_generation_provider": "xai",
            "video_create_prefix": ["生成视频", "视频生成", "画个视频"],
            "grok_video_model": "grok-imagine-video",
            "grok_video_duration": 8,
            "grok_video_aspect_ratio": "16:9",
            "grok_video_resolution": "720p",
            "grok_video_timeout_seconds": 240,
            "grok_video_poll_interval_seconds": 5,
            "grok_video_download_timeout_seconds": 120,
            "grok_tts_voice_id": "eve",
            "grok_tts_language": "zh",
            "grok_tts_sample_rate": 24000,
            "grok_tts_bit_rate": 128000,
            "grok_tts_auto_speech_tags": False,
            "grok_voice_reply_enabled": True,
            "grok_voice_mode_enabled": True,
            "grok_voice_conversation_mode_enabled": True,
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

    def test_template_contains_all_pr1_to_pr4_grok_config_keys(self):
        expected_keys = {
            "grok_model",
            "grok_api_base",
            "grok_proxy",
            "grok_auth_file",
            "grok_auth_prefer_oauth",
            "grok_import_hermes_auth",
            "grok_import_hermes_auth_overwrite",
            "grok_wire_api",
            "grok_api_key",
            "text_to_voice",
            "grok_tts_voice_id",
            "grok_tts_language",
            "grok_tts_sample_rate",
            "grok_tts_bit_rate",
            "grok_tts_auto_speech_tags",
            "grok_voice_mode_enabled",
            "grok_voice_reply_enabled",
            "grok_voice_conversation_mode_enabled",
            "grok_voice_reply_channels",
            "grok_voice_streaming_enabled",
            "grok_voice_require_low_reasoning",
            "grok_voice_require_low_reasoning_when_not_conversation_mode",
            "grok_voice_force_voice_for_voice_input_in_conversation_mode",
            "grok_voice_force_reasoning_effort",
            "grok_voice_low_latency_backend",
            "grok_voice_low_latency_model",
            "grok_voice_max_output_tokens",
            "grok_voice_short_answer_prompt_enabled",
            "grok_voice_max_segment_chars",
            "grok_voice_min_segment_chars",
            "grok_voice_flush_idle_ms",
            "grok_voice_tts_queue_size",
            "wecom_voice_max_seconds",
            "wecom_voice_max_bytes",
            "wecom_voice_normalize_enabled",
            "wecom_voice_normalize_target_dbfs",
            "wecom_voice_normalize_headroom_db",
            "wecom_voice_amr_bitrate",
            "reasoning_effort_policy_low_effort",
            "text_to_image",
            "grok_image_model",
            "grok_image_resolution",
            "grok_image_aspect_ratio",
            "grok_image_timeout_seconds",
            "grok_image_download_timeout_seconds",
            "image_recognition_image_create_auto_ref_window_seconds",
            "video_generation_provider",
            "video_create_prefix",
            "grok_video_model",
            "grok_video_duration",
            "grok_video_aspect_ratio",
            "grok_video_resolution",
            "grok_video_timeout_seconds",
            "grok_video_poll_interval_seconds",
            "grok_video_download_timeout_seconds",
        }

        missing = sorted(expected_keys.difference(self.template))

        self.assertEqual(missing, [])

    def test_image_create_recent_ref_window_matches_config_default(self):
        from config import available_setting

        key = "image_recognition_image_create_auto_ref_window_seconds"
        self.assertIn(key, available_setting)
        self.assertIn(key, self.template)
        self.assertEqual(available_setting[key], self.template[key])

    def test_template_defaults_to_recommended_voice_backend_model_and_reasoning(self):
        llm_backend = self.template["llm_backend"]
        self.assertEqual(llm_backend["current_backend"], "codex")
        self.assertEqual(llm_backend["providers"]["codex"]["model"], "gpt-5.5")
        self.assertEqual(llm_backend["providers"]["codex"]["reasoning_effort"], "xhigh")
        self.assertEqual(self.template["grok_voice_force_reasoning_effort"], "low")


if __name__ == "__main__":
    unittest.main()
