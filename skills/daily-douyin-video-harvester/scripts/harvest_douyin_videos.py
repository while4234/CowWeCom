#!/usr/bin/env python3

import argparse
import base64
import copy
import dataclasses
import datetime as dt
import email.message
import hashlib
import html
import json
import logging
import math
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_CONFIG_PATH = "~/.cow-douyin-video-harvester/config.json"
DEFAULT_BROWSER_PROFILE_DIR = "~/.cow-douyin-video-harvester/browser-profile"
BROWSER_PROFILE_ENV = "DOUYIN_BROWSER_USER_DATA_DIR"
WECOM_WS_URL = "wss://openws.work.weixin.qq.com"
WECOM_MEDIA_CHUNK_SIZE = 512 * 1024

DEFAULT_CONFIG: Dict[str, Any] = {
    "config_version": 2,
    "output_dir": "~/cow/douyin-videos",
    "timezone": "Asia/Shanghai",
    "max_total": 3,
    "max_candidates": 30,
    "since_hours": 48,
    "dedupe_days": 7,
    "delete_after_hours": 24,
    "min_video_bytes": 4096,
    "max_video_bytes": 80_000_000,
    "max_cover_bytes": 5_000_000,
    "user_agent": "CowWechat daily-douyin-video-harvester/1.0",
    "commentary_style": "sharp",
    "send_after_download": True,
    "schedule_cleanup": True,
    "cleanup_on_start": True,
    "exclude_same_day_topics": True,
    "max_per_hot_term": 1,
    "collection_mode": "browser",
    "unknown_created_at_penalty": 1500,
    "freshness_bonus": 1000,
    "douyin": {
        "cookie_env": "DOUYIN_COOKIE",
        "use_hot_terms": False,
        "endpoint_hotsearch": [
            "https://www.douyin.com/aweme/v1/web/hot/search/list/",
            "https://www.douyin.com/aweme/v1/hot/search/list/",
        ],
        "hotsearch_params": {"device_platform": "webapp", "aid": "6383"},
        "endpoint_search": "https://www.douyin.com/search/{keyword}",
        "search_patterns": ["{term}", "{term} 名场面", "{term} 反转", "{term} 笑死", "{term} 二创"],
        "fallback_keywords": [
            "轻擦边舞蹈",
            "氛围感美女跳舞",
            "甜妹变装",
            "辣妹热舞",
            "美女热舞名场面",
            "情侣搞笑日常",
            "显眼包名场面",
            "离谱整活",
            "评论区笑死",
            "抽象短视频",
            "反转名场面",
            "上头舞蹈",
        ],
        "max_hot_terms": 25,
        "max_search_queries": 30,
        "request_interval_seconds": 2,
        "request_timeout_seconds": 12,
        "disable_proxy": True,
    },
    "meme_filter": {
        "min_meme_score": 900,
        "strong_signal_score": 2500,
        "positive_keywords": [
            "名场面",
            "离谱",
            "离大谱",
            "笑死",
            "爆笑",
            "反转",
            "破防",
            "整活",
            "抽象",
            "社死",
            "显眼包",
            "魔性",
            "吐槽",
            "二创",
            "模仿",
            "挑战",
            "瓜",
            "热梗",
            "表情包",
            "绷不住",
            "蚌埠住",
            "上头",
            "安卓人",
            "封号",
            "上号",
            "翻车",
            "塌房",
            "嘴硬",
            "抽象",
            "偷感",
            "逆天",
            "锐评",
            "搞笑",
            "擦边",
            "轻擦边",
            "氛围感",
            "美女",
            "甜妹",
            "辣妹",
            "热舞",
            "舞蹈",
            "变装",
            "纯欲",
            "钓系",
        ],
        "hotspot_keywords": [
            "热榜",
            "热搜",
            "热点",
            "全网热议",
            "网友热议",
            "冲上热搜",
            "爆了",
            "刷屏",
            "出圈",
            "空军一号",
            "黄仁勋",
            "峰哥",
            "安卓人",
        ],
        "conversation_keywords": ["怎么", "为什么", "原来", "竟然", "不是", "这也", "谁懂", "网友", "全网", "哈哈", "评论区"],
        "boring_keywords": [
            "发布会",
            "财报",
            "股价",
            "指数",
            "天气",
            "考试",
            "政策",
            "会议",
            "声明",
            "公告",
            "倡议",
            "电影",
            "预告",
            "漫威",
            "好剧",
            "解说",
            "一口气",
            "电影推荐",
            "好剧推荐",
            "影视解说",
            "电视剧",
            "电影解说",
            "追剧",
            "vlog",
            "生活vlog",
            "旅行",
            "旅游",
            "美食",
            "穿搭",
            "探店",
            "攻略",
            "带货",
            "种草",
            "好物",
            "护肤",
            "妆容",
            "巴菲特",
            "投资",
            "财富",
            "读书",
            "深度解读",
            "认知提升",
            "人生哲理",
            "知识分享",
            "游戏实况",
            "剪辑",
            "二创剪辑",
            "亚军",
            "冠军",
            "亚洲杯",
            "虽败犹荣",
            "未来可期",
            "足球小将",
            "比赛集锦",
            "赛事",
        ],
        "serious_keywords": [
            "国防部",
            "外交部",
            "警方通报",
            "通报",
            "事故",
            "地震",
            "火灾",
            "死亡",
            "遇难",
            "战争",
            "袭击",
            "癌症",
            "违法",
            "犯罪",
            "辟谣",
            "裸露",
            "露骨",
            "色情",
            "成人",
            "约炮",
            "成人视频",
            "未成年",
            "nsfw",
        ],
    },
    "proxy_guard": {
        "enabled": True,
        "script": "scripts/clash_verge_rule_guard.py",
        "timeout_seconds": 20,
    },
    "browser_fallback": {
        "enabled": True,
        "use_persistent_profile": True,
        "user_data_dir": DEFAULT_BROWSER_PROFILE_DIR,
        "close_locked_profile_processes": True,
        "channel": "chrome",
        "headless": False,
        "timeout_seconds": 45,
        "wait_seconds": 8,
        "max_queries": 6,
        "start_urls": [
            "https://www.douyin.com/hot",
            "https://www.douyin.com/jingxuan",
        ],
    },
    "wecom": {
        "enabled": True,
        "mode": "websocket",
        "receiver": "",
        "is_group": False,
        "receiver_env": "WECOM_BOT_RECEIVER",
        "is_group_env": "WECOM_BOT_IS_GROUP",
        "bot_id_env": "WECOM_BOT_ID",
        "secret_env": "WECOM_BOT_SECRET",
        "project_config": "D:/CowWechat/config.json",
        "websocket_url": WECOM_WS_URL,
    },
}


@dataclasses.dataclass
class HotTerm:
    word: str
    rank: int
    raw_hot: float = 0.0
    score: float = 0.0
    source_url: str = ""
    label: Optional[str] = None
    meme_score: float = 0.0
    meme_reasons: List[str] = dataclasses.field(default_factory=list)
    extra: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class DouyinVideoCandidate:
    source_id: str
    source_url: str
    video_url: str
    cover_url: str = ""
    title: str = ""
    author: Optional[str] = None
    created_at: Optional[str] = None
    score: float = 0.0
    metrics: Dict[str, Any] = dataclasses.field(default_factory=dict)
    hot_term: Optional[str] = None
    meme_score: float = 0.0
    commentary: str = ""
    extra: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class DownloadedDouyinVideo(DouyinVideoCandidate):
    local_path: str = ""
    cover_local_path: str = ""
    sha256: str = ""
    content_type: str = ""
    size_bytes: int = 0
    downloaded_at: str = ""
    sent_at: Optional[str] = None
    delete_after: Optional[str] = None
    send_status: str = "pending"


class FetchError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, headers: Optional[Dict[str, str]] = None) -> None:
        super().__init__(message)
        self.status = status
        self.headers = headers or {}


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def expand_path(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path))).resolve()


def configure_browser_profile(config: Dict[str, Any], environ: Dict[str, str]) -> None:
    browser_config = config.setdefault("browser_fallback", {})
    env_profile = environ.get(BROWSER_PROFILE_ENV)
    if env_profile:
        browser_config["user_data_dir"] = env_profile
        return

    configured_profile = str(browser_config.get("user_data_dir") or "").strip()
    if not configured_profile:
        browser_config["user_data_dir"] = DEFAULT_BROWSER_PROFILE_DIR


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(str(tmp_path), str(path))


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def ensure_config_file(config_path: Path) -> None:
    if config_path.exists():
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(config_path, DEFAULT_CONFIG)


