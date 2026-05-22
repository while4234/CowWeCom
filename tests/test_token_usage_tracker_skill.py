import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
