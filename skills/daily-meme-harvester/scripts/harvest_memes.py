#!/usr/bin/env python3

import argparse
import copy
import dataclasses
import datetime as dt
import email.message
import hashlib
import html
import importlib.util
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


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
PROVIDER_ALIASES = {
    "xhs": "xiaohongshu",
    "小红书": "xiaohongshu",
    "red": "xiaohongshu",
}
DEFAULT_CONFIG_PATH = "~/.cow-meme-harvester/config.json"
DEFAULT_CONFIG: Dict[str, Any] = {
    "config_version": 2,
    "output_dir": "~/cow/memes",
    "timezone": "Asia/Shanghai",
    "providers": ["weibo", "xiaohongshu"],
    "max_total": 6,
    "max_per_provider": 30,
    "max_downloads_per_provider": 3,
    "dedupe_same_content": True,
    "dedupe_cross_provider_topics": True,
    "exclude_same_day_topics": True,
    "dedupe_days": 90,
    "skip_sensitive": True,
    "download_videos": False,
    "min_image_bytes": 2048,
    "max_image_bytes": 15000000,
    "user_agent": "CowWechat daily-meme-harvester/1.0",
    "send_after_download": False,
    "weibo": {
        "enabled": True,
        "max_hot_terms": 20,
        "search_suffixes": ["", "名场面", "表情包", "梗", "搞笑图", "meme"],
        "request_interval_seconds": 2,
        "endpoint_hotsearch": "https://weibo.com/ajax/side/hotSearch",
        "cookie_env": "WEIBO_COOKIE",
    },
    "xiaohongshu": {
        "enabled": True,
        "cookie_env": "XHS_COOKIE",
        "search_keywords": ["梗图", "表情包", "搞笑图", "meme"],
        "fallback_keywords": ["今日热梗", "热门表情包", "名场面", "搞笑图"],
        "use_hot_terms": True,
        "max_hot_terms": 8,
        "max_search_queries": 12,
        "search_patterns": ["{term}", "{term} 名场面", "{term} 表情包", "{term} 梗"],
        "request_interval_seconds": 2,
        "endpoint_search": "https://www.xiaohongshu.com/search_result",
        "disable_proxy": True,
        "request_timeout_seconds": 20,
        "use_requests": True,
    },
    "proxy_guard": {
        "enabled": True,
        "script": "scripts/clash_verge_rule_guard.py",
        "providers": ["xiaohongshu"],
        "timeout_seconds": 20,
    },
    "wecom": {
        "enabled": True,
        "receiver": "",
        "is_group": False,
        "receiver_env": "WECOM_BOT_RECEIVER",
        "is_group_env": "WECOM_BOT_IS_GROUP",
        "bot_id_env": "WECOM_BOT_ID",
        "secret_env": "WECOM_BOT_SECRET",
        "project_config": "D:/CowWechat/config.json",
    },
    "reddit": {
        "enabled": False,
        "subreddits": ["memes", "dankmemes", "meirl", "wholesomememes"],
        "listing": "top",
        "time": "day",
    },
    "block_keywords": ["nsfw", "血腥", "露骨", "成人", "色情"],
}


@dataclasses.dataclass
class MemeCandidate:
    provider: str
    source_id: str
    source_url: str
    image_url: str
    title: str = ""
    author: Optional[str] = None
    created_at: Optional[str] = None
    score: float = 0.0
    metrics: Dict[str, Any] = dataclasses.field(default_factory=dict)
    possibly_sensitive: bool = False
    extra: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class DownloadedMeme(MemeCandidate):
    local_path: str = ""
    sha256: str = ""
    content_type: str = ""
    size_bytes: int = 0
    downloaded_at: str = ""


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


def looks_like_legacy_weibo_only_default(raw_config: Dict[str, Any]) -> bool:
    providers = raw_config.get("providers")
    if raw_config.get("config_version") is not None:
        return False
    if providers != ["weibo"]:
        return False
    return int(raw_config.get("max_total", 3)) == 3 and raw_config.get("max_downloads_per_provider") is None


def parse_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    providers = []
    for part in value.split(","):
        provider = part.strip().lower()
        if provider:
            providers.append(PROVIDER_ALIASES.get(provider, provider))
    return providers


