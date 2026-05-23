import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "skills" / "daily-meme-harvester" / "scripts" / "harvest_memes.py"


def load_harvester_module():
    spec = importlib.util.spec_from_file_location("daily_meme_harvester", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DailyMemeHarvesterTest(unittest.TestCase):
    def setUp(self):
        self.module = load_harvester_module()

    def test_weibo_hotsearch_parses_terms_and_scores_by_rank(self):
        payload = {
            "data": {
                "realtime": [
                    {"word": "第一热搜", "num": 100000, "label_name": "热"},
                    {"word": "第二热搜", "raw_hot": 50000},
                ]
            }
        }

        terms = self.module.parse_weibo_hot_terms(payload, max_terms=2)

        self.assertEqual([term["word"] for term in terms], ["第一热搜", "第二热搜"])
        self.assertEqual(terms[0]["rank"], 1)
        self.assertGreater(terms[0]["score"], terms[1]["score"])

    def test_weibo_cards_extract_mblog_pics_and_clean_html_text(self):
        payload = {
            "data": {
                "cards": [
                    {
                        "card_group": [
                            {
                                "mblog": {
                                    "id": "m123",
                                    "bid": "BID123",
                                    "text": "<span>超好笑</span> &amp; 梗图",
                                    "user": {"id": "u123", "screen_name": "微博作者"},
                                    "pics": [
                                        {"large": {"url": "https://wx1.sinaimg.cn/large/a.jpg"}},
                                        {"url": "https://wx2.sinaimg.cn/orj360/b.jpg"},
                                    ],
                                    "attitudes_count": 10,
                                    "comments_count": 3,
                                    "reposts_count": 2,
                                    "created_at": "Sat May 23 09:00:00 +0800 2026",
                                }
                            }
                        ]
                    }
                ]
            }
        }

        candidates = self.module.parse_weibo_search_cards(payload, {"word": "热词", "score": 1000}, "热词 梗图")

        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0].title, "超好笑 & 梗图")
        self.assertEqual(candidates[0].source_url, "https://weibo.com/u123/BID123")
        self.assertEqual(candidates[0].score, 1000 + 10 + 3 * 2 + 2 * 4)

    def test_reddit_listing_filters_nsfw_and_non_image_urls(self):
        payload = {
            "data": {
                "children": [
                    {
                        "data": {
                            "id": "ok",
                            "title": "daily meme",
                            "post_hint": "image",
                            "url_overridden_by_dest": "https://i.redd.it/meme.webp",
                            "over_18": False,
                            "permalink": "/r/memes/comments/ok/daily_meme/",
                            "ups": 100,
                            "num_comments": 10,
                            "upvote_ratio": 0.95,
                            "author": "redditor",
                        }
                    },
                    {
                        "data": {
                            "id": "bad1",
                            "post_hint": "image",
                            "url_overridden_by_dest": "https://i.redd.it/nsfw.jpg",
                            "over_18": True,
                        }
                    },
                    {
                        "data": {
                            "id": "bad2",
                            "post_hint": "link",
                            "url_overridden_by_dest": "https://example.com/page",
                            "over_18": False,
                        }
                    },
                ]
            }
        }

        candidates = self.module.parse_reddit_listing(payload, subreddit="memes")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_id, "ok")
        self.assertEqual(candidates[0].score, 100 + 10 * 2 + 0.95 * 100)

    def test_xiaohongshu_public_html_extracts_image_urls_without_login_logic(self):
        html_text = (
            '<script>window.__INITIAL_STATE__={"url":"https:\\/\\/sns-webpic-qc.xhscdn.com\\/abc.jpg?imageView2=2",'
            '"another":"https://sns-webpic-qc.xhscdn.com/def.webp"}</script>'
        )

        candidates = self.module.parse_xiaohongshu_search_html(
            html_text,
            keyword="梗图",
            source_url="https://www.xiaohongshu.com/search_result?keyword=%E6%A2%97%E5%9B%BE",
        )

        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0].provider, "xiaohongshu")
        self.assertTrue(candidates[0].image_url.startswith("https://sns-webpic-qc.xhscdn.com/"))

    def test_download_candidate_validates_image_and_dedupes_by_sha256(self):
        image_bytes = b"fake-jpeg-bytes"
        expected_sha = hashlib.sha256(image_bytes).hexdigest()
        candidate = self.module.MemeCandidate(
            provider="weibo",
            source_id="100:1",
            source_url="https://weibo.com/100/ABC",
            image_url="https://wx1.sinaimg.cn/large/one.jpg",
            title="hello meme",
            author="Alice",
            score=123,
        )
        config = {"min_image_bytes": 1, "max_image_bytes": 1024, "user_agent": "test-agent"}

        def fake_http_get_bytes(url, headers=None, timeout=20, max_bytes=1024):
            return image_bytes, "image/jpeg", {"content-type": "image/jpeg"}

        with tempfile.TemporaryDirectory() as tmp:
            day_dir = Path(tmp) / "2026-05-23"
            with patch.object(self.module, "http_get_bytes", side_effect=fake_http_get_bytes):
                downloaded = self.module.download_candidate(
                    candidate,
                    rank=1,
                    day_dir=day_dir,
                    config=config,
                    seen_hashes={},
                    downloaded_at="2026-05-23T09:00:00+08:00",
                )
                duplicate = self.module.download_candidate(
                    candidate,
                    rank=2,
                    day_dir=day_dir,
                    config=config,
                    seen_hashes={expected_sha: "2026-05-23T09:00:00+08:00"},
                    downloaded_at="2026-05-23T09:00:01+08:00",
                )

            self.assertIsNotNone(downloaded)
            self.assertEqual(downloaded.sha256, expected_sha)
            self.assertTrue(Path(downloaded.local_path).exists())
            self.assertEqual(list(day_dir.rglob("*.tmp")), [])
            self.assertIsNone(duplicate)

    def test_download_candidate_rejects_non_image_content_type_without_image_extension(self):
        candidate = self.module.MemeCandidate(
            provider="weibo",
            source_id="100:1",
            source_url="https://weibo.com/100/ABC",
            image_url="https://cdn.example.com/file.bin",
            title="not image",
        )
        config = {"min_image_bytes": 1, "max_image_bytes": 1024, "user_agent": "test-agent"}

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                self.module,
                "http_get_bytes",
                return_value=(b"not-image-but-large-enough", "text/plain", {"content-type": "text/plain"}),
            ):
                with self.assertRaises(self.module.FetchError):
                    self.module.download_candidate(
                        candidate,
                        rank=1,
                        day_dir=Path(tmp),
                        config=config,
                        seen_hashes={},
                        downloaded_at="2026-05-23T09:00:00+08:00",
                    )

    def test_cli_skips_unknown_provider_and_outputs_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            out_dir = Path(tmp) / "out"
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--providers",
                    "unknown",
                    "--dry-run",
                    "--json",
                    "--config",
                    str(config_path),
                    "--out",
                    str(out_dir),
                ],
                cwd=str(PROJECT_ROOT),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertTrue(summary["dry_run"])
            self.assertIn("unknown provider skipped: unknown", summary["warnings"])

    def test_out_argument_has_priority_over_env_and_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"output_dir": str(Path(tmp) / "config-out")}), encoding="utf-8")
            args = Namespace(
                config=str(config_path),
                providers="weibo",
                max_total=None,
                max_per_provider=None,
                since_hours=None,
                dry_run=True,
                json=True,
                debug=False,
                out=str(Path(tmp) / "cli-out"),
            )

            config = self.module.build_config(args, env={"MEME_OUTPUT_DIR": str(Path(tmp) / "env-out")})

            self.assertEqual(Path(config["output_dir"]), (Path(tmp) / "cli-out").resolve())

    def test_config_loader_accepts_utf8_bom_from_powershell(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text('{"output_dir": "C:/memes"}', encoding="utf-8-sig")

            config = self.module.load_config(config_path)

            self.assertEqual(config["output_dir"], "C:/memes")

    def test_xiaohongshu_requests_fetch_disables_proxy_trust_env(self):
        calls = []

        class FakeResponse:
            status_code = 200
            text = "ok"
            headers = {"content-type": "text/html"}

        class FakeSession:
            def __init__(self):
                self.trust_env = True

            def get(self, url, params=None, headers=None, timeout=20, allow_redirects=True):
                calls.append(
                    {
                        "url": url,
                        "params": params,
                        "headers": headers,
                        "timeout": timeout,
                        "allow_redirects": allow_redirects,
                        "trust_env": self.trust_env,
                    }
                )
                return FakeResponse()

        with patch("requests.Session", return_value=FakeSession()):
            text, headers = self.module.http_get_text_requests(
                "https://www.xiaohongshu.com/search_result",
                params={"keyword": "meme"},
                headers={"User-Agent": "test"},
                timeout=45,
                disable_proxy=True,
            )

        self.assertEqual(text, "ok")
        self.assertEqual(headers["content-type"], "text/html")
        self.assertEqual(calls[0]["trust_env"], False)
        self.assertEqual(calls[0]["timeout"], 45)


if __name__ == "__main__":
    unittest.main()
