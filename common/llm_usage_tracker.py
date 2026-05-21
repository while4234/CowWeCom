# encoding:utf-8

"""LLM usage telemetry focused on prompt-cache visibility.

Only token counters and hashed identifiers are persisted. Prompt text, tool
arguments, API keys, and raw session IDs must never be written here.
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

    by_model: Dict[str, Dict[str, Any]] = {}
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

    models = []
    for bucket in by_model.values():
        bucket["cache_hit_rate"] = (
            bucket["cached_tokens"] / bucket["prompt_tokens"]
            if bucket["prompt_tokens"]
            else 0.0
        )
        models.append(bucket)
    models.sort(key=lambda item: item["prompt_tokens"], reverse=True)

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
        },
        "models": models,
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
    }
    session_id = metadata.get("session_id")
    if session_id:
        safe["session_hash"] = _hash_value(session_id)
    user_id = metadata.get("user_id")
    if user_id:
        safe["user_hash"] = _hash_value(user_id)
    cache_key = metadata.get("prompt_cache_key")
    if cache_key:
        safe["prompt_cache_key_hash"] = _hash_value(cache_key)
    retention = metadata.get("prompt_cache_retention")
    if retention:
        safe["prompt_cache_retention"] = _safe_text(retention)
    return {k: v for k, v in safe.items() if v}


def _safe_text(value: Any, max_len: int = 96) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text[:max_len]


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
