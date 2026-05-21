import unittest

from common.llm_usage_tracker import normalize_usage


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


if __name__ == "__main__":
    unittest.main()
