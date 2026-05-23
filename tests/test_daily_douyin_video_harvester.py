import contextlib
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "skills" / "daily-douyin-video-harvester" / "scripts" / "harvest_douyin_videos.py"


def load_harvester_module():
    spec = importlib.util.spec_from_file_location("daily_douyin_video_harvester", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DailyDouyinVideoHarvesterTest(unittest.TestCase):
    def setUp(self):
        self.module = load_harvester_module()

    def test_meme_potential_prefers_funny_topics_over_hard_news(self):
        payload = {
            "data": {
                "word_list": [
                    {"word": "国防部正告日方", "hot_value": "11849702"},
                    {"word": "男子离谱名场面笑死网友", "hot_value": "300000"},
                    {"word": "公司发布会公布财报", "hot_value": "200000"},
                ]
            }
        }

        terms = self.module.parse_douyin_hot_terms(payload, max_terms=3)
        ranked = self.module.rank_hot_terms_for_memes(terms, self.module.DEFAULT_CONFIG)

        self.assertEqual(ranked[0].word, "男子离谱名场面笑死网友")
        self.assertTrue(all("国防部" not in term.word for term in ranked))
        self.assertGreater(ranked[0].meme_score, 0)

    def test_public_hot_meme_topics_outrank_movie_recommendations(self):
        hot_score, hot_reasons = self.module.score_meme_potential("安卓人全网热议 网友锐评这也太离谱", self.module.DEFAULT_CONFIG)
        movie_score, _movie_reasons = self.module.score_meme_potential("电影推荐 好剧推荐 影视解说 生活vlog", self.module.DEFAULT_CONFIG)
        nvidia_score, nvidia_reasons = self.module.score_meme_potential("黄仁勋最后一刻上空军一号 网友热议名场面", self.module.DEFAULT_CONFIG)

        self.assertGreater(hot_score, 3000)
        self.assertGreater(nvidia_score, 3000)
        self.assertLess(movie_score, 0)
        self.assertTrue(any("热点" in reason for reason in hot_reasons + nvidia_reasons))

    def test_browser_hot_board_terms_feed_search_queries(self):
        artifacts = [
            {
                "source_url": "https://www.douyin.com/hot",
                "payload": {
                    "data": {
                        "word_list": [
                            {"word": "黄仁勋最后一刻上空军一号", "hot_value": 9000000},
                            {"word": "电影推荐好剧解说", "hot_value": 8000000},
                            {"word": "安卓人又被网友锐评", "hot_value": 7000000},
                        ]
                    }
                },
            }
        ]

        terms = self.module.rank_hot_terms_for_memes(
            self.module.browser_hot_terms_from_artifacts(artifacts, self.module.DEFAULT_CONFIG),
            self.module.DEFAULT_CONFIG,
        )
        queries = self.module.build_search_queries(terms, self.module.DEFAULT_CONFIG)

        self.assertTrue(any(term.word == "黄仁勋最后一刻上空军一号" for term in terms))
        self.assertTrue(any("安卓人" in item["query"] for item in queries))
        self.assertFalse(any("电影推荐" in item["query"] for item in queries[:3]))

    def test_neutral_hot_terms_are_searched_before_generic_fallbacks(self):
        terms = [
            self.module.HotTerm(word="某明星机场回应", rank=1, score=9000),
            self.module.HotTerm(word="电影推荐好剧解说", rank=2, score=8000),
        ]

        ranked = self.module.rank_hot_terms_for_search(terms, self.module.DEFAULT_CONFIG)
        queries = self.module.build_search_queries(ranked, self.module.DEFAULT_CONFIG)

        self.assertEqual(ranked[0].word, "某明星机场回应")
        self.assertFalse(any(term.word == "电影推荐好剧解说" for term in ranked))
        self.assertEqual(queries[0]["query"], "某明星机场回应")

    def test_fallback_queries_do_not_pin_stale_specific_memes(self):
        queries = self.module.build_search_queries([], self.module.DEFAULT_CONFIG)
        query_text = " ".join(item["query"] for item in queries)

        self.assertNotIn("安卓人", query_text)
        self.assertNotIn("黄仁勋 空军一号", query_text)
        self.assertTrue(all(("今日" in item["query"] or "热" in item["query"] or "全网" in item["query"] or "网友" in item["query"]) for item in queries))

    def test_parse_search_html_extracts_video_candidate_and_scores(self):
        html_text = """
        <script>
        window.__DATA__ = {
          "aweme_id": "7373",
          "desc": "这也太离谱了 名场面",
          "author": {"nickname": "creator"},
          "statistics": {"digg_count": 10, "comment_count": 2, "share_count": 3, "collect_count": 1},
          "video": {
            "play_addr": {"url_list": ["https:\\/\\/v3-dy-o.zjcdn.com\\/abc.mp4"]},
            "cover": {"url_list": ["https:\\/\\/p3-pc.douyinpic.com\\/cover.jpeg"]}
          }
        };
        </script>
        """

        candidates = self.module.parse_douyin_search_html(
            html_text,
            keyword="离谱名场面",
            source_url="https://www.douyin.com/search/test",
            base_score=1000,
            config=self.module.DEFAULT_CONFIG,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_id, "7373:video:1")
        self.assertIn("zjcdn.com/abc.mp4", candidates[0].video_url)
        self.assertEqual(candidates[0].cover_url, "https://p3-pc.douyinpic.com/cover.jpeg")
        self.assertEqual(candidates[0].author, "creator")
        self.assertGreater(candidates[0].score, 1000)

    def test_sharp_commentary_is_not_stiff(self):
        candidate = self.module.DouyinVideoCandidate(
            source_id="1",
            source_url="https://www.douyin.com/video/1",
            video_url="https://v3-dy-o.zjcdn.com/1.mp4",
            title="离谱名场面笑死网友",
            meme_score=2000,
            extra={"meme_reasons": ["梗感:离谱", "梗感:名场面"]},
        )

        commentary = self.module.generate_commentary(candidate, "sharp")

        self.assertIn("锐评", commentary)
        self.assertIn("离谱", commentary)
        self.assertNotIn("这是一个热点视频", commentary)

    def test_dedupe_candidates_keeps_one_item_for_same_video_content(self):
        candidates = [
            self.module.DouyinVideoCandidate(
                source_id=f"7373:video:{index}",
                source_url="https://www.douyin.com/video/7373",
                video_url=f"https://v3-dy-o.zjcdn.com/{index}.mp4",
                title="同一个视频多地址",
                score=100 - index,
                meme_score=100,
            )
            for index in range(1, 4)
        ]

        deduped = self.module.dedupe_candidates(candidates)

        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].source_id, "7373:video:1")

    def test_browser_artifacts_extract_candidates_without_signature_generation(self):
        payload = {
            "aweme_list": [
                {
                    "aweme_id": "browser-1",
                    "desc": "反转名场面 笑死网友",
                    "statistics": {"digg_count": 100, "comment_count": 20, "share_count": 5},
                    "video": {"play_addr": {"url_list": ["https://v26-web.douyinvod.com/browser.mp4"]}},
                }
            ]
        }

        candidates = self.module.browser_candidate_artifacts_to_candidates(
            [
                {
                    "source_url": "https://www.douyin.com/aweme/v2/web/module/feed/",
                    "keyword": "反转名场面",
                    "base_score": 500,
                    "payload": payload,
                }
            ],
            self.module.DEFAULT_CONFIG,
        )

        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].source_id.endswith(":video:1"))
        self.assertIn("douyinvod.com/browser.mp4", candidates[0].video_url)

    def test_nested_media_nodes_are_not_candidates_by_themselves(self):
        payload = {
            "aweme_list": [
                {
                    "video": {
                        "play_addr": {
                            "url_list": ["https://v26-web.douyinvod.com/nested-only.mp4"]
                        }
                    }
                }
            ]
        }

        candidates = self.module.extract_candidates_from_payload(
            payload,
            source_url="https://www.douyin.com/aweme/v2/web/module/feed/",
            hot_term="",
            base_score=0,
            config=self.module.DEFAULT_CONFIG,
        )

        self.assertEqual(candidates, [])

    def test_generic_browser_feed_artifacts_do_not_get_hot_topic_boost(self):
        payload = {
            "aweme_list": [
                {
                    "aweme_id": "movie-1",
                    "desc": "电影推荐 好剧推荐 影视解说",
                    "statistics": {"digg_count": 99999, "comment_count": 1, "share_count": 1},
                    "video": {"play_addr": {"url_list": ["https://v26-web.douyinvod.com/movie.mp4"]}},
                }
            ]
        }

        candidates = self.module.browser_candidate_artifacts_to_candidates(
            [
                {
                    "source_url": "https://www.douyin.com/aweme/v2/web/module/feed/",
                    "base_score": 0,
                    "payload": payload,
                }
            ],
            self.module.DEFAULT_CONFIG,
        )

        self.assertEqual(candidates, [])

    def test_search_artifact_can_promote_public_discussion_meme_topic(self):
        payload = {
            "aweme_list": [
                {
                    "aweme_id": "android-1",
                    "desc": "安卓人全网热议 网友锐评这也太离谱",
                    "statistics": {"digg_count": 120, "comment_count": 30, "share_count": 10},
                    "video": {"play_addr": {"url_list": ["https://v26-web.douyinvod.com/android.mp4"]}},
                }
            ]
        }

        candidates = self.module.browser_candidate_artifacts_to_candidates(
            [
                {
                    "source_url": "https://www.douyin.com/search/%E5%AE%89%E5%8D%93%E4%BA%BA",
                    "keyword": "安卓人 热梗",
                    "base_score": 3700,
                    "payload": payload,
                }
            ],
            self.module.DEFAULT_CONFIG,
        )

        self.assertEqual(len(candidates), 1)
        self.assertGreater(candidates[0].meme_score, 3000)

    def test_cookie_header_parser_builds_browser_context_cookies(self):
        cookies = self.module.cookies_from_header("sid_guard=abc=123; passport=xyz", [".douyin.com"])

        self.assertEqual(
            cookies,
            [
                {"name": "sid_guard", "value": "abc=123", "domain": ".douyin.com", "path": "/"},
                {"name": "passport", "value": "xyz", "domain": ".douyin.com", "path": "/"},
            ],
        )

    def test_blocking_challenge_checks_visible_text_not_embedded_scripts(self):
        class FakeLocator:
            def inner_text(self, timeout=3000):
                return "正常热点内容"

        class FakePage:
            def locator(self, selector):
                return FakeLocator()

        html_with_captcha_script = "<script>var captcha='secsdk'</script><main>正常热点内容</main>"

        self.assertTrue(self.module.looks_like_blocking_challenge(html_with_captcha_script))
        self.assertFalse(self.module.looks_like_blocking_challenge(self.module.visible_page_text(FakePage())))

    def test_browser_collection_mode_skips_http_hot_payloads(self):
        candidate = self.module.DouyinVideoCandidate(
            source_id="browser:video:1",
            source_url="https://www.douyin.com/video/browser",
            video_url="https://v26-web.douyinvod.com/browser.mp4",
            title="browser only",
            score=100,
            meme_score=100,
        )
        config = {
            **self.module.DEFAULT_CONFIG,
            "_env": {"DOUYIN_COOKIE": "sid=1"},
            "_warnings": [],
            "collection_mode": "browser",
            "max_candidates": 3,
        }

        with patch.object(self.module, "fetch_hot_payloads") as hot_mock, patch.object(
            self.module,
            "collect_douyin_browser_fallback",
            return_value=[candidate],
        ):
            candidates = self.module.collect_douyin(config)

        hot_mock.assert_not_called()
        self.assertEqual(candidates, [candidate])

    def test_same_day_seen_filters_previous_hot_video_before_top_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            now = self.module.now_for_config({"timezone": "Asia/Shanghai"})
            day_key = now.date().isoformat()
            self.module.atomic_write_json(
                self.module.daily_seen_path(output_dir),
                {day_key: {"topic:samehot": now.isoformat()}},
            )
            candidates = [
                self.module.DouyinVideoCandidate(
                    source_id="same:video:1",
                    source_url="https://www.douyin.com/video/same",
                    video_url="https://v26-web.douyinvod.com/same.mp4",
                    title="same",
                    hot_term="same hot",
                    score=100,
                    meme_score=100,
                ),
                self.module.DouyinVideoCandidate(
                    source_id="fresh:video:1",
                    source_url="https://www.douyin.com/video/fresh",
                    video_url="https://v26-web.douyinvod.com/fresh.mp4",
                    title="fresh",
                    hot_term="fresh hot",
                    score=90,
                    meme_score=90,
                ),
            ]

            filtered, skipped = self.module.filter_same_day_seen(
                candidates,
                {"output_dir": str(output_dir), "timezone": "Asia/Shanghai", "dedupe_days": 7},
            )

            self.assertEqual(skipped, 1)
            self.assertEqual([item.source_id for item in filtered], ["fresh:video:1"])

    def test_recent_filter_skips_old_known_videos_and_penalizes_unknown_time(self):
        now = self.module.now_for_config({"timezone": "Asia/Shanghai"})
        old = self.module.DouyinVideoCandidate(
            source_id="old:video:1",
            source_url="https://www.douyin.com/video/old",
            video_url="https://v26-web.douyinvod.com/old.mp4",
            title="old",
            created_at=(now - self.module.dt.timedelta(hours=72)).isoformat(),
            score=5000,
            meme_score=1000,
        )
        unknown = self.module.DouyinVideoCandidate(
            source_id="unknown:video:1",
            source_url="https://www.douyin.com/video/unknown",
            video_url="https://v26-web.douyinvod.com/unknown.mp4",
            title="unknown",
            score=5000,
            meme_score=1000,
        )
        fresh = self.module.DouyinVideoCandidate(
            source_id="fresh:video:1",
            source_url="https://www.douyin.com/video/fresh",
            video_url="https://v26-web.douyinvod.com/fresh.mp4",
            title="fresh",
            created_at=(now - self.module.dt.timedelta(hours=2)).isoformat(),
            score=5000,
            meme_score=1000,
        )

        filtered, skipped = self.module.filter_recent_candidates(
            [old, unknown, fresh],
            {
                "timezone": "Asia/Shanghai",
                "since_hours": 48,
                "unknown_created_at_penalty": 1500,
                "freshness_bonus": 1000,
            },
        )

        self.assertEqual(skipped, 1)
        self.assertEqual([item.source_id for item in filtered], ["unknown:video:1", "fresh:video:1"])
        self.assertLess(filtered[0].score, 5000)
        self.assertGreater(filtered[1].score, 5000)

    def test_select_candidates_limits_one_video_per_hot_term_by_default(self):
        candidates = [
            self.module.DouyinVideoCandidate(
                source_id=f"same-{index}:video:1",
                source_url=f"https://www.douyin.com/video/same-{index}",
                video_url=f"https://v26-web.douyinvod.com/same-{index}.mp4",
                title=f"same topic {index}",
                hot_term="same hot topic",
                score=1000 - index,
                meme_score=2000 - index,
            )
            for index in range(3)
        ]
        candidates.append(
            self.module.DouyinVideoCandidate(
                source_id="other:video:1",
                source_url="https://www.douyin.com/video/other",
                video_url="https://v26-web.douyinvod.com/other.mp4",
                title="other topic",
                hot_term="other hot topic",
                score=1,
                meme_score=1,
            )
        )

        selected = self.module.select_candidates(candidates, {"max_total": 3, "max_per_hot_term": 1})

        self.assertEqual([item.source_id for item in selected], ["same-0:video:1", "other:video:1"])

    def test_download_video_validates_and_dedupes_by_sha256(self):
        video_bytes = b"0000ftypmp4 fake video bytes"
        expected_sha = hashlib.sha256(video_bytes).hexdigest()
        candidate = self.module.DouyinVideoCandidate(
            source_id="7373:video:1",
            source_url="https://www.douyin.com/video/7373",
            video_url="https://v3-dy-o.zjcdn.com/abc.mp4",
            title="离谱名场面",
            score=1234,
            meme_score=1200,
        )
        config = {
            **self.module.DEFAULT_CONFIG,
            "_env": {},
            "min_video_bytes": 1,
            "max_video_bytes": 1024,
            "commentary_style": "sharp",
        }

        def fake_http_get_bytes(url, headers=None, timeout=30, max_bytes=1024):
            return video_bytes, "video/mp4", {"content-type": "video/mp4"}

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
                    downloaded_at="2026-05-23T09:01:00+08:00",
                )

            self.assertIsNotNone(downloaded)
            self.assertEqual(downloaded.sha256, expected_sha)
            self.assertTrue(Path(downloaded.local_path).exists())
            self.assertEqual(list(day_dir.rglob("*.tmp")), [])
            self.assertIsNone(duplicate)

    def test_too_large_video_falls_back_to_cover_download(self):
        candidate = self.module.DouyinVideoCandidate(
            source_id="7373:video:1",
            source_url="https://www.douyin.com/video/7373",
            video_url="https://v3-dy-o.zjcdn.com/huge.mp4",
            cover_url="https://p3-pc.douyinpic.com/cover.jpeg",
            title="funny hot moment",
            score=1234,
            meme_score=1200,
        )
        config = {
            **self.module.DEFAULT_CONFIG,
            "_env": {},
            "min_video_bytes": 1,
            "max_video_bytes": 1024,
            "max_cover_bytes": 1024,
            "commentary_style": "sharp",
        }

        def fake_http_get_bytes(url, headers=None, timeout=20, max_bytes=1024):
            return b"fake-cover", "image/jpeg", {"content-type": "image/jpeg"}

        with tempfile.TemporaryDirectory() as tmp:
            day_dir = Path(tmp) / "2026-05-23"
            with patch.object(
                self.module,
                "fetch_video_with_retries",
                side_effect=self.module.FetchError("video too large by content-length: 999999999"),
            ), patch.object(self.module, "http_get_bytes", side_effect=fake_http_get_bytes):
                downloaded = self.module.download_candidate(
                    candidate,
                    rank=1,
                    day_dir=day_dir,
                    config=config,
                    seen_hashes={},
                    downloaded_at="2026-05-23T09:00:00+08:00",
                )

            self.assertEqual(downloaded.local_path, "")
            self.assertTrue(Path(downloaded.cover_local_path).exists())
            self.assertEqual(downloaded.send_status, "pending_cover_only")

    def test_cleanup_due_files_deletes_only_queued_due_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            due_file = output_dir / "old.mp4"
            future_file = output_dir / "new.mp4"
            due_file.write_bytes(b"old")
            future_file.write_bytes(b"new")
            now = self.module.now_for_config({"timezone": "Asia/Shanghai"})
            self.module.save_cleanup_queue(
                output_dir,
                [
                    {
                        "local_path": str(due_file),
                        "delete_after": (now - self.module.dt.timedelta(minutes=1)).isoformat(),
                        "status": "queued",
                    },
                    {
                        "local_path": str(future_file),
                        "delete_after": (now + self.module.dt.timedelta(hours=1)).isoformat(),
                        "status": "queued",
                    },
                ],
            )

            result = self.module.cleanup_due_files({"output_dir": str(output_dir), "timezone": "Asia/Shanghai"})

            self.assertEqual(result["deleted_count"], 1)
            self.assertFalse(due_file.exists())
            self.assertTrue(future_file.exists())

    def test_schedule_windows_cleanup_uses_short_command_file(self):
        calls = []

        def fake_run(args, capture_output=True, text=True, encoding="utf-8", timeout=20):
            calls.append(args)

            class Completed:
                returncode = 0
                stdout = ""
                stderr = ""

            return Completed()

        with tempfile.TemporaryDirectory() as tmp:
            config = {
                **self.module.DEFAULT_CONFIG,
                "output_dir": str(Path(tmp)),
                "_config_path": str(Path(tmp) / "config.json"),
                "delete_after_hours": 24,
                "schedule_cleanup": True,
            }
            with patch.object(self.module.os, "name", "nt"), patch.object(self.module.subprocess, "run", side_effect=fake_run):
                warning = self.module.schedule_windows_cleanup(config)

            self.assertIsNone(warning)
            self.assertEqual(calls[0][calls[0].index("/TR") + 1].startswith('cmd /c "'), True)
            self.assertLess(len(calls[0][calls[0].index("/TR") + 1]), 261)
            self.assertEqual(len(list((Path(tmp) / "state").glob("cleanup_*.cmd"))), 1)

    def test_send_downloaded_videos_uses_fake_sender(self):
        calls = []

        class FakeSender:
            def __init__(self, bot_id, secret, receiver, is_group, websocket_url):
                calls.append(("init", bot_id, secret, receiver, is_group, websocket_url))

            def connect(self):
                calls.append(("connect",))

            def send_markdown(self, content):
                calls.append(("markdown", content))

            def send_video(self, path):
                calls.append(("video", path))

            def send_image(self, path):
                calls.append(("image", path))

            def close(self):
                calls.append(("close",))

        item = self.module.DownloadedDouyinVideo(
            source_id="1",
            source_url="https://www.douyin.com/video/1",
            video_url="https://v3-dy-o.zjcdn.com/1.mp4",
            title="离谱名场面",
            local_path="D:/tmp/video.mp4",
            commentary="锐评：很好笑",
        )
        config = {
            **self.module.DEFAULT_CONFIG,
            "_env": {"WECOM_BOT_RECEIVER": "user1", "WECOM_BOT_ID": "bot", "WECOM_BOT_SECRET": "secret"},
            "_warnings": [],
        }

        sent = self.module.send_downloaded_videos([item], config, sender_factory=FakeSender)

        self.assertEqual(sent, 1)
        self.assertEqual(item.send_status, "sent_video")
        self.assertTrue(any(call[0] == "markdown" and "锐评" in call[1] for call in calls))
        self.assertIn(("video", "D:/tmp/video.mp4"), calls)

    def test_send_downloaded_videos_sends_cover_when_video_was_too_large(self):
        calls = []

        class FakeSender:
            def __init__(self, bot_id, secret, receiver, is_group, websocket_url):
                pass

            def connect(self):
                calls.append(("connect",))

            def send_markdown(self, content):
                calls.append(("markdown", content))

            def send_video(self, path):
                calls.append(("video", path))

            def send_image(self, path):
                calls.append(("image", path))

            def close(self):
                calls.append(("close",))

        item = self.module.DownloadedDouyinVideo(
            source_id="1",
            source_url="https://www.douyin.com/video/1",
            video_url="https://v3-dy-o.zjcdn.com/1.mp4",
            title="大文件热点",
            local_path="",
            cover_local_path="D:/tmp/cover.jpg",
            commentary="锐评：先看封面也够有梗",
        )
        config = {
            **self.module.DEFAULT_CONFIG,
            "_env": {"WECOM_BOT_RECEIVER": "user1", "WECOM_BOT_ID": "bot", "WECOM_BOT_SECRET": "secret"},
            "_warnings": [],
        }

        sent = self.module.send_downloaded_videos([item], config, sender_factory=FakeSender)

        self.assertEqual(sent, 1)
        self.assertEqual(item.send_status, "sent_cover")
        self.assertIn(("image", "D:/tmp/cover.jpg"), calls)
        self.assertFalse(any(call[0] == "video" for call in calls))

    def test_send_downloaded_videos_counts_text_when_video_upload_fails(self):
        calls = []

        class FakeSender:
            def __init__(self, bot_id, secret, receiver, is_group, websocket_url):
                pass

            def connect(self):
                calls.append(("connect",))

            def send_markdown(self, content):
                calls.append(("markdown", content))

            def send_video(self, path):
                calls.append(("video", path))
                raise TimeoutError("upload timed out")

            def send_image(self, path):
                calls.append(("image", path))

            def close(self):
                calls.append(("close",))

        item = self.module.DownloadedDouyinVideo(
            source_id="1",
            source_url="https://www.douyin.com/video/1",
            video_url="https://v3-dy-o.zjcdn.com/1.mp4",
            title="video timeout hot",
            local_path="D:/tmp/video.mp4",
            commentary="sharp text first",
        )
        config = {
            **self.module.DEFAULT_CONFIG,
            "_env": {"WECOM_BOT_RECEIVER": "user1", "WECOM_BOT_ID": "bot", "WECOM_BOT_SECRET": "secret"},
            "_warnings": [],
        }

        sent = self.module.send_downloaded_videos([item], config, sender_factory=FakeSender)

        self.assertEqual(sent, 1)
        self.assertEqual(item.send_status, "sent_text_only")
        self.assertTrue(any(call[0] == "markdown" for call in calls))

    def test_cli_dry_run_json_outputs_valid_summary(self):
        candidate = self.module.DouyinVideoCandidate(
            source_id="1",
            source_url="https://www.douyin.com/video/1",
            video_url="https://v3-dy-o.zjcdn.com/1.mp4",
            title="离谱名场面",
            score=100,
            meme_score=1000,
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            output = io.StringIO()
            with patch.object(self.module, "collect_douyin", return_value=[candidate]), contextlib.redirect_stdout(output):
                code = self.module.main(
                    [
                        "--dry-run",
                        "--json",
                        "--config",
                        str(config_path),
                        "--out",
                        str(Path(tmp) / "out"),
                    ]
                )

            self.assertEqual(code, 0)
            summary = json.loads(output.getvalue())
            self.assertTrue(summary["dry_run"])
            self.assertEqual(summary["candidate_count"], 1)
            self.assertIn("commentary", summary["candidates"][0])

    def test_out_argument_has_priority_over_env_and_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"output_dir": str(Path(tmp) / "config-out")}), encoding="utf-8")
            args = Namespace(
                config=str(config_path),
                out=str(Path(tmp) / "cli-out"),
                max_total=None,
                max_candidates=None,
                since_hours=None,
                delete_after_hours=None,
                commentary_style=None,
                receiver=None,
                group=False,
                no_send=False,
                send=False,
                cleanup=False,
                dry_run=True,
                json=True,
                debug=False,
            )

            config = self.module.build_config(args, env={"DOUYIN_VIDEO_OUTPUT_DIR": str(Path(tmp) / "env-out")})

            self.assertEqual(Path(config["output_dir"]), (Path(tmp) / "cli-out").resolve())


if __name__ == "__main__":
    unittest.main()
