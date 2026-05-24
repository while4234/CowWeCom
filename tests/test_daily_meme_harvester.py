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
from subprocess import CompletedProcess
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
            '"another":"https://sns-webpic-qc.xhscdn.com/def.webp",'
            '"static":"https://fe-static.xhscdn.com/as/v1/app.js",'
            '"avatar":"https://sns-avatar-qc.xhscdn.com/avatar/user.jpg"}</script>'
        )

        candidates = self.module.parse_xiaohongshu_search_html(
            html_text,
            keyword="梗图",
            source_url="https://www.xiaohongshu.com/search_result?keyword=%E6%A2%97%E5%9B%BE",
        )

        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0].provider, "xiaohongshu")
        self.assertTrue(candidates[0].image_url.startswith("https://sns-webpic-qc.xhscdn.com/"))

    def test_xiaohongshu_search_params_default_to_today_image_notes(self):
        params = self.module.xiaohongshu_search_params(self.module.DEFAULT_CONFIG["xiaohongshu"], "今日热梗")

        self.assertEqual(params["keyword"], "今日热梗")
        self.assertEqual(params["sort_type"], "time_descending")
        self.assertEqual(params["note_type"], "普通笔记")
        self.assertEqual(params["time_filter"], "一天内")

    def test_xiaohongshu_browser_artifacts_extract_image_candidates(self):
        artifact = {
            "spec": {"query": "热点事件 表情包", "base_score": 500, "term": "热点事件"},
            "source_url": "https://www.xiaohongshu.com/search_result?keyword=test",
            "texts": ['{"url":"https:\\/\\/sns-webpic-qc.xhscdn.com\\/abc.jpg"}'],
            "image_urls": [
                "https://sns-webpic-qc.xhscdn.com/def.webp",
                "https://fe-static.xhscdn.com/as/v1/app.js",
                "https://sns-avatar-qc.xhscdn.com/avatar/user.jpg",
            ],
        }

        candidates = self.module.parse_xiaohongshu_browser_artifacts([artifact])

        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0].provider, "xiaohongshu")
        self.assertEqual(candidates[0].extra["source"], "persistent_browser")
        self.assertEqual(candidates[0].extra["term"], "热点事件")
        self.assertGreaterEqual(candidates[0].metrics["query_score"], 500)

    def test_provider_aliases_include_xiaohongshu(self):
        providers = self.module.parse_csv("小红书,xhs,red")

        self.assertEqual(providers, ["xiaohongshu", "xiaohongshu", "xiaohongshu"])

    def test_hot_driven_queries_prefer_terms_before_fallback_keywords(self):
        provider_config = {
            "use_hot_terms": True,
            "max_hot_terms": 1,
            "max_search_queries": 4,
            "search_patterns": ["{term}", "{term} 名场面", "{term} 表情包"],
            "fallback_keywords": ["今日热梗"],
        }
        config = {"_hot_terms": [{"word": "热点事件", "score": 9999}], "max_per_provider": 30}

        queries = self.module.build_hot_driven_queries(provider_config, config)

        self.assertEqual(queries, ["热点事件", "热点事件 名场面", "热点事件 表情包", "今日热梗"])

    def test_select_candidates_limits_each_provider_to_top_three(self):
        candidates = []
        for provider in ("weibo", "xiaohongshu"):
            for index in range(5):
                candidates.append(
                    self.module.MemeCandidate(
                        provider=provider,
                        source_id=f"{provider}-{index}",
                        source_url=f"https://example.com/{provider}/{index}",
                        image_url=f"https://cdn.example.com/{provider}-{index}.jpg",
                        score=100 - index,
                    )
                )

        selected = self.module.select_candidates_for_download(
            candidates,
            {"providers": ["weibo", "xiaohongshu"], "max_total": 6, "max_downloads_per_provider": 3},
        )

        self.assertEqual(len(selected), 6)
        self.assertEqual([item.provider for item in selected[:3]], ["weibo", "weibo", "weibo"])
        self.assertEqual([item.provider for item in selected[3:]], ["xiaohongshu", "xiaohongshu", "xiaohongshu"])

    def test_weibo_search_limits_suffixes_and_forwards_timeout(self):
        term = {"word": "热点事件", "score": 1000}
        config = {
            "_env": {},
            "_warnings": [],
            "_failed_providers": set(),
            "user_agent": "test-agent",
            "weibo": {
                "search_suffixes": ["", "名场面", "表情包", "梗"],
                "max_search_suffixes": 2,
                "request_interval_seconds": 0,
                "request_timeout_seconds": 4,
            },
        }
        payload = {"data": {"cards": []}}

        with patch.object(self.module, "http_get_json", return_value=(payload, {})) as fetch_mock:
            candidates = self.module.search_weibo_images_for_term(term, config)

        self.assertEqual(candidates, [])
        self.assertEqual(fetch_mock.call_count, 2)
        self.assertEqual(fetch_mock.call_args.kwargs["timeout"], 4)

    def test_collect_weibo_limits_search_terms(self):
        config = {
            "_env": {},
            "_warnings": [],
            "_failed_providers": set(),
            "max_per_provider": 10,
            "weibo": {
                "max_search_terms": 2,
                "search_time_budget_seconds": 30,
                "request_interval_seconds": 0,
            },
        }
        terms = [{"word": "热点一", "score": 3}, {"word": "热点二", "score": 2}, {"word": "热点三", "score": 1}]

        with patch.object(self.module, "fetch_weibo_hot_terms", return_value=terms), patch.object(
            self.module,
            "search_weibo_images_for_term",
            return_value=[],
        ) as search_mock:
            candidates = self.module.collect_weibo(config)

        self.assertEqual(candidates, [])
        self.assertEqual(search_mock.call_count, 2)

    def test_filter_candidates_dedupes_same_weibo_content_before_images(self):
        candidates = [
            self.module.MemeCandidate(
                provider="weibo",
                source_id=f"post:pic:{index}",
                source_url="https://weibo.com/100/ABC",
                image_url=f"https://wx1.sinaimg.cn/large/{index}.jpg",
                title="同一条微博多图",
                score=100 - index,
            )
            for index in range(3)
        ]

        filtered, skipped = self.module.filter_candidates(candidates, {"skip_sensitive": True, "dedupe_same_content": True})

        self.assertEqual(len(filtered), 1)
        self.assertEqual(skipped, 2)

    def test_filter_candidates_skips_serious_incident_terms_by_default(self):
        candidate = self.module.MemeCandidate(
            provider="weibo",
            source_id="serious",
            source_url="https://weibo.com/100/ABC",
            image_url="https://wx1.sinaimg.cn/large/serious.jpg",
            title="煤矿爆炸事故救援最新通报",
            score=100,
        )

        filtered, skipped = self.module.filter_candidates(
            [candidate],
            {"skip_sensitive": True, "dedupe_same_content": True, "block_keywords": self.module.DEFAULT_CONFIG["block_keywords"]},
        )

        self.assertEqual(filtered, [])
        self.assertEqual(skipped, 1)

    def test_select_candidates_dedupes_same_hot_topic_across_providers_without_backfill(self):
        candidates = [
            self.module.MemeCandidate(
                provider="weibo",
                source_id="weibo-1",
                source_url="https://weibo.com/1",
                image_url="https://cdn.example.com/weibo-1.jpg",
                score=100,
                extra={"term": "同一个热点"},
            ),
            self.module.MemeCandidate(
                provider="weibo",
                source_id="weibo-2",
                source_url="https://weibo.com/2",
                image_url="https://cdn.example.com/weibo-2.jpg",
                score=90,
                extra={"term": "微博独有"},
            ),
            self.module.MemeCandidate(
                provider="xiaohongshu",
                source_id="xhs-1",
                source_url="https://www.xiaohongshu.com/search_result?keyword=1",
                image_url="https://cdn.example.com/xhs-1.jpg",
                score=99,
                extra={"term": "同一个热点"},
            ),
            self.module.MemeCandidate(
                provider="xiaohongshu",
                source_id="xhs-2",
                source_url="https://www.xiaohongshu.com/search_result?keyword=2",
                image_url="https://cdn.example.com/xhs-2.jpg",
                score=80,
                extra={"term": "小红书独有"},
            ),
        ]

        selected = self.module.select_candidates_for_download(
            candidates,
            {"providers": ["weibo", "xiaohongshu"], "max_total": 4, "max_downloads_per_provider": 2},
        )

        self.assertEqual([item.source_id for item in selected], ["weibo-1", "weibo-2", "xhs-2"])

    def test_same_day_seen_filters_previous_hot_topic_before_top_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            now = self.module.now_for_config({"timezone": "Asia/Shanghai"})
            day_key = now.date().isoformat()
            self.module.atomic_write_json(
                self.module.daily_seen_path(output_dir),
                {day_key: {"topic:samehot": now.isoformat()}},
            )
            candidates = [
                self.module.MemeCandidate(
                    provider="weibo",
                    source_id="same",
                    source_url="https://weibo.com/same",
                    image_url="https://cdn.example.com/same.jpg",
                    title="same",
                    score=100,
                    extra={"term": "same hot"},
                ),
                self.module.MemeCandidate(
                    provider="weibo",
                    source_id="fresh",
                    source_url="https://weibo.com/fresh",
                    image_url="https://cdn.example.com/fresh.jpg",
                    title="fresh",
                    score=90,
                    extra={"term": "fresh hot"},
                ),
            ]

            filtered, skipped = self.module.filter_same_day_seen(
                candidates,
                {"output_dir": str(output_dir), "timezone": "Asia/Shanghai", "dedupe_days": 90},
            )

            self.assertEqual(skipped, 1)
            self.assertEqual([item.source_id for item in filtered], ["fresh"])

    def test_xiaohongshu_runs_proxy_guard_once_then_retries(self):
        html_text = '<script>{"url":"https:\\/\\/sns-webpic-qc.xhscdn.com\\/abc.jpg"}</script>'
        config = {
            "_env": {},
            "_warnings": [],
            "_failed_providers": set(),
            "_hot_terms": [{"word": "热点事件", "score": 1000}],
            "user_agent": "test-agent",
            "max_per_provider": 1,
            "proxy_guard": {
                "enabled": True,
                "script": str(SCRIPT),
                "providers": ["xiaohongshu"],
                "timeout_seconds": 1,
            },
            "xiaohongshu": {
                "search_patterns": ["{term}"],
                "fallback_keywords": [],
                "max_search_queries": 1,
                "request_interval_seconds": 0,
                "request_timeout_seconds": 1,
                "endpoint_search": "https://www.xiaohongshu.com/search_result",
                "disable_proxy": True,
                "use_requests": True,
            },
        }

        with patch.object(
            self.module,
            "http_get_text_requests",
            side_effect=[self.module.FetchError("Network error for xhs"), (html_text, {})],
        ), patch.object(
            self.module.subprocess,
            "run",
            return_value=CompletedProcess(args=["python"], returncode=0, stdout="{}", stderr=""),
        ) as run_mock:
            candidates = self.module.collect_xiaohongshu(config)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].provider, "xiaohongshu")
        self.assertEqual(run_mock.call_count, 1)
        self.assertTrue(any("rule guard" in warning for warning in config["_warnings"]))

    def test_xiaohongshu_dry_run_does_not_run_proxy_guard(self):
        config = {
            "_env": {},
            "_warnings": [],
            "_failed_providers": set(),
            "_hot_terms": [{"word": "热点事件", "score": 1000}],
            "dry_run": True,
            "user_agent": "test-agent",
            "max_per_provider": 1,
            "proxy_guard": {
                "enabled": True,
                "script": str(SCRIPT),
                "providers": ["xiaohongshu"],
                "timeout_seconds": 1,
            },
            "xiaohongshu": {
                "search_patterns": ["{term}"],
                "fallback_keywords": [],
                "max_search_queries": 1,
                "request_interval_seconds": 0,
                "request_timeout_seconds": 1,
                "endpoint_search": "https://www.xiaohongshu.com/search_result",
                "disable_proxy": True,
                "use_requests": True,
            },
        }

        with patch.object(
            self.module,
            "http_get_text_requests",
            side_effect=self.module.FetchError("Network error for xhs"),
        ), patch.object(self.module.subprocess, "run") as run_mock:
            candidates = self.module.collect_xiaohongshu(config)

        self.assertEqual(candidates, [])
        self.assertEqual(run_mock.call_count, 0)
        self.assertTrue(any("xiaohongshu search failed" in warning for warning in config["_warnings"]))

    def test_xiaohongshu_prefers_browser_collection_over_http(self):
        browser_candidate = self.module.MemeCandidate(
            provider="xiaohongshu",
            source_id="browser-1",
            source_url="https://www.xiaohongshu.com/search_result?keyword=test",
            image_url="https://sns-webpic-qc.xhscdn.com/browser.jpg",
        )
        config = {
            "_env": {},
            "_warnings": [],
            "_failed_providers": set(),
            "_hot_terms": [{"word": "热点事件", "score": 1000}],
            "user_agent": "test-agent",
            "max_per_provider": 3,
            "xiaohongshu": {
                "browser": {"enabled": True},
                "search_patterns": ["{term}"],
                "fallback_keywords": [],
                "max_search_queries": 1,
            },
        }

        with patch.object(self.module, "collect_xiaohongshu_browser", return_value=[browser_candidate]) as browser_mock, patch.object(
            self.module, "collect_xiaohongshu_http"
        ) as http_mock:
            candidates = self.module.collect_xiaohongshu(config)

        self.assertEqual(candidates, [browser_candidate])
        self.assertEqual(browser_mock.call_count, 1)
        self.assertEqual(http_mock.call_count, 0)

    def test_xiaohongshu_http_fallback_limits_queries(self):
        html_text = '<script>{"url":"https:\\/\\/sns-webpic-qc.xhscdn.com\\/abc.jpg"}</script>'
        config = {
            "_env": {},
            "_warnings": [],
            "_failed_providers": set(),
            "_hot_terms": [{"word": "热点一", "score": 1000}, {"word": "热点二", "score": 900}, {"word": "热点三", "score": 800}],
            "user_agent": "test-agent",
            "max_per_provider": 10,
            "proxy_guard": {"enabled": False, "providers": ["xiaohongshu"]},
            "xiaohongshu": {
                "search_patterns": ["{term}"],
                "fallback_keywords": [],
                "max_search_queries": 3,
                "request_interval_seconds": 0,
                "request_timeout_seconds": 1,
                "http_fallback_max_queries": 2,
                "http_time_budget_seconds": 10,
                "endpoint_search": "https://www.xiaohongshu.com/search_result",
                "disable_proxy": True,
                "use_requests": True,
            },
        }

        with patch.object(self.module, "collect_xiaohongshu_browser", return_value=[]), patch.object(
            self.module,
            "http_get_text_requests",
            return_value=(html_text, {}),
        ) as fetch_mock:
            candidates = self.module.collect_xiaohongshu(config)

        self.assertEqual(fetch_mock.call_count, 2)
        self.assertGreaterEqual(len(candidates), 1)

    def test_xiaohongshu_dry_run_uses_fallback_terms_without_weibo_hot_fetch(self):
        config = {
            "_env": {},
            "_warnings": [],
            "_failed_providers": set(),
            "dry_run": True,
            "user_agent": "test-agent",
            "max_per_provider": 2,
            "proxy_guard": {"enabled": False, "providers": ["xiaohongshu"]},
            "xiaohongshu": {
                "browser": {"enabled": False},
                "use_hot_terms": True,
                "search_patterns": ["{term}"],
                "fallback_keywords": ["今日热梗"],
                "max_search_queries": 1,
                "request_interval_seconds": 0,
                "request_timeout_seconds": 1,
                "http_fallback_max_queries": 1,
                "http_time_budget_seconds": 5,
                "endpoint_search": "https://www.xiaohongshu.com/search_result",
                "disable_proxy": True,
                "use_requests": True,
            },
        }

        with patch.object(self.module, "fetch_weibo_hot_terms") as hot_terms_mock, patch.object(
            self.module,
            "http_get_text_requests",
            return_value=('<script>{"url":"https:\\/\\/sns-webpic-qc.xhscdn.com\\/abc.jpg"}</script>', {}),
        ):
            candidates = self.module.collect_xiaohongshu(config)

        self.assertEqual(hot_terms_mock.call_count, 0)
        self.assertEqual(candidates[0].extra["query"], "今日热梗")

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

    def test_legacy_weibo_only_default_config_migrates_to_current_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps({"providers": ["weibo"], "max_total": 3, "output_dir": str(Path(tmp) / "out")}),
                encoding="utf-8",
            )
            args = Namespace(
                config=str(config_path),
                providers=None,
                max_total=None,
                max_per_provider=None,
                since_hours=None,
                dry_run=True,
                json=True,
                debug=False,
                out=None,
            )

            config = self.module.build_config(args, env={})

            self.assertEqual(config["providers"], ["weibo", "xiaohongshu"])
            self.assertEqual(config["max_total"], 6)
            self.assertEqual(config["max_downloads_per_provider"], 3)
            self.assertIn("legacy weibo-only default config detected", config["_warnings"][0])

    def test_legacy_xiaohongshu_http_defaults_migrate_to_bounded_browser_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "config_version": 2,
                        "providers": ["xiaohongshu"],
                        "xiaohongshu": {
                            "max_search_queries": 12,
                            "request_interval_seconds": 2,
                            "request_timeout_seconds": 20,
                        },
                    }
                ),
                encoding="utf-8",
            )
            args = Namespace(
                config=str(config_path),
                providers=None,
                max_total=None,
                max_per_provider=None,
                since_hours=None,
                dry_run=True,
                json=True,
                debug=False,
                out=None,
            )

            config = self.module.build_config(args, env={})

            self.assertEqual(config["xiaohongshu"]["max_search_queries"], self.module.DEFAULT_CONFIG["xiaohongshu"]["max_search_queries"])
            self.assertEqual(
                config["xiaohongshu"]["request_timeout_seconds"],
                self.module.DEFAULT_CONFIG["xiaohongshu"]["request_timeout_seconds"],
            )
            self.assertEqual(
                config["xiaohongshu"]["browser"]["user_data_dir"],
                self.module.XIAOHONGSHU_BROWSER_PROFILE_DIR,
            )
            self.assertTrue(any("legacy xiaohongshu HTTP defaults detected" in warning for warning in config["_warnings"]))

    def test_legacy_block_keywords_are_extended_with_serious_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps({"config_version": 3, "block_keywords": ["nsfw"]}),
                encoding="utf-8",
            )
            args = Namespace(
                config=str(config_path),
                providers="weibo",
                max_total=None,
                max_per_provider=None,
                since_hours=None,
                dry_run=True,
                json=True,
                debug=False,
                out=None,
            )

            config = self.module.build_config(args, env={})

            self.assertIn("nsfw", config["block_keywords"])
            self.assertIn("爆炸", config["block_keywords"])
            self.assertTrue(any("legacy block keyword defaults extended" in warning for warning in config["_warnings"]))

    def test_xiaohongshu_browser_profile_env_overrides_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "config_version": 3,
                        "xiaohongshu": {"browser": {"user_data_dir": self.module.XIAOHONGSHU_BROWSER_PROFILE_DIR}},
                    }
                ),
                encoding="utf-8",
            )
            profile_override = str(Path(tmp) / "xhs-profile")
            args = Namespace(
                config=str(config_path),
                providers=None,
                max_total=None,
                max_per_provider=None,
                since_hours=None,
                dry_run=True,
                json=True,
                debug=False,
                out=None,
            )

            config = self.module.build_config(args, env={self.module.XIAOHONGSHU_BROWSER_PROFILE_ENV: profile_override})

            self.assertEqual(config["xiaohongshu"]["browser"]["user_data_dir"], profile_override)

    def test_weibo_browser_profile_env_overrides_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "config_version": 4,
                        "weibo": {"browser": {"user_data_dir": self.module.WEIBO_BROWSER_PROFILE_DIR}},
                    }
                ),
                encoding="utf-8",
            )
            profile_override = str(Path(tmp) / "weibo-profile")
            args = Namespace(
                config=str(config_path),
                providers=None,
                max_total=None,
                max_per_provider=None,
                since_hours=None,
                dry_run=True,
                json=True,
                debug=False,
                out=None,
            )

            config = self.module.build_config(args, env={self.module.WEIBO_BROWSER_PROFILE_ENV: profile_override})

            self.assertEqual(config["weibo"]["browser"]["user_data_dir"], profile_override)

    def test_xiaohongshu_risk_challenge_text_is_detected(self):
        self.assertTrue(self.module.looks_like_xiaohongshu_challenge("当前IP存在风险，请稍后再试"))

    def test_open_xiaohongshu_profile_window_uses_dedicated_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "xhs-profile"
            chrome = Path(tmp) / "chrome.exe"
            chrome.write_text("", encoding="utf-8")
            config = {
                "xiaohongshu": {"browser": {"user_data_dir": str(profile)}},
            }

            with patch.object(self.module, "find_chrome_executable", return_value=chrome), patch.object(
                self.module.subprocess,
                "Popen",
            ) as popen_mock:
                summary = self.module.open_xiaohongshu_profile_window(config)

        self.assertTrue(summary["opened_xiaohongshu_profile"])
        self.assertEqual(Path(summary["profile"]), profile.resolve())
        args = popen_mock.call_args.args[0]
        self.assertIn(f"--user-data-dir={profile.resolve()}", args)
        self.assertIn("https://www.xiaohongshu.com/explore", args)

    def test_open_weibo_profile_window_uses_dedicated_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "weibo-profile"
            chrome = Path(tmp) / "chrome.exe"
            chrome.write_text("", encoding="utf-8")
            config = {
                "weibo": {"browser": {"user_data_dir": str(profile)}},
            }

            with patch.object(self.module, "find_chrome_executable", return_value=chrome), patch.object(
                self.module.subprocess,
                "Popen",
            ) as popen_mock:
                summary = self.module.open_weibo_profile_window(config)

        self.assertTrue(summary["opened_weibo_profile"])
        self.assertEqual(Path(summary["profile"]), profile.resolve())
        args = popen_mock.call_args.args[0]
        self.assertIn(f"--user-data-dir={profile.resolve()}", args)
        self.assertIn("https://weibo.com/", args)

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
