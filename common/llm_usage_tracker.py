# encoding:utf-8

"""LLM usage telemetry focused on prompt-cache visibility.

Only token counters, hashed identifiers, and optional user display labels are
persisted. Prompt text, tool arguments, API keys, and raw session IDs must
never be written here.
"""

import hashlib
import json
import os
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from common.log import logger
from common.utils import expand_path


_LOCK = threading.Lock()
LONG_INPUT_ZERO_HIT_THRESHOLD = 50000

_REQUEST_SHAPE_TEXT_FIELDS = {
    "request_kind",
    "system_hash",
    "tools_hash",
    "messages_prefix_hash",
    "self_evolution_context_hash",
    "retrieved_knowledge_hash",
    "tool_result_hash",
    "tool_failure_class",
    "reasoning_effort_selected",
    "reasoning_effort_decision_source",
    "reasoning_effort_reason",
    "reasoning_effort_backend",
    "reasoning_effort_main_model",
    "reasoning_effort_chat_scope",
    "reasoning_effort_local_rule",
}
_REQUEST_SHAPE_INT_FIELDS = {
    "message_count",
    "turn_count",
    "tool_count",
    "runtime_context_chars",
    "self_evolution_context_chars",
    "retrieved_knowledge_chars",
    "tool_result_chars",
    "tool_attempt_count",
    "tool_attempt_success_count",
    "tool_attempt_error_count",
    "tool_skip_count",
    "tool_duplicate_success_count",
    "tool_memory_rule_hits",
    "tool_compacted_result_count",
}


def normalize_usage(usage: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize provider usage payloads while preserving cache details."""
    if not isinstance(usage, dict):
        usage = {}

    prompt = _to_int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
    completion = _to_int(usage.get("completion_tokens", usage.get("output_tokens", 0)))
    total = _to_int(usage.get("total_tokens", 0)) or prompt + completion

    input_details = _first_dict(
        usage.get("input_tokens_details"),
        usage.get("prompt_tokens_details"),
    )
    completion_details = _first_dict(
        usage.get("output_tokens_details"),
        usage.get("completion_tokens_details"),
    )

    cached_tokens = _first_positive_int(
        input_details.get("cached_tokens"),
        usage.get("cached_tokens"),
        usage.get("cache_read_input_tokens"),
        usage.get("cache_read_tokens"),
        usage.get("cached_prompt_tokens"),
        usage.get("prompt_cache_hit_tokens"),
        usage.get("prompt_cache_read_tokens"),
    )
    cache_creation_tokens = _first_positive_int(
        input_details.get("cache_creation_input_tokens"),
        usage.get("cache_creation_input_tokens"),
        usage.get("cache_write_input_tokens"),
        usage.get("cache_write_tokens"),
        usage.get("prompt_cache_write_tokens"),
    )

    cached_tokens = min(cached_tokens, prompt) if prompt else cached_tokens
    uncached_prompt = max(prompt - cached_tokens, 0)
    hit_rate = (cached_tokens / prompt) if prompt else 0.0

    normalized: Dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_tokens": cached_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "uncached_prompt_tokens": uncached_prompt,
        "cache_hit_rate": hit_rate,
    }
    if input_details:
        normalized["input_tokens_details"] = dict(input_details)
        normalized["prompt_tokens_details"] = dict(input_details)
    if completion_details:
        normalized["output_tokens_details"] = dict(completion_details)
        normalized["completion_tokens_details"] = dict(completion_details)
    return normalized


def record_usage(usage: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Append one usage record and return the normalized usage payload."""
    normalized = normalize_usage(usage)
    if not _tracking_enabled():
        return normalized

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **_safe_metadata(metadata or {}),
        **normalized,
    }

    try:
        path = _usage_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            _trim_history_unlocked(path)
    except Exception as e:
        logger.debug(f"[PromptCache] Failed to record usage telemetry: {e}")
    return normalized


