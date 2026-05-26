# encoding:utf-8

"""Periodic background quota refresh triggered by user-visible model calls."""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Dict, Optional

from common.capi_quota_query import query_capi_quota_snapshot
from common.codex_quota_query import query_codex_quota_json
from common.llm_backend_router import (
    BACKEND_CAPI,
    BACKEND_CAPI_MONTHLY,
    BACKEND_CODEX,
    get_current_backend,
    get_llm_backend_config,
    load_state,
    normalize_backend,
    record_capi_quota_check,
    record_codex_quota_check,
    save_state,
)
from common.log import logger


_LOCK = threading.Lock()
_REFRESHING_BACKENDS: set[str] = set()


def note_user_visible_model_call(
    backend: Optional[str] = None,
    *,
    request_kind: str = "",
    now: Optional[datetime] = None,
    async_refresh: bool = True,
) -> Dict[str, Any]:
    """Count one non-silent model call and refresh quota on the configured interval."""
    now = now or datetime.now()
    cfg = _quota_refresh_config()
    if not bool(cfg.get("enabled", True)):
        return load_state()
    interval = _refresh_interval(cfg)
    if interval <= 0:
        return load_state()

    normalized = normalize_backend(backend or get_current_backend())
    with _LOCK:
        state = load_state()
        refresh = state.get("quota_refresh") if isinstance(state.get("quota_refresh"), dict) else {}
        count = int(_number_or_zero(refresh.get("user_visible_model_call_count"))) + 1
        refresh["user_visible_model_call_count"] = count
        refresh["last_counted_at"] = now.isoformat(timespec="seconds")
        refresh["last_counted_backend"] = normalized
        if request_kind:
            refresh["last_request_kind"] = str(request_kind)
        state["quota_refresh"] = refresh
        save_state(state)

    if count % interval != 0:
        return state

    action = "periodic_model_call_refresh"
    if async_refresh:
        schedule_backend_quota_refresh(normalized, action=action, now=now)
        return load_state()
    return refresh_backend_quota(normalized, action=action, now=now)


def schedule_backend_quota_refresh(
    backend: Optional[str] = None,
    *,
    action: str = "periodic_model_call_refresh",
    now: Optional[datetime] = None,
) -> None:
    normalized = normalize_backend(backend or get_current_backend())
    with _LOCK:
        if normalized in _REFRESHING_BACKENDS:
            return
        _REFRESHING_BACKENDS.add(normalized)
    thread = threading.Thread(
        target=_refresh_worker,
        args=(normalized, action, now or datetime.now()),
        name=f"llm-backend-quota-refresh-{normalized}",
        daemon=True,
    )
    thread.start()


def refresh_backend_quota(
    backend: Optional[str] = None,
    *,
    action: str = "manual_refresh",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    normalized = normalize_backend(backend or get_current_backend())
    if normalized == BACKEND_CODEX:
        snapshot = query_codex_quota_json()
        return record_codex_quota_check(snapshot, action=action, now=now)
    if normalized in {BACKEND_CAPI, BACKEND_CAPI_MONTHLY}:
        snapshot = query_capi_quota_snapshot(normalized, include_usage=False)
        return record_capi_quota_check(normalized, snapshot, action=action, now=now)
    return load_state()


def _refresh_worker(backend: str, action: str, now: datetime) -> None:
    try:
        refresh_backend_quota(backend, action=action, now=now)
        logger.info("[LLMBackend] Quota refreshed: backend=%s action=%s", backend, action)
    except Exception as e:
        logger.warning("[LLMBackend] Quota refresh failed: backend=%s error=%s", backend, str(e)[:300])
    finally:
        with _LOCK:
            _REFRESHING_BACKENDS.discard(backend)


def _quota_refresh_config() -> Dict[str, Any]:
    cfg = get_llm_backend_config()
    quota_cfg = cfg.get("quota_refresh") if isinstance(cfg.get("quota_refresh"), dict) else {}
    return quota_cfg if isinstance(quota_cfg, dict) else {}


def _refresh_interval(cfg: Dict[str, Any]) -> int:
    try:
        interval = int(cfg.get("model_call_interval", 50) or 50)
    except (TypeError, ValueError):
        interval = 50
    return max(0, interval)


def _number_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
