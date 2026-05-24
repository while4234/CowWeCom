import json
import os
import tempfile
import unittest
from unittest.mock import patch

from common.llm_usage_tracker import get_cache_usage_report, normalize_usage, record_usage, stable_metadata_hash
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

    def test_stable_metadata_hash_is_dict_order_insensitive(self):
        left = {"tools": [{"name": "read", "input_schema": {"path": "x", "limit": 1}}]}
        right = {"tools": [{"input_schema": {"limit": 1, "path": "x"}, "name": "read"}]}

        self.assertEqual(stable_metadata_hash(left), stable_metadata_hash(right))

    def test_record_usage_keeps_only_safe_cache_shape_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            usage_path = os.path.join(tmpdir, "usage.jsonl")
            with (
                patch.object(llm_usage_tracker, "_tracking_enabled", return_value=True),
                patch.object(llm_usage_tracker, "_usage_path", return_value=usage_path),
                patch.object(llm_usage_tracker, "_history_limit", return_value=100),
            ):
                record_usage(
                    {"prompt_tokens": 2048, "completion_tokens": 20, "cached_tokens": 1024},
                    {
                        "model": "gpt-5.5",
                        "request_kind": "knowledge_auto",
                        "system_hash": "abc123",
                        "tools_hash": "def456",
                        "messages_prefix_hash": "ghi789",
                        "message_count": "12",
                        "turn_count": 6,
                        "tool_count": 3,
                        "runtime_context_chars": 80,
                        "retrieved_knowledge_chars": 900,
                        "retrieved_knowledge_hash": "knowledgehash",
                        "tool_result_chars": 0,
                        "tool_result_hash": "toolhash",
                        "tool_attempt_count": 4,
                        "tool_attempt_success_count": 3,
                        "tool_attempt_error_count": 1,
                        "tool_skip_count": 2,
                        "tool_duplicate_success_count": 1,
                        "tool_memory_rule_hits": 1,
                        "tool_compacted_result_count": 3,
                        "tool_failure_class": "non_retryable_args",
                        "reasoning_effort_selected": "xhigh",
                        "reasoning_effort_decision_source": "local",
                        "reasoning_effort_reason": "coding",
                        "reasoning_effort_backend": "capi",
                        "reasoning_effort_main_model": "gpt-5.5",
                        "reasoning_effort_classifier_model": "gpt-5-mini",
                        "reasoning_effort_classifier_effort": "low",
                        "reasoning_effort_classifier_status": "success",
                        "reasoning_effort_classifier_latency_ms": 123,
                        "reasoning_effort_local_rule": "coding",
                        "prompt": "raw prompt must not be persisted",
                        "messages": [{"role": "user", "content": "secret"}],
                        "tool_arguments": {"path": "secret.txt"},
                        "api_key": "sk-secret",
                    },
                )

            with open(usage_path, "r", encoding="utf-8") as f:
                record = json.loads(f.readline())

        self.assertEqual(record["request_kind"], "knowledge_auto")
        self.assertEqual(record["message_count"], 12)
        self.assertEqual(record["retrieved_knowledge_chars"], 900)
        self.assertEqual(record["tool_result_hash"], "toolhash")
        self.assertEqual(record["tool_attempt_count"], 4)
        self.assertEqual(record["tool_attempt_success_count"], 3)
        self.assertEqual(record["tool_attempt_error_count"], 1)
        self.assertEqual(record["tool_skip_count"], 2)
        self.assertEqual(record["tool_failure_class"], "non_retryable_args")
        self.assertEqual(record["reasoning_effort_selected"], "xhigh")
        self.assertEqual(record["reasoning_effort_decision_source"], "local")
        self.assertEqual(record["reasoning_effort_classifier_model"], "gpt-5-mini")
        self.assertEqual(record["reasoning_effort_classifier_latency_ms"], 123)
        self.assertNotIn("prompt", record)
        self.assertNotIn("messages", record)
        self.assertNotIn("tool_arguments", record)
        self.assertNotIn("api_key", record)

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

    def test_cache_report_groups_request_kind_and_long_zero_cache(self):
        records = [
            {
                "timestamp": "2026-05-22T01:00:00+00:00",
                "request_kind": "normal",
                "prompt_tokens": 60000,
                "cached_tokens": 0,
                "completion_tokens": 100,
                "total_tokens": 60100,
                "tool_attempt_count": 2,
                "tool_attempt_error_count": 1,
                "tool_skip_count": 1,
            },
            {
                "timestamp": "2026-05-22T01:01:00+00:00",
                "request_kind": "normal",
                "prompt_tokens": 60000,
                "cached_tokens": 30000,
                "completion_tokens": 100,
                "total_tokens": 60100,
                "tool_attempt_count": 1,
                "tool_attempt_success_count": 1,
                "tool_duplicate_success_count": 1,
            },
            {
                "timestamp": "2026-05-22T01:02:00+00:00",
                "request_kind": "knowledge_auto",
                "prompt_tokens": 1200,
                "cached_tokens": 0,
                "completion_tokens": 50,
                "total_tokens": 1250,
                "tool_memory_rule_hits": 1,
                "tool_compacted_result_count": 2,
            },
        ]

        with patch.object(llm_usage_tracker, "_read_records", return_value=records):
            report = get_cache_usage_report(limit=10)

        self.assertEqual(report["summary"]["long_input_threshold"], 50000)
        self.assertEqual(report["summary"]["long_input_requests"], 2)
        self.assertEqual(report["summary"]["long_input_zero_cache_requests"], 1)
        self.assertAlmostEqual(report["summary"]["long_input_zero_cache_rate"], 0.5)
        self.assertEqual(report["summary"]["tool_attempt_count"], 3)
        self.assertEqual(report["summary"]["tool_attempt_error_count"], 1)
        self.assertEqual(report["summary"]["tool_skip_count"], 1)
        self.assertEqual(report["summary"]["tool_memory_rule_hits"], 1)
        self.assertEqual(report["summary"]["tool_compacted_result_count"], 2)

        kinds = {item["request_kind"]: item for item in report["request_kinds"]}
        self.assertEqual(kinds["normal"]["requests"], 2)
        self.assertEqual(kinds["normal"]["long_input_requests"], 2)
        self.assertEqual(kinds["normal"]["long_input_zero_cache_requests"], 1)
        self.assertAlmostEqual(kinds["normal"]["cache_hit_rate"], 30000 / 120000)
        self.assertEqual(kinds["normal"]["tool_attempt_count"], 3)
        self.assertEqual(kinds["normal"]["tool_duplicate_success_count"], 1)
        self.assertEqual(kinds["knowledge_auto"]["tool_memory_rule_hits"], 1)
        self.assertEqual(kinds["knowledge_auto"]["tool_compacted_result_count"], 2)

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