def get_cache_usage_report(limit: int = 50) -> Dict[str, Any]:
    """Return aggregate prompt-cache metrics and recent records."""
    limit = max(1, min(_to_int(limit) or 50, 500))
    history_limit = max(limit, _history_limit())
    records = _read_records(history_limit)
    recent = list(reversed(records[-limit:]))

    prompt_tokens = sum(_to_int(r.get("prompt_tokens")) for r in records)
    cached_tokens = sum(_to_int(r.get("cached_tokens")) for r in records)
    completion_tokens = sum(_to_int(r.get("completion_tokens")) for r in records)
    total_tokens = sum(_to_int(r.get("total_tokens")) for r in records)
    cache_hits = sum(1 for r in records if _to_int(r.get("cached_tokens")) > 0)
    cacheable_requests = sum(1 for r in records if _to_int(r.get("prompt_tokens")) >= 1024)
    long_input_requests = sum(
        1 for r in records if _to_int(r.get("prompt_tokens")) >= LONG_INPUT_ZERO_HIT_THRESHOLD
    )
    long_input_zero_cache_requests = sum(
        1
        for r in records
        if _to_int(r.get("prompt_tokens")) >= LONG_INPUT_ZERO_HIT_THRESHOLD
        and _to_int(r.get("cached_tokens")) == 0
    )
    tool_attempt_count = sum(_to_int(r.get("tool_attempt_count")) for r in records)
    tool_attempt_success_count = sum(_to_int(r.get("tool_attempt_success_count")) for r in records)
    tool_attempt_error_count = sum(_to_int(r.get("tool_attempt_error_count")) for r in records)
    tool_skip_count = sum(_to_int(r.get("tool_skip_count")) for r in records)
    tool_duplicate_success_count = sum(_to_int(r.get("tool_duplicate_success_count")) for r in records)
    tool_memory_rule_hits = sum(_to_int(r.get("tool_memory_rule_hits")) for r in records)
    tool_compacted_result_count = sum(_to_int(r.get("tool_compacted_result_count")) for r in records)

    user_aliases = _user_aliases(records)
    user_labels = _known_user_labels(records)
    by_model: Dict[str, Dict[str, Any]] = {}
    by_user: Dict[str, Dict[str, Any]] = {}
    by_request_kind: Dict[str, Dict[str, Any]] = {}
    for record in records:
        model = str(record.get("model") or "unknown")
        bucket = by_model.setdefault(model, {
            "model": model,
            "requests": 0,
            "prompt_tokens": 0,
            "cached_tokens": 0,
            "completion_tokens": 0,
        })
        bucket["requests"] += 1
        bucket["prompt_tokens"] += _to_int(record.get("prompt_tokens"))
        bucket["cached_tokens"] += _to_int(record.get("cached_tokens"))
        bucket["completion_tokens"] += _to_int(record.get("completion_tokens"))

        request_kind = str(record.get("request_kind") or "unknown")
        kind_bucket = by_request_kind.setdefault(request_kind, {
            "request_kind": request_kind,
            "requests": 0,
            "prompt_tokens": 0,
            "cached_tokens": 0,
            "completion_tokens": 0,
            "long_input_requests": 0,
            "long_input_zero_cache_requests": 0,
            "tool_attempt_count": 0,
            "tool_attempt_success_count": 0,
            "tool_attempt_error_count": 0,
            "tool_skip_count": 0,
            "tool_duplicate_success_count": 0,
            "tool_memory_rule_hits": 0,
            "tool_compacted_result_count": 0,
        })
        kind_bucket["requests"] += 1
        kind_bucket["prompt_tokens"] += _to_int(record.get("prompt_tokens"))
        kind_bucket["cached_tokens"] += _to_int(record.get("cached_tokens"))
        kind_bucket["completion_tokens"] += _to_int(record.get("completion_tokens"))
        kind_bucket["tool_attempt_count"] += _to_int(record.get("tool_attempt_count"))
        kind_bucket["tool_attempt_success_count"] += _to_int(record.get("tool_attempt_success_count"))
        kind_bucket["tool_attempt_error_count"] += _to_int(record.get("tool_attempt_error_count"))
        kind_bucket["tool_skip_count"] += _to_int(record.get("tool_skip_count"))
        kind_bucket["tool_duplicate_success_count"] += _to_int(record.get("tool_duplicate_success_count"))
        kind_bucket["tool_memory_rule_hits"] += _to_int(record.get("tool_memory_rule_hits"))
        kind_bucket["tool_compacted_result_count"] += _to_int(record.get("tool_compacted_result_count"))
        if _to_int(record.get("prompt_tokens")) >= LONG_INPUT_ZERO_HIT_THRESHOLD:
            kind_bucket["long_input_requests"] += 1
            if _to_int(record.get("cached_tokens")) == 0:
                kind_bucket["long_input_zero_cache_requests"] += 1

        user_key = _user_key(record, user_aliases)
        user_label = _record_user_label(record, user_labels, user_key)
        user_bucket = by_user.setdefault(user_key, {
            "user_key": user_key,
            "user_label": user_label,
            "requests": 0,
            "prompt_tokens": 0,
            "cached_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "channels": set(),
            "sessions": set(),
            "last_seen": "",
        })
        user_bucket["requests"] += 1
        user_bucket["prompt_tokens"] += _to_int(record.get("prompt_tokens"))
        user_bucket["cached_tokens"] += _to_int(record.get("cached_tokens"))
        user_bucket["completion_tokens"] += _to_int(record.get("completion_tokens"))
        user_bucket["total_tokens"] += _to_int(record.get("total_tokens"))
        if record.get("channel_type"):
            user_bucket["channels"].add(str(record.get("channel_type")))
        if record.get("session_hash"):
            user_bucket["sessions"].add(str(record.get("session_hash")))
        if record.get("timestamp"):
            user_bucket["last_seen"] = str(record.get("timestamp"))
        if user_label and not user_label.startswith("user-"):
            user_bucket["user_label"] = user_label

    models = _finalize_buckets(by_model.values())
    models.sort(key=lambda item: item["prompt_tokens"], reverse=True)
    request_kinds = _finalize_request_kind_buckets(by_request_kind.values())
    request_kinds.sort(key=lambda item: item["prompt_tokens"], reverse=True)
    users = _finalize_user_buckets(by_user.values())
    users.sort(key=lambda item: item["total_tokens"], reverse=True)

    return {
        "summary": {
            "requests": len(records),
            "cache_hits": cache_hits,
            "cacheable_requests": cacheable_requests,
            "prompt_tokens": prompt_tokens,
            "cached_tokens": cached_tokens,
            "uncached_prompt_tokens": max(prompt_tokens - cached_tokens, 0),
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cache_hit_rate": (cached_tokens / prompt_tokens) if prompt_tokens else 0.0,
            "long_input_threshold": LONG_INPUT_ZERO_HIT_THRESHOLD,
            "long_input_requests": long_input_requests,
            "long_input_zero_cache_requests": long_input_zero_cache_requests,
            "long_input_zero_cache_rate": (
                long_input_zero_cache_requests / long_input_requests
                if long_input_requests
                else 0.0
            ),
            "tool_attempt_count": tool_attempt_count,
            "tool_attempt_success_count": tool_attempt_success_count,
            "tool_attempt_error_count": tool_attempt_error_count,
            "tool_skip_count": tool_skip_count,
            "tool_duplicate_success_count": tool_duplicate_success_count,
            "tool_memory_rule_hits": tool_memory_rule_hits,
            "tool_compacted_result_count": tool_compacted_result_count,
        },
        "models": models,
        "request_kinds": request_kinds,
        "users": users,
        "recent": recent,
        "tracking_enabled": _tracking_enabled(),
    }