def load_config(config_path: Path) -> Dict[str, Any]:
    ensure_config_file(config_path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid config JSON: {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"Config must be a JSON object: {config_path}")
    merged = deep_merge(DEFAULT_CONFIG, raw)
    merged["_raw_config"] = raw
    return merged


def raw_config_version(config: Dict[str, Any]) -> int:
    raw_config = config.get("_raw_config", {})
    if not isinstance(raw_config, dict):
        return 0
    try:
        return int(raw_config.get("config_version", 0) or 0)
    except (TypeError, ValueError):
        return 0


def maybe_migrate_interest_seed_defaults(config: Dict[str, Any]) -> None:
    if raw_config_version(config) >= 2:
        return
    raw_config = config.get("_raw_config", {})
    if not isinstance(raw_config, dict):
        raw_config = {}
    raw_douyin = raw_config.get("douyin", {})
    if not isinstance(raw_douyin, dict):
        raw_douyin = {}
    default_douyin = DEFAULT_CONFIG["douyin"]
    douyin_config = config.setdefault("douyin", {})
    old_fallbacks = [
        "今日热榜 热梗",
        "全网热议 名场面",
        "今日离谱热点",
        "网友锐评 热点",
        "互联网热梗",
    ]
    changed = False
    if raw_douyin.get("use_hot_terms", True) is True:
        douyin_config["use_hot_terms"] = default_douyin["use_hot_terms"]
        changed = True
    if raw_douyin.get("fallback_keywords", old_fallbacks) == old_fallbacks:
        douyin_config["fallback_keywords"] = copy.deepcopy(default_douyin["fallback_keywords"])
        changed = True
    if changed:
        add_warning(config, "legacy douyin hot-board defaults replaced with interest-seeded profile search for this run")


def build_config(args: argparse.Namespace, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    environ = env if env is not None else os.environ
    config_path = expand_path(args.config or DEFAULT_CONFIG_PATH)
    config = load_config(config_path)
    config["_config_path"] = str(config_path)
    config["_env"] = environ
    config["_warnings"] = []
    config["_failed"] = False
    maybe_migrate_interest_seed_defaults(config)
    config["output_dir"] = str(expand_path(args.out or environ.get("DOUYIN_VIDEO_OUTPUT_DIR") or config["output_dir"]))
    config["max_total"] = args.max_total if args.max_total is not None else int(config.get("max_total", 3))
    config["max_candidates"] = (
        args.max_candidates if args.max_candidates is not None else int(config.get("max_candidates", 30))
    )
    config["since_hours"] = args.since_hours if args.since_hours is not None else int(config.get("since_hours", 48))
    config["delete_after_hours"] = (
        args.delete_after_hours
        if args.delete_after_hours is not None
        else int(config.get("delete_after_hours", 24))
    )
    if args.commentary_style:
        config["commentary_style"] = args.commentary_style
    if args.receiver:
        config.setdefault("wecom", {})["receiver"] = args.receiver
    if args.group:
        config.setdefault("wecom", {})["is_group"] = True
    if args.no_send:
        config["send_after_download"] = False
    if args.send:
        config["send_after_download"] = True
    config["dry_run"] = bool(args.dry_run)
    config["json"] = bool(args.json)
    config["debug"] = bool(args.debug)
    config["cleanup"] = bool(args.cleanup)
    configure_browser_profile(config, environ)
    return config


def setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )


def add_warning(config: Dict[str, Any], message: str) -> None:
    config.setdefault("_warnings", []).append(message)
    logging.warning(message)


def short_error_message(exc: BaseException, limit: int = 220) -> str:
    first_line = str(exc).splitlines()[0] if str(exc).splitlines() else exc.__class__.__name__
    return first_line[:limit]


def now_for_config(config: Dict[str, Any]) -> dt.datetime:
    timezone = str(config.get("timezone", "Asia/Shanghai"))
    if timezone == "Asia/Shanghai":
        tzinfo = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")
    elif timezone.upper() == "UTC":
        tzinfo = dt.timezone.utc
    else:
        tzinfo = dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
    return dt.datetime.now(tzinfo)


def normalize_headers(headers: Any) -> Dict[str, str]:
    if isinstance(headers, email.message.Message):
        return {key.lower(): value for key, value in headers.items()}
    return {str(key).lower(): str(value) for key, value in dict(headers or {}).items()}


def build_url(url: str, params: Optional[Dict[str, Any]] = None) -> str:
    if not params:
        return url
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
    separator = "&" if urllib.parse.urlsplit(url).query else "?"
    return f"{url}{separator}{query}"


def http_get_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    request = urllib.request.Request(build_url(url, params), headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(body.decode(charset, errors="replace")), normalize_headers(response.headers)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} for {url}", status=exc.code, headers=normalize_headers(exc.headers)) from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"Network error for {url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise FetchError(f"Invalid JSON from {url}: {exc}") from exc


def http_get_text(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
    disable_proxy: bool = False,
) -> Tuple[str, Dict[str, str]]:
    request = urllib.request.Request(build_url(url, params), headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if disable_proxy else None
    try:
        response_context = opener.open(request, timeout=timeout) if opener else urllib.request.urlopen(request, timeout=timeout)
        with response_context as response:
            body = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return body.decode(charset, errors="replace"), normalize_headers(response.headers)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} for {url}", status=exc.code, headers=normalize_headers(exc.headers)) from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"Network error for {url}: {exc.reason}") from exc


def http_get_bytes(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 30,
    max_bytes: int = 50_000_000,
) -> Tuple[bytes, str, Dict[str, str]]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_headers = normalize_headers(response.headers)
            content_length = response_headers.get("content-length")
            if content_length and int(content_length) > max_bytes:
                raise FetchError(f"video too large by content-length: {content_length}")
            body = response.read(max_bytes + 1)
            content_type = response_headers.get("content-type", "").split(";", 1)[0].strip().lower()
            return body, content_type, response_headers
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} for video {url}", status=exc.code, headers=normalize_headers(exc.headers)) from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"Network error for video {url}: {exc.reason}") from exc


def as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        multiplier = 1.0
        if "亿" in lowered:
            multiplier = 100_000_000.0
        elif "万" in lowered or "w" in lowered:
            multiplier = 10_000.0
        cleaned = re.sub(r"[^\d.]", "", lowered)
        if cleaned:
            try:
                return float(cleaned) * multiplier
            except ValueError:
                return default
    return default


def strip_html(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalized_topic_key(value: Any) -> str:
    text = strip_html(value)
    text = re.sub(r"#([^#]+)#", r"\1", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text.lower())
    return text[:80]


def unique_preserve_order(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        cleaned = normalize_url(value)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def iter_dict_values(data: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from iter_dict_values(value)
    elif isinstance(data, list):
        for item in data:
            yield from iter_dict_values(item)


def iter_string_values(data: Any) -> Iterable[str]:
    if isinstance(data, str):
        yield data
    elif isinstance(data, dict):
        for value in data.values():
            yield from iter_string_values(value)
    elif isinstance(data, list):
        for item in data:
            yield from iter_string_values(item)


def normalize_url(value: Any) -> str:
    url = html.unescape(str(value or "").strip())
    url = url.replace("\\/", "/").replace("\\u0026", "&").replace("\\u003d", "=")
    if url.startswith("//"):
        url = "https:" + url
    return url


def search_url_for_word(word: str) -> str:
    return f"https://www.douyin.com/search/{urllib.parse.quote(word, safe='')}"


def parse_douyin_hot_terms(payload: Dict[str, Any], max_terms: int = 25) -> List[HotTerm]:
    terms: List[HotTerm] = []
    seen = set()
    for item in iter_dict_values(payload):
        word = str(
            item.get("word")
            or item.get("sentence")
            or item.get("title")
            or item.get("hot_word")
            or item.get("event_word")
            or ""
        ).strip()
        if not word or word in seen or len(word) > 100:
            continue
        raw_hot = as_float(
            item.get(
                "hot_value",
                item.get("view_count", item.get("video_count", item.get("discuss_video_count", item.get("num", 0)))),
            )
        )
        rank = len(terms) + 1
        score = max(max_terms - rank + 1, 1) * 1000.0 + math.log1p(max(raw_hot, 0.0)) * 100.0
        terms.append(
            HotTerm(
                word=word,
                rank=rank,
                raw_hot=raw_hot,
                score=score,
                source_url=str(item.get("link") or item.get("url") or search_url_for_word(word)),
                label=item.get("label") or item.get("label_name") or item.get("tag"),
                extra={"raw": item},
            )
        )
        seen.add(word)
        if len(terms) >= max_terms:
            break
    return terms


def parse_chinese_hot_number(text: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\s*([万亿]?)", str(text or ""))
    if not match:
        return 0.0
    value = float(match.group(1))
    unit = match.group(2)
    if unit == "万":
        return value * 10_000
    if unit == "亿":
        return value * 100_000_000
    return value


def parse_hot_terms_from_visible_text(text: str, source_url: str, max_terms: int = 25) -> List[HotTerm]:
    blocked_exact = {
        "首页",
        "推荐",
        "精选",
        "直播",
        "关注",
        "朋友",
        "我的",
        "搜索",
        "热榜",
        "热点",
        "抖音热榜",
        "登录",
        "查看更多",
    }
    terms: List[HotTerm] = []
    seen = set()
    for raw_line in str(text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line.strip())
        if not line:
            continue
        line = re.sub(r"^(?:TOP)?\s*\d{1,2}\s*[.、]?\s*", "", line, flags=re.IGNORECASE).strip()
        line = re.sub(r"^(?:热榜|热点|热搜)\s*", "", line).strip()
        if not line or line in blocked_exact or "http" in line.lower():
            continue
        raw_hot = parse_chinese_hot_number(line) if re.search(r"\d+(?:\.\d+)?\s*[万亿]?\s*(?:热度|人看过|播放|讨论)?", line) else 0.0
        word = re.sub(r"\s*\d+(?:\.\d+)?\s*[万亿]?\s*(?:热度|人看过|播放|讨论)?\s*$", "", line).strip()
        word = re.sub(r"[\u200b\ufeff]+", "", word).strip()
        if word in blocked_exact or word in seen:
            continue
        if not (2 <= len(word) <= 48):
            continue
        if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", word):
            continue
        rank = len(terms) + 1
        score = max(max_terms - rank + 1, 1) * 1000.0 + math.log1p(max(raw_hot, 0.0)) * 100.0
        terms.append(HotTerm(word=word, rank=rank, raw_hot=raw_hot, score=score, source_url=source_url))
        seen.add(word)
        if len(terms) >= max_terms:
            break
    return terms


def browser_hot_terms_from_artifacts(artifacts: Sequence[Dict[str, Any]], config: Dict[str, Any]) -> List[HotTerm]:
    max_terms = int(config.get("douyin", {}).get("max_hot_terms", 25))
    collected: List[HotTerm] = []
    for artifact in artifacts:
        source_url = str(artifact.get("source_url") or "https://www.douyin.com/hot")
        payload = artifact.get("payload")
        if isinstance(payload, dict):
            collected.extend(parse_douyin_hot_terms(payload, max_terms=max_terms))
        html_text = artifact.get("html")
        if isinstance(html_text, str) and html_text:
            for payload_item in json_payloads_from_html(html_text):
                if isinstance(payload_item, dict):
                    collected.extend(parse_douyin_hot_terms(payload_item, max_terms=max_terms))
        visible_text = artifact.get("visible_text")
        if isinstance(visible_text, str) and visible_text:
            collected.extend(parse_hot_terms_from_visible_text(visible_text, source_url, max_terms=max_terms))
    result: List[HotTerm] = []
    seen = set()
    for term in sorted(collected, key=lambda item: item.score, reverse=True):
        key = normalized_topic_key(term.word)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(term)
        if len(result) >= max_terms:
            break
    return result


def score_meme_potential(text: str, config: Dict[str, Any]) -> Tuple[float, List[str]]:
    filter_config = config.get("meme_filter", {})
    lowered = str(text or "").lower()
    score = 0.0
    reasons: List[str] = []

    for keyword in filter_config.get("positive_keywords", []):
        if keyword.lower() in lowered:
            score += 1200.0
            reasons.append(f"梗感:{keyword}")

    for keyword in filter_config.get("hotspot_keywords", []):
        if keyword.lower() in lowered:
            score += float(filter_config.get("strong_signal_score", 2500))
            reasons.append(f"热点:{keyword}")

    for keyword in filter_config.get("conversation_keywords", []):
        if keyword.lower() in lowered:
            score += 150.0
            reasons.append(f"可聊:{keyword}")

    for keyword in filter_config.get("boring_keywords", []):
        if keyword.lower() in lowered:
            score -= 1800.0
            reasons.append(f"降权:{keyword}")

    for keyword in filter_config.get("serious_keywords", []):
        if keyword.lower() in lowered:
            score -= 5000.0
            reasons.append(f"严肃:{keyword}")

    if re.search(r"[!?？！]{1,3}", text):
        score += 300.0
        reasons.append("强情绪标点")
    if re.search(r"(哈哈|笑不活|绷不住|蚌埠住|绝了|离谱)", text):
        score += 900.0
        reasons.append("评论区口吻")
    if len(text) <= 4:
        score -= 500.0
        reasons.append("标题过短")
    return score, reasons


def rank_hot_terms_for_memes(terms: Sequence[HotTerm], config: Dict[str, Any]) -> List[HotTerm]:
    ranked: List[HotTerm] = []
    min_score = float(config.get("meme_filter", {}).get("min_meme_score", 1))
    for term in terms:
        meme_score, reasons = score_meme_potential(term.word, config)
        updated = dataclasses.replace(
            term,
            meme_score=meme_score,
            meme_reasons=reasons,
            score=term.score + meme_score,
        )
        if meme_score >= min_score:
            ranked.append(updated)
    ranked.sort(key=lambda item: (item.meme_score, item.score), reverse=True)
    return ranked


def rank_hot_terms_for_search(terms: Sequence[HotTerm], config: Dict[str, Any]) -> List[HotTerm]:
    ranked: List[HotTerm] = []
    for term in terms:
        meme_score, reasons = score_meme_potential(term.word, config)
        if meme_score < 0:
            continue
        ranked.append(
            dataclasses.replace(
                term,
                meme_score=meme_score,
                meme_reasons=reasons,
                score=term.score + meme_score,
            )
        )
    ranked.sort(key=lambda item: (item.meme_score, item.score), reverse=True)
    return ranked


def build_search_queries(terms: Sequence[HotTerm], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    douyin_config = config.get("douyin", {})
    queries: List[Dict[str, Any]] = []
    seen = set()
    min_query_score = float(config.get("meme_filter", {}).get("min_meme_score", 900))
    for term in terms:
        for pattern in douyin_config.get("search_patterns", ["{term}"]):
            query = str(pattern).format(term=term.word).strip()
            if query and query not in seen:
                seen.add(query)
                queries.append({"query": query, "term": term.word, "base_score": term.score, "meme_score": term.meme_score})
            if len(queries) >= int(douyin_config.get("max_search_queries", 30)):
                return queries
    for keyword in douyin_config.get("fallback_keywords", []):
        if keyword not in seen:
            seen.add(keyword)
            meme_score, reasons = score_meme_potential(keyword, config)
            if meme_score < min_query_score:
                continue
            queries.append({"query": keyword, "term": keyword, "base_score": 1000 + meme_score, "meme_score": meme_score, "reasons": reasons})
        if len(queries) >= int(douyin_config.get("max_search_queries", 30)):
            return queries
    return queries


def headers_for_douyin(config: Dict[str, Any], accept: str) -> Dict[str, str]:
    headers = {
        "User-Agent": config.get("user_agent", DEFAULT_CONFIG["user_agent"]),
        "Referer": "https://www.douyin.com/",
        "Accept": accept,
    }
    cookie = config.get("_env", os.environ).get(config.get("douyin", {}).get("cookie_env", "DOUYIN_COOKIE"))
    if cookie:
        headers["Cookie"] = cookie
    return headers


def should_run_proxy_guard(config: Dict[str, Any]) -> bool:
    guard_config = config.get("proxy_guard", {})
    return bool(guard_config.get("enabled", True)) and not config.get("_proxy_guard_ran")


def run_proxy_guard(config: Dict[str, Any], reason: str) -> bool:
    if not should_run_proxy_guard(config):
        return False
    config["_proxy_guard_ran"] = True
    guard_config = config.get("proxy_guard", {})
    script_value = str(guard_config.get("script") or "scripts/clash_verge_rule_guard.py")
    script_path = expand_path(script_value) if os.path.isabs(script_value) else project_root() / script_value
    if not script_path.is_file():
        add_warning(config, f"proxy guard unavailable: script not found at {script_path}")
        return False
    add_warning(config, f"douyin access failed ({reason}); running Clash Verge rule guard once")
    try:
        completed = subprocess.run(
            [sys.executable, str(script_path), "--json"],
            cwd=str(project_root()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=int(guard_config.get("timeout_seconds", 20)),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        add_warning(config, f"proxy guard failed: {exc}")
        return False
    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip()
        add_warning(config, f"proxy guard failed: {stderr or 'non-zero exit'}")
        return False
    add_warning(config, "proxy guard completed; retrying Douyin request once")
    return True


def fetch_hot_payloads(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    douyin_config = config.get("douyin", {})
    endpoints = douyin_config.get("endpoint_hotsearch") or []
    if isinstance(endpoints, str):
        endpoints = [endpoints]
    headers = headers_for_douyin(config, "application/json, text/plain, */*")
    payloads: List[Dict[str, Any]] = []
    for endpoint in endpoints:
        try:
            payload, _headers = http_get_json(
                endpoint,
                params=douyin_config.get("hotsearch_params") or {},
                headers=headers,
                timeout=int(douyin_config.get("request_timeout_seconds", 12)),
            )
        except FetchError as exc:
            if run_proxy_guard(config, str(exc)):
                try:
                    payload, _headers = http_get_json(
                        endpoint,
                        params=douyin_config.get("hotsearch_params") or {},
                        headers=headers,
                        timeout=int(douyin_config.get("request_timeout_seconds", 12)),
                    )
                except FetchError as retry_exc:
                    add_warning(config, f"douyin hot list failed for {endpoint}: {retry_exc}")
                    continue
            else:
                add_warning(config, f"douyin hot list failed for {endpoint}: {exc}")
                continue
        payloads.append(payload)
    return payloads


def balanced_json_from(text: str) -> Optional[str]:
    start = -1
    for index, char in enumerate(text):
        if char in "{[":
            start = index
            break
    if start < 0:
        return None
    stack: List[str] = []
    in_string = False
    escape = False
    pairs = {"{": "}", "[": "]"}
    for index in range(start, len(text)):
        char = text[index]
        if escape:
            escape = False
            continue
        if in_string and char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
            if not stack:
                return text[start : index + 1]
    return None


def json_payloads_from_html(html_text: str) -> List[Any]:
    payloads: List[Any] = []
    script_bodies = re.findall(r"<script[^>]*>(.*?)</script>", html_text, flags=re.IGNORECASE | re.DOTALL)
    if not script_bodies:
        script_bodies = [html_text]
    for body in script_bodies:
        unescaped = html.unescape(body)
        for marker in ("window.__DATA__", "window.__INITIAL_STATE__", "RENDER_DATA", "__NEXT_DATA__"):
            if marker not in unescaped:
                continue
            json_text = balanced_json_from(unescaped[unescaped.find(marker) :])
            if not json_text:
                continue
            try:
                payloads.append(json.loads(json_text))
            except json.JSONDecodeError:
                decoded = urllib.parse.unquote(json_text)
                try:
                    payloads.append(json.loads(decoded))
                except json.JSONDecodeError:
                    continue
        json_text = balanced_json_from(unescaped)
        if json_text:
            try:
                payloads.append(json.loads(json_text))
            except json.JSONDecodeError:
                pass
    return payloads


def is_video_url(url: str) -> bool:
    lowered = normalize_url(url).lower()
    if Path(urllib.parse.urlsplit(lowered).path).suffix in VIDEO_EXTENSIONS:
        return True
    return any(
        token in lowered
        for token in (
            "douyinvod.com",
            "douyinvideo",
            "douyin.com/aweme/v1/play",
            "bytecdntp.com",
            "bytecdn.cn",
            "zjcdn.com",
            "v3-",
        )
    )


def is_cover_url(url: str) -> bool:
    lowered = normalize_url(url).lower()
    if Path(urllib.parse.urlsplit(lowered).path).suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return any(token in lowered for token in ("douyinpic", "douyinstatic", "byteimg", "pstatp"))
    return False


def urls_from_media_node(node: Any) -> List[str]:
    urls: List[str] = []
    if isinstance(node, dict):
        url_list = node.get("url_list")
        if isinstance(url_list, list):
            urls.extend(str(item) for item in url_list if item)
        for key in ("url", "uri", "play_url", "main_url"):
            value = node.get(key)
            if isinstance(value, str):
                urls.append(value)
    elif isinstance(node, list):
        urls.extend(str(item) for item in node if item)
    elif isinstance(node, str):
        urls.append(node)
    return urls


def extract_video_urls(item: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    for container in (item, video):
        for key in ("play_addr", "download_addr", "play_api", "play_url"):
            urls.extend(urls_from_media_node(container.get(key)))
        bit_rate = container.get("bit_rate")
        if isinstance(bit_rate, list):
            for entry in bit_rate:
                if isinstance(entry, dict):
                    urls.extend(urls_from_media_node(entry.get("play_addr")))
    for value in iter_string_values(item):
        if "http" in value and is_video_url(value):
            urls.append(value)
    return [url for url in unique_preserve_order(urls) if is_video_url(url)]


def extract_cover_urls(item: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    for container in (item, video):
        for key in ("cover", "origin_cover", "dynamic_cover"):
            urls.extend(urls_from_media_node(container.get(key)))
    for value in iter_string_values(item):
        if "http" in value and is_cover_url(value):
            urls.append(value)
    return [url for url in unique_preserve_order(urls) if is_cover_url(url)]


def engagement_score(metrics: Dict[str, Any]) -> float:
    return (
        as_float(metrics.get("digg_count"))
        + as_float(metrics.get("comment_count")) * 2
        + as_float(metrics.get("share_count")) * 4
        + as_float(metrics.get("collect_count")) * 3
        + as_float(metrics.get("play_count")) / 100
    )


def created_at_from_item(item: Dict[str, Any]) -> Optional[str]:
    timestamp = as_float(item.get("create_time") or item.get("created_at") or 0)
    if timestamp <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def looks_like_douyin_video_item(item: Dict[str, Any]) -> bool:
    identity = item.get("aweme_id") or item.get("group_id") or item.get("id")
    title = item.get("desc") or item.get("title") or item.get("caption")
    has_stats = isinstance(item.get("statistics"), dict) or any(
        key in item for key in ("digg_count", "comment_count", "share_count", "collect_count", "play_count")
    )
    has_author = isinstance(item.get("author"), dict) or bool(item.get("author_name"))
    return bool(identity or title or has_stats or has_author)


def extract_candidates_from_payload(
    payload: Any,
    source_url: str,
    hot_term: Optional[str] = None,
    base_score: float = 0.0,
    config: Optional[Dict[str, Any]] = None,
) -> List[DouyinVideoCandidate]:
    config = config or DEFAULT_CONFIG
    candidates: List[DouyinVideoCandidate] = []
    seen_urls = set()
    for item in iter_dict_values(payload):
        video_urls = extract_video_urls(item)
        if not video_urls:
            continue
        if not looks_like_douyin_video_item(item):
            continue
        source_id = str(item.get("aweme_id") or item.get("group_id") or item.get("id") or "").strip()
        title = strip_html(item.get("desc") or item.get("title") or item.get("caption") or hot_term or source_id or "抖音视频")
        meme_score, meme_reasons = score_meme_potential(f"{title} {hot_term or ''}", config)
        if meme_score < float(config.get("meme_filter", {}).get("min_meme_score", 1)):
            continue
        author_info = item.get("author") if isinstance(item.get("author"), dict) else {}
        stats = item.get("statistics") if isinstance(item.get("statistics"), dict) else {}
        metrics = {
            "digg_count": int(as_float(stats.get("digg_count", item.get("digg_count", 0)))),
            "comment_count": int(as_float(stats.get("comment_count", item.get("comment_count", 0)))),
            "share_count": int(as_float(stats.get("share_count", item.get("share_count", 0)))),
            "collect_count": int(as_float(stats.get("collect_count", item.get("collect_count", 0)))),
            "play_count": int(as_float(stats.get("play_count", item.get("play_count", 0)))),
        }
        detail_url = str(item.get("share_url") or item.get("url") or "")
        if not detail_url:
            detail_url = f"https://www.douyin.com/video/{source_id}" if source_id else source_url
        cover_urls = extract_cover_urls(item)
        for index, video_url in enumerate(video_urls, start=1):
            if video_url in seen_urls:
                continue
            seen_urls.add(video_url)
            fallback_id = hashlib.sha1(video_url.encode("utf-8")).hexdigest()[:16]
            candidate = DouyinVideoCandidate(
                source_id=f"{source_id or fallback_id}:video:{index}",
                source_url=detail_url,
                video_url=video_url,
                cover_url=cover_urls[0] if cover_urls else "",
                title=title,
                author=author_info.get("nickname") or author_info.get("unique_id") or item.get("author_name"),
                created_at=created_at_from_item(item),
                score=base_score + engagement_score(metrics) + meme_score,
                metrics=metrics,
                hot_term=hot_term,
                meme_score=meme_score,
                extra={"search_url": source_url, "meme_reasons": meme_reasons},
            )
            candidates.append(candidate)
    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    return candidates


def parse_douyin_search_html(
    html_text: str,
    keyword: str,
    source_url: str,
    base_score: float = 0.0,
    config: Optional[Dict[str, Any]] = None,
) -> List[DouyinVideoCandidate]:
    config = config or DEFAULT_CONFIG
    candidates: List[DouyinVideoCandidate] = []
    for payload in json_payloads_from_html(html_text):
        candidates.extend(extract_candidates_from_payload(payload, source_url, keyword, base_score, config=config))
    if candidates:
        return dedupe_candidates(candidates)
    for index, match in enumerate(re.findall(r"https?:\\/\\/[^\"'\\\s<>]+", html_text), start=1):
        video_url = normalize_url(match)
        if not is_video_url(video_url):
            continue
        meme_score, reasons = score_meme_potential(keyword, config)
        if meme_score < float(config.get("meme_filter", {}).get("min_meme_score", 1)):
            continue
        fallback_id = hashlib.sha1(video_url.encode("utf-8")).hexdigest()[:16]
        candidates.append(
            DouyinVideoCandidate(
                source_id=f"{fallback_id}:regex:{index}",
                source_url=source_url,
                video_url=video_url,
                title=f"{keyword} 热点视频",
                score=base_score + meme_score + max(1.0, 1000.0 - index),
                metrics={"rank": index},
                hot_term=keyword,
                meme_score=meme_score,
                extra={"search_url": source_url, "meme_reasons": reasons, "source": "regex"},
            )
        )
    return candidates


def looks_like_risk_control(html_text: str) -> bool:
    lowered = html_text.lower()
    return any(token in lowered for token in ("a_bogus", "__ac_signature", "acrawler", "captcha", "secsdk", "验证码", "安全验证"))


def looks_like_blocking_challenge(text: str) -> bool:
    lowered = text.lower()
    return any(
        token in lowered
        for token in (
            "captcha",
            "secsdk-captcha",
            "__ac_signature",
            "安全验证",
            "验证码",
            "滑块",
            "security verification",
        )
    )


def visible_page_text(page: Any) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def browser_candidate_artifacts_to_candidates(
    artifacts: Sequence[Dict[str, Any]],
    config: Dict[str, Any],
) -> List[DouyinVideoCandidate]:
    candidates: List[DouyinVideoCandidate] = []
    for artifact in artifacts:
        source_url = str(artifact.get("source_url") or "https://www.douyin.com/")
        keyword = str(artifact.get("keyword") or artifact.get("hot_term") or "")
        base_score = float(artifact.get("base_score") or 0.0)
        payload = artifact.get("payload")
        if payload is not None:
            candidates.extend(
                extract_candidates_from_payload(
                    payload,
                    source_url=source_url,
                    hot_term=keyword,
                    base_score=base_score,
                    config=config,
                )
            )
        html_text = artifact.get("html")
        if isinstance(html_text, str) and html_text:
            candidates.extend(
                parse_douyin_search_html(
                    html_text,
                    keyword=keyword,
                    source_url=source_url,
                    base_score=base_score,
                    config=config,
                )
            )
    return dedupe_candidates(candidates)


def browser_response_is_interesting(url: str) -> bool:
    lowered = url.lower()
    return any(
        token in lowered
        for token in (
            "/aweme/v1/web/hot/search/list",
            "/aweme/v1/hot/search/list",
            "/aweme/v2/web/module/feed",
            "/aweme/v1/web/general/search",
            "/aweme/v1/web/search",
            "/aweme/v1/web/aweme/post",
            "/aweme/v1/web/tab/feed",
        )
    )


def cookies_from_header(cookie_header: str, domains: Sequence[str]) -> List[Dict[str, Any]]:
    cookies: List[Dict[str, Any]] = []
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        for domain in domains:
            cookies.append({"name": name, "value": value.strip(), "domain": domain, "path": "/"})
    return cookies


def looks_like_profile_lock_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "user data directory is already in use",
            "processsingleton",
            "profile appears to be in use",
            "failed to create a processsingleton",
            "session not created",
        )
    )


def close_browser_processes_for_profile(profile_dir: Path, config: Dict[str, Any]) -> bool:
    browser_config = config.get("browser_fallback", {})
    if not browser_config.get("close_locked_profile_processes", True):
        return False
    if os.name != "nt":
        return False
    profile_text = str(profile_dir)
    if not profile_text:
        return False
    env = os.environ.copy()
    env["COW_DOUYIN_PROFILE_DIR"] = profile_text
    powershell = (
        "$profile=$env:COW_DOUYIN_PROFILE_DIR; "
        "$escaped=$profile.Replace(\"'\", \"''\"); "
        "$matches=Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -and $_.CommandLine -like \"*$escaped*\" -and $_.ProcessId -ne $PID }; "
        "$ids=@(); "
        "foreach ($p in $matches) { "
        "try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; $ids += $p.ProcessId } catch {} "
        "}; "
        "$ids -join ','"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", powershell],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        add_warning(config, f"douyin persistent browser profile lock cleanup failed: {short_error_message(exc)}")
        return False
    if completed.returncode != 0:
        add_warning(config, f"douyin persistent browser profile lock cleanup failed: {short_error_message(completed.stderr)}")
        return False
    killed = (completed.stdout or "").strip()
    if killed:
        add_warning(config, "closed stale Douyin browser profile process before retrying persistent profile")
        return True
    return False


def add_douyin_cookies_to_context(context: Any, config: Dict[str, Any]) -> None:
    cookie_header = config.get("_env", os.environ).get(config.get("douyin", {}).get("cookie_env", "DOUYIN_COOKIE"), "")
    parsed_cookies = cookies_from_header(cookie_header, [".douyin.com", "www.douyin.com", ".iesdouyin.com"])
    if parsed_cookies:
        context.add_cookies(parsed_cookies)


def open_douyin_browser_context(
    playwright: Any,
    config: Dict[str, Any],
    browser_config: Dict[str, Any],
    user_data_dir: Path,
    timeout_ms: int,
) -> Tuple[Any, Optional[Any]]:
    channel = str(browser_config.get("channel") or "").strip()
    if browser_config.get("use_persistent_profile", False):
        persistent_kwargs = {
            "headless": bool(browser_config.get("headless", False)),
            "viewport": {"width": 1365, "height": 900},
            "locale": "zh-CN",
            "timeout": timeout_ms,
        }
        if channel:
            persistent_kwargs["channel"] = channel
        try:
            context = playwright.chromium.launch_persistent_context(str(user_data_dir), **persistent_kwargs)
            add_douyin_cookies_to_context(context, config)
            return context, None
        except Exception as exc:
            if looks_like_profile_lock_error(exc) and close_browser_processes_for_profile(user_data_dir, config):
                try:
                    context = playwright.chromium.launch_persistent_context(str(user_data_dir), **persistent_kwargs)
                    add_douyin_cookies_to_context(context, config)
                    return context, None
                except Exception as retry_exc:
                    exc = retry_exc
            cookie = config.get("_env", os.environ).get(config.get("douyin", {}).get("cookie_env", "DOUYIN_COOKIE"), "")
            if not cookie:
                raise
            add_warning(
                config,
                "douyin persistent browser profile unavailable; trying temporary browser context with DOUYIN_COOKIE: "
                f"{short_error_message(exc)}",
            )

    launch_kwargs = {"headless": bool(browser_config.get("headless", False)), "timeout": timeout_ms}
    if channel:
        launch_kwargs["channel"] = channel
    browser = playwright.chromium.launch(**launch_kwargs)
    context = browser.new_context(
        viewport={"width": 1365, "height": 900},
        locale="zh-CN",
        user_agent=str(config.get("user_agent") or DEFAULT_CONFIG["user_agent"]),
    )
    add_douyin_cookies_to_context(context, config)
    return context, browser


def collect_douyin_browser_fallback(
    config: Dict[str, Any],
    queries: Sequence[Dict[str, Any]],
) -> List[DouyinVideoCandidate]:
    browser_config = config.get("browser_fallback", {})
    if not browser_config.get("enabled", True):
        return []
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        add_warning(config, f"douyin browser fallback unavailable: Playwright import failed: {exc}")
        return []

    user_data_dir = expand_path(str(browser_config.get("user_data_dir") or DEFAULT_BROWSER_PROFILE_DIR))
    user_data_dir.mkdir(parents=True, exist_ok=True)
    timeout_ms = int(float(browser_config.get("timeout_seconds", 45)) * 1000)
    wait_ms = int(float(browser_config.get("wait_seconds", 8)) * 1000)
    max_queries = int(browser_config.get("max_queries", 6))
    term_artifacts: List[Dict[str, Any]] = []
    candidate_artifacts: List[Dict[str, Any]] = []
    fallback_query_specs: List[Dict[str, Any]] = []
    discovery_specs: List[Dict[str, Any]] = []
    use_hot_terms = bool(config.get("douyin", {}).get("use_hot_terms", False))
    hot_endpoints = config.get("douyin", {}).get("endpoint_hotsearch") or []
    if use_hot_terms:
        if isinstance(hot_endpoints, str):
            hot_endpoints = [hot_endpoints]
        for endpoint in hot_endpoints:
            discovery_specs.append(
                {
                    "phase": "discovery",
                    "query": "",
                    "term": "",
                    "base_score": 0.0,
                    "url": build_url(str(endpoint), config.get("douyin", {}).get("hotsearch_params") or {}),
                }
            )
    for spec in list(queries)[:max_queries]:
        keyword = str(spec.get("query") or spec.get("term") or "").strip()
        if not keyword:
            continue
        fallback_query_specs.append(
            {
                "query": keyword,
                "term": spec.get("term") or keyword,
                "base_score": float(spec.get("base_score") or 0.0),
                "url": f"https://www.douyin.com/search/{urllib.parse.quote(keyword, safe='')}?type=general",
            }
        )
    if use_hot_terms:
        for start_url in browser_config.get("start_urls") or []:
            url_text = str(start_url)
            label = ""
            if "/search/" in url_text:
                encoded_label = urllib.parse.urlsplit(url_text).path.rsplit("/", 1)[-1]
                label = urllib.parse.unquote(encoded_label).strip()
            label_score, _reasons = score_meme_potential(label, config) if label else (0.0, [])
            discovery_specs.append(
                {
                    "phase": "discovery",
                    "query": label,
                    "term": label,
                    "base_score": max(label_score, 0.0),
                    "url": url_text,
                }
            )
    if not discovery_specs and not fallback_query_specs:
        return []

    if str(config.get("collection_mode", "browser")).lower() == "browser":
        profile_mode = "persistent" if browser_config.get("use_persistent_profile", True) else "temporary"
        add_warning(config, f"douyin collection is using a {profile_mode} logged-in browser context")
    else:
        add_warning(config, "douyin HTTP collection produced no usable candidates; trying logged-in browser fallback")
    try:
        with sync_playwright() as playwright:
            context, browser = open_douyin_browser_context(playwright, config, browser_config, user_data_dir, timeout_ms)
            try:
                context.set_default_timeout(timeout_ms)
                page = context.pages[0] if context.pages else context.new_page()
                active_spec: Dict[str, Any] = {"query": "", "term": "", "base_score": 0.0}

                def capture_response(response: Any) -> None:
                    try:
                        if not browser_response_is_interesting(response.url):
                            return
                        payload = response.json()
                    except Exception:
                        return
                    keyword = str(active_spec.get("query") or active_spec.get("term") or "")
                    artifact = {
                        "source_url": response.url,
                        "keyword": keyword,
                        "hot_term": str(active_spec.get("term") or keyword),
                        "base_score": float(active_spec.get("base_score") or 0.0),
                        "payload": payload,
                    }
                    if active_spec.get("phase") == "discovery":
                        term_artifacts.append(artifact)
                    else:
                        candidate_artifacts.append(artifact)

                page.on("response", capture_response)
                try:
                    browser_ua = page.evaluate("() => navigator.userAgent")
                    if isinstance(browser_ua, str) and browser_ua:
                        config["user_agent"] = browser_ua
                except Exception:
                    pass

                def visit_spec(spec: Dict[str, Any]) -> Optional[str]:
                    nonlocal active_spec
                    active_spec = dict(spec)
                    url = str(spec["url"])
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                        page.wait_for_timeout(wait_ms)
                        try:
                            page.mouse.wheel(0, 1800)
                            page.wait_for_timeout(min(wait_ms, 4000))
                        except Exception:
                            pass
                        html_text = page.content()
                    except PlaywrightTimeoutError as exc:
                        add_warning(config, f"douyin browser fallback timed out for {url}: {short_error_message(exc)}")
                        return None
                    visible_text = visible_page_text(page)
                    if looks_like_blocking_challenge(visible_text):
                        add_warning(config, "douyin browser fallback reached a login/security challenge; manual browser action is required")
                        return "challenge"
                    artifact = {
                        "source_url": url,
                        "keyword": str(spec.get("query") or spec.get("term") or ""),
                        "hot_term": str(spec.get("term") or spec.get("query") or ""),
                        "base_score": float(spec.get("base_score") or 0.0),
                        "html": html_text,
                        "visible_text": visible_text,
                    }
                    if spec.get("phase") == "discovery":
                        term_artifacts.append(artifact)
                    else:
                        candidate_artifacts.append(artifact)
                    return None

                for spec in discovery_specs:
                    result = visit_spec(spec)
                    if result == "challenge":
                        return []

                hot_terms = (
                    rank_hot_terms_for_search(browser_hot_terms_from_artifacts(term_artifacts, config), config)
                    if use_hot_terms
                    else []
                )
                search_specs: List[Dict[str, Any]] = []
                seen_queries = set()
                for spec in build_search_queries(hot_terms, config) + fallback_query_specs:
                    keyword = str(spec.get("query") or spec.get("term") or "").strip()
                    if not keyword or keyword in seen_queries:
                        continue
                    seen_queries.add(keyword)
                    search_specs.append(
                        {
                            "query": keyword,
                            "term": spec.get("term") or keyword,
                            "base_score": float(spec.get("base_score") or 0.0),
                            "url": f"https://www.douyin.com/search/{urllib.parse.quote(keyword, safe='')}?type=general",
                        }
                    )
                    if len(search_specs) >= max_queries:
                        break
                if hot_terms:
                    add_warning(config, f"douyin hot-board discovery produced {len(hot_terms)} meme-worthy terms")
                elif fallback_query_specs and not use_hot_terms:
                    add_warning(config, "douyin browser collection is using interest-seeded profile search")
                elif fallback_query_specs:
                    add_warning(config, "douyin hot-board discovery produced no usable terms; using fallback meme queries")

                for spec in search_specs:
                    result = visit_spec(spec)
                    if result == "challenge":
                        break
                    extracted = browser_candidate_artifacts_to_candidates(candidate_artifacts, config)
                    if len(extracted) >= int(config.get("max_candidates", 30)):
                        break
                return browser_candidate_artifacts_to_candidates(candidate_artifacts, config)
            finally:
                context.close()
                if browser is not None:
                    browser.close()
    except PlaywrightError as exc:
        add_warning(config, f"douyin browser fallback failed: {short_error_message(exc)}")
    except Exception as exc:
        add_warning(config, f"douyin browser fallback failed: {short_error_message(exc)}")
    return []


def collect_douyin(config: Dict[str, Any]) -> List[DouyinVideoCandidate]:
    douyin_config = config.get("douyin", {})
    candidates: List[DouyinVideoCandidate] = []
    headers_json = headers_for_douyin(config, "application/json, text/plain, */*")
    headers_html = headers_for_douyin(config, "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    collection_mode = str(config.get("collection_mode", "browser")).lower()
    browser_config = config.get("browser_fallback", {})
    uses_persistent_browser = (
        collection_mode == "browser"
        and browser_config.get("enabled", True)
        and browser_config.get("use_persistent_profile", False)
    )
    if "Cookie" not in headers_html and not uses_persistent_browser:
        add_warning(config, "DOUYIN_COOKIE not set; public Douyin pages may return risk-control shells and be skipped")

    max_candidates = int(config.get("max_candidates", 30))
    if collection_mode == "browser":
        queries = build_search_queries([], config)
        browser_candidates = collect_douyin_browser_fallback(config, queries)
        browser_candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        return browser_candidates[:max_candidates]

    terms: List[HotTerm] = []
    if douyin_config.get("use_hot_terms", False):
        hot_payloads = fetch_hot_payloads(config)
        for payload in hot_payloads:
            terms.extend(parse_douyin_hot_terms(payload, max_terms=int(douyin_config.get("max_hot_terms", 25))))
            candidates.extend(extract_candidates_from_payload(payload, "https://www.douyin.com/hot", None, 0.0, config=config))
        terms = rank_hot_terms_for_search(terms, config)
    queries = build_search_queries(terms, config)
    endpoint_template = douyin_config.get("endpoint_search", "https://www.douyin.com/search/{keyword}")

    for index, spec in enumerate(queries):
        if len(candidates) >= max_candidates:
            break
        if index:
            time.sleep(float(douyin_config.get("request_interval_seconds", 2)))
        keyword = str(spec["query"])
        encoded_keyword = urllib.parse.quote(keyword, safe="")
        if "{keyword}" in endpoint_template:
            endpoint = endpoint_template.format(keyword=encoded_keyword)
            params = {"type": "general"}
        else:
            endpoint = endpoint_template
            params = {"keyword": keyword, "type": "general"}
        source_url = build_url(endpoint, params)
        try:
            html_text, _headers = http_get_text(
                endpoint,
                params=params,
                headers=headers_html,
                timeout=int(douyin_config.get("request_timeout_seconds", 12)),
                disable_proxy=bool(douyin_config.get("disable_proxy", True)),
            )
        except FetchError as exc:
            if run_proxy_guard(config, str(exc)):
                try:
                    html_text, _headers = http_get_text(
                        endpoint,
                        params=params,
                        headers=headers_json,
                        timeout=int(douyin_config.get("request_timeout_seconds", 12)),
                        disable_proxy=bool(douyin_config.get("disable_proxy", True)),
                    )
                except FetchError as retry_exc:
                    add_warning(config, f"douyin search skipped for {keyword}: {retry_exc}")
                    continue
            else:
                add_warning(config, f"douyin search skipped for {keyword}: {exc}")
                continue
        provider_candidates = parse_douyin_search_html(
            html_text,
            keyword=keyword,
            source_url=source_url,
            base_score=float(spec.get("base_score", 0.0)),
            config=config,
        )
        if not provider_candidates and looks_like_risk_control(html_text):
            add_warning(config, "douyin returned risk-control content; skip without bypassing platform controls")
            config["_failed"] = True
            break
        candidates.extend(provider_candidates)

    if len(candidates) < int(config.get("max_total", 3)):
        candidates.extend(collect_douyin_browser_fallback(config, queries))

    deduped = dedupe_candidates(candidates)
    deduped.sort(key=lambda candidate: candidate.score, reverse=True)
    return deduped[:max_candidates]


def dedupe_candidates(candidates: Sequence[DouyinVideoCandidate]) -> List[DouyinVideoCandidate]:
    result: List[DouyinVideoCandidate] = []
    seen_urls = set()
    seen_content = set()
    for candidate in candidates:
        url_key = (candidate.source_url, candidate.video_url)
        content_id = candidate.source_id.split(":video:", 1)[0].split(":regex:", 1)[0]
        content_key = candidate.source_url if "/video/" in candidate.source_url else content_id
        if not candidate.video_url or url_key in seen_urls or content_key in seen_content:
            continue
        seen_urls.add(url_key)
        if content_key:
            seen_content.add(content_key)
        result.append(candidate)
    return result


def load_state_file(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def clean_state(state: Dict[str, str], now: dt.datetime, dedupe_days: int) -> Dict[str, str]:
    cutoff = now - dt.timedelta(days=dedupe_days)
    cleaned: Dict[str, str] = {}
    for key, value in state.items():
        try:
            timestamp = dt.datetime.fromisoformat(value)
        except ValueError:
            continue
        if timestamp >= cutoff:
            cleaned[key] = value
    return cleaned


def state_paths(output_dir: Path) -> Tuple[Path, Path, Path]:
    state_dir = output_dir / "state"
    return state_dir / "seen_urls.json", state_dir / "seen_hashes.json", state_dir / "cleanup_queue.json"


def daily_seen_path(output_dir: Path) -> Path:
    return output_dir / "state" / "daily_seen.json"


def load_state(output_dir: Path, now: dt.datetime, dedupe_days: int) -> Tuple[Dict[str, str], Dict[str, str]]:
    seen_urls_path, seen_hashes_path, _cleanup_path = state_paths(output_dir)
    return (
        clean_state(load_state_file(seen_urls_path), now, dedupe_days),
        clean_state(load_state_file(seen_hashes_path), now, dedupe_days),
    )


def save_state(output_dir: Path, seen_urls: Dict[str, str], seen_hashes: Dict[str, str]) -> None:
    seen_urls_path, seen_hashes_path, _cleanup_path = state_paths(output_dir)
    atomic_write_json(seen_urls_path, seen_urls)
    atomic_write_json(seen_hashes_path, seen_hashes)


def load_daily_seen(output_dir: Path, now: dt.datetime, dedupe_days: int) -> Dict[str, Dict[str, str]]:
    path = daily_seen_path(output_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    cutoff = (now - dt.timedelta(days=dedupe_days)).date().isoformat()
    cleaned: Dict[str, Dict[str, str]] = {}
    for day, values in raw.items():
        if str(day) < cutoff or not isinstance(values, dict):
            continue
        cleaned[str(day)] = {str(key): str(value) for key, value in values.items()}
    return cleaned


def save_daily_seen(output_dir: Path, daily_seen: Dict[str, Dict[str, str]]) -> None:
    atomic_write_json(daily_seen_path(output_dir), daily_seen)


def daily_dedupe_keys(candidate: DouyinVideoCandidate) -> List[str]:
    keys: List[str] = []
    topic_key = normalized_topic_key(candidate.hot_term)
    if topic_key:
        keys.append(f"topic:{topic_key}")
    title_key = normalized_topic_key(candidate.title)
    if title_key:
        keys.append(f"title:{title_key}")
    content_id = candidate.source_id.split(":video:", 1)[0].split(":regex:", 1)[0]
    if content_id:
        keys.append(f"content:{content_id}")
    if candidate.source_url:
        keys.append(f"source:{candidate.source_url}")
    if candidate.video_url:
        keys.append(f"video:{candidate.video_url}")
    return list(dict.fromkeys(keys))


def filter_same_day_seen(
    candidates: Sequence[DouyinVideoCandidate],
    config: Dict[str, Any],
) -> Tuple[List[DouyinVideoCandidate], int]:
    if not config.get("exclude_same_day_topics", True):
        return list(candidates), 0
    output_dir = Path(config["output_dir"])
    now = now_for_config(config)
    daily_seen = load_daily_seen(output_dir, now, int(config.get("dedupe_days", 7)))
    seen_keys = set(daily_seen.get(now.date().isoformat(), {}))
    if not seen_keys:
        return list(candidates), 0
    filtered: List[DouyinVideoCandidate] = []
    skipped = 0
    for candidate in candidates:
        if seen_keys.intersection(daily_dedupe_keys(candidate)):
            skipped += 1
            continue
        filtered.append(candidate)
    return filtered, skipped


def parse_candidate_timestamp(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def filter_recent_candidates(
    candidates: Sequence[DouyinVideoCandidate],
    config: Dict[str, Any],
) -> Tuple[List[DouyinVideoCandidate], int]:
    since_hours = int(config.get("since_hours", 48) or 0)
    if since_hours <= 0:
        return list(candidates), 0
    now = now_for_config(config)
    freshness_bonus = float(config.get("freshness_bonus", 1000))
    unknown_penalty = float(config.get("unknown_created_at_penalty", 1500))
    filtered: List[DouyinVideoCandidate] = []
    skipped = 0
    for candidate in candidates:
        created_at = parse_candidate_timestamp(candidate.created_at)
        extra = dict(candidate.extra or {})
        if created_at is None:
            extra["recency"] = "unknown"
            filtered.append(dataclasses.replace(candidate, score=candidate.score - unknown_penalty, extra=extra))
            continue
        age_hours = max(0.0, (now - created_at.astimezone(now.tzinfo)).total_seconds() / 3600.0)
        if age_hours > since_hours:
            skipped += 1
            continue
        extra["recency_age_hours"] = round(age_hours, 2)
        bonus = freshness_bonus * max(0.0, (since_hours - age_hours) / since_hours)
        filtered.append(dataclasses.replace(candidate, score=candidate.score + bonus, extra=extra))
    return filtered, skipped


def slugify(value: str, fallback: str, max_length: int = 48) -> str:
    text = strip_html(value) or fallback
    text = re.sub(r"[\\/:*?\"<>|]+", " ", text)
    text = re.sub(r"\s+", "-", text.strip())
    text = re.sub(r"-{2,}", "-", text)
    text = text.strip(".-_")
    if not text:
        text = fallback
    return text[:max_length].strip(".-_") or fallback[:max_length]


def extension_from_url(url: str) -> Optional[str]:
    path = urllib.parse.urlsplit(url).path
    ext = Path(path).suffix.lower()
    return ext if ext in VIDEO_EXTENSIONS else None


def image_extension_from_url(url: str) -> Optional[str]:
    path = urllib.parse.urlsplit(url).path
    ext = Path(path).suffix.lower()
    return ext if ext in IMAGE_EXTENSIONS else None


def extension_from_content_type(content_type: str) -> Optional[str]:
    if content_type == "video/mp4":
        return ".mp4"
    ext = mimetypes.guess_extension(content_type or "")
    return ext if ext in VIDEO_EXTENSIONS else None


def image_extension_from_content_type(content_type: str) -> Optional[str]:
    if content_type == "image/jpeg":
        return ".jpg"
    ext = mimetypes.guess_extension(content_type or "")
    return ext if ext in IMAGE_EXTENSIONS else None


def accepts_video(content_type: str, video_url: str) -> bool:
    if "video" in (content_type or "").lower():
        return True
    if extension_from_url(video_url) is not None:
        return True
    if content_type in {"application/octet-stream", "binary/octet-stream", ""} and is_video_url(video_url):
        return True
    return False


def accepts_image(content_type: str, image_url: str) -> bool:
    return "image" in (content_type or "").lower() or image_extension_from_url(image_url) is not None


def build_video_headers(candidate: DouyinVideoCandidate, config: Dict[str, Any]) -> Dict[str, str]:
    headers = headers_for_douyin(config, "*/*")
    headers["Referer"] = candidate.source_url or "https://www.douyin.com/"
    return headers


def build_cover_headers(candidate: DouyinVideoCandidate, config: Dict[str, Any]) -> Dict[str, str]:
    headers = headers_for_douyin(config, "image/avif,image/webp,image/apng,image/*,*/*;q=0.8")
    headers["Referer"] = candidate.source_url or "https://www.douyin.com/"
    return headers


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Unable to find unique filename for {path}")


def fetch_video_with_retries(candidate: DouyinVideoCandidate, config: Dict[str, Any]) -> Tuple[bytes, str]:
    max_bytes = int(config.get("max_video_bytes", 50_000_000))
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            data, content_type, _headers = http_get_bytes(
                candidate.video_url,
                headers=build_video_headers(candidate, config),
                timeout=30,
                max_bytes=max_bytes,
            )
            return data, content_type
        except FetchError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    raise FetchError(f"download failed after retries for {candidate.video_url}: {last_error}")


def is_too_large_error(exc: FetchError) -> bool:
    message = str(exc).lower()
    return "too large" in message or "content-length" in message


def download_cover_candidate(
    candidate: DouyinVideoCandidate,
    rank: int,
    day_dir: Path,
    config: Dict[str, Any],
    sha_prefix: str,
) -> Optional[Tuple[Path, str, str, int]]:
    if not candidate.cover_url:
        return None
    max_bytes = int(config.get("max_cover_bytes", 5_000_000))
    data, content_type, _headers = http_get_bytes(
        candidate.cover_url,
        headers=build_cover_headers(candidate, config),
        timeout=20,
        max_bytes=max_bytes,
    )
    size_bytes = len(data)
    if not accepts_image(content_type, candidate.cover_url):
        raise FetchError(f"not an image content-type for cover: {content_type or 'unknown'}")
    if size_bytes <= 0 or size_bytes > max_bytes:
        raise FetchError(f"cover image invalid size: {size_bytes} bytes")
    sha256 = hashlib.sha256(data).hexdigest()
    extension = image_extension_from_content_type(content_type) or image_extension_from_url(candidate.cover_url) or ".jpg"
    provider_dir = day_dir / "douyin"
    provider_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(candidate.title, fallback=re.sub(r"[^A-Za-z0-9_-]+", "-", candidate.source_id) or "douyin")
    final_path = unique_path(provider_dir / f"{rank:03d}_douyin_cover_{slug}_{sha_prefix or sha256[:8]}{extension}")
    tmp_path = final_path.with_name(final_path.name + ".tmp")
    tmp_path.write_bytes(data)
    os.replace(str(tmp_path), str(final_path))
    return final_path, sha256, content_type or f"image/{extension.lstrip('.')}", size_bytes


def candidate_to_dict(candidate: DouyinVideoCandidate) -> Dict[str, Any]:
    return dataclasses.asdict(candidate)


def downloaded_to_dict(downloaded: DownloadedDouyinVideo) -> Dict[str, Any]:
    return dataclasses.asdict(downloaded)


def generate_commentary(candidate: DouyinVideoCandidate, style: str = "sharp") -> str:
    if style == "none":
        return ""
    title = strip_html(candidate.title or candidate.hot_term or "这个热点")
    reasons = candidate.extra.get("meme_reasons") if isinstance(candidate.extra, dict) else []
    reason_text = "、".join(str(reason).split(":", 1)[-1] for reason in reasons[:2]) if reasons else "热度和槽点"
    if style == "brief":
        return f"点评：{title} 能冲上来，靠的就是{reason_text}，适合围观但别太上头。"
    if "反转" in title or "反转" in reason_text:
        return f"锐评：{title} 这种反转感，像是评论区先写好剧本，正片只是负责播放。"
    if "离谱" in title or "名场面" in title or "整活" in title:
        return f"锐评：{title} 的看点不是热度，是它离谱得很稳定，属于天然二创素材。"
    if "笑" in title or "哈哈" in title:
        return f"锐评：{title} 这类热点最会偷袭打工人的表情管理，笑点不高级，但够好用。"
    return f"锐评：{title} 不是最严肃的热点，但胜在有梗、有槽点，转发时评论区大概率比正片还热闹。"


def download_candidate(
    candidate: DouyinVideoCandidate,
    rank: int,
    day_dir: Path,
    config: Dict[str, Any],
    seen_hashes: Dict[str, str],
    downloaded_at: str,
) -> Optional[DownloadedDouyinVideo]:
    try:
        data, content_type = fetch_video_with_retries(candidate, config)
    except FetchError as exc:
        if not is_too_large_error(exc):
            raise
        payload = candidate_to_dict(candidate)
        payload["commentary"] = generate_commentary(candidate, str(config.get("commentary_style", "sharp")))
        fallback_sha = hashlib.sha256(f"{candidate.source_url}|{candidate.video_url}".encode("utf-8")).hexdigest()
        cover_local_path = ""
        content_type = "text/markdown"
        size_bytes = 0
        try:
            cover_result = download_cover_candidate(candidate, rank, day_dir, config, fallback_sha[:8])
        except FetchError:
            cover_result = None
        if cover_result:
            cover_path, cover_sha, cover_content_type, cover_size = cover_result
            fallback_sha = cover_sha
            cover_local_path = str(cover_path)
            content_type = cover_content_type
            size_bytes = cover_size
        return DownloadedDouyinVideo(
            **payload,
            local_path="",
            cover_local_path=cover_local_path,
            sha256=fallback_sha,
            content_type=content_type,
            size_bytes=size_bytes,
            downloaded_at=downloaded_at,
            send_status="pending_cover_only" if cover_local_path else "pending_text_only",
        )
    size_bytes = len(data)
    min_bytes = int(config.get("min_video_bytes", 4096))
    max_bytes = int(config.get("max_video_bytes", 50_000_000))
    if not accepts_video(content_type, candidate.video_url):
        raise FetchError(f"not a video content-type: {content_type or 'unknown'}")
    if size_bytes < min_bytes:
        raise FetchError(f"video too small: {size_bytes} bytes")
    if size_bytes > max_bytes:
        raise FetchError(f"video too large: {size_bytes} bytes")

    sha256 = hashlib.sha256(data).hexdigest()
    if sha256 in seen_hashes:
        return None
    extension = extension_from_content_type(content_type) or extension_from_url(candidate.video_url) or ".mp4"
    provider_dir = day_dir / "douyin"
    provider_dir.mkdir(parents=True, exist_ok=True)
    score_int = int(max(candidate.score, 0))
    slug = slugify(candidate.title, fallback=re.sub(r"[^A-Za-z0-9_-]+", "-", candidate.source_id) or "douyin")
    filename = f"{rank:03d}_douyin_{score_int}_{slug}_{sha256[:8]}{extension}"
    final_path = unique_path(provider_dir / filename)
    tmp_path = final_path.with_name(final_path.name + ".tmp")
    tmp_path.write_bytes(data)
    os.replace(str(tmp_path), str(final_path))
    commentary = generate_commentary(candidate, str(config.get("commentary_style", "sharp")))
    payload = candidate_to_dict(candidate)
    payload["commentary"] = commentary
    return DownloadedDouyinVideo(
        **payload,
        local_path=str(final_path),
        sha256=sha256,
        content_type=content_type or f"video/{extension.lstrip('.')}",
        size_bytes=size_bytes,
        downloaded_at=downloaded_at,
    )


def planned_filename(candidate: DouyinVideoCandidate, rank: int) -> str:
    extension = extension_from_url(candidate.video_url) or ".mp4"
    slug = slugify(candidate.title, fallback=re.sub(r"[^A-Za-z0-9_-]+", "-", candidate.source_id) or "douyin")
    return f"douyin/{rank:03d}_douyin_{int(max(candidate.score, 0))}_{slug}_pending{extension}"


def select_candidates(candidates: Sequence[DouyinVideoCandidate], config: Dict[str, Any]) -> List[DouyinVideoCandidate]:
    sorted_candidates = dedupe_candidates(candidates)
    sorted_candidates.sort(key=lambda candidate: (candidate.meme_score, candidate.score), reverse=True)
    max_per_hot_term = int(config.get("max_per_hot_term", 1) or 0)
    if max_per_hot_term <= 0:
        return sorted_candidates[: int(config.get("max_total", 3))]
    selected: List[DouyinVideoCandidate] = []
    topic_counts: Dict[str, int] = {}
    for candidate in sorted_candidates:
        topic_key = normalized_topic_key(candidate.hot_term) or normalized_topic_key(candidate.title)
        if topic_key and topic_counts.get(topic_key, 0) >= max_per_hot_term:
            continue
        selected.append(candidate)
        if topic_key:
            topic_counts[topic_key] = topic_counts.get(topic_key, 0) + 1
        if len(selected) >= int(config.get("max_total", 3)):
            break
    return selected


def run_dry_run(candidates: Sequence[DouyinVideoCandidate], config: Dict[str, Any]) -> Dict[str, Any]:
    planned = []
    for rank, candidate in enumerate(candidates, start=1):
        item = candidate_to_dict(candidate)
        item["planned_path"] = planned_filename(candidate, rank)
        item["commentary"] = generate_commentary(candidate, str(config.get("commentary_style", "sharp")))
        planned.append(item)
    return {
        "dry_run": True,
        "output_dir": config.get("output_dir"),
        "candidate_count": len(candidates),
        "downloaded_count": 0,
        "sent_count": 0,
        "warnings": list(config.get("_warnings", [])),
        "candidates": planned,
    }


def download_candidates(candidates: Sequence[DouyinVideoCandidate], config: Dict[str, Any]) -> Tuple[List[DownloadedDouyinVideo], Dict[str, Any]]:
    output_dir = Path(config["output_dir"])
    now = now_for_config(config)
    day_dir = output_dir / now.date().isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    dedupe_days = int(config.get("dedupe_days", 7))
    seen_urls, seen_hashes = load_state(output_dir, now, dedupe_days)
    daily_seen = load_daily_seen(output_dir, now, dedupe_days)
    day_key = now.date().isoformat()
    today_seen = daily_seen.setdefault(day_key, {})
    timestamp = now.isoformat()
    downloaded: List[DownloadedDouyinVideo] = []
    skipped_count = 0
    state_delta = {"urls": {}, "hashes": {}, "daily_seen": {}}

    for candidate in candidates:
        if len(downloaded) >= int(config.get("max_total", 3)):
            break
        if candidate.source_url in seen_urls or candidate.video_url in seen_urls:
            skipped_count += 1
            continue
        try:
            result = download_candidate(
                candidate,
                rank=len(downloaded) + 1,
                day_dir=day_dir,
                config=config,
                seen_hashes=seen_hashes,
                downloaded_at=timestamp,
            )
        except FetchError as exc:
            skipped_count += 1
            add_warning(config, f"download skipped {candidate.video_url}: {exc}")
            continue
        if result is None:
            skipped_count += 1
            continue
        downloaded.append(result)
        seen_urls[candidate.source_url] = timestamp
        seen_urls[candidate.video_url] = timestamp
        if result.sha256:
            seen_hashes[result.sha256] = timestamp
        state_delta["urls"][candidate.source_url] = timestamp
        state_delta["urls"][candidate.video_url] = timestamp
        if result.sha256:
            state_delta["hashes"][result.sha256] = timestamp
        for key in daily_dedupe_keys(result):
            today_seen[key] = timestamp
            state_delta["daily_seen"][key] = timestamp

    save_state(output_dir, seen_urls, seen_hashes)
    save_daily_seen(output_dir, daily_seen)
    summary_bits = {"day_dir": str(day_dir), "skipped_count": skipped_count, "state_delta": state_delta}
    return downloaded, summary_bits


def load_json_file(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def load_project_config(path_value: str) -> Dict[str, Any]:
    path = expand_path(path_value)
    data = load_json_file(path)
    return data if isinstance(data, dict) else {}


def resolve_wecom_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    env = config.get("_env", os.environ)
    wecom_config = config.get("wecom", {})
    project_config = load_project_config(str(wecom_config.get("project_config") or "D:/CowWechat/config.json"))
    bot_id = env.get(wecom_config.get("bot_id_env", "WECOM_BOT_ID")) or project_config.get("wecom_bot_id") or ""
    secret = env.get(wecom_config.get("secret_env", "WECOM_BOT_SECRET")) or project_config.get("wecom_bot_secret") or ""
    receiver = env.get(wecom_config.get("receiver_env", "WECOM_BOT_RECEIVER")) or wecom_config.get("receiver") or ""
    is_group_raw = env.get(wecom_config.get("is_group_env", "WECOM_BOT_IS_GROUP"))
    is_group = bool(wecom_config.get("is_group", False))
    if is_group_raw is not None:
        is_group = is_group_raw.strip().lower() in {"1", "true", "yes", "y"}
    if not receiver:
        profiles = project_config.get("agent_user_profiles") or {}
        if isinstance(profiles, dict):
            admin_profiles = [
                item for key, item in profiles.items() if str(key).startswith("wecom_bot:") and isinstance(item, dict) and item.get("role") == "admin"
            ]
            fallback_profiles = [
                item for key, item in profiles.items() if str(key).startswith("wecom_bot:") and isinstance(item, dict)
            ]
            for item in admin_profiles + fallback_profiles:
                receiver = str(item.get("raw_user_id") or item.get("receiver") or "").strip()
                if receiver:
                    break
    return {
        "enabled": bool(wecom_config.get("enabled", True)),
        "bot_id": bot_id,
        "secret": secret,
        "receiver": receiver,
        "is_group": is_group,
        "websocket_url": wecom_config.get("websocket_url") or WECOM_WS_URL,
    }


class WecomBotWebSocketSender:
    def __init__(self, bot_id: str, secret: str, receiver: str, is_group: bool = False, websocket_url: str = WECOM_WS_URL):
        self.bot_id = bot_id
        self.secret = secret
        self.receiver = receiver
        self.is_group = is_group
        self.websocket_url = websocket_url
        self.ws = None

    @staticmethod
    def gen_req_id() -> str:
        return hashlib.sha1(f"{time.time()}:{os.getpid()}".encode("utf-8")).hexdigest()[:16]

    def connect(self) -> None:
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError("websocket-client is required for direct WeCom sending") from exc
        self.ws = websocket.create_connection(self.websocket_url, timeout=15)
        req_id = self.gen_req_id()
        self.send_json(
            {
                "cmd": "aibot_subscribe",
                "headers": {"req_id": req_id},
                "body": {"bot_id": self.bot_id, "secret": self.secret},
            }
        )
        response = self.recv_until(req_id=req_id, timeout=15, accept_subscribe=True)
        if response.get("errcode") not in (0, None):
            raise RuntimeError(f"WeCom subscribe failed: {response.get('errmsg') or response}")

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    def send_json(self, payload: Dict[str, Any]) -> None:
        if self.ws is None:
            raise RuntimeError("WeCom websocket is not connected")
        self.ws.send(json.dumps(payload, ensure_ascii=False))

    def recv_until(self, req_id: str, timeout: float = 15, accept_subscribe: bool = False) -> Dict[str, Any]:
        if self.ws is None:
            raise RuntimeError("WeCom websocket is not connected")
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.ws.recv()
            data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace"))
            headers = data.get("headers") if isinstance(data.get("headers"), dict) else {}
            if headers.get("req_id") == req_id:
                return data
            if accept_subscribe and data.get("errcode") == 0 and not data.get("cmd"):
                return data
        raise TimeoutError(f"Timed out waiting for WeCom response {req_id}")

    def send_markdown(self, content: str) -> None:
        req_id = self.gen_req_id()
        self.send_json(
            {
                "cmd": "aibot_send_msg",
                "headers": {"req_id": req_id},
                "body": {
                    "chatid": self.receiver,
                    "chat_type": 2 if self.is_group else 1,
                    "msgtype": "markdown",
                    "markdown": {"content": content},
                },
            }
        )

    def upload_media(self, file_path: str, media_type: str = "video") -> str:
        path = Path(file_path)
        file_size = path.stat().st_size
        total_chunks = math.ceil(file_size / WECOM_MEDIA_CHUNK_SIZE)
        if total_chunks > 100:
            raise RuntimeError(f"WeCom upload limit exceeded: {total_chunks} chunks")
        md5 = hashlib.md5()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(8192), b""):
                md5.update(block)
        init_req = self.gen_req_id()
        self.send_json(
            {
                "cmd": "aibot_upload_media_init",
                "headers": {"req_id": init_req},
                "body": {
                    "type": media_type,
                    "filename": path.name,
                    "total_size": file_size,
                    "total_chunks": total_chunks,
                    "md5": md5.hexdigest(),
                },
            }
        )
        init_resp = self.recv_until(init_req, timeout=15)
        if init_resp.get("errcode") not in (0, None):
            raise RuntimeError(f"WeCom upload init failed: {init_resp}")
        upload_id = init_resp.get("body", {}).get("upload_id")
        if not upload_id:
            raise RuntimeError("WeCom upload init did not return upload_id")
        with path.open("rb") as stream:
            for index in range(total_chunks):
                chunk_req = self.gen_req_id()
                chunk = stream.read(WECOM_MEDIA_CHUNK_SIZE)
                self.send_json(
                    {
                        "cmd": "aibot_upload_media_chunk",
                        "headers": {"req_id": chunk_req},
                        "body": {
                            "upload_id": upload_id,
                            "chunk_index": index,
                            "base64_data": base64.b64encode(chunk).decode("utf-8"),
                        },
                    }
                )
                chunk_resp = self.recv_until(chunk_req, timeout=30)
                if chunk_resp.get("errcode") not in (0, None):
                    raise RuntimeError(f"WeCom upload chunk failed: {chunk_resp}")
        finish_req = self.gen_req_id()
        self.send_json(
            {
                "cmd": "aibot_upload_media_finish",
                "headers": {"req_id": finish_req},
                "body": {"upload_id": upload_id},
            }
        )
        finish_resp = self.recv_until(finish_req, timeout=30)
        if finish_resp.get("errcode") not in (0, None):
            raise RuntimeError(f"WeCom upload finish failed: {finish_resp}")
        media_id = finish_resp.get("body", {}).get("media_id", "")
        if not media_id:
            raise RuntimeError("WeCom upload finish did not return media_id")
        return media_id

    def send_media(self, file_path: str, media_type: str) -> None:
        media_id = self.upload_media(file_path, media_type)
        req_id = self.gen_req_id()
        self.send_json(
            {
                "cmd": "aibot_send_msg",
                "headers": {"req_id": req_id},
                "body": {
                    "chatid": self.receiver,
                    "chat_type": 2 if self.is_group else 1,
                    "msgtype": media_type,
                    media_type: {"media_id": media_id},
                },
            }
        )

    def send_video(self, file_path: str) -> None:
        self.send_media(file_path, "video")

    def send_image(self, file_path: str) -> None:
        self.send_media(file_path, "image")


def format_wecom_message(item: DownloadedDouyinVideo, index: int) -> str:
    lines = [
        f"### 抖音有梗热点 TOP{index}",
        "",
        f"**{item.title or item.hot_term or item.source_id}**",
    ]
    if item.commentary:
        lines.extend(["", item.commentary])
    if not item.local_path:
        lines.extend(["", "视频源文件超过本次下载上限或不可稳定下载，先发锐评和封面/来源，避免为了大文件拖住整轮热点推送。"])
    lines.extend(
        [
            "",
            f"热度分: {item.score:.1f} | 梗感分: {item.meme_score:.1f}",
            f"来源: {item.source_url}",
        ]
    )
    if item.author:
        lines.append(f"作者: {item.author}")
    return "\n".join(lines)


def send_downloaded_videos(
    downloaded: Sequence[DownloadedDouyinVideo],
    config: Dict[str, Any],
    sender_factory: Any = WecomBotWebSocketSender,
) -> int:
    if not downloaded or not config.get("send_after_download", True):
        return 0
    settings = resolve_wecom_settings(config)
    if not settings["enabled"]:
        return 0
    if not settings["bot_id"] or not settings["secret"] or not settings["receiver"]:
        add_warning(config, "WeCom send skipped: missing bot credentials or receiver")
        for item in downloaded:
            item.send_status = "skipped_missing_wecom_config"
        return 0
    sender = sender_factory(
        settings["bot_id"],
        settings["secret"],
        settings["receiver"],
        bool(settings["is_group"]),
        settings["websocket_url"],
    )
    sent_count = 0
    try:
        sender.connect()
        for index, item in enumerate(downloaded, start=1):
            try:
                sender.send_markdown(format_wecom_message(item, index))
            except Exception as exc:
                item.send_status = f"send_failed: {exc}"
                add_warning(config, f"WeCom send failed for {item.local_path or item.cover_local_path or item.source_url}: {exc}")
                continue
            media_status = "text_only"
            if item.local_path:
                try:
                    sender.send_video(item.local_path)
                    media_status = "video"
                except Exception as exc:
                    add_warning(config, f"WeCom video send failed for {item.local_path}; commentary was sent: {exc}")
                    if item.cover_local_path:
                        try:
                            sender.send_image(item.cover_local_path)
                            media_status = "cover"
                        except Exception as cover_exc:
                            add_warning(config, f"WeCom cover fallback failed for {item.cover_local_path}: {cover_exc}")
            elif item.cover_local_path:
                try:
                    sender.send_image(item.cover_local_path)
                    media_status = "cover"
                except Exception as exc:
                    add_warning(config, f"WeCom cover send failed for {item.cover_local_path}; commentary was sent: {exc}")
            item.sent_at = now_for_config(config).isoformat()
            item.send_status = f"sent_{media_status}"
            sent_count += 1
    except Exception as exc:
        add_warning(config, f"WeCom send skipped: {exc}")
        for item in downloaded:
            if item.send_status == "pending":
                item.send_status = f"send_failed: {exc}"
    finally:
        try:
            sender.close()
        except Exception:
            pass
    return sent_count


def cleanup_queue_path(output_dir: Path) -> Path:
    return state_paths(output_dir)[2]


def load_cleanup_queue(output_dir: Path) -> List[Dict[str, Any]]:
    data = load_json_file(cleanup_queue_path(output_dir))
    return data if isinstance(data, list) else []


def save_cleanup_queue(output_dir: Path, queue: List[Dict[str, Any]]) -> None:
    atomic_write_json(cleanup_queue_path(output_dir), queue)


def path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def queue_cleanup(downloaded: Sequence[DownloadedDouyinVideo], config: Dict[str, Any]) -> None:
    if not downloaded:
        return
    output_dir = Path(config["output_dir"])
    queue = load_cleanup_queue(output_dir)
    now = now_for_config(config)
    hours = int(config.get("delete_after_hours", 24))
    for item in downloaded:
        paths_to_queue = [value for value in (item.local_path, item.cover_local_path) if value]
        if not paths_to_queue:
            continue
        delete_after = (now + dt.timedelta(hours=hours)).isoformat()
        item.delete_after = delete_after
        for local_path in paths_to_queue:
            queue.append(
                {
                    "local_path": local_path,
                    "sha256": item.sha256,
                    "sent_at": item.sent_at,
                    "delete_after": delete_after,
                    "status": "queued",
                }
            )
    save_cleanup_queue(output_dir, queue)


def cleanup_due_files(config: Dict[str, Any]) -> Dict[str, Any]:
    output_dir = Path(config["output_dir"])
    queue = load_cleanup_queue(output_dir)
    now = now_for_config(config)
    deleted: List[str] = []
    kept: List[Dict[str, Any]] = []
    for item in queue:
        local_path = Path(str(item.get("local_path") or ""))
        try:
            delete_after = dt.datetime.fromisoformat(str(item.get("delete_after")))
        except ValueError:
            continue
        if delete_after > now:
            kept.append(item)
            continue
        if local_path.exists() and path_is_under(local_path, output_dir):
            try:
                local_path.unlink()
                deleted.append(str(local_path))
            except OSError as exc:
                item["status"] = f"delete_failed: {exc}"
                kept.append(item)
        else:
            item["status"] = "missing_or_outside_output_dir"
    save_cleanup_queue(output_dir, kept)
    return {"deleted_count": len(deleted), "deleted": deleted, "queued_count": len(kept)}


def schedule_windows_cleanup(config: Dict[str, Any]) -> Optional[str]:
    if not config.get("schedule_cleanup", True) or os.name != "nt":
        return None
    run_at = now_for_config(config) + dt.timedelta(hours=int(config.get("delete_after_hours", 24)))
    task_hash = hashlib.sha1(f"{config.get('output_dir')}:{run_at.isoformat()}".encode("utf-8")).hexdigest()[:8]
    task_name = f"CowDouyinVideoCleanup-{task_hash}"
    command_file = Path(config["output_dir"]) / "state" / f"cleanup_{task_hash}.cmd"
    command_file.parent.mkdir(parents=True, exist_ok=True)
    cleanup_command = (
        f'@echo off\r\n'
        f'"{sys.executable}" "{Path(__file__).resolve()}" '
        f'--config "{config.get("_config_path", DEFAULT_CONFIG_PATH)}" '
        f'--out "{config["output_dir"]}" --cleanup --json\r\n'
    )
    command_file.write_text(cleanup_command, encoding="utf-8")
    scheduled_command = f'cmd /c ""{command_file}""'
    try:
        completed = subprocess.run(
            [
                "schtasks",
                "/Create",
                "/SC",
                "ONCE",
                "/TN",
                task_name,
                "/TR",
                scheduled_command,
                "/ST",
                run_at.strftime("%H:%M"),
                "/SD",
                run_at.strftime("%Y/%m/%d"),
                "/F",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"cleanup task scheduling failed: {exc}"
    if completed.returncode != 0:
        return f"cleanup task scheduling failed: {(completed.stderr or completed.stdout).strip()}"
    return None


def relative_posix(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def write_outputs(day_dir: Path, downloaded: Sequence[DownloadedDouyinVideo], summary: Dict[str, Any], state_delta: Dict[str, Any]) -> None:
    manifest_lines = [json.dumps(downloaded_to_dict(item), ensure_ascii=False, sort_keys=True) for item in downloaded]
    atomic_write_text(day_dir / "manifest.jsonl", "\n".join(manifest_lines) + ("\n" if manifest_lines else ""))
    atomic_write_json(day_dir / "state_delta.json", state_delta)
    index_lines = [
        f"# Daily Douyin Video Harvest - {day_dir.name}",
        "",
        "## Summary",
        "",
        f"- downloaded_count: {summary.get('downloaded_count', 0)}",
        f"- sent_count: {summary.get('sent_count', 0)}",
        f"- skipped_count: {summary.get('skipped_count', 0)}",
        f"- warnings: {len(summary.get('warnings', []))}",
        "",
    ]
    if summary.get("warnings"):
        index_lines.extend(["## Warnings", ""])
        for warning in summary["warnings"]:
            index_lines.append(f"- {warning}")
        index_lines.append("")
    index_lines.extend(["## Videos", ""])
    for index, item in enumerate(downloaded, start=1):
        index_lines.append(f"### {index}. {item.title or item.source_id}")
        if item.commentary:
            index_lines.append(item.commentary)
        if item.local_path:
            index_lines.append(f"- local_video: `{relative_posix(Path(item.local_path), day_dir)}`")
        else:
            index_lines.append("- local_video: skipped (too large or unavailable)")
        if item.cover_local_path:
            index_lines.append(f"- local_cover: `{relative_posix(Path(item.cover_local_path), day_dir)}`")
        index_lines.append(f"- score: {item.score:.1f}")
        index_lines.append(f"- source_url: {item.source_url}")
        index_lines.append(f"- author: {item.author or ''}")
        index_lines.append(f"- send_status: {item.send_status}")
        index_lines.append("")
    atomic_write_text(day_dir / "index.md", "\n".join(index_lines).rstrip() + "\n")


def run(config: Dict[str, Any]) -> Dict[str, Any]:
    if config.get("cleanup"):
        result = cleanup_due_files(config)
        result["warnings"] = list(config.get("_warnings", []))
        result["cleanup"] = True
        return result
    if config.get("cleanup_on_start", True):
        cleanup_due_files(config)
    raw_candidates = collect_douyin(config)
    recent_candidates, recency_skipped = filter_recent_candidates(raw_candidates, config)
    fresh_candidates, same_day_skipped = filter_same_day_seen(recent_candidates, config)
    selected = select_candidates(fresh_candidates, config)
    if config.get("dry_run"):
        summary = run_dry_run(selected, config)
        summary["available_candidate_count"] = len(raw_candidates)
        summary["skipped_count"] = same_day_skipped + recency_skipped
        return summary

    downloaded, bits = download_candidates(selected, config)
    sent_count = send_downloaded_videos(downloaded, config)
    queue_cleanup(downloaded, config)
    schedule_warning = schedule_windows_cleanup(config) if downloaded else None
    if schedule_warning:
        add_warning(config, schedule_warning)
    day_dir = Path(bits.get("day_dir") or Path(config["output_dir"]) / now_for_config(config).date().isoformat())
    summary = {
        "dry_run": False,
        "output_dir": str(Path(config["output_dir"])),
        "date_dir": str(day_dir),
        "candidate_count": len(selected),
        "available_candidate_count": len(raw_candidates),
        "downloaded_count": len(downloaded),
        "sent_count": sent_count,
        "skipped_count": bits.get("skipped_count", 0) + same_day_skipped + recency_skipped + max(0, len(fresh_candidates) - len(selected)),
        "warnings": list(config.get("_warnings", [])),
        "manifest": str(day_dir / "manifest.jsonl"),
        "index": str(day_dir / "index.md"),
        "downloaded": [downloaded_to_dict(item) for item in downloaded],
    }
    write_outputs(day_dir, downloaded, summary, bits.get("state_delta", {}))
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch, rank, download, sharp-comment, and send top Douyin meme-worthy hot videos.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out", default=None, help="Output directory.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Config JSON path.")
    parser.add_argument("--max-total", type=int, default=None, help="Maximum videos to download and send.")
    parser.add_argument("--max-candidates", type=int, default=None, help="Maximum candidate videos to keep before TOP selection.")
    parser.add_argument("--since-hours", type=int, default=None, help="Keep known-timestamp videos from the last N hours.")
    parser.add_argument("--delete-after-hours", type=int, default=None, help="Delete downloaded videos after this many hours.")
    parser.add_argument("--commentary-style", choices=["sharp", "brief", "none"], default=None, help="Commentary style.")
    parser.add_argument("--receiver", default=None, help="WeCom receiver userid/chatid.")
    parser.add_argument("--group", action="store_true", help="Treat receiver as a group chatid.")
    parser.add_argument("--send", action="store_true", help="Force WeCom send even if config disables it.")
    parser.add_argument("--no-send", action="store_true", help="Download only; do not send to WeCom.")
    parser.add_argument("--cleanup", action="store_true", help="Delete due videos from cleanup queue and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Print candidates without downloading.")
    parser.add_argument("--json", action="store_true", help="Print JSON summary to stdout.")
    parser.add_argument("--debug", action="store_true", help="Print debug logs to stderr.")
    return parser


def print_summary(summary: Dict[str, Any]) -> None:
    if summary.get("cleanup"):
        print(f"daily-douyin-video-harvester cleanup: deleted={summary.get('deleted_count', 0)} queued={summary.get('queued_count', 0)}")
        return
    print(
        "daily-douyin-video-harvester: "
        f"downloaded={summary.get('downloaded_count', 0)} "
        f"sent={summary.get('sent_count', 0)} "
        f"candidates={summary.get('candidate_count', 0)}"
    )
    if summary.get("date_dir"):
        print(f"output: {summary['date_dir']}")
    for warning in summary.get("warnings", []):
        print(f"warning: {warning}", file=sys.stderr)


def should_exit_two(summary: Dict[str, Any], config: Dict[str, Any]) -> bool:
    if summary.get("dry_run") or summary.get("cleanup"):
        return False
    return summary.get("downloaded_count", 0) == 0 and bool(config.get("_failed"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.debug)
    config = build_config(args)
    summary = run(config)
    if config.get("json"):
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_summary(summary)
    return 2 if should_exit_two(summary, config) else 0


if __name__ == "__main__":
    sys.exit(main())