def build_config(args: argparse.Namespace, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    environ = env if env is not None else os.environ
    config_path = expand_path(args.config or DEFAULT_CONFIG_PATH)
    config = load_config(config_path)
    config["_config_path"] = str(config_path)
    config["_warnings"] = []
    config["_failed_providers"] = set()
    config["_skipped_providers"] = set()

    cli_providers = parse_csv(args.providers)
    if cli_providers:
        config["providers"] = cli_providers
    elif looks_like_legacy_weibo_only_default(config.get("_raw_config", {})):
        config["providers"] = list(DEFAULT_CONFIG["providers"])
        config["max_total"] = DEFAULT_CONFIG["max_total"]
        config["max_downloads_per_provider"] = DEFAULT_CONFIG["max_downloads_per_provider"]
        add_warning(
            config,
            "legacy weibo-only default config detected; using current default providers weibo,xiaohongshu for this run",
        )
    else:
        config["providers"] = config.get("providers") or DEFAULT_CONFIG["providers"]
    config["max_total"] = args.max_total if args.max_total is not None else int(config.get("max_total", 50))
    config["max_per_provider"] = (
        args.max_per_provider if args.max_per_provider is not None else int(config.get("max_per_provider", 30))
    )
    config["since_hours"] = args.since_hours if args.since_hours is not None else int(config.get("since_hours", 24))
    config["dry_run"] = bool(args.dry_run)
    config["json"] = bool(args.json)
    config["debug"] = bool(args.debug)
    if getattr(args, "send_wecom", False):
        config["send_after_download"] = True
    if getattr(args, "receiver", None):
        config.setdefault("wecom", {})["receiver"] = args.receiver
    if getattr(args, "group", False):
        config.setdefault("wecom", {})["is_group"] = True
    output_dir = args.out or environ.get("MEME_OUTPUT_DIR") or config.get("output_dir") or DEFAULT_CONFIG["output_dir"]
    config["output_dir"] = str(expand_path(output_dir))
    config["_env"] = environ
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


def mark_provider_failed(config: Dict[str, Any], provider: str) -> None:
    config.setdefault("_failed_providers", set()).add(provider)


def mark_provider_skipped(config: Dict[str, Any], provider: str) -> None:
    config.setdefault("_skipped_providers", set()).add(provider)


def should_run_proxy_guard_for(config: Dict[str, Any], provider: str) -> bool:
    guard_config = config.get("proxy_guard", {})
    if not guard_config.get("enabled", True):
        return False
    if provider not in set(guard_config.get("providers") or []):
        return False
    return provider not in set(config.get("_proxy_guard_ran_for", set()))


def run_proxy_guard(config: Dict[str, Any], provider: str, reason: str) -> bool:
    if not should_run_proxy_guard_for(config, provider):
        return False
    config.setdefault("_proxy_guard_ran_for", set()).add(provider)
    guard_config = config.get("proxy_guard", {})
    script_value = str(guard_config.get("script") or "scripts/clash_verge_rule_guard.py")
    script_path = expand_path(script_value) if os.path.isabs(script_value) else project_root() / script_value
    if not script_path.is_file():
        add_warning(config, f"proxy guard unavailable for {provider}: script not found at {script_path}")
        return False
    add_warning(config, f"{provider} access failed ({reason}); running Clash Verge rule guard once")
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
        add_warning(config, f"proxy guard failed for {provider}: {exc}")
        return False
    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip()
        add_warning(config, f"proxy guard failed for {provider}: {stderr or 'non-zero exit'}")
        return False
    add_warning(config, f"proxy guard completed for {provider}; retrying request once")
    return True


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
    response_url = build_url(url, params)
    request = urllib.request.Request(response_url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            payload = json.loads(body.decode(charset, errors="replace"))
            return payload, normalize_headers(response.headers)
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
    response_url = build_url(url, params)
    request = urllib.request.Request(response_url, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if disable_proxy else None
    try:
        if opener:
            response_context = opener.open(request, timeout=timeout)
        else:
            response_context = urllib.request.urlopen(request, timeout=timeout)
        with response_context as response:
            body = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return body.decode(charset, errors="replace"), normalize_headers(response.headers)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} for {url}", status=exc.code, headers=normalize_headers(exc.headers)) from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"Network error for {url}: {exc.reason}") from exc


def http_get_text_requests(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
    disable_proxy: bool = False,
) -> Tuple[str, Dict[str, str]]:
    try:
        import requests
    except ImportError as exc:
        raise FetchError("requests is not installed") from exc

    session = requests.Session()
    session.trust_env = not disable_proxy
    try:
        response = session.get(
            url,
            params=params,
            headers=headers or {},
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise FetchError(f"Network error for {url}: {exc}") from exc
    if response.status_code >= 400:
        raise FetchError(
            f"HTTP {response.status_code} for {url}",
            status=response.status_code,
            headers=normalize_headers(response.headers),
        )
    return response.text, normalize_headers(response.headers)


def fetch_provider_text(
    url: str,
    params: Optional[Dict[str, Any]],
    headers: Dict[str, str],
    timeout: int,
    disable_proxy: bool,
    use_requests: bool,
) -> Tuple[str, Dict[str, str]]:
    if use_requests:
        return http_get_text_requests(
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            disable_proxy=disable_proxy,
        )
    return http_get_text(
        url,
        params=params,
        headers=headers,
        timeout=timeout,
        disable_proxy=disable_proxy,
    )


def http_get_bytes(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
    max_bytes: int = 15000000,
) -> Tuple[bytes, str, Dict[str, str]]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_headers = normalize_headers(response.headers)
            content_length = response_headers.get("content-length")
            if content_length and int(content_length) > max_bytes:
                raise FetchError(f"Image too large by content-length: {content_length}")
            body = response.read(max_bytes + 1)
            content_type = response_headers.get("content-type", "").split(";", 1)[0].strip().lower()
            return body, content_type, response_headers
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} for image {url}", status=exc.code, headers=normalize_headers(exc.headers)) from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"Network error for image {url}: {exc.reason}") from exc


def as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.]", "", value)
        if cleaned:
            try:
                return float(cleaned)
            except ValueError:
                return default
    return default


def strip_html(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_nested(data: Dict[str, Any], path: Sequence[str]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def candidate_to_dict(candidate: MemeCandidate) -> Dict[str, Any]:
    return dataclasses.asdict(candidate)


def downloaded_to_dict(downloaded: DownloadedMeme) -> Dict[str, Any]:
    return dataclasses.asdict(downloaded)


def load_wecom_helper() -> Any:
    helper_path = project_root() / "skills" / "daily-douyin-video-harvester" / "scripts" / "harvest_douyin_videos.py"
    if not helper_path.is_file():
        raise RuntimeError(f"WeCom helper not found: {helper_path}")
    spec = importlib.util.spec_from_file_location("daily_douyin_video_harvester_wecom_helper", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load WeCom helper: {helper_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def format_wecom_meme_message(item: DownloadedMeme, index: int) -> str:
    title = item.title or item.source_id
    lines = [
        f"### 今日有梗图片 TOP{index}",
        "",
        f"**{title}**",
        "",
        f"来源: {item.provider} | 分数: {item.score:.1f}",
        f"原帖: {item.source_url}",
    ]
    if item.author:
        lines.append(f"作者: {item.author}")
    return "\n".join(lines)


def send_downloaded_memes(downloaded: Sequence[DownloadedMeme], config: Dict[str, Any]) -> int:
    if not downloaded or not config.get("send_after_download", False):
        return 0
    try:
        helper = load_wecom_helper()
        helper_config = helper.deep_merge(
            helper.DEFAULT_CONFIG,
            {"wecom": config.get("wecom", {}), "_env": config.get("_env", os.environ)},
        )
        settings = helper.resolve_wecom_settings(helper_config)
    except Exception as exc:
        add_warning(config, f"WeCom send skipped: {exc}")
        return 0
    if not settings.get("bot_id") or not settings.get("secret") or not settings.get("receiver"):
        add_warning(config, "WeCom send skipped: missing bot credentials or receiver")
        return 0
    sender = helper.WecomBotWebSocketSender(
        settings["bot_id"],
        settings["secret"],
        settings["receiver"],
        bool(settings.get("is_group", False)),
        settings.get("websocket_url") or helper.WECOM_WS_URL,
    )
    sent_count = 0
    try:
        sender.connect()
        for index, item in enumerate(downloaded, start=1):
            try:
                sender.send_markdown(format_wecom_meme_message(item, index))
                sender.send_image(item.local_path)
            except Exception as exc:
                add_warning(config, f"WeCom send failed for {item.local_path}: {exc}")
                continue
            sent_count += 1
    except Exception as exc:
        add_warning(config, f"WeCom send skipped: {exc}")
    finally:
        try:
            sender.close()
        except Exception:
            pass
    return sent_count


def fetch_weibo_hot_terms(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    weibo_config = config.get("weibo", {})
    headers = {"User-Agent": config.get("user_agent", DEFAULT_CONFIG["user_agent"]), "Referer": "https://weibo.com/"}
    cookie = config.get("_env", os.environ).get(weibo_config.get("cookie_env", "WEIBO_COOKIE"))
    if cookie:
        headers["Cookie"] = cookie
    endpoint = weibo_config.get("endpoint_hotsearch")
    if not endpoint:
        return []
    try:
        payload, _headers = http_get_json(endpoint, headers=headers)
    except FetchError as exc:
        add_warning(config, f"weibo hotSearch failed: {exc}")
        if "weibo" in set(config.get("providers", [])):
            mark_provider_failed(config, "weibo")
        return []
    return parse_weibo_hot_terms(payload, max_terms=int(weibo_config.get("max_hot_terms", 20)))


def parse_weibo_hot_terms(payload: Dict[str, Any], max_terms: int = 20) -> List[Dict[str, Any]]:
    realtime = extract_nested(payload, ["data", "realtime"]) or []
    terms: List[Dict[str, Any]] = []
    for index, item in enumerate(realtime[:max_terms], start=1):
        if not isinstance(item, dict):
            continue
        word = str(item.get("word") or item.get("note") or "").strip()
        if not word:
            continue
        raw_hot = as_float(item.get("num", item.get("raw_hot", item.get("rank", 0))))
        rank_score = max(max_terms - index + 1, 1) * 1000.0
        hot_score = rank_score + math.log1p(max(raw_hot, 0.0)) * 100.0
        terms.append(
            {
                "word": word,
                "rank": index,
                "num": item.get("num"),
                "raw_hot": item.get("raw_hot"),
                "label_name": item.get("label_name"),
                "score": hot_score,
            }
        )
    return terms


def cache_hot_terms(config: Dict[str, Any], terms: Sequence[Dict[str, Any]]) -> None:
    if terms and not config.get("_hot_terms"):
        config["_hot_terms"] = list(terms)


def get_shared_hot_terms(config: Dict[str, Any], max_terms: int) -> List[Dict[str, Any]]:
    terms = config.get("_hot_terms")
    if terms is None:
        terms = fetch_weibo_hot_terms(config)
        cache_hot_terms(config, terms)
    if not isinstance(terms, list):
        return []
    return terms[:max_terms]


def term_word(term: Any) -> str:
    if isinstance(term, dict):
        return str(term.get("word") or term.get("title") or "").strip()
    return str(term or "").strip()


def term_score(term: Any, fallback: float = 0.0) -> float:
    if isinstance(term, dict):
        return as_float(term.get("score", term.get("hot_value", term.get("num", fallback))), fallback)
    return fallback


def build_hot_driven_search_specs(
    provider_config: Dict[str, Any],
    config: Dict[str, Any],
    hot_terms: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    specs: List[Dict[str, Any]] = []
    if provider_config.get("use_hot_terms", True):
        max_hot_terms = int(provider_config.get("max_hot_terms", 8))
        patterns = provider_config.get("search_patterns") or ["{term}", "{term} 名场面", "{term} 表情包"]
        terms = list(hot_terms or [])
        if not terms:
            terms = get_shared_hot_terms(config, max_terms=max_hot_terms)
        for rank, term in enumerate(terms[:max_hot_terms], start=1):
            word = term_word(term)
            if not word:
                continue
            score = term_score(term, fallback=max(max_hot_terms - rank + 1, 1) * 1000.0)
            for pattern in patterns:
                query = str(pattern).format(term=word).strip()
                if query:
                    specs.append({"query": query, "base_score": score, "term": word})
    for index, keyword in enumerate(provider_config.get("fallback_keywords") or provider_config.get("search_keywords") or []):
        query = str(keyword).strip()
        if query:
            specs.append({"query": query, "base_score": max(1.0, 500.0 - index), "term": query})
    max_queries = int(provider_config.get("max_search_queries", config.get("max_per_provider", 30)))
    result: List[Dict[str, Any]] = []
    seen = set()
    for spec in specs:
        query = str(spec.get("query") or "").strip()
        if not query or query in seen:
            continue
        seen.add(query)
        result.append(spec)
        if len(result) >= max_queries:
            break
    return result


def build_hot_driven_queries(provider_config: Dict[str, Any], config: Dict[str, Any]) -> List[str]:
    return [spec["query"] for spec in build_hot_driven_search_specs(provider_config, config)]


def iter_weibo_mblogs(cards: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for card in cards:
        if not isinstance(card, dict):
            continue
        mblog = card.get("mblog")
        if isinstance(mblog, dict):
            yield mblog
        group = card.get("card_group")
        if isinstance(group, list):
            yield from iter_weibo_mblogs(group)


def weibo_image_urls(mblog: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    pics = mblog.get("pics") or []
    if isinstance(pics, list):
        for pic in pics:
            if not isinstance(pic, dict):
                continue
            url = extract_nested(pic, ["large", "url"]) or pic.get("url")
            if url:
                urls.append(str(url))
    page_pic = extract_nested(mblog, ["page_info", "page_pic", "url"])
    if page_pic:
        urls.append(str(page_pic))
    return list(dict.fromkeys(urls))


def parse_weibo_search_cards(payload: Dict[str, Any], term: Dict[str, Any], query: str) -> List[MemeCandidate]:
    cards = extract_nested(payload, ["data", "cards"]) or []
    candidates: List[MemeCandidate] = []
    for mblog in iter_weibo_mblogs(cards):
        source_id = str(mblog.get("id") or mblog.get("mid") or "")
        if not source_id:
            continue
        user = mblog.get("user") if isinstance(mblog.get("user"), dict) else {}
        user_id = user.get("id") or user.get("idstr")
        bid = mblog.get("bid") or source_id
        if user_id:
            source_url = f"https://weibo.com/{user_id}/{bid}"
        else:
            source_url = f"https://weibo.com/i/{source_id}"
        metrics = {
            "attitudes_count": int(as_float(mblog.get("attitudes_count"))),
            "reposts_count": int(as_float(mblog.get("reposts_count"))),
            "comments_count": int(as_float(mblog.get("comments_count"))),
            "hot_term_score": term.get("score", 0.0),
        }
        score = (
            float(term.get("score", 0.0))
            + metrics["attitudes_count"]
            + metrics["comments_count"] * 2
            + metrics["reposts_count"] * 4
        )
        for image_index, image_url in enumerate(weibo_image_urls(mblog), start=1):
            candidates.append(
                MemeCandidate(
                    provider="weibo",
                    source_id=f"{source_id}:{image_index}",
                    source_url=source_url,
                    image_url=image_url,
                    title=strip_html(mblog.get("text")) or str(term.get("word") or query),
                    author=user.get("screen_name") if isinstance(user, dict) else None,
                    created_at=mblog.get("created_at"),
                    score=score,
                    metrics=metrics,
                    possibly_sensitive=False,
                    extra={"query": query, "term": term.get("word")},
                )
            )
    return candidates


def search_weibo_images_for_term(term: Dict[str, Any], config: Dict[str, Any]) -> List[MemeCandidate]:
    if config.get("_weibo_search_blocked"):
        return []
    weibo_config = config.get("weibo", {})
    headers = {"User-Agent": config.get("user_agent", DEFAULT_CONFIG["user_agent"]), "Referer": "https://m.weibo.cn/"}
    cookie = config.get("_env", os.environ).get(weibo_config.get("cookie_env", "WEIBO_COOKIE"))
    if cookie:
        headers["Cookie"] = cookie
    suffixes = weibo_config.get("search_suffixes") or ["", "名场面", "表情包", "梗", "搞笑图"]
    candidates: List[MemeCandidate] = []
    for suffix_index, suffix in enumerate(suffixes):
        if suffix_index:
            time.sleep(float(weibo_config.get("request_interval_seconds", 2)))
        query = f"{term.get('word', '')} {suffix}".strip()
        params = {
            "containerid": "100103type=1&q=" + query,
            "page_type": "searchall",
            "page": 1,
        }
        try:
            payload, _headers = http_get_json(
                "https://m.weibo.cn/api/container/getIndex",
                params=params,
                headers=headers,
            )
        except FetchError as exc:
            add_warning(config, f"weibo search failed for {query}: {exc}")
            if exc.status in {401, 403, 429, 432}:
                config["_weibo_search_blocked"] = True
                mark_provider_failed(config, "weibo")
                break
            continue
        candidates.extend(parse_weibo_search_cards(payload, term, query))
    return candidates


def collect_weibo(config: Dict[str, Any]) -> List[MemeCandidate]:
    max_per_provider = int(config.get("max_per_provider", 30))
    terms = fetch_weibo_hot_terms(config)[:max(1, max_per_provider)]
    cache_hot_terms(config, terms)
    candidates: List[MemeCandidate] = []
    limit = max_per_provider
    for term_index, term in enumerate(terms):
        if term_index:
            time.sleep(float(config.get("weibo", {}).get("request_interval_seconds", 2)))
        candidates.extend(search_weibo_images_for_term(term, config))
        if len(candidates) >= limit or config.get("_weibo_search_blocked"):
            break
    return candidates


def unescape_url(value: str) -> str:
    url = value.replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&")
    return html.unescape(url)


def embedded_image_urls(text: str, host_keywords: Optional[Sequence[str]] = None) -> List[str]:
    search_text = unescape_url(text)
    url_pattern = re.compile(r"https?://[^\"'<>\\\s]+", re.IGNORECASE)
    urls: List[str] = []
    seen_urls = set()
    for match in url_pattern.finditer(search_text):
        image_url = unescape_url(match.group(0)).strip().rstrip("),.;]}")
        parsed = urllib.parse.urlsplit(image_url)
        host = parsed.netloc.lower()
        if host_keywords and not any(keyword in host for keyword in host_keywords):
            continue
        if extension_from_url(image_url) is None:
            path = parsed.path.lower()
            if not any(token in path for token in ("/img/", "image", "tos-", "/obj/")):
                continue
        if image_url in seen_urls:
            continue
        seen_urls.add(image_url)
        urls.append(image_url)
    return urls


def parse_xiaohongshu_search_html(html_text: str, keyword: str, source_url: str) -> List[MemeCandidate]:
    candidates: List[MemeCandidate] = []
    for index, image_url in enumerate(embedded_image_urls(html_text, host_keywords=("xhscdn.com",)), start=1):
        source_id = hashlib.sha1(image_url.encode("utf-8")).hexdigest()[:16]
        candidates.append(
            MemeCandidate(
                provider="xiaohongshu",
                source_id=source_id,
                source_url=source_url,
                image_url=image_url,
                title=f"{keyword} 小红书图片",
                author=None,
                created_at=None,
                score=max(1.0, 1000.0 - index),
                metrics={"rank": index},
                possibly_sensitive=False,
                extra={"query": keyword, "source": "public_search_html"},
            )
        )
    return candidates


def collect_xiaohongshu(config: Dict[str, Any]) -> List[MemeCandidate]:
    xhs_config = config.get("xiaohongshu", {})
    headers = {
        "User-Agent": config.get("user_agent", DEFAULT_CONFIG["user_agent"]),
        "Referer": "https://www.xiaohongshu.com/",
    }
    cookie = config.get("_env", os.environ).get(xhs_config.get("cookie_env", "XHS_COOKIE"))
    if cookie:
        headers["Cookie"] = cookie
    else:
        add_warning(config, "XHS_COOKIE not set; xiaohongshu public pages may be unavailable and will be skipped on access failure")

    endpoint = xhs_config.get("endpoint_search", "https://www.xiaohongshu.com/search_result")
    candidates: List[MemeCandidate] = []
    max_per_provider = int(config.get("max_per_provider", 30))
    for index, spec in enumerate(build_hot_driven_search_specs(xhs_config, config)[:max_per_provider]):
        keyword = spec["query"]
        if index:
            time.sleep(float(xhs_config.get("request_interval_seconds", 2)))
        params = {"keyword": keyword, "source": "web_search_result_notes"}
        source_url = build_url(endpoint, params)
        try:
            html_text, _headers = fetch_provider_text(
                endpoint,
                params=params,
                headers=headers,
                timeout=int(xhs_config.get("request_timeout_seconds", 20)),
                disable_proxy=bool(xhs_config.get("disable_proxy", True)),
                use_requests=bool(xhs_config.get("use_requests", True)),
            )
        except FetchError as exc:
            if run_proxy_guard(config, "xiaohongshu", str(exc)):
                try:
                    html_text, _headers = fetch_provider_text(
                        endpoint,
                        params=params,
                        headers=headers,
                        timeout=int(xhs_config.get("request_timeout_seconds", 20)),
                        disable_proxy=bool(xhs_config.get("disable_proxy", True)),
                        use_requests=bool(xhs_config.get("use_requests", True)),
                    )
                except FetchError as retry_exc:
                    exc = retry_exc
                else:
                    provider_candidates = parse_xiaohongshu_search_html(html_text, keyword=keyword, source_url=source_url)
                    for candidate in provider_candidates:
                        candidate.score += float(spec.get("base_score", 0.0))
                        candidate.metrics["query_score"] = float(spec.get("base_score", 0.0))
                        candidate.extra["term"] = spec.get("term")
                    candidates.extend(provider_candidates)
                    if len(candidates) >= max_per_provider:
                        break
                    continue
            if exc.status in {401, 403, 429}:
                add_warning(config, f"xiaohongshu search skipped for {keyword}: HTTP {exc.status}; login cookie may be required")
            else:
                add_warning(config, f"xiaohongshu search failed for {keyword}: {exc}")
            mark_provider_failed(config, "xiaohongshu")
            if exc.status in {None, 401, 403, 429}:
                break
            continue
        provider_candidates = parse_xiaohongshu_search_html(html_text, keyword=keyword, source_url=source_url)
        for candidate in provider_candidates:
            candidate.score += float(spec.get("base_score", 0.0))
            candidate.metrics["query_score"] = float(spec.get("base_score", 0.0))
            candidate.extra["term"] = spec.get("term")
        candidates.extend(provider_candidates)
        if len(candidates) >= max_per_provider:
            break
    return candidates


def is_image_url(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path.lower()
    return Path(path).suffix in IMAGE_EXTENSIONS


def parse_reddit_listing(payload: Dict[str, Any], subreddit: str) -> List[MemeCandidate]:
    children = extract_nested(payload, ["data", "children"]) or []
    candidates: List[MemeCandidate] = []
    for child in children:
        post = child.get("data") if isinstance(child, dict) and isinstance(child.get("data"), dict) else None
        if not post:
            continue
        if post.get("over_18"):
            continue
        image_url = post.get("url_overridden_by_dest") or post.get("url")
        if post.get("post_hint") != "image" or not image_url or not is_image_url(str(image_url)):
            continue
        permalink = post.get("permalink") or ""
        metrics = {
            "ups": int(as_float(post.get("ups"))),
            "num_comments": int(as_float(post.get("num_comments"))),
            "upvote_ratio": as_float(post.get("upvote_ratio")),
        }
        score = metrics["ups"] + metrics["num_comments"] * 2 + metrics["upvote_ratio"] * 100
        created_at = None
        if post.get("created_utc"):
            created_at = dt.datetime.fromtimestamp(as_float(post.get("created_utc")), dt.timezone.utc).isoformat()
        candidates.append(
            MemeCandidate(
                provider="reddit",
                source_id=str(post.get("id") or ""),
                source_url=f"https://www.reddit.com{permalink}",
                image_url=str(image_url),
                title=strip_html(post.get("title")),
                author=post.get("author"),
                created_at=created_at,
                score=score,
                metrics=metrics,
                possibly_sensitive=False,
                extra={"subreddit": subreddit},
            )
        )
    return candidates


def collect_reddit(config: Dict[str, Any]) -> List[MemeCandidate]:
    reddit_config = config.get("reddit", {})
    headers = {"User-Agent": config.get("user_agent", DEFAULT_CONFIG["user_agent"])}
    candidates: List[MemeCandidate] = []
    failures = 0
    for subreddit in reddit_config.get("subreddits") or []:
        listing = reddit_config.get("listing", "top")
        params = {"limit": 50, "t": reddit_config.get("time", "day")}
        url = f"https://www.reddit.com/r/{subreddit}/{listing}.json"
        try:
            payload, _headers = http_get_json(url, params=params, headers=headers)
        except FetchError as exc:
            if exc.status in {403, 429}:
                add_warning(config, f"reddit {subreddit} skipped: HTTP {exc.status}")
            else:
                add_warning(config, f"reddit {subreddit} failed: {exc}")
            failures += 1
            continue
        candidates.extend(parse_reddit_listing(payload, subreddit=subreddit))
    if failures and failures == len(reddit_config.get("subreddits") or []):
        mark_provider_failed(config, "reddit")
    return candidates


def contains_block_keyword(candidate: MemeCandidate, block_keywords: Sequence[str]) -> bool:
    haystack = f"{candidate.title} {candidate.source_url} {candidate.image_url}".lower()
    return any(str(keyword).lower() in haystack for keyword in block_keywords)


def content_dedupe_key(candidate: MemeCandidate) -> Optional[Tuple[str, str]]:
    if candidate.provider == "weibo" and candidate.source_url:
        return candidate.provider, candidate.source_url
    if candidate.provider == "reddit" and candidate.source_url:
        return candidate.provider, candidate.source_url
    return None


def normalized_topic_key(value: Any) -> str:
    text = strip_html(value)
    text = re.sub(r"#([^#]+)#", r"\1", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text.lower())
    return text[:80]


def cross_provider_topic_key(candidate: MemeCandidate) -> Optional[str]:
    if not isinstance(candidate.extra, dict):
        return None
    topic = candidate.extra.get("term") or candidate.extra.get("query")
    key = normalized_topic_key(topic)
    return key or None


def dedupe_selected_across_providers(candidates: Sequence[MemeCandidate], config: Dict[str, Any]) -> List[MemeCandidate]:
    if not config.get("dedupe_cross_provider_topics", True):
        return list(candidates)
    selected: List[MemeCandidate] = []
    seen_topics = set()
    for candidate in candidates:
        topic_key = cross_provider_topic_key(candidate)
        if topic_key and topic_key in seen_topics:
            continue
        if topic_key:
            seen_topics.add(topic_key)
        selected.append(candidate)
    return selected


def filter_candidates(candidates: Iterable[MemeCandidate], config: Dict[str, Any]) -> Tuple[List[MemeCandidate], int]:
    filtered: List[MemeCandidate] = []
    skipped = 0
    seen_keys = set()
    seen_content = set()
    block_keywords = config.get("block_keywords") or []
    for candidate in candidates:
        if not candidate.image_url:
            skipped += 1
            continue
        if config.get("skip_sensitive", True) and candidate.possibly_sensitive:
            skipped += 1
            continue
        if contains_block_keyword(candidate, block_keywords):
            skipped += 1
            continue
        if config.get("dedupe_same_content", True):
            content_key = content_dedupe_key(candidate)
            if content_key and content_key in seen_content:
                skipped += 1
                continue
            if content_key:
                seen_content.add(content_key)
        key = (candidate.source_url, candidate.image_url)
        if key in seen_keys:
            skipped += 1
            continue
        seen_keys.add(key)
        filtered.append(candidate)
    filtered.sort(key=lambda item: item.score, reverse=True)
    return filtered, skipped


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
    cleaned = {}
    for key, value in state.items():
        try:
            timestamp = dt.datetime.fromisoformat(value)
        except ValueError:
            continue
        if timestamp >= cutoff:
            cleaned[key] = value
    return cleaned


def state_paths(output_dir: Path) -> Tuple[Path, Path]:
    state_dir = output_dir / "state"
    return state_dir / "seen_urls.json", state_dir / "seen_hashes.json"


def daily_seen_path(output_dir: Path) -> Path:
    return output_dir / "state" / "daily_seen.json"


def load_state(output_dir: Path, now: dt.datetime, dedupe_days: int) -> Tuple[Dict[str, str], Dict[str, str]]:
    seen_urls_path, seen_hashes_path = state_paths(output_dir)
    return (
        clean_state(load_state_file(seen_urls_path), now, dedupe_days),
        clean_state(load_state_file(seen_hashes_path), now, dedupe_days),
    )


def save_state(output_dir: Path, seen_urls: Dict[str, str], seen_hashes: Dict[str, str]) -> None:
    seen_urls_path, seen_hashes_path = state_paths(output_dir)
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


def daily_dedupe_keys(candidate: MemeCandidate) -> List[str]:
    keys: List[str] = []
    topic_key = cross_provider_topic_key(candidate)
    if topic_key:
        keys.append(f"topic:{topic_key}")
    title_key = normalized_topic_key(candidate.title)
    if title_key:
        keys.append(f"title:{title_key}")
    if candidate.source_url:
        keys.append(f"source:{candidate.source_url}")
    if candidate.image_url:
        keys.append(f"image:{candidate.image_url}")
    return list(dict.fromkeys(keys))


def filter_same_day_seen(candidates: Sequence[MemeCandidate], config: Dict[str, Any]) -> Tuple[List[MemeCandidate], int]:
    if not config.get("exclude_same_day_topics", True):
        return list(candidates), 0
    output_dir = Path(config["output_dir"])
    now = now_for_config(config)
    daily_seen = load_daily_seen(output_dir, now, int(config.get("dedupe_days", 90)))
    seen_keys = set(daily_seen.get(now.date().isoformat(), {}))
    if not seen_keys:
        return list(candidates), 0
    filtered: List[MemeCandidate] = []
    skipped = 0
    for candidate in candidates:
        if seen_keys.intersection(daily_dedupe_keys(candidate)):
            skipped += 1
            continue
        filtered.append(candidate)
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
    return ext if ext in IMAGE_EXTENSIONS else None


def extension_from_content_type(content_type: str) -> Optional[str]:
    if content_type == "image/jpeg":
        return ".jpg"
    ext = mimetypes.guess_extension(content_type or "")
    if ext == ".jpe":
        ext = ".jpg"
    return ext if ext in IMAGE_EXTENSIONS else None


def accepts_image(content_type: str, image_url: str) -> bool:
    return "image" in (content_type or "").lower() or extension_from_url(image_url) is not None


def build_image_headers(candidate: MemeCandidate, config: Dict[str, Any]) -> Dict[str, str]:
    headers = {"User-Agent": config.get("user_agent", DEFAULT_CONFIG["user_agent"])}
    if "sinaimg" in candidate.image_url:
        headers["Referer"] = "https://weibo.com/"
    elif candidate.provider == "xiaohongshu" or "xhscdn" in candidate.image_url:
        headers["Referer"] = "https://www.xiaohongshu.com/"
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


def fetch_image_with_retries(candidate: MemeCandidate, config: Dict[str, Any]) -> Tuple[bytes, str]:
    max_bytes = int(config.get("max_image_bytes", 15000000))
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            data, content_type, _headers = http_get_bytes(
                candidate.image_url,
                headers=build_image_headers(candidate, config),
                timeout=20,
                max_bytes=max_bytes,
            )
            return data, content_type
        except FetchError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    raise FetchError(f"download failed after retries for {candidate.image_url}: {last_error}")


def download_candidate(
    candidate: MemeCandidate,
    rank: int,
    day_dir: Path,
    config: Dict[str, Any],
    seen_hashes: Dict[str, str],
    downloaded_at: str,
) -> Optional[DownloadedMeme]:
    data, content_type = fetch_image_with_retries(candidate, config)
    size_bytes = len(data)
    min_bytes = int(config.get("min_image_bytes", 2048))
    max_bytes = int(config.get("max_image_bytes", 15000000))
    if not accepts_image(content_type, candidate.image_url):
        raise FetchError(f"not an image content-type: {content_type or 'unknown'}")
    if size_bytes < min_bytes:
        raise FetchError(f"image too small: {size_bytes} bytes")
    if size_bytes > max_bytes:
        raise FetchError(f"image too large: {size_bytes} bytes")

    sha256 = hashlib.sha256(data).hexdigest()
    if sha256 in seen_hashes:
        return None
    extension = extension_from_content_type(content_type) or extension_from_url(candidate.image_url) or ".jpg"
    provider_dir = day_dir / candidate.provider
    provider_dir.mkdir(parents=True, exist_ok=True)
    score_int = int(max(candidate.score, 0))
    slug = slugify(candidate.title, fallback=re.sub(r"[^A-Za-z0-9_-]+", "-", candidate.source_id) or "meme")
    filename = f"{rank:03d}_{candidate.provider}_{score_int}_{slug}_{sha256[:8]}{extension}"
    final_path = unique_path(provider_dir / filename)
    tmp_path = final_path.with_name(final_path.name + ".tmp")
    tmp_path.write_bytes(data)
    os.replace(str(tmp_path), str(final_path))
    return DownloadedMeme(
        **candidate_to_dict(candidate),
        local_path=str(final_path),
        sha256=sha256,
        content_type=content_type or f"image/{extension.lstrip('.')}",
        size_bytes=size_bytes,
        downloaded_at=downloaded_at,
    )


def planned_filename(candidate: MemeCandidate, rank: int) -> str:
    extension = extension_from_url(candidate.image_url) or ".jpg"
    slug = slugify(candidate.title, fallback=re.sub(r"[^A-Za-z0-9_-]+", "-", candidate.source_id) or "meme")
    return f"{candidate.provider}/{rank:03d}_{candidate.provider}_{int(max(candidate.score, 0))}_{slug}_pending{extension}"


def collect_candidates(config: Dict[str, Any]) -> List[MemeCandidate]:
    collectors = {
        "weibo": collect_weibo,
        "xiaohongshu": collect_xiaohongshu,
        "reddit": collect_reddit,
    }
    all_candidates: List[MemeCandidate] = []
    for provider in config.get("providers", []):
        collector = collectors.get(provider)
        if not collector:
            add_warning(config, f"unknown provider skipped: {provider}")
            mark_provider_skipped(config, provider)
            continue
        try:
            provider_candidates = collector(config)
        except Exception as exc:  # Provider isolation is intentional here.
            add_warning(config, f"{provider} provider failed: {exc}")
            mark_provider_failed(config, provider)
            continue
        provider_candidates.sort(key=lambda item: item.score, reverse=True)
        max_per_provider = int(config.get("max_per_provider", 30))
        all_candidates.extend(provider_candidates[:max_per_provider])
    return all_candidates


def select_candidates_for_download(candidates: List[MemeCandidate], config: Dict[str, Any]) -> List[MemeCandidate]:
    max_total = int(config.get("max_total", 50))
    if max_total <= 0:
        return []
    per_provider = config.get("max_downloads_per_provider")
    if per_provider in (None, "", 0, False):
        return candidates[:max_total]
    per_provider_limit = int(per_provider)
    provider_order = list(dict.fromkeys(list(config.get("providers", [])) + [candidate.provider for candidate in candidates]))
    grouped: Dict[str, List[MemeCandidate]] = {provider: [] for provider in provider_order}
    for candidate in candidates:
        grouped.setdefault(candidate.provider, []).append(candidate)
    selected: List[MemeCandidate] = []
    for provider in provider_order:
        for candidate in grouped.get(provider, [])[:per_provider_limit]:
            if len(selected) >= max_total:
                return dedupe_selected_across_providers(selected, config)
            selected.append(candidate)
    return dedupe_selected_across_providers(selected, config)


def run_dry_run(candidates: List[MemeCandidate], config: Dict[str, Any]) -> Dict[str, Any]:
    max_total = int(config.get("max_total", 50))
    planned = []
    for rank, candidate in enumerate(candidates[:max_total], start=1):
        item = candidate_to_dict(candidate)
        item["planned_path"] = planned_filename(candidate, rank)
        planned.append(item)
    return {
        "dry_run": True,
        "output_dir": config.get("output_dir"),
        "providers": config.get("providers", []),
        "candidate_count": len(candidates),
        "downloaded_count": 0,
        "sent_count": 0,
        "skipped_count": 0,
        "warnings": list(config.get("_warnings", [])),
        "candidates": planned,
    }


def download_candidates(candidates: List[MemeCandidate], config: Dict[str, Any]) -> Dict[str, Any]:
    output_dir = Path(config["output_dir"])
    now = now_for_config(config)
    day_dir = output_dir / now.date().isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    dedupe_days = int(config.get("dedupe_days", 90))
    seen_urls, seen_hashes = load_state(output_dir, now, dedupe_days)
    timestamp = now.isoformat()
    daily_seen = load_daily_seen(output_dir, now, dedupe_days)
    day_key = now.date().isoformat()
    today_seen = daily_seen.setdefault(day_key, {})
    state_delta = {"urls": {}, "hashes": {}, "daily_seen": {}}
    downloaded: List[DownloadedMeme] = []
    skipped_count = 0

    for candidate in candidates:
        if len(downloaded) >= int(config.get("max_total", 50)):
            break
        if candidate.source_url in seen_urls or candidate.image_url in seen_urls:
            skipped_count += 1
            continue
        rank = len(downloaded) + 1
        try:
            result = download_candidate(candidate, rank, day_dir, config, seen_hashes, downloaded_at=timestamp)
        except FetchError as exc:
            skipped_count += 1
            add_warning(config, f"download skipped {candidate.image_url}: {exc}")
            continue
        if result is None:
            skipped_count += 1
            continue
        downloaded.append(result)
        seen_urls[candidate.source_url] = timestamp
        seen_urls[candidate.image_url] = timestamp
        seen_hashes[result.sha256] = timestamp
        state_delta["urls"][candidate.source_url] = timestamp
        state_delta["urls"][candidate.image_url] = timestamp
        state_delta["hashes"][result.sha256] = timestamp
        for key in daily_dedupe_keys(result):
            today_seen[key] = timestamp
            state_delta["daily_seen"][key] = timestamp

    save_state(output_dir, seen_urls, seen_hashes)
    save_daily_seen(output_dir, daily_seen)
    sent_count = send_downloaded_memes(downloaded, config)
    summary = {
        "dry_run": False,
        "output_dir": str(output_dir),
        "date_dir": str(day_dir),
        "providers": config.get("providers", []),
        "candidate_count": len(candidates),
        "downloaded_count": len(downloaded),
        "sent_count": sent_count,
        "skipped_count": skipped_count,
        "warnings": list(config.get("_warnings", [])),
        "manifest": str(day_dir / "manifest.jsonl"),
        "index": str(day_dir / "index.md"),
        "downloaded": [downloaded_to_dict(item) for item in downloaded],
    }
    write_outputs(day_dir, downloaded, summary, state_delta)
    return summary


def relative_posix(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def write_outputs(day_dir: Path, downloaded: List[DownloadedMeme], summary: Dict[str, Any], state_delta: Dict[str, Any]) -> None:
    manifest_lines = [json.dumps(downloaded_to_dict(item), ensure_ascii=False, sort_keys=True) for item in downloaded]
    atomic_write_text(day_dir / "manifest.jsonl", "\n".join(manifest_lines) + ("\n" if manifest_lines else ""))
    atomic_write_json(day_dir / "state_delta.json", state_delta)
    index_lines = [
        f"# Daily Meme Harvest - {day_dir.name}",
        "",
        "## Summary",
        "",
        f"- providers: {', '.join(summary.get('providers', []))}",
        f"- downloaded_count: {summary.get('downloaded_count', 0)}",
        f"- sent_count: {summary.get('sent_count', 0)}",
        f"- skipped_count: {summary.get('skipped_count', 0)}",
        f"- warnings: {len(summary.get('warnings', []))}",
        "",
    ]
    if summary.get("warnings"):
        index_lines.append("## Warnings")
        index_lines.append("")
        for warning in summary["warnings"]:
            index_lines.append(f"- {warning}")
        index_lines.append("")
    index_lines.append("## Images")
    index_lines.append("")
    for item in downloaded:
        title = (item.title or item.source_id).replace("\n", " ")
        rel_path = relative_posix(Path(item.local_path), day_dir)
        index_lines.append(f"![{title}]({rel_path})")
        index_lines.append(
            f"来源: {item.provider} | 分数: {item.score:.1f} | 原帖: {item.source_url} | 作者: {item.author or ''}"
        )
        index_lines.append("")
    atomic_write_text(day_dir / "index.md", "\n".join(index_lines).rstrip() + "\n")


def run(config: Dict[str, Any]) -> Dict[str, Any]:
    raw_candidates = collect_candidates(config)
    filtered_candidates, skipped = filter_candidates(raw_candidates, config)
    filtered_candidates, same_day_skipped = filter_same_day_seen(filtered_candidates, config)
    skipped += same_day_skipped
    candidates = select_candidates_for_download(filtered_candidates, config)
    skipped += max(0, len(filtered_candidates) - len(candidates))
    if config.get("dry_run"):
        summary = run_dry_run(candidates, config)
        summary["skipped_count"] = skipped
        summary["available_candidate_count"] = len(filtered_candidates)
        return summary
    summary = download_candidates(candidates, config)
    summary["available_candidate_count"] = len(filtered_candidates)
    summary["skipped_count"] += skipped
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch, rank, deduplicate, and download daily meme images.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--providers",
        default=None,
        help="Comma-separated providers; default comes from config (weibo,xiaohongshu).",
    )
    parser.add_argument("--out", default=None, help="Output directory.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Config JSON path.")
    parser.add_argument("--max-total", type=int, default=None, help="Maximum images to download.")
    parser.add_argument("--max-per-provider", type=int, default=None, help="Maximum candidates per provider.")
    parser.add_argument("--since-hours", type=int, default=None, help="Recency window for providers that support it.")
    parser.add_argument("--send-wecom", action="store_true", help="Send downloaded images to Enterprise WeChat.")
    parser.add_argument("--receiver", default=None, help="WeCom receiver userid/chatid.")
    parser.add_argument("--group", action="store_true", help="Treat receiver as a WeCom group chatid.")
    parser.add_argument("--dry-run", action="store_true", help="Print candidates without downloading.")
    parser.add_argument("--json", action="store_true", help="Print JSON summary to stdout.")
    parser.add_argument("--debug", action="store_true", help="Print debug logs to stderr.")
    return parser


def print_summary(summary: Dict[str, Any]) -> None:
    print(
        "daily-meme-harvester: "
        f"downloaded={summary.get('downloaded_count', 0)} "
        f"sent={summary.get('sent_count', 0)} "
        f"candidates={summary.get('candidate_count', 0)}"
    )
    if summary.get("date_dir"):
        print(f"output: {summary['date_dir']}")
    for warning in summary.get("warnings", []):
        print(f"warning: {warning}", file=sys.stderr)


def should_exit_two(summary: Dict[str, Any], config: Dict[str, Any]) -> bool:
    if summary.get("dry_run"):
        return False
    if summary.get("downloaded_count", 0) > 0:
        return False
    providers = set(config.get("providers", []))
    failed = set(config.get("_failed_providers", set()))
    skipped = set(config.get("_skipped_providers", set()))
    return bool(providers) and providers.issubset(failed | skipped)


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