def _usage_path() -> str:
    from config import conf

    workspace = expand_path(conf().get("agent_workspace", "~/cow"))
    return os.path.join(workspace, "data", "llm_cache_usage.jsonl")


def _tracking_enabled() -> bool:
    from config import conf

    return bool(conf().get("llm_usage_tracking", True))


def _history_limit() -> int:
    from config import conf

    value = _to_int(conf().get("llm_usage_history_limit", 2000))
    return max(100, min(value or 2000, 50000))


def _read_records(limit: int) -> List[Dict[str, Any]]:
    path = _usage_path()
    if not os.path.isfile(path):
        return []
    records: deque = deque(maxlen=max(1, limit))
    try:
        with _LOCK:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        records.append(item)
    except Exception as e:
        logger.debug(f"[PromptCache] Failed to read usage telemetry: {e}")
    return list(records)


def _trim_history_unlocked(path: str) -> None:
    limit = _history_limit()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if len(lines) <= limit:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[-limit:])
    except Exception as e:
        logger.debug(f"[PromptCache] Failed to trim usage telemetry: {e}")


def _safe_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    safe = {
        "model": _safe_text(metadata.get("model")),
        "wire_api": _safe_text(metadata.get("wire_api")),
        "channel_type": _safe_text(metadata.get("channel_type")),
        "project_optimizer_request_id": _safe_text(metadata.get("project_optimizer_request_id"), max_len=32),
    }
    session_id = metadata.get("session_id")
    if session_id:
        safe["session_hash"] = _hash_value(session_id)
    user_id = metadata.get("user_id")
    if user_id:
        safe["user_hash"] = _hash_value(user_id)
    user_label = metadata.get("user_label")
    if user_label and not _looks_internal_user_label(user_label):
        safe["user_label"] = _safe_text(user_label, max_len=160)
    cache_key = metadata.get("prompt_cache_key")
    if cache_key:
        safe["prompt_cache_key_hash"] = _hash_value(cache_key)
    retention = metadata.get("prompt_cache_retention")
    if retention:
        safe["prompt_cache_retention"] = _safe_text(retention)
    safe.update(_safe_request_shape_metadata(metadata))
    return {k: v for k, v in safe.items() if v}


