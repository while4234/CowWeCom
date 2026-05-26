# encoding:utf-8

"""Post-task quota checks for the CAPI monthly-card backend."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from common.capi_quota_query import query_capi_quota_snapshot
from common.codex_quota_query import query_codex_quota_json
from common.llm_backend_router import (
    BACKEND_CAPI,
    BACKEND_CAPI_MONTHLY,
    get_llm_backend_config,
    record_auto_check,
    record_monthly_quota_check,
    select_backend_after_monthly_quota_low,
)
from common.log import logger


_CHECK_IN_PROGRESS = False


def maybe_check_capi_monthly_after_task(task_backend: Optional[str]) -> Dict[str, Any]:
    """Query monthly-card quota after a completed task that used the monthly backend."""
    global _CHECK_IN_PROGRESS
    if task_backend != BACKEND_CAPI_MONTHLY:
        return {}
    auto_cfg = _auto_config()
    if not bool(auto_cfg.get("monthly_post_task_check_enabled", True)):
        return {}
    if _CHECK_IN_PROGRESS:
        return {}

    _CHECK_IN_PROGRESS = True
    try:
        snapshot = _query_monthly_snapshot()
        remaining_percent = _remaining_percent(snapshot)
        threshold = float(auto_cfg.get("monthly_min_remaining_percent", 10) or 10)
        if remaining_percent >= threshold:
            return record_monthly_quota_check(snapshot, action="kept_monthly")

        record_monthly_quota_check(snapshot, action="monthly_quota_low")
        try:
            codex_payload = _query_codex_quota_json()
        except Exception as e:
            logger.warning("[CapiMonthly] Codex quota query failed during fallback: %s", str(e)[:300])
            return record_auto_check(
                decision="monthly_low_switched_to_capi",
                reason="codex_quota_query_failed",
                switched_backend=BACKEND_CAPI,
                now=datetime.now(),
            )
        return select_backend_after_monthly_quota_low(codex_payload, now=datetime.now())
    except Exception as e:
        logger.warning("[CapiMonthly] Post-task quota check failed: %s", str(e))
        try:
            return record_monthly_quota_check({"quota": {}}, action="check_error")
        except Exception:
            return {}
    finally:
        _CHECK_IN_PROGRESS = False


def _auto_config() -> Dict[str, Any]:
    cfg = get_llm_backend_config()
    auto = cfg.get("auto_switch") if isinstance(cfg.get("auto_switch"), dict) else {}
    return auto if isinstance(auto, dict) else {}


def _remaining_percent(snapshot: Mapping[str, Any]) -> float:
    quota = snapshot.get("quota") if isinstance(snapshot.get("quota"), dict) else {}
    total = _to_float(quota.get("total"))
    remaining = _to_float(quota.get("remaining"))
    return (remaining / total * 100.0) if total > 0 else 0.0


def _query_monthly_snapshot() -> Dict[str, Any]:
    return query_capi_quota_snapshot(BACKEND_CAPI_MONTHLY, include_usage=False, timeout_seconds=60)


def _query_codex_quota_json() -> Dict[str, Any]:
    return query_codex_quota_json()


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
