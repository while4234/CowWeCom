import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch


def _load_token_usage_module():
    script = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "token-usage-tracker"
        / "scripts"
        / "token_usage.py"
    )
    spec = importlib.util.spec_from_file_location("token_usage_skill", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


token_usage = _load_token_usage_module()


class TestTokenUsageTrackerSkill(unittest.TestCase):
    def test_auto_summary_falls_back_to_cowagent_llm_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "token-usage-tracker"
            cache_file = root / "llm_cache_usage.jsonl"
            cache_file.write_text(
                json.dumps({
                    "timestamp": "2026-05-22T03:36:27+00:00",
                    "model": "gpt-5.5",
                    "channel_type": "weixin",
                    "user_hash": "userhash12345678",
                    "user_label": "wxid_real",
                    "prompt_tokens": 1200,
                    "completion_tokens": 80,
                    "total_tokens": 1280,
                    "cached_tokens": 300,
                    "completion_tokens_details": {"reasoning_tokens": 25},
                }) + "\n",
                encoding="utf-8",
            )

            args = token_usage.build_parser().parse_args([
                "summary",
                "--all",
                "--data-dir",
                str(data_dir),
                "--llm-cache-file",
                str(cache_file),
            ])
            events, source, _ = token_usage.events_for_summary(token_usage.get_data_dir(args), args)
            summary = token_usage.summarize_events(events)

            self.assertEqual(source, "llm-cache")
            self.assertEqual(summary["events"], 1)
            self.assertEqual(summary["input_tokens"], 1200)
            self.assertEqual(summary["cached_tokens"], 300)
            self.assertEqual(summary["reasoning_tokens"], 25)
            self.assertAlmostEqual(summary["cache_hit_rate"], 0.25)

    def test_user_identifier_can_match_llm_cache_display_name(self):
        events = [{
            "user_hash": "hashed-user",
            "display_name": "wxid_real",
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
        }]

        resolved_hash, matched = token_usage.events_for_user_identifier(events, "wxid_real")

        self.assertEqual(resolved_hash, "hashed-user")
        self.assertEqual(matched, events)

    def test_today_period_uses_shanghai_day_for_utc_llm_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "token-usage-tracker"
            cache_file = root / "llm_cache_usage.jsonl"
            records = [
                ("before-local-day", "2026-05-25T15:59:59+00:00", 100),
                ("local-day-start", "2026-05-25T16:00:00+00:00", 200),
                ("local-day-end", "2026-05-26T15:59:59+00:00", 300),
                ("after-local-day", "2026-05-26T16:00:00+00:00", 400),
            ]
            cache_file.write_text(
                "\n".join(
                    json.dumps({
                        "id": record_id,
                        "timestamp": timestamp,
                        "user_hash": "userhash12345678",
                        "prompt_tokens": tokens,
                        "completion_tokens": 0,
                        "total_tokens": tokens,
                    })
                    for record_id, timestamp, tokens in records
                ) + "\n",
                encoding="utf-8",
            )

            args = token_usage.build_parser().parse_args([
                "summary",
                "--all",
                "--period",
                "today",
                "--data-dir",
                str(data_dir),
                "--llm-cache-file",
                str(cache_file),
            ])

            local_noon = datetime(2026, 5, 26, 12, 0, tzinfo=token_usage.LOCAL_TZ)
            with patch.object(token_usage, "now_local", return_value=local_noon):
                events, source, _ = token_usage.events_for_summary(token_usage.get_data_dir(args), args)
            summary = token_usage.summarize_events(events)

            self.assertEqual(source, "llm-cache")
            self.assertEqual(summary["events"], 2)
            self.assertEqual(summary["input_tokens"], 500)

    def test_summary_merges_configured_user_aliases_by_display_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "token-usage-tracker"
            cache_file = root / "llm_cache_usage.jsonl"
            cache_file.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in [
                        {
                            "timestamp": "2026-05-22T03:36:27+00:00",
                            "user_hash": "canonicalhash123",
                            "user_label": "LiuHao",
                            "prompt_tokens": 100,
                            "completion_tokens": 20,
                            "total_tokens": 120,
                        },
                        {
                            "timestamp": "2026-05-22T03:37:27+00:00",
                            "user_hash": "aliashash1234567",
                            "user_label": "Rondo0323",
                            "prompt_tokens": 200,
                            "completion_tokens": 30,
                            "total_tokens": 230,
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            args = token_usage.build_parser().parse_args([
                "summary",
                "--all",
                "--source",
                "llm-cache",
                "--data-dir",
                str(data_dir),
                "--llm-cache-file",
                str(cache_file),
                "--user-alias",
                "Rondo0323=LiuHao",
            ])
            events, source, _ = token_usage.events_for_summary(token_usage.get_data_dir(args), args)
            summary = token_usage.summarize_events(events)
            meta = token_usage.user_meta_from_events(events)

            self.assertEqual(source, "llm-cache")
            self.assertEqual({event["user_hash"] for event in events}, {"canonicalhash123"})
            self.assertEqual(summary["events"], 2)
            self.assertEqual(summary["total_tokens"], 350)
            self.assertEqual(meta["canonicalhash123"]["display_name"], "LiuHao")


if __name__ == "__main__":
    unittest.main()
