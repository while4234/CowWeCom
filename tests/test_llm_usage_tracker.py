import unittest
from unittest.mock import patch

from common.llm_usage_tracker import get_cache_usage_report, normalize_usage
from common import llm_usage_tracker


class TestLLMUsageTracker(unittest.TestCase):
    def test_normalize_usage_preserves_cache_details(self):
        usage = normalize_usage({
            "input_tokens": 4096,
            "output_tokens": 128,
            "input_tokens_details": {
                "cached_tokens": 3072,
                "cache_creation_input_tokens": 512,
            },
        })

        self.assertEqual(usage["prompt_tokens"], 4096)
        self.assertEqual(usage["completion_tokens"], 128)
        self.assertEqual(usage["cached_tokens"], 3072)
        self.assertEqual(usage["cache_creation_tokens"], 512)
        self.assertEqual(usage["uncached_prompt_tokens"], 1024)
        self.assertAlmostEqual(usage["cache_hit_rate"], 0.75)

    def test_normalize_usage_accepts_chat_completion_details(self):
        usage = normalize_usage({
            "prompt_tokens": 2048,
            "completion_tokens": 64,
            "prompt_tokens_details": {"cached_tokens": 1024},
        })

        self.assertEqual(usage["cached_tokens"], 1024)
        self.assertEqual(usage["prompt_tokens_details"]["cached_tokens"], 1024)

    def test_cache_report_includes_user_token_ranking(self):
        records = [
            {
                "timestamp": "2026-05-22T01:00:00+00:00",
                "model": "gpt-5.5",
                "channel_type": "weixin",
                "user_hash": "aaaaaaaaaaaaaaaa",
                "session_hash": "session1",
                "prompt_tokens": 1000,
                "cached_tokens": 400,
                "completion_tokens": 100,
                "total_tokens": 1100,
            },
            {
                "timestamp": "2026-05-22T01:01:00+00:00",
                "model": "gpt-5.5",
                "channel_type": "weixin",
                "user_hash": "bbbbbbbbbbbbbbbb",
                "user_label": "wechat-display-name",
                "session_hash": "session2",
                "prompt_tokens": 3000,
                "cached_tokens": 2400,
                "completion_tokens": 200,
                "total_tokens": 3200,
            },
            {
                "timestamp": "2026-05-22T01:02:00+00:00",
                "model": "gpt-5.5",
                "channel_type": "weixin",
                "user_hash": "aaaaaaaaaaaaaaaa",
                "session_hash": "session1",
                "prompt_tokens": 500,
                "cached_tokens": 250,
                "completion_tokens": 50,
                "total_tokens": 550,
            },
        ]

        with patch.object(llm_usage_tracker, "_read_records", return_value=records):
            report = get_cache_usage_report(limit=10)

        self.assertEqual(report["summary"]["requests"], 3)
        self.assertEqual(report["users"][0]["user_key"], "bbbbbbbbbbbbbbbb")
        self.assertEqual(report["users"][0]["total_tokens"], 3200)
        self.assertEqual(report["users"][0]["user_label"], "wechat-display-name")
        self.assertEqual(report["users"][1]["requests"], 2)
        self.assertEqual(report["users"][1]["session_count"], 1)
        self.assertAlmostEqual(report["users"][1]["cache_hit_rate"], 650 / 1500)

    def test_cache_report_labels_legacy_session_records_from_new_wechat_record(self):
        records = [
            {
                "timestamp": "2026-05-22T01:00:00+00:00",
                "model": "gpt-5.5",
                "channel_type": "weixin",
                "session_hash": "legacy-session",
                "prompt_tokens": 1000,
                "cached_tokens": 0,
                "completion_tokens": 100,
                "total_tokens": 1100,
            },
            {
                "timestamp": "2026-05-22T01:01:00+00:00",
                "model": "gpt-5.5",
                "channel_type": "weixin",
                "user_hash": "wechat-user-hash",
                "user_label": "wxid_actual_user",
                "session_hash": "legacy-session",
                "prompt_tokens": 2000,
                "cached_tokens": 1500,
                "completion_tokens": 100,
                "total_tokens": 2100,
            },
        ]

        with patch.object(llm_usage_tracker, "_read_records", return_value=records):
            report = get_cache_usage_report(limit=10)

        self.assertEqual(len(report["users"]), 1)
        self.assertEqual(report["users"][0]["user_key"], "wechat-user-hash")
        self.assertEqual(report["users"][0]["user_label"], "wxid_actual_user")
        self.assertEqual(report["users"][0]["total_tokens"], 3200)
        self.assertEqual(report["users"][0]["requests"], 2)

    def test_cache_report_prefers_configured_label_over_internal_wechat_id(self):
        records = [
            {
                "timestamp": "2026-05-22T01:00:00+00:00",
                "model": "gpt-5.5",
                "channel_type": "weixin",
                "user_hash": "c77e6fcbf887ddbf",
                "user_label": "weixin:opaque-user@im.wechat",
                "prompt_tokens": 1000,
                "cached_tokens": 500,
                "completion_tokens": 100,
                "total_tokens": 1100,
            },
        ]

        with (
            patch.object(llm_usage_tracker, "_read_records", return_value=records),
            patch.object(
                llm_usage_tracker,
                "_configured_user_labels",
                return_value={"c77e6fcbf887ddbf": "wechat-display-name"},
            ),
        ):
            report = get_cache_usage_report(limit=10)

        self.assertEqual(report["users"][0]["user_label"], "wechat-display-name")


if __name__ == "__main__":
    unittest.main()