def stable_metadata_hash(value: Any) -> str:
    """Return a stable short hash for normalized telemetry-only structures."""
    normalized = _normalize_for_hash(value)
    try:
        text = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(normalized)
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _normalize_for_hash(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _normalize_for_hash(value[key])
            for key in sorted(value.keys(), key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_for_hash(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _safe_request_shape_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    for field in _REQUEST_SHAPE_TEXT_FIELDS:
        value = _safe_text(metadata.get(field), max_len=96)
        if value:
            safe[field] = value
    for field in _REQUEST_SHAPE_INT_FIELDS:
        value = _to_int(metadata.get(field))
        if value:
            safe[field] = value
    return safe


def _finalize_buckets(buckets) -> List[Dict[str, Any]]:
    result = []
    for bucket in buckets:
        item = dict(bucket)
        item["total_tokens"] = (
            _to_int(item.get("total_tokens"))
            or _to_int(item.get("prompt_tokens")) + _to_int(item.get("completion_tokens"))
        )
        item["cache_hit_rate"] = (
            item["cached_tokens"] / item["prompt_tokens"]
            if item.get("prompt_tokens")
            else 0.0
        )
        result.append(item)
    return result


def _finalize_user_buckets(buckets) -> List[Dict[str, Any]]:
    result = []
    for bucket in buckets:
        item = dict(bucket)
        channels = sorted(item.pop("channels", set()))
        sessions = sorted(item.pop("sessions", set()))
        item["channels"] = channels
        item["session_count"] = len(sessions)
        item["cache_hit_rate"] = (
            item["cached_tokens"] / item["prompt_tokens"]
            if item.get("prompt_tokens")
            else 0.0
        )
        result.append(item)
    return result


def _finalize_request_kind_buckets(buckets) -> List[Dict[str, Any]]:
    result = _finalize_buckets(buckets)
    for item in result:
        long_requests = _to_int(item.get("long_input_requests"))
        long_zero = _to_int(item.get("long_input_zero_cache_requests"))
        item["long_input_zero_cache_rate"] = (long_zero / long_requests) if long_requests else 0.0
    return result


def _user_aliases(records: List[Dict[str, Any]]) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for record in records:
        user_hash = _safe_text(record.get("user_hash"))
        session_hash = _safe_text(record.get("session_hash"))
        if user_hash and session_hash:
            aliases[session_hash] = user_hash
    return aliases


def _known_user_labels(records: List[Dict[str, Any]]) -> Dict[str, str]:
    labels = _configured_user_labels()
    for record in records:
        label = _safe_text(record.get("user_label"), max_len=160)
        if not label or _looks_internal_user_label(label):
            continue
        for key_name in ("user_hash", "session_hash"):
            key = _safe_text(record.get(key_name))
            if key:
                labels[key] = label
    return labels


def _configured_user_labels() -> Dict[str, str]:
    try:
        from config import conf, global_config
    except Exception:
        return {}

    labels: Dict[str, str] = {}

    def add_identity(value: Any, label: Any = None, allow_self_label: bool = True) -> None:
        text = _safe_text(value, max_len=160)
        if not text:
            return
        display = _safe_text(label, max_len=160)
        if not display and allow_self_label and not _looks_internal_user_label(text):
            display = text
        if not display:
            return
        candidates = {text}
        if ":" not in text:
            candidates.add(f"weixin:{text}")
        for candidate in candidates:
            labels[candidate] = display
            labels[_hash_value(candidate)] = display

    configured_admin_users = conf().get("agent_admin_users", []) or []
    if isinstance(configured_admin_users, str):
        configured_admin_users = [item.strip() for item in configured_admin_users.split(",")]
    for item in configured_admin_users:
        add_identity(item)
    for item in global_config.get("admin_users", []) or []:
        add_identity(item)

    profiles = conf().get("agent_user_profiles", {}) or {}
    if isinstance(profiles, dict):
        for key, profile in profiles.items():
            label = None
            if isinstance(profile, dict):
                label = (
                    profile.get("wechat_id")
                    or profile.get("raw_user_id")
                    or profile.get("display_name")
                    or profile.get("name")
                )
            add_identity(key, label)

    configured_user_labels = conf().get("llm_usage_user_labels", {}) or {}
    if isinstance(configured_user_labels, dict):
        for key, label in configured_user_labels.items():
            add_identity(key, label, allow_self_label=False)
    return labels


def _user_key(record: Dict[str, Any], aliases: Optional[Dict[str, str]] = None) -> str:
    key = str(
        record.get("user_hash")
        or record.get("session_hash")
        or record.get("channel_type")
        or "unknown"
    )
    return (aliases or {}).get(key, key)


def _record_user_label(record: Dict[str, Any], labels: Dict[str, str], user_key: str) -> str:
    for key_name in ("user_hash", "session_hash"):
        key = _safe_text(record.get(key_name))
        if key and labels.get(key):
            return labels[key]
    if labels.get(user_key):
        return labels[user_key]
    label = _safe_text(record.get("user_label"), max_len=160)
    if label and not _looks_internal_user_label(label):
        return label
    return _user_label(user_key)


def _user_label(user_key: str) -> str:
    if not user_key or user_key == "unknown":
        return "unknown"
    return f"user-{user_key[:8]}"


def _safe_text(value: Any, max_len: int = 96) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text[:max_len]


def _looks_internal_user_label(value: Any) -> bool:
    text = _safe_text(value, max_len=240).lower()
    if not text:
        return False
    return "@im.wechat" in text or text.startswith("weixin:o")


def _hash_value(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8", errors="ignore")).hexdigest()[:16]


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _first_dict(*values: Any) -> Dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _first_positive_int(*values: Any) -> int:
    for value in values:
        count = _to_int(value)
        if count > 0:
            return count
    return 0
