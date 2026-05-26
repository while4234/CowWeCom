#!/usr/bin/env python3
"""Local per-user token usage tracker.

Stores token events locally only. No network calls.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8 and older.
    ZoneInfo = None

USER_ENV_KEYS = [
    "COW_CURRENT_USER_ID",
    "COW_USER_ID",
    "WEIXIN_USER_ID",
    "WECHAT_USER_ID",
    "CURRENT_USER_ID",
    "USER_ID",
]

SOURCE_CHOICES = ("auto", "token-tracker", "llm-cache", "both")
LOCAL_TZ_NAME = "Asia/Shanghai"
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME) if ZoneInfo else timezone(timedelta(hours=8), LOCAL_TZ_NAME)


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def now_iso() -> str:
    return now_local().isoformat(timespec="seconds")


def parse_dt(value: str) -> datetime:
    if not value:
        raise ValueError("empty datetime")
    value = value.strip().replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return datetime.fromisoformat(value).replace(tzinfo=LOCAL_TZ)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


def workspace_root() -> Path:
    env = os.getenv("COW_WORKSPACE") or os.getenv("COW_HOME")
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve()
    # .../cow/skills/token-usage-tracker/scripts/token_usage.py
    if len(here.parents) >= 4:
        return here.parents[3]
    return Path.cwd().resolve()


def get_data_dir(args: argparse.Namespace) -> Path:
    raw = args.data_dir or os.getenv("COW_TOKEN_USAGE_DIR")
    path = Path(raw).expanduser() if raw else workspace_root() / "data" / "token-usage-tracker"
    path.mkdir(parents=True, exist_ok=True)
    (path / "users").mkdir(parents=True, exist_ok=True)
    return path.resolve()


def get_llm_cache_path(args: argparse.Namespace) -> Path:
    raw = getattr(args, "llm_cache_file", None) or os.getenv("COW_LLM_CACHE_USAGE_FILE")
    return (Path(raw).expanduser() if raw else workspace_root() / "data" / "llm_cache_usage.jsonl").resolve()


def atomic_write_json(path: Path, obj: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def user_id_from_args(args: argparse.Namespace, required: bool = True) -> str:
    if getattr(args, "user_id", None):
        return str(args.user_id)
    for key in USER_ENV_KEYS:
        value = os.getenv(key)
        if value:
            return value
    if getattr(args, "use_default_user", False):
        return "default"
    if required:
        raise SystemExit(
            "Error: no user id. Pass --user-id, set COW_CURRENT_USER_ID/COW_USER_ID, "
            "or explicitly add --use-default-user."
        )
    return ""


def user_hash(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]


def user_log_path(data_dir: Path, uhash: str) -> Path:
    return data_dir / "users" / f"{uhash}.jsonl"


def append_jsonl(path: Path, obj: object) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


def estimate_tokens(text: str) -> int:
    """Best-effort local token estimate. Uses tiktoken if installed, otherwise heuristic."""
    if not text:
        return 0
    try:
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        pass

    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text))
    latin_words = len(re.findall(r"[A-Za-z0-9_]+", text))
    symbols = len(re.findall(r"[^\w\s\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text))
    whitespace = len(re.findall(r"\s+", text))
    estimated = int(round(cjk * 1.0 + latin_words * 1.25 + symbols * 0.5 + whitespace * 0.05))
    return max(1, estimated)


def resolve_tokens(args: argparse.Namespace) -> tuple[int, int, int, bool, str]:
    explicit = any(
        value is not None
        for value in [args.input_tokens, args.output_tokens, args.total_tokens]
    )
    if explicit:
        input_tokens = int(args.input_tokens or 0)
        output_tokens = int(args.output_tokens or 0)
        total_tokens = int(args.total_tokens) if args.total_tokens is not None else input_tokens + output_tokens
        return input_tokens, output_tokens, total_tokens, bool(args.estimated), "manual"

    input_tokens = estimate_tokens(args.input_text or "")
    output_tokens = estimate_tokens(args.output_text or "")
    total_tokens = input_tokens + output_tokens
    if total_tokens <= 0:
        raise SystemExit("Error: provide token counts or --input-text/--output-text for estimation.")
    return input_tokens, output_tokens, total_tokens, True, "estimated-local"


def load_events(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def load_llm_cache_events(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield llm_cache_record_to_event(record)


def llm_cache_record_to_event(record: dict) -> dict:
    input_details = first_dict(
        record.get("input_tokens_details"),
        record.get("prompt_tokens_details"),
    )
    output_details = first_dict(
        record.get("output_tokens_details"),
        record.get("completion_tokens_details"),
    )
    prompt_tokens = to_int(record.get("prompt_tokens") or record.get("input_tokens"))
    completion_tokens = to_int(record.get("completion_tokens") or record.get("output_tokens"))
    total_tokens = to_int(record.get("total_tokens")) or prompt_tokens + completion_tokens
    cached_tokens = to_int(record.get("cached_tokens") or input_details.get("cached_tokens"))
    reasoning_tokens = to_int(
        record.get("reasoning_tokens")
        or output_details.get("reasoning_tokens")
        or output_details.get("reasoning")
    )
    event = {
        "id": record.get("id") or record.get("request_id"),
        "ts": record.get("timestamp") or record.get("ts"),
        "user_hash": record.get("user_hash") or record.get("session_hash"),
        "display_name": record.get("user_label"),
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "uncached_prompt_tokens": to_int(record.get("uncached_prompt_tokens")) or max(prompt_tokens - cached_tokens, 0),
        "cache_creation_tokens": to_int(record.get("cache_creation_tokens")),
        "reasoning_tokens": reasoning_tokens,
        "estimated": False,
        "source": "llm-cache",
        "model": record.get("model"),
        "channel": record.get("channel_type") or record.get("channel"),
        "conversation_id": record.get("session_hash"),
        "request_id": record.get("request_id"),
        "meta": {
            "wire_api": record.get("wire_api"),
            "prompt_cache_key_hash": record.get("prompt_cache_key_hash"),
        },
    }
    return {k: v for k, v in event.items() if v not in (None, {}, "")}


def first_dict(*values) -> dict:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def to_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def update_index(data_dir: Path, uhash: str, args: argparse.Namespace, event: dict) -> None:
    index_path = data_dir / "users" / "index.json"
    index = read_json(index_path, {"users": {}})
    users = index.setdefault("users", {})
    item = users.setdefault(uhash, {})
    item.setdefault("user_hash", uhash)
    if getattr(args, "display_name", None):
        item["display_name"] = args.display_name
    item.setdefault("first_seen", event["ts"])
    item["last_seen"] = event["ts"]
    item["event_count"] = int(item.get("event_count", 0)) + 1
    item["total_tokens"] = int(item.get("total_tokens", 0)) + int(event.get("total_tokens", 0))
    item["updated_at"] = now_iso()
    atomic_write_json(index_path, index)


def event_in_range(event: dict, args: argparse.Namespace) -> bool:
    ts = event.get("ts")
    if not ts:
        return False
    try:
        dt = parse_dt(ts).astimezone(LOCAL_TZ)
    except Exception:
        return False
    local_now = now_local()

    period = getattr(args, "period", "all")
    from_time = getattr(args, "from_time", None)
    to_time = getattr(args, "to_time", None)

    if period == "today" and dt.date() != local_now.date():
        return False
    if period == "month" and (dt.year, dt.month) != (local_now.year, local_now.month):
        return False
    if from_time and dt < parse_dt(from_time):
        return False
    if to_time:
        end = parse_dt(to_time)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", to_time.strip()):
            end = end + timedelta(days=1)
        if dt >= end:
            return False
    return True


def summarize_events(events) -> dict:
    total_events = 0
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    cached_tokens = 0
    uncached_prompt_tokens = 0
    cache_creation_tokens = 0
    reasoning_tokens = 0
    estimated_events = 0
    first_ts = None
    last_ts = None
    by_model: dict[str, dict[str, int]] = {}
    by_channel: dict[str, dict[str, int]] = {}

    for event in events:
        total_events += 1
        it = int(event.get("input_tokens") or 0)
        ot = int(event.get("output_tokens") or 0)
        tt = int(event.get("total_tokens") or it + ot)
        ct = int(event.get("cached_tokens") or 0)
        upt = int(event.get("uncached_prompt_tokens") or max(it - ct, 0))
        cct = int(event.get("cache_creation_tokens") or 0)
        rt = int(event.get("reasoning_tokens") or 0)
        input_tokens += it
        output_tokens += ot
        total_tokens += tt
        cached_tokens += ct
        uncached_prompt_tokens += upt
        cache_creation_tokens += cct
        reasoning_tokens += rt
        if event.get("estimated"):
            estimated_events += 1
        ts = event.get("ts")
        if ts and (first_ts is None or ts < first_ts):
            first_ts = ts
        if ts and (last_ts is None or ts > last_ts):
            last_ts = ts

        model = str(event.get("model") or "unknown")
        channel = str(event.get("channel") or "unknown")
        by_model.setdefault(model, {
            "events": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
        })
        by_model[model]["events"] += 1
        by_model[model]["input_tokens"] += it
        by_model[model]["output_tokens"] += ot
        by_model[model]["total_tokens"] += tt
        by_model[model]["cached_tokens"] += ct
        by_model[model]["reasoning_tokens"] += rt
        by_channel.setdefault(channel, {
            "events": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
        })
        by_channel[channel]["events"] += 1
        by_channel[channel]["input_tokens"] += it
        by_channel[channel]["output_tokens"] += ot
        by_channel[channel]["total_tokens"] += tt
        by_channel[channel]["cached_tokens"] += ct
        by_channel[channel]["reasoning_tokens"] += rt

    return {
        "events": total_events,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "uncached_prompt_tokens": uncached_prompt_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_hit_rate": (cached_tokens / input_tokens) if input_tokens else 0.0,
        "estimated_events": estimated_events,
        "exact_events": total_events - estimated_events,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "by_model": by_model,
        "by_channel": by_channel,
    }


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", help="Override local storage directory. Default: <workspace>/data/token-usage-tracker")
    parser.add_argument("--user-id", help="Stable user identifier. Stored only as SHA-256 short hash.")
    parser.add_argument("--use-default-user", action="store_true", help="Use literal 'default' if no user id is available. Avoid this in multi-user deployments.")


def add_read_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source",
        choices=SOURCE_CHOICES,
        default="auto",
        help="Read source: token-tracker JSONL, CowAgent llm_cache_usage JSONL, both, or auto fallback.",
    )
    parser.add_argument(
        "--llm-cache-file",
        help="Override CowAgent llm_cache_usage.jsonl path. Default: <workspace>/data/llm_cache_usage.jsonl",
    )


def print_json(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_record(args: argparse.Namespace) -> None:
    data_dir = get_data_dir(args)
    user_id = user_id_from_args(args)
    uhash = user_hash(user_id)
    input_tokens, output_tokens, total_tokens, estimated, source = resolve_tokens(args)
    meta = {}
    for item in args.meta or []:
        if "=" in item:
            k, v = item.split("=", 1)
            meta[k] = v

    event = {
        "id": args.event_id or str(uuid.uuid4()),
        "ts": args.ts or now_iso(),
        "user_hash": uhash,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated": estimated,
        "source": source,
        "model": args.model,
        "channel": args.channel,
        "conversation_id": args.conversation_id,
        "message_id": args.message_id,
        "request_id": args.request_id,
        "meta": meta,
    }
    event = {k: v for k, v in event.items() if v not in (None, {}, "")}
    append_jsonl(user_log_path(data_dir, uhash), event)
    update_index(data_dir, uhash, args, event)
    print_json({"ok": True, "action": "record", "user_hash": uhash, "event": event, "data_dir": str(data_dir)})


def iter_user_hashes(data_dir: Path):
    users_dir = data_dir / "users"
    for path in sorted(users_dir.glob("*.jsonl")):
        yield path.stem


def filtered_events_for_user(data_dir: Path, uhash: str, args: argparse.Namespace):
    for event in load_events(user_log_path(data_dir, uhash)) or []:
        if event_in_range(event, args):
            yield event


def selected_source(args: argparse.Namespace) -> str:
    source = getattr(args, "source", "auto") or "auto"
    if source not in SOURCE_CHOICES:
        raise SystemExit(f"Error: invalid source '{source}'.")
    return source


def token_tracker_events(data_dir: Path, args: argparse.Namespace) -> list[dict]:
    events = []
    for uhash in iter_user_hashes(data_dir):
        events.extend(filtered_events_for_user(data_dir, uhash, args))
    return events


def llm_cache_events(args: argparse.Namespace) -> list[dict]:
    path = get_llm_cache_path(args)
    return [event for event in (load_llm_cache_events(path) or []) if event_in_range(event, args)]


def events_for_summary(data_dir: Path, args: argparse.Namespace) -> tuple[list[dict], str, Optional[str]]:
    source = selected_source(args)
    token_events = [] if source == "llm-cache" else token_tracker_events(data_dir, args)
    cache_events = [] if source == "token-tracker" else llm_cache_events(args)
    cache_path = str(get_llm_cache_path(args)) if source in ("auto", "llm-cache", "both") else None

    if source == "token-tracker":
        return token_events, "token-tracker", None
    if source == "llm-cache":
        return cache_events, "llm-cache", cache_path
    if source == "both":
        return token_events + cache_events, "both", cache_path
    if token_events:
        return token_events, "token-tracker", None
    return cache_events, "llm-cache" if cache_events else "auto", cache_path


def events_for_user_from_list(events: list[dict], uhash: str) -> list[dict]:
    return [event for event in events if str(event.get("user_hash") or "") == uhash]


def events_for_user_identifier(events: list[dict], user_id: str) -> tuple[str, list[dict]]:
    uhash = user_hash(user_id)
    matched = events_for_user_from_list(events, uhash)
    if matched:
        return uhash, matched

    label_matched = [
        event for event in events
        if str(event.get("display_name") or "").strip() == str(user_id).strip()
    ]
    if label_matched:
        return str(label_matched[0].get("user_hash") or uhash), label_matched
    return uhash, []


def user_meta_from_events(events: list[dict]) -> dict[str, dict]:
    meta: dict[str, dict] = {}
    for event in events:
        uhash = str(event.get("user_hash") or "")
        if not uhash:
            continue
        item = meta.setdefault(uhash, {"user_hash": uhash})
        display_name = event.get("display_name")
        if display_name:
            item["display_name"] = display_name
    return meta


def cmd_summary(args: argparse.Namespace) -> None:
    data_dir = get_data_dir(args)
    index = read_json(data_dir / "users" / "index.json", {"users": {}})
    users_meta = index.get("users", {}) if isinstance(index, dict) else {}
    events, source, cache_path = events_for_summary(data_dir, args)
    event_meta = user_meta_from_events(events)

    if args.all:
        result_users = {}
        for uhash in sorted({str(event.get("user_hash") or "") for event in events if event.get("user_hash")}):
            user_events = events_for_user_from_list(events, uhash)
            meta = users_meta.get(uhash, {}) or event_meta.get(uhash, {})
            result_users[uhash] = {
                "user_hash": uhash,
                "display_name": meta.get("display_name"),
                **summarize_events(user_events),
            }
        result = {
            "scope": "all-users",
            "source": source,
            "period": args.period,
            "from_time": args.from_time,
            "to_time": args.to_time,
            "summary": summarize_events(events),
            "users": result_users,
            "data_dir": str(data_dir),
        }
    else:
        user_id = user_id_from_args(args)
        uhash, user_events = events_for_user_identifier(events, user_id)
        meta = users_meta.get(uhash, {}) or event_meta.get(uhash, {})
        result = {
            "scope": "single-user",
            "source": source,
            "user_hash": uhash,
            "display_name": meta.get("display_name"),
            "period": args.period,
            "from_time": args.from_time,
            "to_time": args.to_time,
            "summary": summarize_events(user_events),
            "data_dir": str(data_dir),
        }
    if cache_path:
        result["llm_cache_file"] = cache_path
    print_json(result)


def cmd_list_users(args: argparse.Namespace) -> None:
    data_dir = get_data_dir(args)
    index = read_json(data_dir / "users" / "index.json", {"users": {}})
    users_meta = index.get("users", {}) if isinstance(index, dict) else {}
    events, source, cache_path = events_for_summary(data_dir, args)
    event_meta = user_meta_from_events(events)
    users = []
    user_hashes = sorted({*iter_user_hashes(data_dir), *event_meta.keys()})
    for uhash in user_hashes:
        meta = users_meta.get(uhash, {}) if isinstance(users_meta, dict) else {}
        if not meta:
            meta = event_meta.get(uhash, {})
        summary = summarize_events(events_for_user_from_list(events, uhash))
        users.append({
            "user_hash": uhash,
            "display_name": meta.get("display_name"),
            "event_count_indexed": meta.get("event_count") or summary.get("events"),
            "total_tokens_indexed": meta.get("total_tokens") or summary.get("total_tokens"),
            "first_seen": meta.get("first_seen") or summary.get("first_ts"),
            "last_seen": meta.get("last_seen") or summary.get("last_ts"),
        })
    result = {"ok": True, "source": source, "users": users, "data_dir": str(data_dir)}
    if cache_path:
        result["llm_cache_file"] = cache_path
    print_json(result)


def cmd_reset(args: argparse.Namespace) -> None:
    if not args.yes:
        raise SystemExit("Refusing to reset without --yes.")
    data_dir = get_data_dir(args)
    removed = []

    if args.all:
        for uhash in list(iter_user_hashes(data_dir)):
            path = user_log_path(data_dir, uhash)
            if path.exists():
                path.unlink()
                removed.append(uhash)
        index_path = data_dir / "users" / "index.json"
        if index_path.exists():
            index_path.unlink()
    else:
        user_id = user_id_from_args(args)
        uhash = user_hash(user_id)
        path = user_log_path(data_dir, uhash)
        if path.exists():
            path.unlink()
            removed.append(uhash)
        index_path = data_dir / "users" / "index.json"
        index = read_json(index_path, {"users": {}})
        if isinstance(index, dict) and isinstance(index.get("users"), dict):
            index["users"].pop(uhash, None)
            atomic_write_json(index_path, index)
    print_json({"ok": True, "action": "reset", "removed_user_hashes": removed, "data_dir": str(data_dir)})


def scoped_user_hashes(data_dir: Path, args: argparse.Namespace) -> list[str]:
    if getattr(args, "all", False):
        return list(iter_user_hashes(data_dir))
    user_id = user_id_from_args(args)
    return [user_hash(user_id)]


def cmd_export_csv(args: argparse.Namespace) -> None:
    data_dir = get_data_dir(args)
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    events, source, cache_path = events_for_summary(data_dir, args)
    if args.all:
        hashes = sorted({str(event.get("user_hash") or "") for event in events if event.get("user_hash")})
        scoped_events = events
    else:
        uhash, user_events = events_for_user_identifier(events, user_id_from_args(args))
        hashes = [uhash]
        scoped_events = user_events
    fieldnames = [
        "ts", "user_hash", "input_tokens", "output_tokens", "total_tokens",
        "cached_tokens", "uncached_prompt_tokens", "reasoning_tokens",
        "estimated", "source", "model", "channel", "conversation_id",
        "message_id", "request_id", "event_id",
    ]
    rows = []
    for uhash in hashes:
        for event in events_for_user_from_list(scoped_events, uhash):
            rows.append({
                "ts": event.get("ts"),
                "user_hash": uhash,
                "input_tokens": event.get("input_tokens", 0),
                "output_tokens": event.get("output_tokens", 0),
                "total_tokens": event.get("total_tokens", 0),
                "cached_tokens": event.get("cached_tokens", 0),
                "uncached_prompt_tokens": event.get("uncached_prompt_tokens", 0),
                "reasoning_tokens": event.get("reasoning_tokens", 0),
                "estimated": event.get("estimated", False),
                "source": event.get("source"),
                "model": event.get("model"),
                "channel": event.get("channel"),
                "conversation_id": event.get("conversation_id"),
                "message_id": event.get("message_id"),
                "request_id": event.get("request_id"),
                "event_id": event.get("id"),
            })
    with output.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    result = {
        "ok": True,
        "action": "export-csv",
        "source": source,
        "output": str(output),
        "rows": len(rows),
        "data_dir": str(data_dir),
    }
    if cache_path:
        result["llm_cache_file"] = cache_path
    print_json(result)


def cmd_rebuild_index(args: argparse.Namespace) -> None:
    data_dir = get_data_dir(args)
    index_path = data_dir / "users" / "index.json"
    old_index = read_json(index_path, {"users": {}})
    old_users = old_index.get("users", {}) if isinstance(old_index, dict) else {}
    new_index = {"users": {}}
    for uhash in iter_user_hashes(data_dir):
        events = list(load_events(user_log_path(data_dir, uhash)) or [])
        summary = summarize_events(events)
        if summary["events"] <= 0:
            continue
        old_meta = old_users.get(uhash, {}) if isinstance(old_users, dict) else {}
        item = {
            "user_hash": uhash,
            "display_name": old_meta.get("display_name"),
            "first_seen": summary.get("first_ts"),
            "last_seen": summary.get("last_ts"),
            "event_count": summary.get("events", 0),
            "total_tokens": summary.get("total_tokens", 0),
            "updated_at": now_iso(),
        }
        item = {k: v for k, v in item.items() if v is not None}
        new_index["users"][uhash] = item
    atomic_write_json(index_path, new_index)
    print_json({"ok": True, "action": "rebuild-index", "users": len(new_index["users"]), "data_dir": str(data_dir)})


def extract_usage_counts(payload: object) -> tuple[int, int, int]:
    if not isinstance(payload, dict):
        raise SystemExit("Error: usage JSON must be an object.")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else payload
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", usage.get("prompt", 0)))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens", usage.get("completion", 0)))
    total_tokens = usage.get("total_tokens", usage.get("total", None))
    input_tokens = int(input_tokens or 0)
    output_tokens = int(output_tokens or 0)
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens
    total_tokens = int(total_tokens or 0)
    if total_tokens <= 0 and input_tokens <= 0 and output_tokens <= 0:
        raise SystemExit("Error: no usage counts found. Expected usage.input_tokens/prompt_tokens, output_tokens/completion_tokens, or total_tokens.")
    return input_tokens, output_tokens, total_tokens


def cmd_record_json(args: argparse.Namespace) -> None:
    if args.file:
        raw = Path(args.file).read_text(encoding="utf-8")
    elif args.json:
        raw = args.json
    else:
        raw = sys.stdin.read()
    payload = json.loads(raw)
    input_tokens, output_tokens, total_tokens = extract_usage_counts(payload)
    args.input_tokens = input_tokens
    args.output_tokens = output_tokens
    args.total_tokens = total_tokens
    args.estimated = False
    cmd_record(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local per-user token usage tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="Record one token usage event for one user")
    add_common_args(record)
    record.add_argument("--display-name", help="Optional local display name for index only")
    record.add_argument("--input-tokens", type=int)
    record.add_argument("--output-tokens", type=int)
    record.add_argument("--total-tokens", type=int)
    record.add_argument("--input-text", help="Estimate input tokens locally if exact token count is unavailable")
    record.add_argument("--output-text", help="Estimate output tokens locally if exact token count is unavailable")
    record.add_argument("--estimated", action="store_true", help="Mark manually supplied token counts as estimated")
    record.add_argument("--model")
    record.add_argument("--channel")
    record.add_argument("--conversation-id")
    record.add_argument("--message-id")
    record.add_argument("--request-id")
    record.add_argument("--event-id")
    record.add_argument("--ts", help="ISO timestamp; default is now")
    record.add_argument("--meta", action="append", help="Extra metadata key=value; may be repeated")
    record.set_defaults(func=cmd_record)

    record_json = sub.add_parser("record-json", help="Record one event from OpenAI-compatible usage JSON")
    add_common_args(record_json)
    record_json.add_argument("--display-name", help="Optional local display name for index only")
    record_json.add_argument("--json", help="JSON string containing usage object; stdin is used if omitted")
    record_json.add_argument("--file", help="Path to JSON file containing usage object")
    record_json.add_argument("--model")
    record_json.add_argument("--channel")
    record_json.add_argument("--conversation-id")
    record_json.add_argument("--message-id")
    record_json.add_argument("--request-id")
    record_json.add_argument("--event-id")
    record_json.add_argument("--ts", help="ISO timestamp; default is now")
    record_json.add_argument("--meta", action="append", help="Extra metadata key=value; may be repeated")
    record_json.set_defaults(func=cmd_record_json)

    summary = sub.add_parser("summary", help="Summarize usage for one user or all users")
    add_common_args(summary)
    add_read_source_args(summary)
    summary.add_argument("--all", action="store_true", help="Summarize all local users")
    summary.add_argument("--period", choices=["all", "today", "month"], default="all")
    summary.add_argument("--from-time", dest="from_time", help="Inclusive ISO/date lower bound")
    summary.add_argument("--to-time", dest="to_time", help="Exclusive ISO/date upper bound; date means end of day")
    summary.set_defaults(func=cmd_summary)

    export_csv = sub.add_parser("export-csv", help="Export token usage events to CSV")
    add_common_args(export_csv)
    add_read_source_args(export_csv)
    export_csv.add_argument("--all", action="store_true", help="Export all local users")
    export_csv.add_argument("--period", choices=["all", "today", "month"], default="all")
    export_csv.add_argument("--from-time", dest="from_time", help="Inclusive ISO/date lower bound")
    export_csv.add_argument("--to-time", dest="to_time", help="Exclusive ISO/date upper bound; date means end of day")
    export_csv.add_argument("--output", required=True)
    export_csv.set_defaults(func=cmd_export_csv)

    rebuild = sub.add_parser("rebuild-index", help="Rebuild local user index from JSONL event files")
    add_common_args(rebuild)
    rebuild.set_defaults(func=cmd_rebuild_index)

    list_users = sub.add_parser("list-users", help="List locally known user hashes")
    add_common_args(list_users)
    add_read_source_args(list_users)
    list_users.set_defaults(func=cmd_list_users)

    reset = sub.add_parser("reset", help="Delete local usage data for one user or all users")
    add_common_args(reset)
    reset.add_argument("--all", action="store_true")
    reset.add_argument("--yes", action="store_true", help="Required confirmation flag")
    reset.set_defaults(func=cmd_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
